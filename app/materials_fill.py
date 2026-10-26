"""Fill the "OG copy" campaign from the bank (Emmanuel, 2026-10-21): campaign 2157 uses the original spintax copy, which needs only
{{COMPANY}} and {{MATERIALS_HAULED}} per lead, so no research brief and no email writing.

One pass takes a batch of ready contacts (verified, fit-checked company, not loaded anywhere, not contacted in the recontact window, never replied or
bounced), runs the standing TFP gates, adds the two merge values to the locked field standards (app/materials.py), and loads them into the campaign.

  gates in order   company fit check (cached) -> Mimecast/Barracuda MX block -> MATERIALS_HAULED + COMPANY standards
  held, not sent   a company the model cannot describe, or any value that fails the standards, goes to needs_review with the reason
  order            safety / EHS titles first (5 of the 8 interested leads on the old campaigns were safety titles, 2.0 per 1,000 sends against 0.18 for
                   plant and operations), then everyone else, 5 seats per company
Enabled by MATERIALS_CAMPAIGN_ID (the campaign id) and MATERIALS_TARGET (default 3500). testrun.py calls run_pass() instead of the angle fill.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import db, materials, scaling
from .company_name_cleaner import clean_company_name

SAFETY_RX = re.compile(r"safety|ehs|\bhse\b|health|environment|risk|loss prevention|compliance", re.I)
SHORT_DESCRIPTION = 40
_lock = threading.Lock()


def specs() -> list[dict]:
    """The campaigns this fill loads, in order: [{"campaign": id, "target": n, "materials": bool}].

    MATERIALS_CAMPAIGNS="2174:2100:company,2190:1500:materials" (campaign:target:company|materials), or the older single-campaign trio
    MATERIALS_CAMPAIGN_ID / MATERIALS_TARGET / MATERIALS_NEEDED. A target raised by a Slack button is kept in fill_targets and wins when it is larger."""
    out: list[dict] = []
    raw = os.getenv("MATERIALS_CAMPAIGNS", "").strip()
    if raw:
        for part in raw.split(","):
            bits = [b.strip() for b in part.split(":")]
            if bits and bits[0].isdigit() and int(bits[0]) > 0:
                out.append({"campaign": int(bits[0]), "target": int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 2000,
                            "materials": (bits[2].lower() == "materials") if len(bits) > 2 else False})
    else:
        legacy = os.getenv("MATERIALS_CAMPAIGN_ID", "").strip()
        if legacy.isdigit() and int(legacy) > 0:
            out.append({"campaign": int(legacy), "target": int(os.getenv("MATERIALS_TARGET", "3500")), "materials": needs_materials()})
    try:
        ensure_tables()
        with db.cursor() as cur:
            # Campaigns registered from Slack (`register <campaign> <target> company|materials`), so adding one needs no setting and no deploy.
            cur.execute("SELECT campaign_id, target, materials FROM fill_campaigns ORDER BY added_at")
            for r in cur.fetchall():
                if int(r["campaign_id"]) not in {x["campaign"] for x in out}:
                    out.append({"campaign": int(r["campaign_id"]), "target": int(r["target"]), "materials": bool(r["materials"])})
            cur.execute("SELECT campaign_id, target FROM fill_targets")
            raised = {int(r["campaign_id"]): int(r["target"]) for r in cur.fetchall()}
        for sp in out:
            sp["target"] = max(sp["target"], raised.get(sp["campaign"], 0))
    except Exception:
        pass
    return out


def enabled() -> bool:
    return bool(specs())


def campaign_id() -> int | None:
    """The first campaign (kept for callers that deal with one)."""
    sp = specs()
    return sp[0]["campaign"] if sp else None


def target(cid: int | None = None) -> int:
    sp = specs()
    return sum(x["target"] for x in sp) if cid is None else next((x["target"] for x in sp if x["campaign"] == cid), 0)


def needs_materials(cid: int | None = None) -> bool:
    """False for a campaign whose copy has no {{MATERIALS_HAULED}} (2174, "Simple one liners"): that step is skipped and only COMPANY is set."""
    if cid is not None:
        for x in specs():
            if x["campaign"] == cid:
                return x["materials"]
    return os.getenv("MATERIALS_NEEDED", "true").strip().lower() not in ("0", "false", "no")


def raise_target(cid: int, new_target: int) -> None:
    """A Slack button raised this campaign's goal. Stored in the database so it survives a redeploy."""
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("""INSERT INTO fill_targets (campaign_id, target) VALUES (%s, %s)
                       ON CONFLICT (campaign_id) DO UPDATE SET target = GREATEST(fill_targets.target, EXCLUDED.target), updated_at = now()""", (cid, new_target))


def all_done() -> bool:
    sp = specs()
    return bool(sp) and all(loaded(x["campaign"]) >= x["target"] for x in sp)


def label() -> str:
    return ", ".join(str(x["campaign"]) for x in specs())


def ensure_tables() -> None:
    with db.cursor() as cur:
        cur.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS materials_hauled TEXT")
        cur.execute("""CREATE TABLE IF NOT EXISTS fill_targets (campaign_id BIGINT PRIMARY KEY, target INTEGER NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS fill_campaigns (campaign_id BIGINT PRIMARY KEY, target INTEGER NOT NULL, materials BOOLEAN NOT NULL DEFAULT FALSE,
            added_by TEXT, added_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS materials_cache (
            domain TEXT PRIMARY KEY, phrase TEXT, status TEXT NOT NULL, model TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def loaded(cid: int | None = None) -> int:
    """Leads in the campaign (all campaigns summed when cid is None). EmailBison's own total when it can be read, because a campaign may already hold
    leads this service did not load (2174 had 602 from another source); else this database's count of what the fill pushed."""
    if cid is None:
        return sum(loaded(x["campaign"]) for x in specs())
    try:
        from .clients import emailbison
        total = (emailbison.get_campaign(cid) or {}).get("total_leads")
        if total is not None:
            return int(total)
    except Exception:
        pass
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM contacts WHERE status = 'pushed' AND eb_campaign_id = %s", (cid,))
        return int(cur.fetchone()["n"])


_existing_cache: dict = {}


def existing_emails(cid: int, max_age: float = 600.0) -> set[str]:
    """Emails already in the campaign (loaded by anyone), so the fill never counts or re-pushes them. Cached for ten minutes."""
    import time
    hit = _existing_cache.get(cid)
    if hit and time.time() - hit["at"] < max_age:
        return hit["emails"]
    from .clients import emailbison
    rows = emailbison._paged(f"/api/campaigns/{cid}/leads", None, cap=400)
    emails = {(r.get("email") or "").lower() for r in rows if r.get("email")}
    _existing_cache[cid] = {"at": time.time(), "emails": emails}
    return emails


def candidates() -> list[dict]:
    """Ready contacts, safety titles first, at most _seats() per company."""
    from . import client
    from .wave1_fill import PERSONAL_DOMAINS, _seats
    days = client.profile().recontact_days
    with db.cursor() as cur:
        cur.execute("""SELECT c.email, c.first_name, c.last_name, c.title, c.company_domain, c.company_name, c.vertical
                       FROM contacts c LEFT JOIN company_funnel f ON f.domain = c.company_domain
                       WHERE c.status = 'verified' AND c.eb_campaign_id IS NULL AND coalesce(c.first_name, '') <> ''
                         AND coalesce(c.personalization_1, '') = '' AND coalesce(c.company_domain, '') <> ''
                         AND coalesce(f.fit, '') NOT IN ('LOW', 'NOT_FIT') AND NOT coalesce(f.replied, false)
                         AND NOT (c.company_domain = ANY(%s))
                         AND NOT EXISTS (SELECT 1 FROM recent_contacts rc WHERE lower(rc.email) = lower(c.email)
                                         AND rc.campaign_date >= current_date - %s::int)
                         AND NOT EXISTS (SELECT 1 FROM history_replies r WHERE lower(r.email) = lower(c.email)
                                         AND (r.replied_at IS NOT NULL OR r.bounced_at IS NOT NULL))""",
                    (list(PERSONAL_DOMAINS), days))
        rows = [dict(r) for r in cur.fetchall()]
    by_dom: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_dom[r["company_domain"]].append(r)
    out: list[dict] = []
    for dom, people in by_dom.items():
        people.sort(key=lambda p: (0 if SAFETY_RX.search(p.get("title") or "") else 1, p["email"]))
        out += people[:_seats()]
    out.sort(key=lambda p: (0 if SAFETY_RX.search(p.get("title") or "") else 1, hashlib.sha256(p["company_domain"].encode()).hexdigest()))
    return out


def _context(domains: list[str]) -> dict[str, tuple[str, str, str]]:
    """{domain: (name, description, industry)} from the sourcing record the company arrived with."""
    ctx: dict[str, tuple[str, str, str]] = {}
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT ON (domain) domain, payload FROM staged_companies WHERE domain = ANY(%s) ORDER BY domain, staged_at DESC", (domains,))
        for r in cur.fetchall():
            p = r["payload"] if isinstance(r["payload"], dict) else {}
            ctx[r["domain"]] = (str(p.get("name") or p.get("company_name") or ""), str(p.get("description") or p.get("short_description") or ""),
                                str(p.get("industry") or p.get("industries") or ""))
    return ctx


def phrases(rows: list[dict], workers: int = 12) -> tuple[dict[str, str | None], dict]:
    """MATERIALS_HAULED per company domain (None = could not be described). Cached in materials_cache. Returns (phrases, stats)."""
    from .clients import jina
    ensure_tables()
    domains = sorted({r["company_domain"] for r in rows})
    names = {r["company_domain"]: r.get("company_name") or "" for r in rows}
    out: dict[str, str | None] = {}
    with db.cursor() as cur:
        cur.execute("SELECT domain, phrase, status FROM materials_cache WHERE domain = ANY(%s)", (domains,))
        for r in cur.fetchall():
            out[r["domain"]] = r["phrase"] if r["status"] == "ok" else None
    todo = [d for d in domains if d not in out]
    ctx = _context(todo) if todo else {}

    def one(dom: str) -> tuple[str, str | None]:
        name, desc, industry = ctx.get(dom, ("", "", ""))
        if len(desc.strip()) < SHORT_DESCRIPTION:
            try:
                desc = (desc + " " + jina.website_text(dom, 900)).strip()      # no description from sourcing: read the home page instead
            except Exception:
                pass
        try:
            return dom, materials.infer(name or names.get(dom) or dom, desc, industry)
        except Exception:
            return dom, "ERR"

    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, todo))
    stats = {"companies": len(domains), "cached": len(domains) - len(todo), "inferred": 0, "unclear": 0, "errors": 0}
    save = []
    for dom, phrase in results:
        if phrase == "ERR":
            stats["errors"] += 1          # not cached: the next pass tries again
            out[dom] = None
            continue
        out[dom] = phrase
        stats["inferred" if phrase else "unclear"] += 1
        save.append((dom, phrase, "ok" if phrase else "unclear", materials.MODEL))
    if save:
        with db.cursor() as cur:
            cur.executemany("""INSERT INTO materials_cache (domain, phrase, status, model) VALUES (%s,%s,%s,%s)
                               ON CONFLICT (domain) DO UPDATE SET phrase = EXCLUDED.phrase, status = EXCLUDED.status, model = EXCLUDED.model, updated_at = now()""", save)
    return out, stats


def _hold(rows: list[dict], reason: str) -> None:
    if rows:
        with db.cursor() as cur:
            cur.executemany("UPDATE contacts SET status = 'needs_review', eb_campaign_id = NULL, drop_reason = %s WHERE email = %s",
                            [(reason, r["email"]) for r in rows])


def run_pass(batch: int | None = None) -> dict:
    """One batch for the first campaign that still has room. Safe to call repeatedly; returns counts for the scale-up job to report."""
    sp = specs()
    if not sp:
        return {"status": "not_enabled"}
    if not _lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        for x in sp:
            if loaded(x["campaign"]) < x["target"]:
                return _pass(x["campaign"], batch)
        return {"status": "complete", "loaded": loaded(), "target": target()}
    finally:
        _lock.release()


def _pass(cid: int, batch: int | None) -> dict:
    """One batch into one campaign: gates, merge values, load."""
    from . import wave1_fill
    ensure_tables()
    run_id = "materials-" + __import__("time").strftime("%Y%m%dT%H%M%S")
    db.start_run(run_id)
    room = target(cid) - loaded(cid)
    if room <= 0:
        db.finish_run(run_id, "complete", "target reached")
        return {"status": "complete", "campaign": cid, "loaded": loaded(cid), "target": target(cid)}
    limit = min(batch or int(os.getenv("MATERIALS_BATCH", "300")), int(room * 1.15) + 5)
    have = existing_emails(cid)
    rows = [r for r in candidates() if r["email"].lower() not in have][:limit]
    pulled = len(rows)
    rows = wave1_fill.fit_filter(rows, run_id)
    rows = wave1_fill.mx_filter(rows, run_id)
    need_mat = needs_materials(cid)
    phr, stats = phrases(rows) if need_mat else ({}, {"companies": 0, "cached": 0, "inferred": 0, "unclear": 0, "errors": 0})
    keep: list[dict] = []
    held = Counter()
    unclear_rows, bad_rows = [], []
    for r in rows:
        p = phr.get(r["company_domain"]) if need_mat else None
        if need_mat and not p:
            unclear_rows.append(r)
            continue
        company = clean_company_name(r.get("company_name") or r["company_domain"])
        issues = materials.check_company(company) + (materials.check_materials(p) if need_mat else [])
        if issues:
            bad_rows.append((r, "; ".join(issues)))
            held[issues[0]] += 1
            continue
        keep.append({**r, "materials_hauled": p} if need_mat else dict(r))
    # Only rows the model could not describe are held for good; an API error is left in the bank for the next pass.
    really_unclear = [r for r in unclear_rows if r["company_domain"] in _unclear_domains(unclear_rows)]
    _hold(really_unclear, "MATERIALS_HAULED unclear: the model could not say what crosses this company's dock (held, never sent with filler)")
    for r, why in bad_rows:
        _hold([r], f"copy-field standard failed: {why}")
    keep = keep[:max(room, 0)]
    if keep and need_mat:
        with db.cursor() as cur:
            cur.executemany("UPDATE contacts SET materials_hauled = %s WHERE email = %s", [(r["materials_hauled"], r["email"]) for r in keep])
    sent = scaling.load_leads(cid, keep, run_id) if keep else []
    total = loaded(cid)
    unclear_pct = 100.0 * stats["unclear"] / max(stats["companies"] - stats["cached"], 1) if stats["inferred"] + stats["unclear"] else 0.0
    note = {"status": "complete" if total >= target(cid) else "partial", "campaign": cid, "pulled": pulled, "loaded_now": len(sent), "loaded": total, "target": target(cid),
            "held_unclear": len(really_unclear), "held_standard": len(bad_rows), "standard_reasons": dict(held), "unclear_pct": round(unclear_pct, 1),
            "samples": [(clean_company_name(r.get("company_name") or r["company_domain"]), r.get("materials_hauled") or "(company only)") for r in sent[:8]]}
    if stats["inferred"] + stats["unclear"] >= 30 and unclear_pct > 10:
        try:
            from .clients import slack
            slack.post(f":warning: Campaign {cid}: {unclear_pct:.0f}% of the companies in this batch could not be described for MATERIALS_HAULED "
                       f"(standard: above 10% means the prompt or the fit is wrong). They were held, not sent.")
        except Exception:
            pass
    db.finish_run(run_id, "complete", str({k: v for k, v in note.items() if k != "samples"})[:300])
    return note


def _unclear_domains(rows: list[dict]) -> set[str]:
    """Domains whose cache row says 'unclear' (a deliberate answer), as opposed to a call that errored."""
    doms = sorted({r["company_domain"] for r in rows})
    with db.cursor() as cur:
        cur.execute("SELECT domain FROM materials_cache WHERE domain = ANY(%s) AND status = 'unclear'", (doms,))
        return {r["domain"] for r in cur.fetchall()}


def status() -> list[dict] | None:
    """What the runway page and report show for each campaign this fill loads, or None when the mode is off."""
    sp = specs()
    if not sp:
        return None
    out = []
    for x in sp:
        cid = x["campaign"]
        with db.cursor() as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE status = 'pushed' AND eb_campaign_id = %s AND updated_at > now() - interval '60 minutes') per_hour,
                                  count(*) FILTER (WHERE drop_reason LIKE 'MATERIALS_HAULED unclear%%') held_unclear,
                                  count(*) FILTER (WHERE drop_reason LIKE 'copy-field standard failed%%') held_standard
                           FROM contacts""", (cid,))
            row = dict(cur.fetchone())
        live: dict = {}
        try:
            from .clients import emailbison
            c = emailbison.get_campaign(cid) or {}
            live = {"name": c.get("name"), "status": c.get("status"), "sent": int(c.get("emails_sent") or 0), "interested": int(c.get("interested") or 0)}
        except Exception:
            pass
        out.append({"campaign": cid, "target": x["target"], "loaded": loaded(cid), **{k: int(v or 0) for k, v in row.items()}, **live})
    return out


MAX_ADD = int(os.getenv("MAX_ADD_LEADS", "3000"))
MIN_ADD_WEB = 500


def register(cid: int, target_leads: int, materials: bool = False, user: str | None = None) -> dict:
    """Start filling another campaign. The campaign must exist in this client's EmailBison workspace."""
    from .clients import emailbison
    camp = emailbison.get_campaign(cid) or {}
    if not camp.get("id") and not camp.get("name"):
        return {"ok": False, "reason": f"campaign {cid} was not found in this client's EmailBison workspace"}
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("""INSERT INTO fill_campaigns (campaign_id, target, materials, added_by) VALUES (%s,%s,%s,%s)
                       ON CONFLICT (campaign_id) DO UPDATE SET target = EXCLUDED.target, materials = EXCLUDED.materials""", (cid, target_leads, materials, user))
    _nudge()
    return {"ok": True, "campaign": cid, "name": camp.get("name"), "target": target_leads, "loaded": int(camp.get("total_leads") or 0), "materials": materials}


def _nudge() -> None:
    """Wake the scale-up job now instead of waiting for its five-minute watchdog."""
    try:
        from . import testrun
        testrun.ensure_running()
    except Exception:
        pass


def overview() -> list[dict]:
    """Every campaign the controls can add leads to: the copy-test angle campaigns and the fill campaigns, with where each stands."""
    from .clients import emailbison
    out: list[dict] = []
    try:
        from . import testgrid
        fill_target = int(((testgrid.load_config() or {}).get("fill") or {}).get("target", 0))
        with db.cursor() as cur:
            cur.execute("SELECT angle, campaign_id FROM copy_test_campaigns WHERE is_primary ORDER BY angle")
            angles = [(r["angle"], int(r["campaign_id"])) for r in cur.fetchall()]
            cur.execute("SELECT angle, target_leads FROM scale_targets")
            raised = {r["angle"]: int(r["target_leads"]) for r in cur.fetchall()}
    except Exception:
        angles, raised, fill_target = [], {}, 0
    for angle, cid in angles:
        c = emailbison.get_campaign(cid) or {}
        out.append({"campaign": cid, "kind": "angle", "angle": angle, "name": c.get("name") or angle, "loaded": int(c.get("total_leads") or 0),
                    "target": max(fill_target, raised.get(angle, 0)), "sent": int(c.get("emails_sent") or 0), "interested": int(c.get("interested") or 0)})
    for c in (status() or []):
        out.append({"campaign": c["campaign"], "kind": "fill", "angle": None, "name": c.get("name") or f"campaign {c['campaign']}", "loaded": c["loaded"],
                    "target": c["target"], "sent": c.get("sent", 0), "interested": c.get("interested", 0)})
    return out


def add_leads(cid: int, n: int, user: str | None = None) -> dict:
    """The button: add n more leads to a campaign this fill loads. The goal becomes what is in the campaign now plus n; the job picks it up and the
    producer finds the contacts. Capped per press (MAX_ADD_LEADS, default 2,000)."""
    n = int(n)
    if n <= 0 or n > MAX_ADD:
        return {"ok": False, "reason": f"pick between 1 and {MAX_ADD:,} leads"}
    ang = next((x for x in overview() if x["campaign"] == cid and x["kind"] == "angle"), None)
    if ang:
        # A copy-test angle: the existing scale-up path raises its goal and the two-hourly tick writes the copy and loads it.
        from . import testgrid
        new_target = max(ang["target"], ang["loaded"] + n)
        res = testgrid.auto_expand_champion(ang["angle"], None, target_leads=new_target)
        db.log(None, "materials_fill", f"add_leads: angle {ang['angle']} (campaign {cid}) {ang['loaded']} -> {new_target} (+{n}) by {user or 'unknown'}: {res}")
        return {"ok": bool(res.get("target_set")), "campaign": cid, "was": ang["loaded"], "target": new_target, "added": n,
                "reason": res.get("reason", "nothing changed"),
                "note": f"{ang['angle']} is a copy-test angle: copy is written for each lead and the scale-up runs every 2 hours, so it is slower than a fill campaign. "
                        "Adding to one angle also tips the balance of the test."}
    sp = next((x for x in specs() if x["campaign"] == cid), None)
    if not sp:
        return {"ok": False, "reason": f"campaign {cid} is not one of this client's campaigns. Register a fill campaign with `register {cid} <target> company`"}
    now = loaded(cid)
    new_target = max(sp["target"], now + n)
    raise_target(cid, new_target)
    note = ""
    try:
        from . import runway, testrun
        ready = testrun._bank()
        pool = runway.numbers()["left"]
        note = f"{ready:,} contacts are ready now and {pool:,} companies are still searchable (about 0.4 contacts each)."
    except Exception:
        pass
    db.log(None, "materials_fill", f"add_leads: campaign {cid} {now} -> {new_target} (+{n}) by {user or 'unknown'}")
    _nudge()
    return {"ok": True, "campaign": cid, "was": now, "target": new_target, "added": n, "note": note}


def controls_blocks() -> list[dict]:
    """A Slack message: each fill campaign with its progress and +500 / +1,000 buttons (each asks 'are you sure' first)."""
    blocks: list[dict] = []
    for c in overview():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{c.get('name') or 'Campaign'}* (campaign {c['campaign']}{', copy test ' + c['angle'] if c.get('angle') else ''}): {c['loaded']:,} of {c['target']:,} loaded"
                       + (f", {c['sent']:,} sent, {c['interested']} interested" if c.get("sent") else ", nothing sent yet")}})
        elements = []
        for n in (500, 1000, 3000):
            elements.append({"type": "button", "text": {"type": "plain_text", "text": f"Add {n:,} leads"}, "action_id": f"add_leads_{n}", "value": f"{c['campaign']}|{n}",
                             "confirm": {"title": {"type": "plain_text", "text": f"Add {n:,} leads?"},
                                         "text": {"type": "mrkdwn", "text": f"Raises campaign {c['campaign']}'s goal to {c['loaded'] + n:,} and starts finding contacts. It spends contact search and verification credits."},
                                         "confirm": {"type": "plain_text", "text": "Add them"}, "deny": {"type": "plain_text", "text": "Cancel"}}})
        blocks.append({"type": "actions", "elements": elements})
    if not blocks:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No fill campaigns yet. Add one with `register <campaign id> <target> company|materials`."}})
    return blocks
