"""Company-level stop-on-reply.

EmailBison stops the individual who replied. It does not stop their colleagues, and
with 4 contacts per company that means three more people at the same firm keep getting
pitched after someone there has already answered. This worker closes that gap.

Mechanism, confirmed live 2026-09-07: there is no stop/pause/detach route (all 404).
The only lever is DELETE /api/campaigns/{id}/leads with {"lead_ids": [...]}, which
removes the lead from the campaign.

That removal has a scoring consequence, handled in scored_lead_count() below: removing
siblings decrements EmailBison's own total_leads, so a campaign that gets lots of
replies ends up with a smaller denominator and an inflated interested rate -- exactly
the campaigns whose numbers matter most for picking next quarter's verticals. Stage 1
scores VE campaigns against pushed_lead_count recorded at push time instead.

Runs on its own schedule (every few hours), not with the weekly pipeline: a reply on
Tuesday should not wait until the next Monday to stop the follow-up.
"""

from __future__ import annotations

from datetime import datetime

from .. import db
from ..clients import emailbison


def _when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def contact_outcomes(replies: list[dict]) -> list[dict]:
    """One outcome row per contact from a campaign's reply list.

    Verified live 2026-09-14: each reply carries lead.email, interested,
    automated_reply, folder (Inbox | Bounced | ...) and date_received. An automated
    reply is an out-of-office, not an answer, so it is neither a reply nor a bounce;
    a row in the Bounced folder is a bounce. Interested is OR-ed across a person's
    replies. This is what gives every pushed contact a result, which the pipeline
    had never stored.
    """
    out: dict[str, dict] = {}
    for reply in replies:
        lead = reply.get("lead") or {}
        email = (lead.get("email") or reply.get("from_email_address") or "").strip().lower()
        if "@" not in email:
            continue
        when = _when(reply.get("date_received")) or _when(reply.get("created_at"))
        row = out.setdefault(email, {"email": email, "replied_at": None,
                                     "interested": False, "bounced_at": None})
        if (reply.get("folder") or "").strip().lower() == "bounced":
            row["bounced_at"] = row["bounced_at"] or when
            continue
        if reply.get("automated_reply"):
            continue
        if row["replied_at"] is None or (when and when < row["replied_at"]):
            row["replied_at"] = when
        row["interested"] = row["interested"] or bool(reply.get("interested"))
    return [r for r in out.values() if r["replied_at"] or r["bounced_at"]]


def _domain_of(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    return db.normalize_domain(email.rsplit("@", 1)[1])


def _ve_campaigns() -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM ve_campaigns ORDER BY created_at DESC")
        return cur.fetchall()


def sweep_campaign(campaign: dict) -> dict:
    """Find replies in one campaign and pull the repliers' colleagues."""
    campaign_id = campaign["campaign_id"]
    replies = emailbison.list_replies(campaign_id)

    # Contact-level memory first: every reply and bounce lands on the contact row.
    outcomes = contact_outcomes(replies)
    if outcomes:
        try:
            db.record_contact_outcomes(outcomes)
        except Exception as exc:
            db.log(campaign.get("run_id"), "stop_on_reply",
                   f"outcomes not recorded for campaign {campaign_id}: {exc}",
                   level="warn")

    replied_domains: dict[str, dict] = {}
    for reply in replies:
        # An out-of-office is not a human saying "stop pitching my company".
        if reply.get("automated_reply"):
            continue
        lead = reply.get("lead") or {}
        email = lead.get("email") or reply.get("from_email_address")
        domain = _domain_of(email)
        if not domain:
            continue
        existing = replied_domains.get(domain)
        if not existing or (reply.get("interested") and not existing.get("interested")):
            replied_domains[domain] = {
                "email": email,
                "interested": bool(reply.get("interested")),
                "lead_id": lead.get("id") or reply.get("lead_id"),
            }

    if not replied_domains:
        return {"campaign_id": campaign_id, "replied_companies": 0, "siblings_removed": 0,
                "outcomes_recorded": len(outcomes)}

    # Siblings = our contacts at those domains, in this campaign, that we have not
    # already pulled and that are not the replier themselves.
    with db.cursor() as cur:
        cur.execute(
            """SELECT email, company_domain, eb_lead_id FROM contacts
                WHERE eb_campaign_id = %s
                  AND company_domain = ANY(%s)
                  AND eb_lead_id IS NOT NULL
                  AND status <> 'stopped_sibling'""",
            (campaign_id, list(replied_domains.keys())),
        )
        rows = cur.fetchall()

    to_remove, removed_emails = [], []
    for row in rows:
        replier = replied_domains[row["company_domain"]]
        if row["email"] == (replier["email"] or "").lower():
            continue
        to_remove.append(row["eb_lead_id"])
        removed_emails.append(row["email"])

    removed = emailbison.remove_leads(campaign_id, to_remove) if to_remove else 0

    with db.cursor() as cur:
        if removed_emails:
            cur.execute(
                "UPDATE contacts SET status='stopped_sibling', updated_at=now() "
                " WHERE email = ANY(%s)",
                (removed_emails,),
            )
        for domain, info in replied_domains.items():
            cur.execute(
                """INSERT INTO replied_companies
                       (company_domain, campaign_id, replied_email, interested, siblings_removed)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (company_domain, campaign_id) DO UPDATE
                      SET interested = replied_companies.interested OR EXCLUDED.interested,
                          siblings_removed = replied_companies.siblings_removed
                                             + EXCLUDED.siblings_removed""",
                (domain, campaign_id, info["email"], info["interested"],
                 sum(1 for r in rows if r["company_domain"] == domain)),
            )
        if removed:
            cur.execute(
                "UPDATE ve_campaigns SET removed_count = removed_count + %s "
                " WHERE campaign_id = %s",
                (removed, campaign_id),
            )

    db.log(campaign.get("run_id"), "stop_on_reply",
           f"{campaign['campaign_name']}: {len(replied_domains)} companies replied, "
           f"{removed} siblings removed",
           campaign_id=campaign_id)
    return {"campaign_id": campaign_id, "replied_companies": len(replied_domains),
            "siblings_removed": removed, "outcomes_recorded": len(outcomes)}


def sweep_all() -> list[dict]:
    results = []
    for campaign in _ve_campaigns():
        try:
            results.append(sweep_campaign(campaign))
        except Exception as exc:
            db.log(None, "stop_on_reply",
                   f"sweep failed for campaign {campaign['campaign_id']}: {exc}", level="error")
    return results


def scored_lead_count(campaign_id: int, live_total: int | None) -> int | None:
    """The denominator Stage 1 should use for a VE campaign.

    pushed_lead_count is what we actually sent. EmailBison's live total_leads shrinks
    every time stop-on-reply removes a sibling, so scoring against it would reward a
    campaign for getting replies twice: once in the numerator, once by shrinking the
    denominator.
    """
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT pushed_lead_count FROM ve_campaigns WHERE campaign_id = %s",
                (campaign_id,),
            )
            row = cur.fetchone()
    except Exception:
        return live_total
    if row and row["pushed_lead_count"]:
        return row["pushed_lead_count"]
    return live_total


MEETING_BOOKED_TAG_ID = 49   # EmailBison's own "Meeting Booked" tag (Troy, 2026-10-01)


def sync_booked_from_tags(limit: int = 200) -> dict:
    """Check EmailBison's own 'Meeting Booked' tag for replied contacts, and record it.

    Troy, 2026-10-01: "the meeting booked tag on bison is a booked call" -- a human on
    his team tags a lead in the EmailBison UI once a meeting is booked, and that should
    count in scoring the same as the CLI's `python -m app.runner booked`.

    EmailBison has no working way to bulk-filter or bulk-list leads by tag (the
    documented filter-ignored trap, see memory/emailbison-read-api): tags only appear on
    the single-lead endpoint, GET /api/leads/{id}, one call per lead. So this checks only
    contacts who have ALREADY replied and have no booked_at yet -- a small, bounded pool
    -- never the whole lead universe. `limit` caps the API calls in one pass.
    """
    with db.cursor() as cur:
        cur.execute("""SELECT email, eb_lead_id FROM contacts
                       WHERE replied_at IS NOT NULL AND booked_at IS NULL AND eb_lead_id IS NOT NULL
                       ORDER BY replied_at DESC LIMIT %s""", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    booked = []
    for r in rows:
        try:
            lead = emailbison._get(f"/api/leads/{int(r['eb_lead_id'])}")
        except Exception:
            continue
        # Found 2026-10-05 (Troy: "kirk jones has the meeting booked tag, youre not doing
        # something right"): GET /api/leads/{id} wraps the lead in {"data": {...}}, same
        # as every other EmailBison response, but this read the tags off the UNWRAPPED
        # top level -- which is always empty -- so this had checked 51 replied leads and
        # could never once have found a real tag, by construction, not bad luck.
        tags = ((lead or {}).get("data") or {}).get("tags") or []
        if any(int(t.get("id") or 0) == MEETING_BOOKED_TAG_ID for t in tags if isinstance(t, dict)):
            if db.mark_booked(r["email"], actor="emailbison-tag-sync"):
                booked.append(r["email"])
    if booked:
        db.log(None, "stop_on_reply", f"sync_booked_from_tags: {len(booked)} newly booked from the EmailBison tag")
    return {"checked": len(rows), "newly_booked": len(booked)}
