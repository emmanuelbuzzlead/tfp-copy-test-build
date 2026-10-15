"""The ProductEVO client-facing weekly one-pager: markets tested, the offers, what's
resonating, with real copy and real company names.

Troy, 2026-10-06: a weekly exec report for ProductEVO, structure confirmed. Troy proofs it
every Friday before anything goes to the client -- this module only generates the draft
and posts it to Slack (internal) for that review. There is no email-sending tool in this
codebase, so actually emailing ProductEVO stays a manual step for Troy once he's happy with
the draft, unless he points this at a real email integration later.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db, testgrid

ANGLE_PITCH = {
    "MMM": "Make More Money — \"you've earned your buyers' trust, here's what else they'd "
          "buy from you instead of a competitor\"",
    "TARIFF": "Second Source — \"tariffs are a real cost risk, here's a backup factory ready to go\"",
    "TIME": "Give Founders Their Time Back — \"you're the bottleneck in your own business, "
           "here's how that stops\"",
}
IN_SCOPE_VERTICAL_PREFIXES = (  # Troy's declared test segments, for picking the BEST example
    "Sporting", "Hunting", "Home Fitness", "Housewares", "Kitchen Appliances", "Furniture",
    "Hardware", "Consumer Electronics", "Automotive", "RV",
)


def _client_name() -> str:
    from . import client
    return client.profile().display_name


def _apply_client_pitch(cfg: dict) -> dict:
    """A client's own one-line offers, from copy_test.toml [angle_pitch]; ProductEVO's stay the default."""
    own = cfg.get("angle_pitch") or {}
    if own:
        ANGLE_PITCH.clear()
        ANGLE_PITCH.update(own)
    return ANGLE_PITCH


def _markets_line(cfg: dict) -> str:
    seen, out = set(), []
    for verts in cfg["segments"].values():
        for v in verts:
            name = v.split(" (")[0].strip()
            if name not in seen:
                seen.add(name)
                out.append(name)
    return ", ".join(out) + ", plus a reactivation pass on companies we'd contacted before"


def _best_example(angle: str, variant: str) -> dict | None:
    """Booked beats interested beats a plain reply; an in-scope vertical beats one outside
    the declared test segments, so the example naturally improves as more data comes in."""
    with db.cursor() as cur:
        cur.execute("""SELECT email, first_name, last_name, title, company_name, vertical,
                              personalization_1, personalization_2, booked_at, interested, replied_at
                       FROM contacts
                       WHERE copy_variant = %s AND coalesce(personalization_1,'') <> ''
                         AND (booked_at IS NOT NULL OR interested = true OR replied_at IS NOT NULL)
                       ORDER BY (booked_at IS NOT NULL) DESC, (interested IS TRUE) DESC,
                                (coalesce(vertical,'') LIKE ANY(%s)) DESC,
                                coalesce(booked_at, replied_at) DESC
                       LIMIT 1""",
                    (variant, [f"{p}%" for p in IN_SCOPE_VERTICAL_PREFIXES]))
        row = cur.fetchone()
    return dict(row) if row else None


def generate_weekly_report() -> dict:
    """Build the one-pager's content (not formatted prose -- the caller decides the
    wrapper), using this week's real numbers and real examples.

    Troy, 2026-10-08: scoped to the ORIGINAL test angles only -- a derived/lookalike
    angle (first one: NEXT_SEASON_LINEUP) gets registered the same way MMM/TARIFF/TIME
    are, in copy_test_campaigns and cfg["angles"], but has no entry in ANGLE_PITCH and
    isn't one of "the three offers" this report describes. Without this filter, a
    launched lookalike crashes report generation outright (r['pitch'][a] -> KeyError)
    the next time this runs -- found 2026-10-08, the day before the first Friday it
    would have hit.
    """
    cfg = testgrid.load_config()
    if cfg is None:
        return {"status": "no_config"}
    with db.cursor() as cur:
        cur.execute("SELECT angle FROM angle_derivations")
        derived = {r["angle"] for r in cur.fetchall()}
    core_angles = [a for a in cfg["angles"] if a not in derived]
    roll = testgrid.rollup(testgrid.read_grid(), cfg)
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id, launched_at FROM copy_test_campaigns WHERE is_primary")
        primaries = {r["angle"]: dict(r) for r in cur.fetchall() if r["angle"] in core_angles}
    if len(primaries) < len(core_angles):
        return {"status": "no_data", "reason": "not every angle's primary campaign is registered"}
    launched = min(p["launched_at"] for p in primaries.values() if p["launched_at"])
    day = (datetime.now(timezone.utc) - launched).days if launched else None

    from .clients import emailbison
    sent = {}
    for a, p in primaries.items():
        c = emailbison.get_campaign(int(p["campaign_id"]))
        sent[a] = int((c or {}).get("emails_sent") or 0)

    angle_prompts = cfg.get("angle_prompts") or {}
    examples = {a: _best_example(a, angle_prompts.get(a)) for a in core_angles}

    # "Where this is heading" -- the one line Troy's first draft had and the automated
    # version was missing. Pulls the SAME verdict the nightly decision logic produces,
    # worded for a client instead of the internal status strings (found 2026-10-06: this
    # was hand-written prose in the chat draft, never wired to the real decide() call).
    verdict = testgrid.decide(roll["by_angle"], launched, cfg) if launched else {"status": "frozen"}
    heading = _heading_line(verdict, roll["by_angle"])

    return {
        "status": "ok",
        "week_of": datetime.now(timezone.utc).strftime("%-d %B %Y"),
        "day": day,
        "angles": core_angles,
        "pitch": _apply_client_pitch(cfg),
        "markets": _markets_line(cfg),
        "by_angle": roll["by_angle"],
        "sent": sent,
        "examples": examples,
        "heading": heading,
    }


def _heading_line(verdict: dict, by_angle: dict) -> str:
    """A client-facing sentence for wherever the test actually is, driven by the same
    decide() verdict the automation acts on -- so this line changes on its own as the
    test moves through its stages, instead of being re-written by hand each week."""
    status = verdict.get("status")
    if status == "frozen":
        return "Too early to call anything yet. We're letting the data build before judging it."
    if status == "monitoring":
        rate = lambda r: (r["interested"] + 3 * r["booked"]) / max(r["leads"], 1)
        leader = max(by_angle, key=lambda a: rate(by_angle[a]))
        return (f"{ANGLE_PITCH.get(leader, leader).split(' —')[0]} is ahead on today's numbers. "
                f"Not yet a proven win, so we're already putting more budget behind it while the "
                f"other two keep running, rather than wait and lose time.")
    if status == "decided":
        winner = ANGLE_PITCH.get(verdict.get("winner"), verdict.get("winner"))
        losers = [ANGLE_PITCH.get(l, l).split(" —")[0] for l in verdict.get("losers", [])]
        return (f"The data is clear enough now: we've stopped {' and '.join(losers)} "
                f"and are putting the full budget behind {winner.split(' —')[0]}.")
    return "Still gathering enough replies to say anything definitive yet."


def _fmt_example(angle: str, label: str, ex: dict | None) -> str:
    if not ex:
        return f"*{label}* -- no reply with a result strong enough to show yet."
    who = f"{ex.get('first_name') or ''} {ex.get('last_name') or ''}".strip()
    title = ex.get("title") or ""
    company = ex.get("company_name") or ex.get("email", "").split("@")[-1]
    result = "booked a call" if ex.get("booked_at") else ("genuinely interested" if ex.get("interested") else "replied")
    snippet = (ex.get("personalization_1") or "")[:260].rsplit(" ", 1)[0] + "..."
    return (f"*{label} -> {result}.* {who}, {title} at {company}, replied to:\n> \"{snippet}\"")


def post_weekly_report_for_proofing(run_id: str | None = None) -> dict:
    """Entry point: call this on Fridays. Posts the DRAFT to Slack for Troy to proof --
    never sent to ProductEVO automatically, there is no email tool wired up for that."""
    r = generate_weekly_report()
    if r.get("status") != "ok":
        db.log(run_id, "weekly_report", f"skipped: {r}")
        return r
    from .clients import slack
    lines = [
        f":memo: *{_client_name()} weekly one-pager -- DRAFT for your proofing, day {r['day']} of the test*",
        "",
        "*The three offers:*",
    ] + [f"- {r['pitch'][a]}" for a in r["angles"]] + [
        "",
        f"*Markets:* {r['markets']}",
        "",
        "*This week:*",
    ]
    for a in r["angles"]:
        b = r["by_angle"][a]
        lines.append(f"- {a}: reached {b['leads']:,}, sent {r['sent'].get(a, 0):,}, replied {b['replied']}, "
                     f"interested {b['interested']}, booked {b['booked']}")
    lines.append("")
    lines.append("*What's resonating:*")
    for a in r["angles"]:
        lines.append(_fmt_example(a, r["pitch"][a].split(" —")[0], r["examples"].get(a)))
        lines.append("")
    lines.append("*Where this is heading:*")
    lines.append(r["heading"])
    lines.append("")
    lines.append(f"Reply here with edits, or say *send it* once you're happy -- this still goes to {_client_name()} "
                 "only after you've proofed it, same as always.")
    slack.post("\n".join(lines))
    db.log(run_id, "weekly_report", f"draft posted for proofing, day {r['day']}")
    return {"status": "posted", "day": r["day"]}
