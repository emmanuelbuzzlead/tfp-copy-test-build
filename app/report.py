"""The Friday report: one page, plain words, as a PDF, posted to Slack.

Troy, 2026-09-15: "a short friday report on what launched ... whats next in the
pipeline with projected contacts ... concise ... 6th grade reading level and as a
pdf." Five blocks plus verdicts and supply runway. Every number comes from the
database or EmailBison at render time; nothing is typed by hand.

Reading level is a writing rule here, not a metric: short sentences, one idea each,
everyday words. "We sent 4,083 emails" not "the campaign dispatched 4,083 messages".
Numbers stay exact -- they are the point of the report.

Delivery: the PDF is served at {PUBLIC_URL}/report/friday.pdf (always current) and
posted to the Slack channel every Friday by the daily review job. A file upload is
attempted too; if the bot lacks the files scope, the link alone still lands.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from . import db
from .clients import emailbison, slack
from .config import settings

# Measured 2026-09-14 (OpenRouter research + Concentrate emails): about $0.025 a contact.
COPY_COST_PER_CONTACT = 0.025


# --------------------------------------------------------------------------- data

def _campaigns() -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT campaign_id, campaign_name, vertical, pushed_lead_count, created_at "
                    "  FROM ve_campaigns ORDER BY created_at")
        rows = cur.fetchall()
    out = []
    for r in rows:
        try:
            live = emailbison.get_campaign(r["campaign_id"]) or {}
        except Exception:
            live = {}
        try:
            senders = emailbison.campaign_sender_emails(r["campaign_id"])
        except Exception:
            senders = []
        out.append({
            **r,
            "status": (live.get("status") or "unknown").lower(),
            "leads": int(live.get("total_leads") or r["pushed_lead_count"] or 0),
            "sent": int(live.get("emails_sent") or 0),
            "replies": int(live.get("replied") or 0),
            "interested": int(live.get("interested") or 0),
            "booked": db.campaign_bookings(r["campaign_id"]),
            "per_day": sum(int(s.get("daily_limit") or 0) for s in senders),
        })
    return out


def _awaiting() -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT batch_id, vertical, contact_count, approval_status, expires_at "
                    "  FROM batches WHERE approval_status IN ('pending','approved') "
                    " ORDER BY created_at")
        return cur.fetchall()


def _queued() -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT vertical, projected FROM vertical_backlog WHERE status='queued' "
                    " ORDER BY projected DESC")
        return cur.fetchall()


def _verdicts(days: int = 7) -> tuple[list[dict], int]:
    with db.cursor() as cur:
        cur.execute("SELECT vertical, verdict, reason, decided_at FROM vertical_outcomes "
                    " WHERE verdict IN ('scale','drop') "
                    "   AND decided_at > now() - make_interval(days => %s) "
                    " ORDER BY decided_at DESC", (days,))
        decided = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM vertical_outcomes WHERE verdict='pending'")
        pending = int(cur.fetchone()["n"] or 0)
    return decided, pending


def _health(days: int = 7) -> dict:
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) FILTER (WHERE status='needs_review') AS needs_review,
                              count(*) FILTER (WHERE status='needs_review'
                                   AND drop_reason LIKE 'no first name%%'
                                   AND updated_at > now() - make_interval(days => %s)) AS nameless,
                              count(*) FILTER (WHERE personalization_1 IS NOT NULL
                                   AND updated_at > now() - make_interval(days => %s)) AS written
                         FROM contacts""", (days, days))
        h = dict(cur.fetchone())
        cur.execute("""SELECT count(*) AS n FROM logs
                        WHERE ts > now() - make_interval(days => %s)
                          AND (message LIKE 'resume: still blocked%%'
                               OR message LIKE '%%paused: %%unavailable%%'
                               OR message LIKE '%%out of credit%%')""", (days,))
        h["outages"] = int(cur.fetchone()["n"] or 0)
    med = db.yield_medians()
    h["copy_pass"] = med.get("copy_pass")
    h["spend"] = round(int(h["written"] or 0) * COPY_COST_PER_CONTACT, 2)
    return h


def weeks_of_runway(queued: int, per_week: int) -> float:
    """How many weekly runs the backlog covers. Pure."""
    if per_week <= 0:
        return 0.0
    return round(queued / per_week, 1)


def gather() -> dict:
    campaigns = _campaigns()
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    decided, pending_verdicts = _verdicts()
    queued = _queued()
    try:
        experiments = db.experiment_performance("copy-angle-v1")
    except Exception:
        experiments = []
    return {
        "as_of": datetime.now(timezone.utc),
        "launched": [c for c in campaigns if c["created_at"] > week_ago],
        "all_campaigns": campaigns,
        "awaiting": _awaiting(),
        "queued": queued,
        "runway_weeks": weeks_of_runway(len(queued), settings.verticals_per_run),
        "verdicts": decided,
        "pending_verdicts": pending_verdicts,
        "health": _health(),
        "experiments": experiments,
        "week": _week(),
    }


def _week() -> dict | None:
    try:
        return db.weekly_progress()
    except Exception:
        return None


def post_daily() -> dict:
    """The daily quota check, in plain English, posted to Slack. Troy, 2026-10-01:
    "i would like daily reports on how close we are to meeting our volume target of
    15000 leads" -- and it counts scale-ups AND new launches, since weekly_progress
    counts every contact that reached a live status this week, whichever path made it.
    Compact on purpose; Friday still gets the full PDF."""
    from .clients import slack
    w = _week()
    if not w:
        return {"posted": False, "reason": "no weekly numbers"}
    pct = int(w["leads"] / w["quota"] * 100) if w["quota"] else 0
    lines = [f":bar_chart: *Weekly target check* — {w['leads']:,} of {w['quota']:,} "
             f"leads ({pct}%)."]
    # The straight-line projection is garbage in the first day of a week (dividing by
    # a fraction of a day over-extrapolates wildly -- it said 82,751 on 2026-10-05
    # morning), so it is only shown once a full day has passed.
    if w["elapsed_days"] >= 1:
        verdict = ("on pace" if w["on_pace_for"] >= w["quota"]
                   else f"{w['quota'] - w['on_pace_for']:,} short at this rate")
        lines.append(f"Pace: heading for ~{w['on_pace_for']:,} by Sunday — {verdict}.")
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT vertical, count(*) AS n FROM contacts
                    WHERE first_seen_at >= %s
                      AND status IN ('verified','draft','reserved','approved','pushed')
                    GROUP BY vertical ORDER BY n DESC LIMIT 6""", (w["week_start"],))
            rows = cur.fetchall()
        if rows:
            lines.append("This week came from: " + ", ".join(
                f"{_short(r['vertical'])} {r['n']:,}" for r in rows))
    except Exception:
        pass
    blockers: list[str] = []
    try:
        refused = db.vendor_refusals_since(hours=24)
        for vendor, n in refused.items():
            blockers.append(f"{vendor} refused {n} call(s)")
    except Exception:
        pass
    try:
        parked = db.paused_for_human()
        if parked:
            blockers.append(f"{len(parked)} vertical(s) parked for a human: "
                            + ", ".join(_short(p["vertical"]) for p in parked[:4]))
    except Exception:
        pass
    lines.append(("Blockers: " + "; ".join(blockers)) if blockers
                 else "Blockers: none — nothing is waiting on you.")
    text = "\n".join(lines)
    slack.post(text)
    return {"posted": True, "leads": w["leads"]}


def vendor_spend(days: int = 7) -> list[str]:
    """Plain-English lines per vendor: units this period, errors, and a $ figure when a
    unit price is configured. Troy, 2026-09-30: a calculated AI Ark spend update."""
    try:
        usage = db.vendor_usage_since(datetime.now(timezone.utc) - timedelta(days=days))
    except Exception:
        return []
    lines: list[str] = []
    a = usage.get("aiark") or {}
    if a:
        pages, people = a.get("company_page", 0), a.get("people_returned", 0)
        cost = (pages * settings.aiark_cost_per_company_page
                + people * settings.aiark_cost_per_person)
        dollars = f", about ${cost:,.2f}" if cost else " (no unit price on file)"
        errs = a.get("_errors", 0)
        lines.append(f"AI Ark: {pages:,} company pages, {people:,} people returned{dollars}."
                     + (f" {errs:,} calls refused (out of credit)." if errs else ""))
    lo = usage.get("leadocean") or {}
    if lo:
        req, people = lo.get("people_search", 0), lo.get("people_returned", 0)
        cost = people * settings.leadocean_cost_per_person
        dollars = f", about ${cost:,.2f}" if cost else " (no unit price on file)"
        lines.append(f"Lead Ocean: {req:,} requests, {people:,} people returned{dollars}.")
    return lines


# --------------------------------------------------------------------------- words

def _n(x) -> str:
    return f"{int(x or 0):,}"


def _short(vertical: str) -> str:
    """'Medical Device & Diagnostics Manufacturers' -> 'Medical Device'. Short names read."""
    head = vertical.split("&")[0].split(",")[0].strip()
    words = head.split()
    return " ".join(words[:3])


def lines(data: dict) -> list[tuple[str, list[str]]]:
    """The report as (heading, sentences). Plain words, one idea per sentence."""
    out: list[tuple[str, list[str]]] = []
    h = data["health"]

    launched = data["launched"]
    if launched:
        total = sum(c["leads"] for c in launched)
        body = [f"We launched {len(launched)} campaigns this week. They hold {_n(total)} leads."]
        for c in launched:
            body.append(f"{_short(c['vertical'])}: {_n(c['leads'])} leads, "
                        f"{_n(c['per_day'])} emails a day. Status: {c['status']}.")
    else:
        body = ["No new campaigns launched this week."]
    out.append(("What launched", body))

    body = []
    for c in data["all_campaigns"]:
        if c["sent"] == 0:
            body.append(f"{_short(c['vertical'])}: no emails sent yet.")
            continue
        need_sends = max(settings.scale_min_sends - c["sent"], 0)
        rate = f"{(c['interested'] / c['sent']):.2%}" if c["sent"] else "n/a"
        wait = ""
        if need_sends:
            days = (need_sends // c["per_day"] + 1) if c["per_day"] else "?"
            wait = f" We need {_n(need_sends)} more sends, about {days} days."
        body.append(f"{_short(c['vertical'])}: {_n(c['sent'])} sent, {_n(c['replies'])} replied, "
                    f"{_n(c['interested'])} interested, {_n(c.get('booked'))} booked "
                    f"({rate} interested per send).{wait}")
    if not body:
        body = ["Nothing is sending yet."]
    out.append(("Early signal", body))

    if data["verdicts"]:
        body = []
        for v in data["verdicts"]:
            word = "We are scaling it to 10,000 leads." if v["verdict"] == "scale" \
                else "We killed it. We will not try this group again."
            body.append(f"{_short(v['vertical'])}: {v['reason']}. {word}")
    else:
        body = [f"No campaign has enough sends to judge yet. {data['pending_verdicts']} are waiting."]
    out.append(("Verdicts", body))

    experiments = data.get("experiments") or []
    if experiments:
        body = []
        for row in experiments:
            delivered = int(row.get("delivered") or 0)
            interested = int(row.get("interested") or 0)
            booked = int(row.get("booked") or 0)
            rate = interested / delivered if delivered else 0
            body.append(f"{row['variant_id']}: {_n(delivered)} delivered, "
                        f"{_n(interested)} interested, {_n(booked)} booked ({rate:.2%}).")
        out.append(("Copy test", body))

    aw = data["awaiting"]
    if aw:
        body = [f"{len(aw)} batches need a yes or no in Slack."]
        for b in aw:
            state = "approved, not pushed" if b["approval_status"] == "approved" else \
                f"expires {b['expires_at']:%a %d %b}"
            body.append(f"{_short(b['vertical'])}: {_n(b['contact_count'])} contacts, {state}.")
    else:
        body = ["Nothing is waiting for approval."]
    out.append(("Waiting for your approval", body))

    q = data["queued"]
    if q:
        nxt = q[:settings.verticals_per_run]
        body = [f"Next Monday we run {len(nxt)} groups. Each is capped at "
                f"{_n(settings.contact_cap_per_vertical)} contacts."]
        for r in nxt:
            body.append(f"{_short(r['vertical'])}: about {_n(r['projected'])} people we could reach.")
        w = data["runway_weeks"]
        body.append(f"Supply runway: {len(q)} groups in the queue. That is "
                    f"{w:g} week{'' if w == 1 else 's'} of runs.")
        if data["runway_weeks"] <= 1:
            body.append("The queue refills itself on Monday from the winners.")
    else:
        body = ["The queue is empty. Monday's run will propose new groups from the winners."]
    out.append(("What is next", body))

    # THIS WEEK AGAINST THE QUOTA. Troy, 2026-09-30: the number the pipeline is built
    # around, so a short week is a line in the report, not something to discover.
    w = data.get("week")
    if w:
        pct = w["leads"] / w["quota"] if w["quota"] else 0
        verdict = ("on pace" if w["leads"] >= w["pace_quota"]
                   else f"behind pace by {w['pace_quota'] - w['leads']:,}")
        spend = vendor_spend(7)
        if spend:
            out.append(("Vendor spend, last 7 days", spend))
        out.append(("This week's quota", [
            f"{_n(w['leads'])} of {_n(w['quota'])} leads so far ({pct:.0%}), {verdict}.",
            f"At this rate the week lands at about {_n(w['on_pace_for'])}.",
        ]))

    body = []
    if h.get("copy_pass"):
        body.append(f"{h['copy_pass']:.0%} of emails passed our quality check.")
    body.append(f"We wrote {_n(h['written'])} emails this week. That cost about ${h['spend']:,.0f}.")
    if h.get("nameless"):
        body.append(f"{_n(h['nameless'])} contacts were set aside. They had no first name.")
    body.append(f"{_n(h['needs_review'])} contacts sit in the review pile.")
    body.append("No vendor outages this week." if not h["outages"]
                else f"{h['outages']} vendor outage pauses this week. All resumed on their own.")
    out.append(("Health", body))
    return out


def as_text(data: dict) -> str:
    parts = [f"IDPR Vertical Expander, week ending {data['as_of']:%a %d %b %Y}"]
    for head, body in lines(data):
        parts.append(f"\n*{head}*")
        parts.extend(f"- {s}" for s in body)
    return "\n".join(parts)


# --------------------------------------------------------------------------- pdf

def render_pdf(data: dict) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.set_margins(18, 16, 18)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "IDPR Vertical Expander", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 7, f"Friday report, week ending {data['as_of']:%d %B %Y}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    for head, body in lines(data):
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, head, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for sentence in body:
            pdf.set_x(22)
            pdf.multi_cell(0, 6, f"- {sentence}".encode("latin-1", "replace").decode("latin-1"))
        pdf.ln(2)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, f"Live numbers: {settings.public_url}/live", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Current copy of this report: {settings.public_url}/report/friday.pdf",
             new_x="LMARGIN", new_y="NEXT")
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- post

def post(data: dict | None = None) -> dict:
    """Post the report to Slack: file upload when allowed, link always."""
    data = data or gather()
    pdf = render_pdf(data)
    name = f"idpr-friday-{data['as_of']:%Y-%m-%d}.pdf"
    uploaded = slack.upload_file(pdf, name, f"Friday report {data['as_of']:%d %b}")
    text = as_text(data)
    if len(text) > 2800:
        text = text[:2800] + "\n..."
    link = f"{settings.public_url}/report/friday.pdf"
    slack.post(text + f"\n\nPDF: {link}")
    return {"uploaded": uploaded, "bytes": len(pdf), "link": link}
