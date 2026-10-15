"""Nightly reconciliation: catch a silent data gap the same day it happens, not two weeks
later when Troy spots it in the EmailBison UI.

Troy, 2026-10-05, after a day of finding real bugs this way: "how do we take everything to
a solid 7 or 8/10?" The single biggest gap in today's QA was that six separate pipes
(reply sync, launch dates, lead ids, the booked-tag read, scoring's vertical match, which
campaign is primary) were each silently broken for days, and nothing noticed but a human
looking at the UI. This runs every night, cheap (a handful of API calls, no per-lead
paging), and posts ONE Slack alert if anything looks wrong. Silent when everything's fine.
"""
from __future__ import annotations

from . import db, testgrid


def _campaign_checks(angle: str, campaign_id: int, is_primary: bool) -> list[str]:
    from .clients import emailbison
    problems = []
    try:
        c = emailbison.get_campaign(campaign_id)
    except Exception as exc:
        return [f"{angle}/{campaign_id}: could not read from EmailBison ({str(exc)[:100]})"]

    # 1. Is this campaign even registered for the reply sweep? (the ve_campaigns gap,
    # found 2026-10-05 -- it was EMPTY for all 5 campaigns for the first 8 days.)
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM ve_campaigns WHERE campaign_id = %s", (campaign_id,))
        if not cur.fetchone():
            problems.append(f"{angle}/{campaign_id}: NOT registered for the reply sweep (ve_campaigns) "
                            f"-- real replies will never reach our own records")

    # 2. Did the sweep record ANYTHING for this campaign's actual repliers? Checked by
    # EMAIL, not by contacts.eb_campaign_id: a contact Troy moved into a cloned campaign
    # through the EmailBison UI (not through our own loader) keeps whatever campaign_id
    # it had before the clone -- that field goes stale the moment a human moves someone
    # outside the pipeline. Comparing counts by campaign_id flagged both duplicate
    # campaigns as "never synced" when every single reply was in fact recorded, just
    # under the original campaign's id. Checking the real repliers' emails instead, the
    # one check that still catches a truly dead sweep.
    from .clients import emailbison as _eb
    from .stages.stop_on_reply import contact_outcomes as _contact_outcomes
    try:
        replies = _eb.list_replies(campaign_id)
        repliers = [o["email"].lower() for o in _contact_outcomes(replies) if o.get("replied_at")]
    except Exception as exc:
        repliers = None
        problems.append(f"{angle}/{campaign_id}: could not read its reply list to check ({str(exc)[:100]})")
    if repliers:
        with db.cursor() as cur:
            cur.execute("SELECT count(*) n FROM contacts WHERE lower(email) = ANY(%s) AND replied_at IS NOT NULL",
                        (repliers,))
            matched = int(cur.fetchone()["n"])
        if matched < len(repliers) * 0.8:
            problems.append(f"{angle}/{campaign_id}: EmailBison shows {len(repliers)} genuine repliers, "
                            f"only {matched} are recorded with a reply date in our own records")

    # 3. Lead-id coverage: the booked-tag check and sibling-stop both need eb_lead_id.
    # (Found 2026-10-05: scaling.load_leads never saved it; every lead loaded was blind.)
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) n, count(*) FILTER (WHERE eb_lead_id IS NOT NULL) has_id
                       FROM contacts WHERE eb_campaign_id=%s""", (campaign_id,))
        row = cur.fetchone()
    if row["n"] and row["has_id"] < row["n"] * 0.9:
        problems.append(f"{angle}/{campaign_id}: only {row['has_id']} of {row['n']} contacts have an "
                        f"EmailBison lead id -- the booked-tag check can't see the rest")

    # 4. Primary campaigns need a launch date, or the freeze/decision clock never starts.
    if is_primary:
        with db.cursor() as cur:
            cur.execute("SELECT launched_at FROM copy_test_campaigns WHERE campaign_id=%s", (campaign_id,))
            if not (cur.fetchone() or {}).get("launched_at"):
                problems.append(f"{angle}/{campaign_id}: no launched_at set -- the day-7/day-10 clock "
                                f"has not started")
    return problems


def nightly_reconciliation(run_id: str | None = None) -> dict:
    """Entry point: call once a night. Returns the findings either way; posts to Slack
    only when there is something to say."""
    cfg = testgrid.load_config()
    if cfg is None:
        return {"status": "no_config"}
    testgrid.ensure_tables()
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id, is_primary FROM copy_test_campaigns")
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return {"status": "nothing_registered"}

    all_problems: list[str] = []
    for r in rows:
        all_problems += _campaign_checks(r["angle"], int(r["campaign_id"]), bool(r["is_primary"]))

    db.log(run_id, "reconcile", f"nightly reconciliation: {len(all_problems)} issue(s) across {len(rows)} campaigns")
    if all_problems:
        from .clients import slack
        lines = [":rotating_light: *Nightly check found something off* (would otherwise sit silent):"]
        lines += [f"- {p}" for p in all_problems]
        slack.post("\n".join(lines))
    return {"status": "checked", "campaigns": len(rows), "problems": all_problems}
