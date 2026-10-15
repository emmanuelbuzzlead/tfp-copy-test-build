"""Stage 8 -- the look-back loop: judge each tested vertical, then scale it or drop it.

This is what makes the pipeline compound rather than merely repeat. Every VE campaign
that has reached a fair sample gets a verdict, and the verdict is REMEMBERED:

  SCALE  -- cleared the shared winner contract (app/scoring.py: 1,000 sends, 7 days,
            interested/booked outcome value per send). Top the vertical toward scale_target_leads
            (default 10,000) AND split it 3-5 ways into adjacent audiences that get
            queued. A winner that only gets more of the same list does not compound.
  DROP   -- it did not clear the bar on a fair sample. The campaign is archived and the
            avatar is recorded as non-resonant, so Stage 2 stops proposing it.

THE METRIC. Judgement is on interested replies plus explicit booked-meeting value per
send. Share-of-replies and share-of-leads are still stored as diagnostics. Confidence
bounds prevent a thin early result from becoming a scale/drop decision.

A vertical with too few sends is left PENDING, not dropped. Judging an avatar on 200
sends and recording it as a failure would poison the memory with noise, and the memory
is the whole point.
"""

from __future__ import annotations

from .. import db, scoring
from ..clients import emailbison, slack
from ..clients.http import VendorOutage
from ..config import settings

# How many consecutive "label_keep_rate_too_low" nights pause a vertical for a human
# decision, rather than let it grind forever or wrongly declare it exhausted. Troy,
# 2026-09-25.
POOR_RESULT_STREAK_LIMIT = 5


def _ve_campaigns() -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM ve_campaigns ORDER BY created_at")
        return cur.fetchall()


def record_outcome(vertical: str, verdict: str, stats: dict, reason: str,
                   pooled: bool = False) -> None:
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO vertical_outcomes
                   (vertical, verdict, campaign_id, run_id, leads, sends, replies,
                    interested, rate_of_replies, rate_of_leads, seed_industry, reason,
                    booked, objective_rate, confidence_low, confidence_high,
                    pooled, decided_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT (vertical) DO UPDATE
                  SET verdict=EXCLUDED.verdict, campaign_id=EXCLUDED.campaign_id,
                      leads=EXCLUDED.leads, sends=EXCLUDED.sends,
                      replies=EXCLUDED.replies, interested=EXCLUDED.interested,
                      rate_of_replies=EXCLUDED.rate_of_replies,
                      rate_of_leads=EXCLUDED.rate_of_leads, reason=EXCLUDED.reason,
                      booked=EXCLUDED.booked, objective_rate=EXCLUDED.objective_rate,
                      confidence_low=EXCLUDED.confidence_low,
                      confidence_high=EXCLUDED.confidence_high,
                      pooled=EXCLUDED.pooled,
                      decided_at=now(), updated_at=now()""",
            (vertical, verdict, stats.get("campaign_id"), stats.get("run_id"),
             stats.get("leads"), stats.get("sends"), stats.get("replies"),
             stats.get("interested"), round(stats.get("of_replies", 0), 5),
             round(stats.get("of_leads", 0), 5), stats.get("seed_industry"), reason,
             stats.get("booked", 0), stats.get("objective_rate", 0),
             stats.get("confidence_low", 0), stats.get("confidence_high", 0),
             pooled),
        )
        cur.execute(
            """INSERT INTO decision_snapshots
                   (vertical,campaign_id,run_id,verdict,sends,replies,interested,booked,
                    objective_rate,confidence_low,confidence_high,reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (vertical, stats.get("campaign_id"), stats.get("run_id"), verdict,
             stats.get("sends", 0), stats.get("replies", 0), stats.get("interested", 0),
             stats.get("booked", 0), stats.get("objective_rate", 0),
             stats.get("confidence_low", 0), stats.get("confidence_high", 0), reason))


def judged_verticals() -> dict[str, dict]:
    """Everything the pipeline has learned. Fed into Stage 2 so it stops guessing blind."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM vertical_outcomes")
        return {r["vertical"]: r for r in cur.fetchall()}


def review_campaign(camp: dict) -> dict:
    """Judge one VE campaign. Returns the verdict record."""
    campaign_id = camp["campaign_id"]
    vertical = camp["vertical"]

    # A copy-test ARM is not an audience and must not be judged as one. Two things go
    # wrong if it is: the absolute bar is meant to ask "does this market respond", which
    # is not the question a test is asking, and a losing arm would be filed in
    # vertical_outcomes under a name like "copy-test a-sleep-test" -- permanently barring
    # a market that does not exist from ever being proposed. Arms are compared with each
    # other in app/conclude.py instead.
    if camp.get("copy_variant") or str(vertical or "").startswith("copy-test "):
        reason = (f"copy-test arm {camp.get('copy_variant') or vertical!r}: arms are "
                  f"compared with each other, not against the audience bar")
        return {"campaign_id": campaign_id, "vertical": vertical, "verdict": "skipped",
                "reason": reason, "leads": 0, "sends": 0, "replies": 0, "interested": 0,
                "of_replies": 0.0, "of_leads": 0.0}

    live = emailbison.get_campaign(campaign_id) or {}
    # AN ARCHIVED CAMPAIGN IS DONE, not a fresh audience to re-judge. Troy, 2026-10-07:
    # Conversational AI was explicitly dropped and its campaign archived (too small to
    # ever reach scale_min_sends on its own), and the very next nightly review re-judged
    # it on its old, now-frozen sends/replies anyway, overwriting the drop verdict back
    # to 'pending' through record_outcome's upsert and silently re-occupying a test seat.
    # _ve_campaigns() never filters by status -- a row stays forever once created -- so
    # this is the one place that actually stops an archived campaign from being judged
    # again, by anyone, for any reason, ever.
    if (live.get("status") or "").lower() == "archived":
        reason = f"{vertical}: campaign {campaign_id} is archived; not re-judged"
        return {"campaign_id": campaign_id, "vertical": vertical, "verdict": "skipped",
                "reason": reason, "leads": 0, "sends": 0, "replies": 0, "interested": 0,
                "of_replies": 0.0, "of_leads": 0.0}
    sends = live.get("emails_sent") or 0
    replies = live.get("replied") or 0
    interested = live.get("interested") or 0
    try:
        booked = db.campaign_bookings(campaign_id)
    except Exception:
        # Historical/offline reviews may have no local DB outcome projection yet.
        booked = 0

    # The denominator is what WE pushed, not EmailBison's live total_leads: stop-on-reply
    # removes sibling leads from the campaign, which would otherwise shrink the
    # denominator of exactly the campaigns performing best.
    leads = camp.get("pushed_lead_count") or live.get("total_leads") or 0

    verdict = scoring.judge(sends=sends, replies=replies, interested=interested,
                            booked=booked,
                            leads=leads, launched=camp.get("created_at"),
                            progress=live.get("completion_percentage"))
    stats = {
        "campaign_id": campaign_id, "run_id": camp.get("run_id"), "leads": leads,
        "sends": sends, "replies": replies, "interested": interested, "booked": booked,
        "of_replies": verdict.of_replies, "of_leads": verdict.of_leads,
        "of_sends": verdict.of_sends,
        "objective_rate": verdict.rate, "confidence_low": verdict.confidence_low,
        "confidence_high": verdict.confidence_high,
        "seed_industry": camp.get("seed_industry"),
    }
    mapped = {"winner": "scale", "loser": "drop", "pending": "pending"}[verdict.status]
    record_outcome(vertical, mapped, stats, verdict.reason)
    return {**stats, "vertical": vertical, "verdict": mapped, "reason": verdict.reason}


def review_pool(camp: dict, run_id: str | None = None) -> list[dict]:
    """Judge each vertical inside a pooled campaign separately, from our own data.

    EmailBison reports `interested` per CAMPAIGN, so a pool's number is blended across
    every vertical in it and says nothing about which one works. stop-on-reply already
    records each replying company and whether it was interested, and every contact
    carries its vertical, so the numerator is ours. The denominator is not: sends exist
    only at campaign level, so each vertical is credited its SHARE of the campaign's
    sends, in proportion to its leads. Every lead here runs one sequence on one
    schedule, which is what makes that apportionment fair rather than a guess.

    Our reply data SPLITS well but does not COUNT well. Measured on campaign 1084,
    2026-09-22: replied_companies holds 24 of EmailBison's 78 replies (31%) but 7 of its
    8 interested (88%) -- stop-on-reply records a company once, when it sweeps. Using
    our counts raw would have scored Biotechnology at 7/24 = 29% against its true
    10.26%, nearly triple, and every pooled vertical would look like a winner.

    So EmailBison's campaign totals stay the truth and our data only decides how to
    divide them: sends and replies by each vertical's share of the leads, interested by
    the share our own reply data actually observed. The parts always sum to the
    campaign.

    A verdict here is recorded and reported but NOT acted on: apply_drop archives a
    whole campaign, and archiving a pool to kill one vertical inside it would take the
    others with it. Removing one vertical's leads is a different operation and is not
    built yet, so a pooled loser is reported for a human to act on.
    """
    cid = int(camp["campaign_id"])
    live = emailbison.get_campaign(cid) or {}
    sends = live.get("emails_sent") or 0
    replies = live.get("replied") or 0
    interested = live.get("interested") or 0
    progress = live.get("completion_percentage")
    parts = db.verticals_in_campaign(cid)
    total_leads = sum(int(p["leads"] or 0) for p in parts) or 1
    seen_interested = sum(int(p["interested"] or 0) for p in parts)

    out = []
    for p in parts:
        leads = int(p["leads"] or 0)
        share = leads / total_leads
        # Interested is the one signal our data splits credibly -- but only once enough
        # of it exists to be a split rather than a coin landing. With ONE observed
        # interested company in the whole pool, whichever vertical it fell in would take
        # 100% of the campaign's interested against its own small share of the sends: a
        # 200-lead vertical holding 5% of the leads would read ~20x its true rate and go
        # straight to SCALE. Below the floor, fall back to the lead share, which credits
        # everyone proportionally and can crown nobody.
        i_share = share
        if seen_interested >= settings.pool_min_observed_interested:
            i_share = int(p["interested"] or 0) / seen_interested
        out.append(_score_pool_part(camp, p, cid, sends, replies, interested,
                                    share, i_share, progress, run_id))
    if not out:
        db.log(run_id, "stage8",
               f"pooled campaign {cid}: no contacts carry a vertical, nothing to score",
               level="warn")
    return out


def _score_pool_part(camp: dict, part: dict, cid: int, sends: int, camp_replies: int,
                     camp_interested: int, share: float, i_share: float,
                     progress, run_id: str | None) -> dict:
    vertical = part["vertical"]
    leads = int(part["leads"] or 0)
    my_sends = int(sends * share)
    # EmailBison's totals, divided. Never our own raw counts: they undercount replies
    # by about three to one and would inflate every pooled rate.
    my_replies = int(camp_replies * share)
    my_interested = int(round(camp_interested * i_share))
    verdict = scoring.judge(sends=my_sends, replies=my_replies,
                            interested=my_interested, booked=0, leads=leads,
                            launched=camp.get("created_at"), progress=progress)
    stats = {
        "campaign_id": cid, "run_id": camp.get("run_id"), "leads": leads,
        "sends": my_sends, "replies": my_replies, "interested": my_interested,
        "booked": 0,
        "of_replies": verdict.of_replies, "of_leads": verdict.of_leads,
        "of_sends": verdict.of_sends, "objective_rate": verdict.rate,
        "confidence_low": verdict.confidence_low,
        "confidence_high": verdict.confidence_high,
        "seed_industry": camp.get("seed_industry"),
    }
    mapped = {"winner": "scale", "loser": "drop", "pending": "pending"}[verdict.status]
    reason = (f"{verdict.reason} [pooled in campaign {cid}, "
              f"{share:.0%} of its sends]")
    record_outcome(vertical, mapped, stats, reason, pooled=True)
    db.log(run_id, "stage8", f"{vertical} (pooled): {mapped.upper()} -- {reason}")
    # 'pooled' tells review_all not to archive or scale this one: both act on the whole
    # campaign, and the campaign is shared.
    return {**stats, "vertical": vertical, "verdict": mapped, "reason": reason,
            "pooled": True}


def apply_drop(result: dict) -> None:
    """Archive the campaign so a failed avatar stops consuming sending capacity."""
    try:
        out = emailbison.archive_campaign(result["campaign_id"]) or {}
        if out.get("senders_freed"):
            db.log(result.get("run_id"), "stage8",
                   f"archived campaign {result['campaign_id']} ({result['vertical']}): "
                   f"{result['reason']}")
        else:
            # A pause route answered, not /archive. senders.ACTIVE_STATUSES counts
            # "paused", so the accounts are NOT free -- and phase 2 runs drops before
            # scale-ups on the belief that they are. Saying "archived" here is how this
            # went unnoticed for days: two campaigns sat paused, still holding accounts,
            # while the log said they had been dealt with.
            db.log(result.get("run_id"), "stage8",
                   f"campaign {result['campaign_id']} ({result['vertical']}) was only "
                   f"PAUSED via {out.get('route')}, not archived: it has stopped sending "
                   f"but its sender accounts are STILL HELD. Archive it in the UI to get "
                   f"the capacity back.", level="error")
            slack.post(
                f":warning: *{result['vertical']}* was dropped but only *paused*, not "
                f"archived (campaign {result['campaign_id']}). It has stopped sending, "
                f"but its sender accounts are still tied up. Archive it in EmailBison "
                f"to release them.")
    except Exception as exc:
        # Loud, not a warning. The one guarded case -- the last campaign that can serve
        # as a template -- means a drop verdict will silently do nothing on every review
        # from now on while the campaign keeps sending.
        db.log(result.get("run_id"), "stage8",
               f"could not archive campaign {result['campaign_id']} "
               f"({result['vertical']}): {exc}", level="error")
        slack.post(
            f":rotating_light: *{result['vertical']}* was judged DROP but campaign "
            f"{result['campaign_id']} could not be archived: {exc}. It is still sending "
            f"and still holding its sender accounts.")


def recyclable_contacts(vertical: str, limit: int) -> list[dict]:
    """Contacts not emailed in the last recontact_days, for topping a scaled vertical up.

    Backfill rows carry an ingestion date, not a send date, so contacts.updated_at
    cannot answer "when did we last email this person". recent_contacts can. Anyone
    absent from that window is fair game -- 90 days, not forever.
    """
    cutoff = settings.recontact_days
    with db.cursor() as cur:
        cur.execute(
            """SELECT c.email, c.first_name, c.last_name, c.title, c.company_domain
                 FROM contacts c
                 LEFT JOIN companies co ON co.domain = c.company_domain
                WHERE c.status = 'pushed'
                  AND (co.vertical = %s OR %s IS NULL OR co.vertical IS NULL)
                  AND NOT EXISTS (
                        SELECT 1 FROM recent_contacts r
                         WHERE r.email = c.email
                           AND r.campaign_date >= CURRENT_DATE - make_interval(days => %s)
                  )
                ORDER BY c.updated_at ASC
                LIMIT %s""",
            (vertical, vertical, cutoff, limit),
        )
        return cur.fetchall()


def finish_banked_work(run_id: str | None) -> list[dict]:
    """Write copy for contacts already bought, and open a batch. No sourcing.

    Troy, 2026-09-23, on being told Medical Device's banked contacts would be picked up
    "automatically": "why is it still among the 2 gets scaled?" They were the same
    action, and that was wrong. Finishing banked work and scaling are different costs:

      finishing  -- one LLM call per contact, on contacts already sourced, enriched and
                    verified. Minutes. No new companies, no sender accounts.
      scaling    -- hours of sourcing, and send capacity the workspace does not have.

    The cap of two a day exists for the second. Applying it to the first left 3,843
    paid-for contacts rotting across 11 verticals, and left a PENDING vertical with no
    route to a batch at all: Biotechnology sat on 255 it could never finish, because it
    would never win a scale slot.

    This does NOT mean the vertical is done. Medical Device also had 655 companies kept
    and never searched; those are sourcing, they stay sourcing, and they wait for a
    scale slot. This only converts what is already bought into something sendable.

    RUNS SIDE BY SIDE, same move already made for the weekly/resume sourcing path and
    the daily review's scale-up winners (see the parallel lanes below in this file).
    This was sequential, one vertical at a time: Troy, 2026-10-05, after Commercial
    Real Estate's 2,884-contact pile monopolized an entire tick while Specialty Legal
    (603) and Corporate Learning (120) sat waiting their turn behind it, despite
    nothing actually blocking them. Safe to parallelise for the same reason the
    scale-up winners were: this does no sourcing and attaches no sender accounts, so
    verticals are not racing each other for anything, and the LLM semaphore is global
    (app/clients/llm.py's _LLM_GATE, capped at LLM_MAX_CONCURRENCY) -- it already caps
    real concurrent provider calls regardless of how many verticals ask at once, the
    same guarantee that already makes the sourcing path and the scale-up lanes safe.
    """
    import threading
    out: list[dict] = []
    lock = threading.Lock()
    banked = db.banked_awaiting_copy(min_contacts=settings.banked_min_contacts)
    # LEAST-SCALED FIRST, same rule as the nightly lanes. banked_awaiting_copy orders by
    # the size of each backlog, so the verticals with the biggest piles finished first:
    # on 2026-09-26 Venture Capital (4,005) and Medical Device (2,091) went ahead of
    # Product Design (1,045) -- the vertical that had already lost a day to a crash.
    # Troy: "why didn't product design and edtech run first?" A vertical with no
    # campaign yet counts as 0 scaled, so it goes first of all.
    scaled = {c["vertical"]: c.get("pushed_lead_count") or 0 for c in _ve_campaigns()}
    banked.sort(key=lambda row: scaled.get(row["vertical"], 0))
    if not banked:
        return out

    def _run(row: dict) -> None:
        name = row["vertical"]
        try:
            result = _finish_one_vertical(name, row["n"], run_id)
            with lock:
                out.append(result)
        except Exception as exc:
            db.log(run_id, "stage8",
                   f"could not finish banked work for {name}: {exc}", level="error")

    lanes = max(1, min(settings.parallel_verticals, len(banked)))
    db.log(run_id, "stage8",
           f"finishing banked work for {len(banked)} vertical(s), {lanes} at a time, "
           f"least-scaled first: " + ", ".join(
               f"{row['vertical']} ({row['n']:,})" for row in banked))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=lanes) as pool:
        list(pool.map(_run, banked))
    return out


def _finish_one_vertical(name: str, n: int, run_id: str | None) -> dict:
    from .s2_expansion import Vertical
    from . import s5_copy, s6_approval

    cfg = db.vertical_config_for(name)
    if not cfg:
        db.log(run_id, "stage8",
               f"{name}: {n} banked contacts but no stored filter config to batch them "
               f"under", level="warn")
        return {"vertical": name, "finished": 0, "reason": "no config"}

    contacts = db.verified_uncopied(name)
    db.log(run_id, "stage8",
           f"{name}: finishing {len(contacts)} contacts that were already sourced and "
           f"verified -- copy only, no sourcing")
    ready, _ = s5_copy.run(contacts, run_id)
    if not ready:
        return {"vertical": name, "finished": 0, "reason": "no copy passed QA"}

    # Top up the vertical's existing campaign when it has one, so its stats stay on one
    # campaign the review can score, rather than opening a rival to it.
    camp = db.live_campaign_for_vertical(name)
    summary = {"finished_banked_work": True}
    if camp:
        summary["scale_up_of_campaign"] = camp
    companies = [{"domain": d, "name": None, "vertical": name}
                 for d in {c["company_domain"] for c in ready}]
    # Top up an already-open APPROVAL batch too, instead of opening a rival one. The
    # line above only ever guarded the EmailBison campaign; this call still opened a
    # brand new Slack batch every time it ran. Troy, 2026-09-25: "why don't I get one
    # push per vertical" -- a vertical finishing banked work most nights it scaled
    # piled up one more unapproved batch and one more Slack post on top of whatever a
    # human had not yet answered, exactly like the runner.py weekly path already
    # guards against for its own batches.
    existing = db.pending_batch_for(name)
    if existing:
        added = db.add_to_batch(existing["batch_id"], run_id, ready, companies, name)
        batch_id = existing["batch_id"]
        db.log(run_id, "stage8", f"{name}: added {added} contacts to the pending batch "
               f"{batch_id} (was {existing['contact_count']})")
        # Batch small top-ups into one notification -- see runner.py's identical guard.
        running_total = db.record_batch_addition(batch_id, added)
        if running_total >= settings.notify_added_threshold:
            slack.post(f":package: *{name}* — {running_total:,} more contacts added to "
                       f"the batch already awaiting your approval"
                       + (f" (tops up campaign {camp})" if camp else ""))
            db.reset_batch_notification(batch_id)
    else:
        batch_id = s6_approval.open_batch(run_id, Vertical.from_json(cfg), companies,
                                          ready, summary)
        slack.post(f":package: *{name}* — {len(ready):,} contacts that were already paid "
                   f"for now have copy and are waiting for approval"
                   + (f" (tops up campaign {camp})" if camp else ""))
    # AUTO-APPROVE A TOP-UP, same rule as runner.py's auto-topup path (Troy, 2026-09-30:
    # "top-ups into an already-approved campaign auto-approve"). This batch ALWAYS tops
    # up an already-launched campaign -- `camp` is only set when one exists, and this
    # function only ever runs on banked work for a vertical that already has a stored
    # config -- so the check was missing here even though the rule applied. Troy,
    # 2026-10-08: found 5 scale-up batches sitting pending for up to 2 days (Consumer
    # Edtech, Funded Proptech) because this call existed only on runner.py's path, never
    # on s8_review's two batch-opening sites.
    if camp:
        try:
            s6_approval.auto_approve_if_topup(batch_id, name, run_id)
        except Exception as exc:
            db.log(run_id, "stage8", f"{name}: auto-approve check failed: {exc}",
                  level="warn")
    return {"vertical": name, "finished": len(ready), "batch_id": batch_id}


def mark_scale_exhausted(campaign_id: int, vertical: str, at_leads: int,
                         run_id: str | None) -> None:
    """Stop re-scaling a vertical whose supply is spent, and SAY SO plainly.

    Troy, 2026-09-23: "if we dont have up to that then its the end of the scaling for
    that campaign and we dont bother scaling again." Until now report_gap only posted a
    Slack warning and returned, so the next daily review saw the same winner still short
    of scale_target_leads and re-sourced its entire exhausted universe again -- paying
    for the search every day to rediscover the same nothing. Biotechnology alone scanned
    9,557 companies for its last 34 fresh ones.

    Troy, 2026-09-24: "do i get an exhausted tag on slack that x vertical is exhausted
    and closed?" Not clearly. report_gap's message reads as a temporary shortfall
    ("cleared the bar... but only X available"), not "this is now closed" -- and it is
    the ONLY message: the early-exit in scale_vertical returns a reason string every day
    after, but nothing ever posts it, so the vertical goes permanently silent. This is
    now the one place that announces the CLOSED state, distinct from report_gap's gap
    explanation, so it cannot be missed or mistaken for something that will retry.

    Cleared by hand if the audience is widened (employee size, geography, titles), since
    that is a different universe and worth another look.
    """
    with db.cursor() as cur:
        cur.execute("UPDATE ve_campaigns SET scale_exhausted_at = now(), "
                    "       scale_exhausted_at_leads = %s "
                    " WHERE campaign_id = %s AND scale_exhausted_at IS NULL",
                    (at_leads, campaign_id))
        if cur.rowcount:
            db.log(run_id, "stage8",
                   f"campaign {campaign_id}: scaling closed at {at_leads:,} leads of "
                   f"{settings.scale_target_leads:,} -- supply is spent, it will not be "
                   f"re-sourced again unless the audience is widened")
            slack.post(
                f":lock: *{vertical}* is now EXHAUSTED and CLOSED to further scaling, "
                f"at {at_leads:,} of {settings.scale_target_leads:,} leads. Its own "
                f"supply is spent -- this will not be re-sourced or mentioned again "
                f"unless a human widens the audience (employee size, geography or "
                f"titles) to reopen it."
            )


def report_gap(result: dict, available: int) -> None:
    """Tell a human when a SCALE vertical cannot be filled to target."""
    short = settings.scale_target_leads - available
    slack.post(
        f"{settings.gap_notify_slack} *Lead gap on a scaling vertical*\n"
        f"*{result['vertical']}* cleared the bar "
        f"({result['objective_rate']:.2%} outcome value on {result['sends']} sends) "
        f"and should scale to {settings.scale_target_leads:,} leads, but only "
        f"{available:,} are available -- short {short:,}.\n"
        f"Fresh sourcing is exhausted and recycling covers part of the gap. "
        f"Widening employee size, geography or the title list would open more."
    )
    db.log(result.get("run_id"), "stage8",
           f"lead gap on {result['vertical']}: {available} of "
           f"{settings.scale_target_leads}", level="warn")


def scale_vertical(camp: dict, result: dict, run_id: str,
                   deadline_minutes: int | None = None) -> dict:
    """Top a winning vertical up toward scale_target_leads.

    Re-sources against the vertical's OWN stored filter set, so the audience being
    scaled is the audience that was judged rather than a fresh guess at it. Dedup means
    the companies already used are skipped automatically, so this genuinely extends the
    list instead of re-buying it. It never widens into sister labels: a sister spent on
    the winner cannot be tested on its own for 90 days, and its replies would be
    credited to the winner. Sisters get their own test from Monday's proposal instead.

    The top-up still goes through the Slack gate. A scale-up is new people receiving new
    copy, and the rule that nothing sends without approval does not get an exception for
    having done well.
    """
    from .s2_expansion import Vertical
    from . import s3_companies, s4_people, s5_copy, s6_approval

    cfg = camp.get("vertical_config")
    if not cfg:
        db.log(run_id, "stage8",
               f"{result['vertical']}: cannot scale, no stored filter config", level="warn")
        return {"vertical": result["vertical"], "scaled": 0, "reason": "no config"}

    if isinstance(cfg, str):
        import json as _json
        cfg = _json.loads(cfg)
    vertical = Vertical.from_json(cfg)

    have = camp.get("pushed_lead_count") or 0
    need = max(settings.scale_target_leads - have, 0)
    if need <= 0:
        return {"vertical": vertical.name, "scaled": 0, "reason": "already at target"}

    # ALREADY SPENT. Troy, 2026-09-23: "when something is scaled it should be marked if
    # exhausted so the daily doesnt top up scaled ones." A vertical that could not be
    # filled last time cannot be filled today either -- its universe did not grow
    # overnight -- and re-sourcing it means walking the whole thing again to rediscover
    # the same nothing. Biotechnology scanned 9,557 companies for its last 34.
    if camp.get("scale_exhausted_at"):
        return {"vertical": vertical.name, "scaled": 0,
                "reason": f"supply spent at {camp.get('scale_exhausted_at_leads')} leads "
                          f"on {camp['scale_exhausted_at']:%d %b}; widen the audience to "
                          f"reopen it"}

    # A REAL SUPPLY SIGNAL never fires (see genuinely_exhausted below), so this is the
    # other kind of stuck: the vertical's own labels keep coming back a poor match,
    # attempt after attempt, without ever actually running the fetch dry. Left alone
    # that grinds forever -- ~$3 in fit-check spend a night, no automatic ceiling.
    # Troy, 2026-09-25, choosing a human checkpoint over either extreme (permanently
    # closing it, or silently retrying it forever): 5 consecutive poor nights pauses
    # auto-scaling and asks a human to decide (widen employee size/geography/titles,
    # or accept it as genuinely thin) -- NOT scale_exhausted_at, so it is a pause, not
    # a permanent close, and it is cleared the same way.
    if camp.get("scale_needs_human_at"):
        return {"vertical": vertical.name, "scaled": 0,
                "reason": f"{POOR_RESULT_STREAK_LIMIT} consecutive poor-match nights as of "
                          f"{camp['scale_needs_human_at']:%d %b}; paused for a human decision"}

    db.log(run_id, "stage8",
           f"{vertical.name}: scaling, have {have}, need {need} more to reach "
           f"{settings.scale_target_leads}")

    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    # deadline_minutes overrides the config budget when the CALLER has less time than
    # the scale budget -- the two-hourly tick lives inside a 2-hour cron slot, and a
    # 240-minute sourcing deadline inside a 120-minute slot is a guaranteed kill
    # (Consumer Edtech died at its tick's 2-hour mark on 2026-10-04 and again on
    # 2026-10-01). Partial is fine: everything is banked, the next tick continues.
    deadline = _time.monotonic() + (deadline_minutes
                                    or settings.scale_deadline_minutes) * 60

    # BANKED WORK FIRST, and it is free. A scale-up that died mid-flight leaves verified
    # contacts with no copy and no batch -- 2,173 on 2026-09-22, another 1,382 the next
    # day. scale_vertical went straight to sourcing and never looked, so those were
    # orphaned until a human noticed. The weekly run has always picked them up
    # (db.verified_uncopied); only this path did not.
    leftover = db.verified_uncopied(vertical.name)[:need]
    if leftover:
        db.log(run_id, "stage8",
               f"{vertical.name}: {len(leftover)} verified contacts left by an earlier "
               f"run go into copy now, while sourcing looks for more")

    # COPY RUNS ALONGSIDE FINDING. They do not compete -- the finders spend QuickEnrich,
    # AI Ark and the verification waterfall, copy spends the LLM -- and copy is banked
    # per contact as it is written. Sequentially, everything found sits raw until the
    # last company is searched, which is why a death at hour two lost the lot twice.
    # Troy, 2026-09-14: "if we have contacts awaiting copy for the first 3 why not
    # resume copy even at stage 4?" The weekly run has done this since; the scale-up
    # had not.
    source_stats: dict = {}

    def _source() -> tuple[list[dict], list[dict]]:
        # Own supply only -- no sister labels on a scale-up. See s3_companies.find().
        cos, stats = s3_companies.find(vertical, run_id, target_contacts=need, widen=False)
        source_stats.update(stats)
        db.record_market_scan(camp["campaign_id"], stats.get("companies_found", 0),
                              stats.get("available_in_aiark"))
        if not cos:
            return [], []
        found, _ = s4_people.build(cos, run_id, deadline=deadline)
        return cos, found

    ready: list[dict] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_src = pool.submit(_source)
        fut_copy = pool.submit(s5_copy.run, leftover, run_id) if leftover else None
        companies, contacts = fut_src.result()
        if fut_copy:
            ready.extend(fut_copy.result()[0])

    # Only a GENUINE supply signal ("source_exhausted" -- the fetch itself returned
    # nothing more) closes a vertical forever. "label_keep_rate_too_low" means sourcing
    # chose not to keep paying to judge a poorly-matched label -- correct, since
    # widen=False means it cannot fall back to a sibling -- but it says nothing about
    # whether real supply remains; Venture Capital and Product Design were both marked
    # exhausted this way having scanned 12-20% of their own available pool. Troy,
    # 2026-09-25.
    genuinely_exhausted = source_stats.get("stop_reason") == "source_exhausted"

    # A run of POOR MATCHES, not a genuine supply signal, is the other way a vertical
    # gets stuck: never exhausted, but never making real progress either. Troy,
    # 2026-09-25: 5 consecutive nights of this pauses auto-scaling for a human decision
    # instead of grinding forever (~$3 in fit-check spend a night) or wrongly declaring
    # it exhausted. Resets the moment a night actually finds real companies.
    poor_match = (not genuinely_exhausted
                 and source_stats.get("stop_reason") == "label_keep_rate_too_low"
                 and not companies)
    if poor_match:
        streak = db.record_poor_result(camp["campaign_id"])
        if streak >= POOR_RESULT_STREAK_LIMIT:
            db.mark_needs_human(camp["campaign_id"])
            slack.post(
                f":warning: *{vertical.name}* has come back a poor match on its own "
                f"labels for {streak} nights running -- never a genuine \"no companies "
                f"left\" signal, just a weak keep rate every time. Paused rather than "
                f"kept grinding: widen employee size, geography or the title list to "
                f"give it a real shot, or leave it paused if the audience is genuinely "
                f"this thin.")
    elif companies or genuinely_exhausted:
        db.reset_poor_streak(camp["campaign_id"])

    if not companies and not ready:
        report_gap(result, have)
        if genuinely_exhausted:
            mark_scale_exhausted(camp["campaign_id"], vertical.name, have, run_id)
        else:
            db.log(run_id, "stage8",
                   f"{vertical.name}: no fresh companies this pass "
                   f"({source_stats.get('stop_reason', 'unknown')}), but not marking "
                   f"exhausted -- real supply may remain; will try again")
        return {"vertical": vertical.name, "scaled": 0, "reason": "no fresh companies"}
    if not contacts and not ready:
        report_gap(result, have)
        if genuinely_exhausted:
            mark_scale_exhausted(camp["campaign_id"], vertical.name, have, run_id)
        else:
            db.log(run_id, "stage8",
                   f"{vertical.name}: no verifiable contacts this pass "
                   f"({source_stats.get('stop_reason', 'unknown')}), but not marking "
                   f"exhausted -- real supply may remain; will try again")
        return {"vertical": vertical.name, "scaled": 0, "reason": "no verifiable contacts"}

    # Copy for whatever the finders turned up. This always runs to completion even past
    # the deadline: copy is ~95% of the model spend and these contacts are already paid
    # for, so stopping here would throw away the expensive half of the work.
    room = max(need - len(ready), 0)
    if contacts and room:
        ready.extend(s5_copy.run(contacts[:room], run_id)[0])
    if not ready:
        return {"vertical": vertical.name, "scaled": 0, "reason": "no copy passed QA"}
    companies = companies or [{"domain": d, "name": None, "vertical": vertical.name}
                              for d in {c["company_domain"] for c in ready}]

    # Top up an already-open APPROVAL batch instead of opening a rival one -- the same
    # fix as _finish_one_vertical above. This is the path that ran nightly for every
    # scaling winner, so it was the biggest source of the pile-up: a vertical scaled on
    # 5 different nights got 5 separate unapproved batches and 5 separate Slack posts,
    # instead of one growing batch with one thread of updates.
    existing = db.pending_batch_for(vertical.name)
    if existing:
        added = db.add_to_batch(existing["batch_id"], run_id, ready, companies,
                                vertical.name)
        batch_id = existing["batch_id"]
        db.log(run_id, "stage8", f"{vertical.name}: added {added} contacts to the "
               f"pending batch {batch_id} (was {existing['contact_count']})")
        # Batch small top-ups into one notification -- see runner.py's identical guard.
        running_total = db.record_batch_addition(batch_id, added)
        if running_total >= settings.notify_added_threshold:
            slack.post(f":package: *{vertical.name}* — {running_total:,} more contacts "
                       f"added to the batch already awaiting your approval (scale-up of "
                       f"campaign {camp['campaign_id']})")
            db.reset_batch_notification(batch_id)
    else:
        batch_id = s6_approval.open_batch(run_id, vertical, companies, ready,
                                          {"scale_up_of_campaign": camp["campaign_id"]})
    # AUTO-APPROVE A TOP-UP, same rule as runner.py's auto-topup path (Troy, 2026-09-30:
    # "top-ups into an already-approved campaign auto-approve"). A scale-up is BY
    # DEFINITION a top-up of camp["campaign_id"], which already exists and already sent
    # -- yet this call was never wired in here, only on runner.py's own path. Troy,
    # 2026-10-08: found 5 scale-up batches sitting pending for up to 2 days (Consumer
    # Edtech, Funded Proptech) because of exactly this gap -- a winner could scale every
    # night and still never actually reach EmailBison without a human manually
    # approving each batch by hand.
    try:
        s6_approval.auto_approve_if_topup(batch_id, vertical.name, run_id)
    except Exception as exc:
        db.log(run_id, "stage8", f"{vertical.name}: auto-approve check failed: {exc}",
              level="warn")
    # SHORT OF TARGET, but WHY matters. Out of time is not out of supply: marking a
    # vertical exhausted because the clock ran out would permanently stop scaling an
    # audience that still has thousands of companies left, and only a human clearing the
    # flag would bring it back. Out of time means partial -- tomorrow's review picks it
    # up from the higher base, which is the whole point of the deadline. Neither is
    # "sourcing chose not to keep judging a poorly-matched label" (source_stats above) --
    # that is a label-precision signal, not proof the rest of the market does not exist,
    # and conflating it with genuine exhaustion is exactly how Venture Capital and
    # Product Design got permanently closed at 12-20% of their own real supply scanned.
    out_of_time = _time.monotonic() >= deadline
    if have + len(ready) < settings.scale_target_leads:
        if out_of_time or not genuinely_exhausted:
            reason = ("its time budget" if out_of_time
                      else f"a label-precision stop ({source_stats.get('stop_reason', 'unknown')})")
            db.log(run_id, "stage8",
                   f"{vertical.name}: stopped on {reason} at "
                   f"{have + len(ready):,} of {settings.scale_target_leads:,} leads. "
                   f"Supply is NOT confirmed spent; the next review continues from here.")
        else:
            report_gap(result, have + len(ready))
            mark_scale_exhausted(camp["campaign_id"], vertical.name, have + len(ready), run_id)
    return {"vertical": vertical.name, "scaled": len(ready), "batch_id": batch_id,
            "partial": out_of_time or not genuinely_exhausted}


def expand_from_winner(result: dict, run_id: str) -> dict:
    """A scaled vertical becomes a seed: split it 3-5 ways and queue the adjacents.

    This is the compounding loop. Without it a winner only gets more of the same
    list, and Stage 1 kept ranking the old Funded Brands campaigns because new
    verticals never re-entered as seeds.
    """
    from . import s2_expansion, s2b_freshness

    seed = {
        "name": result["vertical"],
        "vertical_label": result.get("seed_industry") or result["vertical"],
        "leads": result.get("leads"),
        "sends": result.get("sends"),
        "replies": result.get("replies"),
        "interested": result.get("interested"),
        "interested_rate": result.get("objective_rate"),
        "of_replies": result.get("of_replies"),
        "of_sends": result.get("of_sends"),
    }
    already = db.recent_verticals(settings.lookback_days)
    already.add(result["vertical"])
    outcomes = judged_verticals()
    try:
        proposed = s2_expansion.propose_for_seed(seed, already, outcomes)
    except Exception as exc:
        db.log(run_id, "stage8",
               f"{result['vertical']}: adjacent split failed: {exc}", level="warn")
        return {"proposed": [], "queued": 0, "error": str(exc)}
    if not proposed:
        db.log(run_id, "stage8", f"{result['vertical']}: no adjacent verticals proposed")
        return {"proposed": [], "queued": 0}
    kept, freshness = s2b_freshness.rank(proposed, run_id)
    queued = db.enqueue_verticals(freshness, {v.name: v.as_dict() for v in kept})
    names = [v.name for v in kept]
    db.log(run_id, "stage8",
           f"{result['vertical']}: queued {queued} adjacent verticals: {names}")
    slack.post(
        f"*Winner → next audiences*\n*{result['vertical']}* cleared the bar. "
        f"Queued {queued} adjacent groups: " + ", ".join(names or ["(none passed supply)"])
    )
    return {"proposed": [v.name for v in proposed], "queued": queued, "kept": names}


def review_all(run_id: str | None = None, scale: bool = True) -> list[dict]:
    """Judge every VE campaign and act on the verdicts."""
    # An open copy test is judged here too. The comparison and its Promote button
    # already existed but nothing triggered them, so a finished test waited for somebody
    # to type /ve conclude. It stays quiet until there is something worth saying.
    try:
        from .. import client as _client, conclude as _conclude
        out = _conclude.check_and_report(_client.profile(), run_id)
        if out.get("status") not in ("no_open_test", "too_early", "no_exposures"):
            db.log(run_id, "stage8", f"copy test: {out}")
    except Exception as exc:
        db.log(run_id, "stage8", f"copy-test check failed: {exc}", level="warn")

    # PHASE 1 -- judge everything first, and record every verdict, before acting on any
    # of them. Acting used to happen inline, which meant a winner's scale-up (a full
    # sourcing + contacts + copy run, hours long) ran inside the loop that scores the
    # other campaigns.
    #
    # Measured 2026-09-22: campaigns are walked in id order, Biotechnology is the lowest
    # id, it won, its scale-up started at 12:04 and the container died at 13:59. The loop
    # never returned, so the other EIGHT campaigns were never judged that day and kept a
    # verdict from 21 Sep, when five of them were still under the 7-day gate and had just
    # become eligible. Three were winners by then and nobody knew.
    #
    # Judging is cheap and reads an API. Acting is expensive and can die. Separating them
    # means a dead container costs a scale-up, never the verdicts.
    results, actionable = [], []
    for camp in _ve_campaigns():
        try:
            if (camp.get("vertical") or "") == db.POOL_VERTICAL:
                results.extend(review_pool(camp, run_id))
                continue
            r = review_campaign(camp)
        except Exception as exc:
            db.log(run_id, "stage8",
                   f"review failed for campaign {camp.get('campaign_id')}: {exc}",
                   level="error")
            continue
        results.append(r)
        if r["verdict"] == "skipped":
            db.log(run_id, "stage8", r["reason"])
            continue
        db.log(run_id, "stage8", f"{r['vertical']}: {r['verdict'].upper()} -- {r['reason']}")
        if r["verdict"] in ("scale", "drop"):
            # TELL THE HUMAN NOW, in phase 1, the moment it is decided. This used to be
            # posted at the END of each phase-2 iteration -- after expand_from_winner AND
            # after scale_vertical, which is a multi-hour sourcing run. On 2026-09-23 the
            # review decided FOUR winners at 12:04 and Slack was still silent 85 minutes
            # later, because the first winner's scale-up was still going and the other
            # three had not been reached. Troy: "I can't see anything in Slack ... this
            # is supposed to be self-sufficient."
            #
            # A verdict is known at 12:04. There is no reason to make it wait on the
            # slowest thing in the pipeline.
            slack.verdict(r["vertical"], r["verdict"], r["reason"],
                          db.vertical_thread(r["vertical"]))
            actionable.append((camp, r))

    # A pooled vertical's verdict is recorded and reported, never acted on here: both
    # actions work on the whole campaign, and a pool's campaign belongs to several
    # verticals. Archiving it to kill one would kill the rest.
    for r in results:
        if not r.get("pooled") or r["verdict"] not in ("scale", "drop"):
            continue
        # A pooled WINNER still splits into adjacent verticals. expand_from_winner
        # reads the result dict, proposes and enqueues -- it touches no campaign at all,
        # unlike apply_drop and scale_vertical. Excluding it here switched off the
        # compounding loop for exactly the verticals where it is cheapest to run.
        if r["verdict"] == "scale":
            try:
                r["expand_result"] = expand_from_winner(r, run_id or "review")
            except Exception as exc:
                db.log(run_id, "stage8",
                       f"adjacent expand failed for {r['vertical']} (pooled): {exc}",
                       level="error")
        if r["verdict"] == "scale":
            # Troy, 2026-09-30: a pooled winner gets its OWN campaign next, not a
            # human call. Back to the backlog at the top, marked standalone so the
            # push never pools it again; the next intake launches it and the normal
            # judge -> scale loop takes it from there.
            try:
                if db.requeue_pooled_winner(r["vertical"]):
                    db.log(run_id, "stage8", f"{r['vertical']} won inside pool "
                           f"{r.get('campaign_id')}; queued for its own campaign")
            except Exception as exc:
                db.log(run_id, "stage8", f"requeue of pooled winner {r['vertical']} "
                       f"failed: {exc}", level="error")
            note = "it wins inside the pool, so it gets its own campaign on the next intake"
        else:
            note = "archiving the campaign would take the other verticals with it"
        slack.verdict(r["vertical"], r["verdict"],
                      r["reason"] + f" — pooled: {note}",
                      db.vertical_thread(r["vertical"]))
    db.log(run_id, "stage8",
           f"judged {len(results)} campaigns: "
           f"{sum(1 for r in results if r['verdict'] == 'scale')} scale, "
           f"{sum(1 for r in results if r['verdict'] == 'drop')} drop, "
           f"{sum(1 for r in results if r['verdict'] == 'pending')} pending")

    # FINISH WHAT IS ALREADY BOUGHT, before anything is capped. This is copy only, on
    # contacts already sourced, enriched and verified, so it costs one LLM call each and
    # no sender accounts. It used to happen only as a side effect of winning a scale
    # slot, which left 3,843 paid-for contacts rotting across 11 verticals and gave a
    # PENDING vertical no route to a batch at all.
    try:
        finished = finish_banked_work(run_id)
        if finished:
            db.log(run_id, "stage8",
                   f"finished banked work for {len(finished)} verticals: "
                   + ", ".join(f"{f['vertical']} +{f.get('finished', 0)}"
                               for f in finished))
    except Exception as exc:
        db.log(run_id, "stage8", f"finishing banked work failed: {exc}", level="error")

    # PHASE 2 -- now act. Drops first: a drop archives a campaign and hands its sender
    # accounts back, which is exactly the capacity a scale-up is about to need.
    actionable.sort(key=lambda cr: 0 if cr[1]["verdict"] == "drop" else 1)

    # Drops are quick and free capacity the scale-ups want, so they go first and alone.
    for camp, r in [x for x in actionable if x[1]["verdict"] == "drop"]:
        apply_drop(r)
        _announce(r, run_id)

    # Scale-ups run SIDE BY SIDE. Sequentially they are hours each: 2026-09-23 produced
    # four winners at roughly three hours apiece, which is most of a day, and the fourth
    # would not have started until 21:00. Two things went wrong with that. The longer
    # the chain, the more of it a container restart destroys -- yesterday's died at the
    # two-hour mark. And the review cron fires at 12:00 regardless, so a chain still
    # running at noon met the next day's review head on.
    #
    # Safe to parallelise HERE specifically because a scale-up is a TOP-UP: since
    # 2026-09-23 it attaches no sender accounts, so the winners are not racing for the
    # free pool. The weekly run has worked verticals this way all along
    # (settings.parallel_verticals); only the review was still single file. The LLM
    # semaphore stays global, so copy cannot stampede the providers.
    winners = [x for x in actionable if x[1]["verdict"] == "scale"]

    # MONDAY BELONGS TO DISCOVERY. The weekly seed run fires 06:05 UTC (07:05 WAT)
    # Monday; this daily review fires 23:00 UTC, which is 00:00 WAT -- the START of
    # the NEXT calendar day in WAT, not the same one. Troy, 2026-09-29: "00:01 on
    # monday [WAT], scaling should not happen since 7am is when weekly runs." Checking
    # raw UTC weekday here was checking the wrong day: the review that lands at 00:00
    # WAT Monday is the one whose hours-long scale-up would still plausibly be running
    # when the weekly seed kicks off at 07:05 WAT that same Monday morning -- and that
    # review fires at 23:00 UTC SUNDAY, so a bare UTC weekday() check (0 = Monday)
    # skips the review landing at 00:00 WAT TUESDAY instead, a full day late and with
    # no real contention left to avoid. Shifting to WAT before comparing fixes this:
    # the day that "belongs to discovery" is judged by the calendar day this review
    # actually lands on for a human reading it in WAT, not the UTC instant it fired.
    from datetime import datetime, timedelta, timezone
    wat_now = datetime.now(timezone.utc) + timedelta(hours=1)
    if scale and winners and settings.scale_skip_weekday >= 0 \
            and wat_now.weekday() == settings.scale_skip_weekday:
        scale = False
        names = ", ".join(r["vertical"] for _, r in winners)
        db.log(run_id, "stage8",
               f"not scaling today: the weekly seed run has this day, and new audiences "
               f"matter more than enlarging old ones. Deferred to tomorrow: {names}")
        slack.post(f":calendar: *{len(winners)} winners deferred* — today is the weekly "
                   f"run's day, so they are judged and split but not scaled. They scale "
                   f"in tomorrow's review: {names}")

    # AND ONLY A FEW A DAY. Best first, so a cap delays the weakest winner rather than
    # an arbitrary one, and the rest are picked up by tomorrow's review.
    if scale and len(winners) > settings.scale_max_per_run:
        winners.sort(key=lambda cr: -(cr[1].get("objective_rate") or 0))
        held = winners[settings.scale_max_per_run:]
        winners = winners[:settings.scale_max_per_run]
        names = ", ".join(f"{r['vertical']} ({r.get('objective_rate', 0):.1%})"
                          for _, r in held)
        db.log(run_id, "stage8",
               f"scaling the top {len(winners)} of {len(winners) + len(held)} winners; "
               f"{settings.scale_target_leads:,} leads each needs "
               f"{len(winners) + len(held)} x 2,400/day, more than the workspace can "
               f"send. Waiting for tomorrow: {names}")
        slack.post(f":hourglass_flowing_sand: Scaling the top "
                   f"{settings.scale_max_per_run} winners today; there is not enough "
                   f"daily sending to fill more than that to "
                   f"{settings.scale_target_leads:,} leads. Tomorrow: {names}")

    if winners:
        import concurrent.futures as _cf
        # LEAST-SCALED FIRST, into the lanes. Winners used to dispatch in
        # _ve_campaigns()'s own order (created_at ascending), which meant the OLDEST
        # winners always claimed the first parallel_verticals lanes and a newer one
        # queued behind them -- not by design, just an accident of creation date.
        # Troy, 2026-09-26: Product Design (created a day after Biotechnology and
        # Medical Device) lost its lane to a crash on 2026-09-24 and then sat queued
        # behind the same two older winners again the next night. Ordering by
        # pushed_lead_count ascending means a never-scaled or barely-scaled vertical
        # gets first crack at a lane, and one already close to its 10,000 target --
        # which has the least to gain from tonight anyway -- waits instead.
        winners.sort(key=lambda cr: cr[0].get("pushed_lead_count") or 0)
        lanes = max(1, min(settings.parallel_verticals, len(winners)))
        order = ", ".join(f"{r['vertical']} ({c.get('pushed_lead_count') or 0:,})"
                          for c, r in winners)
        db.log(run_id, "stage8",
               f"acting on {len(winners)} winners, {lanes} at a time, least-scaled "
               f"first: {order}")
        with _cf.ThreadPoolExecutor(max_workers=lanes) as pool:
            list(pool.map(lambda cr: _act_on_winner(cr[0], cr[1], run_id, scale),
                          winners))
    return _finish_review(results, run_id)


def _announce(r: dict, run_id: str | None) -> None:
    """The OUTCOME of acting; the verdict itself already went out in phase 1."""
    extra = r.get("scale_result") or {}
    if extra.get("scaled"):
        slack.post(f"📈 *{r['vertical']}* — scaled +{extra['scaled']:,} contacts "
                   f"into campaign {r['campaign_id']}",
                   thread_ts=db.vertical_thread(r["vertical"]))


def _act_on_winner(camp: dict, r: dict, run_id: str | None, scale: bool) -> None:
    """One winner's follow-up. Runs in its own lane, so it must not raise."""
    try:
        _do_act_on_winner(camp, r, run_id, scale)
    except Exception as exc:
        db.log(run_id, "stage8",
               f"acting on {r['vertical']} failed: {exc}", level="error")


def _do_act_on_winner(camp: dict, r: dict, run_id: str | None, scale: bool) -> None:
    if r["verdict"] == "scale":
        # CHEAP FIRST. The adjacent split is a proposal plus a supply probe, minutes of
        # work, and it is the compounding loop: no split, no new verticals ever enter
        # the backlog from performance. The scale-up is a full sourcing, contacts and
        # copy run that takes hours and is the thing that dies.
        #
        # Measured 2026-09-22: the split had NEVER FIRED, not once, in the system's
        # whole life. Nothing could scale until Biotechnology cleared 7 days that
        # morning, and when it finally did, the scale-up ran from 12:04 to 13:59 and the
        # container died before the split it was queued behind. A try/except does not
        # help here -- a SIGTERM kills the process, not an exception -- so the only
        # protection is doing the cheap, compounding step first.
        try:
            r["expand_result"] = expand_from_winner(r, run_id or "review")
        except Exception as exc:
            db.log(run_id, "stage8",
                   f"adjacent expand failed for {r['vertical']}: {exc}", level="error")
        if scale:
            # STAMP before, CLEAR after -- in a finally, so a normal return OR a caught
            # exception both clear it, but a container SIGTERM does neither. That
            # asymmetry is deliberate: it is what lets resume_interrupted_scale_ups()
            # tell "finished (however it finished)" apart from "died mid-flight".
            #
            # A VENDOR OUTAGE is a THIRD case, deliberately treated like the SIGTERM
            # one rather than a normal failure. Troy, 2026-09-29, on the OpenRouter/
            # Concentrate outage that killed Biotechnology's and Consumer Edtech's
            # scale-ups that night: a caught VendorOutage used to still clear the
            # stamp in the finally below, so it read as "finished" and sat untouched
            # for up to a full day waiting for tomorrow's review, even though the
            # outage itself often clears in minutes. The stamp is left in place here
            # on purpose so the 2-hourly resume picks it straight back up --
            # _continue_scale_ups() re-fetches the campaign live before retrying, so
            # there is no staleness risk in leaving it stamped across the gap.
            # DEFER ONLY THIS VERTICAL if a 2-hourly tick is right now genuinely still
            # scaling this exact campaign. Troy, 2026-10-01: "the nightly review should
            # take priority" over resume_interrupted_scale_ups/resume_unscaled_winners --
            # the review no longer waits or skips its whole night for ANY other run (see
            # runner.py's review dispatch), so this is the one place left that still has
            # to guard against a genuine double-scale: two scale_vertical calls racing
            # the same campaign. A DEAD run's stamp (db.interrupted_scale_ups' case) is
            # not a reason to defer -- only a run that is truly still alive is.
            other_owner = camp.get("scaling_run_id")
            if other_owner and other_owner != (run_id or "review") \
                    and db.run_is_truly_alive(other_owner):
                db.log(run_id, "stage8",
                       f"{r['vertical']}: campaign {camp['campaign_id']} is already "
                       f"being scaled by {other_owner}, still running -- not starting a "
                       f"duplicate; it finishes there and the verdict stays 'scale' for "
                       f"next time", level="warn")
                r["scale_result"] = {"vertical": r["vertical"], "scaled": 0,
                                     "reason": f"already scaling under {other_owner}"}
            else:
                db.mark_scaling_started(camp["campaign_id"], run_id or "review")
                try:
                    r["scale_result"] = scale_vertical(camp, r, run_id or "review")
                except VendorOutage as exc:
                    db.log(run_id, "stage8",
                           f"scale-up paused for {r['vertical']} (vendor outage: {exc}); "
                           f"the 2-hourly resume will retry it today rather than waiting "
                           f"for tomorrow's review", level="warn")
                except Exception as exc:
                    db.log(run_id, "stage8",
                           f"scale-up failed for {r['vertical']}: {exc}", level="error")
                    db.mark_scaling_finished(camp["campaign_id"])
                else:
                    db.mark_scaling_finished(camp["campaign_id"])
    _announce(r, run_id)


def _continue_scale_ups(candidates: list[dict], run_id: str | None, why: str,
                        budget_minutes: int | None = None) -> list[dict]:
    """Shared body for continuing a SCALE-verdict campaign that stalled -- either
    interrupted mid-flight or (Troy, 2026-09-25) never started at all because it lost
    the review's 3-lanes-at-a-time queue when the process died. Both call sites need
    the exact same care: re-check the verdict AND re-fetch the campaign LIVE rather
    than trust the candidate list's own snapshot, since that snapshot can itself be
    stale by the time a slow EARLIER candidate in the same loop finishes.

    That staleness is exactly how Venture Capital got marked exhausted while a real
    attempt was still finding companies: a candidate list fetched at the top of an
    earlier loop still carried scale_exhausted_at=None for VC, and by the time the loop
    reached VC's turn -- after a slow sibling ahead of it finally finished -- VC itself
    had genuinely run its OWN narrower slice dry in the meantime and this stale view
    never saw it. Re-fetching live, right before acting, closes that gap.
    """
    import time as _time
    stop_at = (_time.monotonic() + budget_minutes * 60) if budget_minutes else None
    out = []
    for camp in candidates:
        vertical = camp["vertical"]
        # The tick's own clock outranks the queue. A candidate started with 5 minutes
        # left just dies at the cron boundary and leaves a stamped corpse -- the exact
        # failure this budget exists to end. The stamp is untouched, so the next tick
        # (or the nightly review) picks it up with a full slice instead.
        if stop_at is not None:
            left = int((stop_at - _time.monotonic()) / 60)
            if left < 15:
                db.log(run_id, "stage8",
                       f"tick budget nearly spent ({left} min left); {vertical} and "
                       f"the rest wait for the next tick rather than dying mid-slice")
                break
        with db.cursor() as cur:
            cur.execute("SELECT * FROM vertical_outcomes WHERE vertical=%s", (vertical,))
            outcome = cur.fetchone()
        if not outcome or dict(outcome).get("verdict") != "scale":
            # The verdict changed (or vanished) since the candidate list was built -- a
            # new daily review already re-judged it. Leave it to that review, not this.
            db.mark_scaling_finished(camp["campaign_id"])
            continue
        live_camp = db.get_ve_campaign(camp["campaign_id"]) or camp
        if live_camp.get("scale_exhausted_at"):
            db.mark_scaling_finished(camp["campaign_id"])
            continue
        result = dict(outcome)
        db.log(run_id, "stage8", f"{vertical}: {why}")
        db.mark_scaling_started(camp["campaign_id"], run_id or "resume")
        try:
            left = (max(int((stop_at - _time.monotonic()) / 60), 1)
                    if stop_at is not None else None)
            res = scale_vertical(live_camp, result, run_id or "resume",
                                 deadline_minutes=left)
            out.append({"vertical": vertical, **res})
        except Exception as exc:
            db.log(run_id, "stage8",
                   f"continuing a stalled scale-up for {vertical} failed: {exc}",
                   level="error")
        finally:
            db.mark_scaling_finished(camp["campaign_id"])
    return out


def resume_interrupted_scale_ups(run_id: str | None,
                                 budget_minutes: int | None = None) -> list[dict]:
    """Continue any scale-up whose run died mid-flight, instead of waiting for tomorrow.

    Troy, 2026-09-24, after a deploy killed Medical Device and Venture Capital's
    scale-ups mid-sourcing: "this is not good. it makes us fall behind schedule. runs
    should also resume if they were failed right?" They should, and until now they did
    not: a scale-up is not a vertical_backlog row, so paused_verticals() -- what the
    two-hourly resume already checks -- cannot see it. A killed scale-up sat idle for up
    to 24 hours waiting for the next daily review to re-decide the same winner, instead
    of being retried on the very next two-hourly tick like every other kind of work.

    Rows are found by db.interrupted_scale_ups(): scaling_run_id is set (a scale-up
    started) and that run is no longer 'running' (it did not finish, or nothing would
    have cleared the stamp).

    BOUNDED TO THE DAY IT STARTED. Troy, 2026-09-29: "if a scale up is out due to
    credits it checks every 2 hours till it completes or the end of the day and stops
    checking so the next scale up can pick up on it." A vendor outage (the case this
    was built for) leaves the stamp in place on purpose so this catches it -- but
    nothing here ever cleared the stamp on its own, so left unbounded this would keep
    retrying the SAME stuck campaign every two hours indefinitely, fighting the next
    day's regular review over the same work rather than handing off to it. A campaign
    whose scaling_started_at has crossed into a new WAT day is left alone here; the
    ordinary daily review re-stamps and retries it fresh regardless, the same as any
    other winner.
    """
    from datetime import datetime, timedelta, timezone
    wat_today = (datetime.now(timezone.utc) + timedelta(hours=1)).date()

    def _started_today(c: dict) -> bool:
        started = c.get("scaling_started_at")
        return bool(started) and (started + timedelta(hours=1)).date() == wat_today

    candidates = db.interrupted_scale_ups()
    fresh = [c for c in candidates if _started_today(c)]
    stale = [c for c in candidates if not _started_today(c)]
    if stale:
        db.log(run_id, "stage8",
               f"{len(stale)} interrupted scale-up(s) from a previous day are no "
               f"longer retried on the two-hourly tick -- today's regular review "
               f"picks them up instead: " + ", ".join(c["vertical"] for c in stale))
    return _continue_scale_ups(fresh, run_id,
                               "resuming a scale-up an earlier run did not finish",
                               budget_minutes=budget_minutes)


def resume_unscaled_winners(run_id: str | None,
                            budget_minutes: int | None = None) -> list[dict]:
    """Start any vertical that WON a review but never got a lane before the process
    died, instead of waiting up to 24h for the next review to re-decide the same
    winner. Troy, 2026-09-25, on Product Design missing an entire scaling day this way.

    Distinct from resume_interrupted_scale_ups(): that finds work that STARTED and
    died; db.unscaled_winners() finds a win that never got a scaling_run_id stamped at
    all, because the review's own 3-lanes-at-a-time limit ran out of process before a
    freed lane could reach it.
    """
    return _continue_scale_ups(db.unscaled_winners(), run_id,
                               "never got a lane before an earlier run died; starting now",
                               budget_minutes=budget_minutes)


def _finish_review(results: list[dict], run_id: str | None) -> list[dict]:
    scaled = [r for r in results if r["verdict"] == "scale"]
    dropped = [r for r in results if r["verdict"] == "drop"]
    if results:
        lines = [f"• *{r['vertical']}* — {r['verdict'].upper()}: {r['reason']}"
                 for r in results]
        slack.post("*Vertical review*\n" + "\n".join(lines[:12]))
    db.log(run_id, "stage8",
           f"reviewed {len(results)} verticals: {len(scaled)} scale, {len(dropped)} drop")
    _read_copy_test(run_id)
    return results


def _read_copy_test(run_id: str | None) -> None:
    """ProductEVO only (copy_test.toml): apply the pre-declared angle rules to the grid."""
    from .. import reconcile, testgrid
    if testgrid.load_config() is None:
        return
    try:
        v = testgrid.evaluate_and_record()
    except Exception as exc:
        db.log(run_id, "stage8", f"copy test read failed: {exc}", level="warn")
        # Troy, 2026-10-06: a log line nobody is watching is the same as silence. If the
        # nightly check itself breaks, that is exactly the night it needs to be loud.
        try:
            slack.post(f":rotating_light: *Copy test check failed to run tonight*: {str(exc)[:250]}. "
                       f"No decision or reconciliation happened; will try again next tick.")
        except Exception:
            pass
        return
    # Troy, 2026-10-05: run this regardless of the verdict above -- today's real bugs (no
    # reply sync, no launch date, no lead ids) would ALL have been caught here, the same
    # night, instead of sitting silent for over a week.
    try:
        reconcile.nightly_reconciliation(run_id)
    except Exception as exc:
        db.log(run_id, "stage8", f"nightly reconciliation failed: {exc}", level="warn")
        try:
            slack.post(f":rotating_light: *Nightly reconciliation check failed to run*: {str(exc)[:250]}. "
                       f"Will try again next tick.")
        except Exception:
            pass
    # Troy, 2026-10-07: a derived (lookalike) angle's day-7 checkpoint, every night --
    # scales it (6%+ interested-of-replies) or kills it outright, same as a day-10 loser.
    try:
        from .. import angle_derivation
        angle_derivation.check_derived_angles(run_id)
    except Exception as exc:
        db.log(run_id, "stage8", f"derived-angle checkpoint failed: {exc}", level="warn")
    # Troy, 2026-10-07: "no need to run the pool refill every 2 hours" -- lives in the
    # once-nightly review instead, with its own twice-a-week gate (testgrid.POOL_REFILL_WEEKDAYS).
    try:
        from .. import testgrid as _testgrid_pool
        _testgrid_pool.refill_pool(run_id)
    except Exception as exc:
        db.log(run_id, "stage8", f"pool refill failed: {exc}", level="warn")
    # Troy, 2026-10-06: the ProductEVO client one-pager, every Friday. Posts a DRAFT to
    # Slack for Troy to proof -- never sent to the client automatically, no email tool is
    # wired up for that step.
    from datetime import datetime as _dt, timezone as _tz
    if _dt.now(_tz.utc).weekday() == 4:   # Friday
        try:
            from .. import weekly_report
            weekly_report.post_weekly_report_for_proofing(run_id)
        except Exception as exc:
            db.log(run_id, "stage8", f"weekly report failed: {exc}", level="warn")
    status = v.get("status")
    if status in (None, "no_config", "no_data", "frozen", "already_running"):
        db.log(run_id, "stage8", f"copy test: {status} {v.get('reason', '')}".strip())
        return
    seg_lines = []
    for seg, cells in (v.get("by_segment") or {}).items():
        seg_lines.append(f"{seg}: " + ", ".join(
            f"{a} {c['positive']}/{c['leads']}" for a, c in cells.items()))
    slack.post(f"*Copy test* — {status.upper()}: {v.get('reason', '')}\n" + "\n".join(seg_lines))
    db.log(run_id, "stage8", f"copy test: {status} {v.get('reason', '')}")
