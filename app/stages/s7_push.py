"""Stage 7 -- EmailBison push, gated on an approved batch.

Two hard rules:
  - Only a batch whose approval_status is 'approved' can be pushed. Pending, killed
    and auto-killed batches are refused.
  - Dry run first, always. The runner's push command defaults to --dry-run, and the
    real push needs an explicit --live.

Bulk-load endpoint is still unconfirmed (spec gate 3). If it rejects the payload the
push falls back to producing an upload-ready CSV and posting it to Slack for manual
load, rather than half-loading a campaign.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import db
from ..clients import emailbison, slack
from ..config import settings


def _contacts_for(batch_id: str) -> list[dict]:
    rows = db.batch_contacts(batch_id)
    out = []
    for row in rows:
        out.append({
            "email": row["email"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "title": row["title"],
            "company_domain": row["company_domain"],
            # The real company name, not the domain. This merges into the copy, and
            # "quidel.com" where "Quidel" belongs is visible in every email that lead
            # receives -- the contacts table simply never stored the name.
            "company_name": row.get("company_name") or row["company_domain"],
            "vertical": row.get("vertical"),
            "personalization_1": row["personalization_1"],
            "personalization_2": row["personalization_2"],
            "research_report": row["research_report"],
            "eb_lead_id": row.get("eb_lead_id"),
            "eb_campaign_id": row.get("eb_campaign_id"),
            "experiment_id": row.get("experiment_id"),
            "copy_variant": row.get("copy_variant"),
            "title_segment": row.get("title_segment"),
            "prompt_version": row.get("prompt_version"),
        })
    return out


def split_for_push(contacts: list[dict], campaign_id: int) -> tuple[list[dict], list[dict]]:
    """(already in this campaign, still to load). Pure; the idempotency rule."""
    done = [c for c in contacts if c.get("eb_campaign_id") == campaign_id]
    todo = [c for c in contacts if c.get("eb_campaign_id") != campaign_id]
    return done, todo


def push(batch_id: str, campaign_id: int | None = None, live: bool = False,
         out_dir: str | None = None) -> dict:
    batch = db.get_batch(batch_id)
    if not batch:
        raise RuntimeError(f"no batch {batch_id}")
    if batch["approval_status"] != "approved":
        raise RuntimeError(
            f"batch {batch_id} is {batch['approval_status']}, not approved. "
            "Nothing is pushed without an approval on the Slack gate."
        )

    contacts = _contacts_for(batch_id)
    # EmailBison requires first_name on every lead and rejects the WHOLE 100-lead chunk
    # when one is blank (422 "leads.18.first_name field is required", 2026-09-14) --
    # which sent two complete batches to the CSV fallback. A lead with no first name
    # cannot be greeted anyway, so it is set aside as needs_review, not sent as "Hi ,".
    nameless = [c for c in contacts if not (c.get("first_name") or "").strip()]
    if nameless:
        for c in nameless:
            db.mark_needs_review(c["email"], "no first name; EmailBison requires one")
        contacts = [c for c in contacts if (c.get("first_name") or "").strip()]
        db.log(batch["run_id"], "stage7",
               f"{batch_id}: {len(nameless)} leads without a first name set aside as "
               f"needs_review", level="warn")
    if not contacts:
        return {"batch_id": batch_id, "lead_count": 0, "note": "nothing left to push"}

    # A fresh campaign per vertical per batch, named VE-{vertical}-{YYYY-MM-DD}.
    # Never one standing campaign: per-vertical stats have to stay readable so the
    # 90-day loop can score them, and the 1,000-lead gate is applied per campaign.
    planned_name = emailbison.campaign_name_for(batch["vertical"])

    if not live:
        result = emailbison.push_leads(campaign_id or 0, contacts, dry_run=True)
        result["campaign_to_create"] = planned_name
        result["batch_id"] = batch_id
        db.log(batch["run_id"], "stage7",
               f"DRY RUN for {batch_id}: {len(contacts)} leads -> campaign {campaign_id}")
        return result

    # A SCALE-UP tops up the campaign it scaled, it does not start a rival to it.
    # s8_review.scale_vertical stamps the winner's id into the batch summary as
    # `scale_up_of_campaign`, but until 2026-09-22 nothing ever read it back, and every
    # automatic push -- the approval handler and the two-hourly held-batch retry -- calls
    # push() with no campaign_id. So a scale-up would have duplicated the template into a
    # SECOND campaign for the same vertical: needing its own 600/day to launch, splitting
    # the vertical's stats across two campaigns so neither reflects it, and writing a
    # second ve_campaigns row whose outcome overwrites the first (ON CONFLICT (vertical)).
    #
    # It had never fired because no scale-up had ever completed; the recovered
    # Biotechnology top-up is the first one to reach approval.
    if not campaign_id:
        target_campaign = (batch.get("summary") or {}).get("scale_up_of_campaign")
        if target_campaign:
            campaign_id = int(target_campaign)
            db.log(batch["run_id"], "stage7",
                   f"{batch_id} is a scale-up of campaign {campaign_id}; topping that up "
                   f"rather than opening a second campaign for the same vertical")
            # Report the campaign that actually exists. planned_name is the name of a
            # would-be NEW campaign, worked out before we know we are topping one up,
            # and nothing updated it afterwards. On 2026-09-23 a perfectly correct
            # top-up into campaign 1084 (VE-...-2026-09-14) announced itself in Slack as
            # "480 sender accounts on VE-Biotechnology-Life-Sciences-2026-09-23" -- a
            # campaign that does not exist -- and read as the pipeline having opened a
            # rival to the winner. A report that names something imaginary is how you
            # stop trusting the logs.
            existing_name = (emailbison.get_campaign(campaign_id) or {}).get("name")
            if existing_name:
                planned_name = existing_name

    # CHAMPION. Once the ProductEVO copy test has named a winning angle, approved
    # batches join that campaign (tagged by vertical) instead of opening a new one. Only
    # ProductEVO has copy_test.toml, so champion_campaign() is None for every other client.
    if not campaign_id:
        from .. import testgrid
        champion = testgrid.champion_campaign()
        if champion:
            campaign_id = champion
            planned_name = (emailbison.get_campaign(campaign_id) or {}).get("name") or planned_name
            db.log(batch["run_id"], "stage7",
                   f"{batch_id}: copy-test champion is campaign {campaign_id}; "
                   f"topping that up rather than opening a new campaign")

    # ANY OTHER VERTICAL THAT ALREADY LAUNCHED. Troy, 2026-09-30, on Cybersecurity's
    # push trying to open a rival campaign: scale_up_of_campaign above is stamped
    # ONLY by the classic scale_vertical() path. The auto-topup loop built the same
    # day (short_launched_campaigns, auto_approve_if_topup) grows a pending batch for
    # a vertical that is ALREADY sending -- Cybersecurity's campaign 1162 had been
    # live and replying for a week -- with no stamp at all, so its approved batch
    # fell through to "open a new one". db.vertical_is_launched is the same check
    # auto-approve already trusts; used here too, a top-up can never miss its own
    # campaign regardless of which path grew the batch.
    if not campaign_id:
        existing = db.vertical_is_launched(batch.get("vertical") or "")
        if existing:
            # A DROPPED vertical's campaign is archived; loading more leads into it
            # sends nothing and corrupts a closed number. Refuse rather than push or
            # silently open a new one for a vertical that is finished.
            outcome = db.vertical_outcome(batch.get("vertical") or "")
            if outcome and outcome.get("verdict") == "drop":
                db.log(batch["run_id"], "stage7",
                       f"{batch_id}: {batch.get('vertical')} is DROPPED (campaign "
                       f"{existing} archived); refusing to push or open a new "
                       f"campaign for it -- kill this batch", level="error")
                return {"batch_id": batch_id, "error": "vertical is dropped",
                       "campaign_id": existing}
            # GRADUATE OUT OF A POOL. Troy, 2026-10-01: "how can we make sure verticals
            # that can be standalone campaigns don't end up pooled?" If the campaign this
            # vertical lives in is a POOL, but the vertical now holds a standalone-sized
            # supply, it gets its own campaign from here on (the leads already in the
            # pool stay there; they are sending). Stamped standalone so it never re-pools.
            if settings.pool_below_leads > 0:
                home = db.get_ve_campaign(existing) or {}
                if (home.get("vertical") or "") == db.POOL_VERTICAL:
                    supply = db.unpushed_supply(batch.get("vertical") or "")
                    if supply >= settings.pool_below_leads:
                        db.mark_standalone(batch.get("vertical") or "")
                        db.log(batch["run_id"], "stage7",
                               f"{batch_id}: {batch.get('vertical')} lives in pool {existing} "
                               f"but now holds {supply:,} unpushed contacts (>= "
                               f"{settings.pool_below_leads:,}); opening its own campaign "
                               f"and marking it standalone")
                        existing = None
            if existing:
                campaign_id = existing
                db.log(batch["run_id"], "stage7",
                       f"{batch_id}: {batch.get('vertical')} already sends from campaign "
                       f"{campaign_id}; topping that up rather than opening a new one")
                existing_name = (emailbison.get_campaign(campaign_id) or {}).get("name")
                if existing_name:
                    planned_name = existing_name

    # POOLING. A vertical too small to earn a verdict alone joins an open pooled
    # campaign instead of taking a slot, its own sender accounts and a 600/day launch
    # gate it will never repay. Troy, 2026-09-22: "it should be wise to loop campaigns
    # where we cannot find up to 3000 together." Scoring needs scale_min_sends (2,000)
    # and at ~2.7 sends per lead that is ~740 leads; Conversational AI shipped 237 and
    # can never be judged on its own. Pooled, it is scored from our own reply data
    # (db.verticals_in_campaign) rather than EmailBison's blended campaign number.
    #
    # Joining an EXISTING pool needs no free senders at all -- it is a top-up of a
    # campaign that already has accounts attached, which is the whole point.
    pooled = False
    # A vertical that already WON inside a pool is re-launched standalone (Troy,
    # 2026-09-30): its backlog config carries `standalone`, and it must never be
    # pooled again however small the batch.
    standalone = False
    try:
        standalone = bool((db.backlog_config(batch.get("vertical") or "") or {}).get("standalone"))
    except Exception:
        standalone = False
    # The VERTICAL's supply decides, not this batch's size. A first batch is often a
    # snapshot taken while sourcing is still running (Biotechnology Research: 123 in
    # the batch, 2,117 banked). Troy, 2026-10-01.
    supply = len(contacts)
    if not campaign_id and settings.pool_below_leads > 0 and not standalone:
        try:
            supply = max(supply, db.unpushed_supply(batch.get("vertical") or ""))
        except Exception:
            pass
        if supply >= settings.pool_below_leads > len(contacts):
            db.log(batch["run_id"], "stage7",
                   f"{batch_id}: {len(contacts)} leads in this batch but the vertical "
                   f"holds {supply:,} unpushed; standalone, not pooled")
    if not campaign_id and settings.pool_below_leads > 0 \
            and supply < settings.pool_below_leads and not standalone:
        # STICKY. push() is designed to be re-run -- that is what split_for_push and the
        # per-chunk progress exist for -- and pool_campaign_with_room re-queries every
        # call, ordering by fullness. Between two pushes of the same batch another batch
        # can make a different pool the fullest-with-room, or fill this one past the cap.
        # The retry would then join pool B while these contacts already carry
        # eb_campaign_id = A, split_for_push would classify every one of them as "still
        # to load", and the same people would be loaded into a second campaign and get
        # the whole sequence twice. So once a batch has landed in a pool, it stays there.
        joined = {int(c["eb_campaign_id"]) for c in contacts
                  if c.get("eb_campaign_id")}
        open_pool = None
        if len(joined) == 1:
            already = joined.pop()
            open_pool = db.get_ve_campaign(already)
            if open_pool and (open_pool.get("vertical") or "") == db.POOL_VERTICAL:
                db.log(batch["run_id"], "stage7",
                       f"{batch_id}: already partly loaded into pool {already}; "
                       f"staying there rather than choosing a pool again")
            else:
                open_pool = None
        # SAME POOL AS THE VERTICAL'S EARLIER LEADS. A top-up batch for a vertical that
        # already lives in a pool carries no eb_campaign_id of its own, so the sticky
        # check above cannot see it and fullest-with-room could scatter one vertical's
        # leads across two pools -- and the per-vertical scoring inside a pool reads
        # one campaign. Troy, 2026-09-30, closing the pooled post-launch top-up gap.
        if open_pool is None:
            home = db.pool_for_vertical(batch.get("vertical") or "")
            if home:
                cand = db.get_ve_campaign(home)
                if cand and int(cand.get("pushed_lead_count") or 0) + len(contacts) \
                        <= settings.pool_max_leads:
                    open_pool = cand
                    db.log(batch["run_id"], "stage7",
                           f"{batch_id}: {batch.get('vertical')} already lives in pool "
                           f"{home}; topping that up rather than choosing a pool again")
        if open_pool is None:
            open_pool = db.pool_campaign_with_room(settings.pool_max_leads,
                                                   need=len(contacts))
        if open_pool:
            campaign_id = int(open_pool["campaign_id"])
            pooled = True
            # Same reason as the scale-up branch: report the pool that exists, not the
            # name of the campaign we decided not to create.
            planned_name = (open_pool.get("campaign_name")
                            or (emailbison.get_campaign(campaign_id) or {}).get("name")
                            or planned_name)
            db.log(batch["run_id"], "stage7",
                   f"{batch_id}: {len(contacts)} leads is under the "
                   f"{settings.pool_below_leads}-lead floor; joining pooled campaign "
                   f"{campaign_id} ({open_pool['pushed_lead_count']} leads) instead of "
                   f"opening one")
        else:
            pooled = True
            planned_name = emailbison.campaign_name_for("Pool")
            db.log(batch["run_id"], "stage7",
                   f"{batch_id}: {len(contacts)} leads is under the "
                   f"{settings.pool_below_leads}-lead floor and no pool is open; "
                   f"starting one")

    is_new = not campaign_id
    # Assigned here, not inside the `if is_new` branch below: the cadence check further
    # down reports the template on EVERY push, top-ups included, and a name bound only
    # on the new-campaign path raises UnboundLocalError on every top-up.
    model_id = settings.model_campaign_id
    # Capacity gate. A campaign launched onto accounts that other campaigns already
    # use sends nothing (six did, 2026-09-15). If the free pool cannot give this batch
    # at least sender_min_daily of DEDICATED capacity, hold it: it stays approved,
    # Slack says how much is free, and the two-hourly job pushes it when campaigns
    # finish.
    #
    # Troy, 2026-10-05: this checked pool["daily"] -- the blended figure that counts
    # thirds of accounts two OTHER campaigns are already using -- against the same
    # number the comment above has always named. A pool of shared thirds can clear
    # sender_min_daily with zero real idle capacity behind it, which is exactly how
    # 13 campaigns ended up live against a workspace that could only ever carry 6: the
    # gate kept saying yes. DEDICATED is the only figure a brand-new campaign can
    # actually depend on; a pool["daily"] it merely shares can be diluted further by
    # the next launch the moment after this one succeeds.
    if is_new and not settings.skip_sender_attach:
        from .. import senders as _senders
        pool = _senders.free_capacity()
        want = settings.sender_min_daily
        if pool["dedicated"] < settings.sender_min_daily:
            db.log(batch["run_id"], "stage7",
                   f"{batch_id} HELD: only {pool['dedicated']}/day dedicated free on "
                   f"{pool['accounts']} accounts, needs {settings.sender_min_daily}/day "
                   f"to launch", level="warn")
            slack.push_held(batch["vertical"], batch_id, pool["dedicated"], pool["accounts"],
                            want, db.vertical_thread(batch["vertical"]))
            return {"batch_id": batch_id, "held": True, "free_daily": pool["dedicated"],
                    "free_accounts": pool["accounts"], "lead_count": len(contacts)}
    if is_new:
        # DUPLICATE the model campaign rather than create a blank one: the copy inherits
        # the sequence, plain_text and send limits that have been tuned in use. It does
        # NOT inherit sender emails, and its schedule is the model's own 09:32-17:48,
        # so the window is set explicitly afterwards.
        # Any live VE campaign works as the template; they share one sequence.
        # Resolved per push so no single campaign is special and archiving one
        # simply moves the template. See emailbison.resolve_model_campaign.
        model_id = emailbison.resolve_model_campaign()
        # A push killed between "duplicate" and "load leads" (a redeploy, 2026-10-08) leaves
        # an empty draft with this exact name and no ve_campaigns row. Reuse it rather than
        # leave an orphan beside a second copy.
        orphan = None
        try:
            for c in emailbison.list_campaigns():
                if ((c.get("name") or "") == planned_name and (c.get("status") or "") == "draft"
                        and not int(c.get("total_leads") or 0)
                        and not db.get_ve_campaign(int(c["id"]))):
                    orphan = c
                    break
        except Exception:
            orphan = None
        if orphan:
            created = orphan
            db.log(batch["run_id"], "stage7",
                   f"reusing the empty draft {planned_name} (id={orphan['id']}) left by an "
                   f"interrupted push, instead of duplicating {model_id} again")
        else:
            created = emailbison.duplicate_campaign(model_id, planned_name)
        campaign_id = created.get("id")
        db.log(batch["run_id"], "stage7",
               f"duplicated campaign {model_id} -> {planned_name} "
               f"(id={campaign_id})")
        try:
            sched = emailbison.set_schedule(campaign_id)
            db.log(batch["run_id"], "stage7",
                   f"schedule set M-F {settings.send_start_time}-{settings.send_end_time} "
                   f"{settings.send_timezone}", schedule_id=sched.get("id"))
        except Exception as exc:
            db.log(batch["run_id"], "stage7",
                   f"could not set schedule on {campaign_id}: {exc}", level="warn")
        existing_leads = 0
    else:
        existing_leads = int((emailbison.get_campaign(campaign_id) or {}).get("total_leads") or 0)

    # Sender alignment on EVERY live push, new campaign or top-up (app/senders.py):
    # connected accounts on fewer than two active campaigns, highest daily limit first,
    # until the campaign has the capacity its lead count calls for -- 1,000/day at
    # 3,000 leads, +200/day per extra 1,000 (Troy, 2026-09-14). Runs BEFORE the leads
    # load so the campaign is never live with nothing to send from.
    #
    # A BRAND-NEW campaign is a TEST SEAT (see runner.py's _intake_size), not a proven
    # winner yet, so it targets the modest seat allocation (sender_min_daily) rather
    # than the full lead-scaled target -- Specialty Legal and Commercial Real Estate
    # grabbed 684 and 542 accounts apiece on 2026-10-05 chasing a lead-scaled target on
    # day one, against a workspace with no room for that. Once a seat's vertical
    # clears its first verdict and graduates to the winners pool, scale_vertical()
    # (app/stages/s8_review.py) brings it up to the full lead-scaled target as part of
    # that pool's own allocation -- this modest number is only ever the launch day.
    from .. import senders as _senders
    target = (settings.sender_min_daily if is_new
              else _senders.target_for(existing_leads + len(contacts)))
    if settings.skip_sender_attach:
        aligned = {"accounts": 0, "daily_capacity": 0, "attached": 0,
                   "note": "senders are attached by hand in EmailBison"}
        db.log(batch["run_id"], "senders",
               f"campaign {campaign_id} ({planned_name}): sender attach skipped, "
               f"accounts are added by hand in EmailBison")
    elif is_new or settings.align_on_topup:
        try:
            aligned = _senders.align(campaign_id, run_id=batch["run_id"],
                                     target_daily=target)
        except Exception as exc:
            aligned = {"accounts": 0, "daily_capacity": 0, "error": str(exc)[:200]}
            db.log(batch["run_id"], "stage7",
                   f"sender alignment failed on {campaign_id}: {exc}", level="warn")
        # align() returns silently when it attaches nothing -- it logs only after a
        # successful attach. On 2026-09-23 it quietly moved 176 accounts onto campaign
        # 1084 (304 -> 480) during a top-up and left not one line behind, so there was
        # no record of where they went. Say what happened either way.
        db.log(batch["run_id"], "senders",
               f"campaign {campaign_id} ({planned_name}): attached "
               f"{aligned.get('attached', 0)} accounts, now {aligned.get('accounts', 0)} "
               f"for {aligned.get('daily_capacity', 0)}/day of a {target}/day target",
               **{k: v for k, v in aligned.items() if k != "campaign_id"})
        # A BRAND-NEW campaign still inherits the model campaign's generic 2,400/day
        # ceiling (Troy, 2026-10-06), far above the modest sender_min_daily share it
        # was actually just given -- the same gap senders.rebalance_winners() closes
        # nightly for proven winners, here from the very first day. EmailBison does
        # not auto-split a shared account between campaigns on it, so an uncapped new
        # launch could still out-eat a winner it happens to share an account with, even
        # though its own real allocation is small. Lower-only, same as the winners path.
        if is_new:
            try:
                cap = _senders.apply_fair_cap(campaign_id, aligned.get("daily_capacity") or 0,
                                              run_id=batch["run_id"])
                if cap.get("new_total"):
                    db.log(batch["run_id"], "senders",
                           f"campaign {campaign_id}: cap lowered to its real fair "
                           f"share -- max_emails_per_day {cap['old_total']} -> "
                           f"{cap['new_total']}")
            except Exception as exc:
                db.log(batch["run_id"], "senders",
                       f"fair-cap on launch failed for campaign {campaign_id}: {exc}",
                       level="warn")
    else:
        # A TOP-UP runs on the accounts the campaign already has. Troy, 2026-09-23:
        # "when we push recovery we dont need to add sender accounts, we just push to
        # the running campaign." Those accounts come out of the free pool, which is the
        # only thing that lets a NEW campaign launch at all -- and a top-up is leads
        # that were already sourced, so it has no claim on capacity a new vertical
        # needs. The campaign sends a little slower; nothing is blocked.
        have = _senders.dedicated_daily(campaign_id)
        aligned = {"accounts": 0, "daily_capacity": have, "attached": 0,
                   "note": "top-up: kept the campaign's existing accounts"}
        db.log(batch["run_id"], "senders",
               f"campaign {campaign_id} ({planned_name}): top-up, no accounts attached; "
               f"it keeps its {have}/day against a {target}/day target")
    # A POOL grows by joining, and a join deliberately skips the launch gate above --
    # that is what makes pooling worth doing. But nothing then checks that the pool can
    # still carry what it now holds: align() is best-effort and its failure is a warn,
    # so a pool that launched with 600/day for 700 leads can accumulate to thousands on
    # the same 600/day with no alarm. That is the 2026-09-15 failure over again, six
    # campaigns live and sending nothing. The leads are already loaded by now and
    # holding them back helps nobody, so this says so loudly rather than blocking.
    if pooled and not settings.skip_sender_attach:
        have = int(aligned.get("daily_capacity") or 0)
        if have < target * settings.pool_capacity_warn_ratio:
            total = existing_leads + len(contacts)
            db.log(batch["run_id"], "stage7",
                   f"pooled campaign {campaign_id} now holds {total:,} leads and needs "
                   f"{target}/day, but only {have}/day of dedicated capacity is attached "
                   f"-- it will send far slower than the review assumes", level="error")
            slack.post(
                f":warning: Pooled campaign `{campaign_id}` holds *{total:,} leads* but "
                f"has only *{have}/day* of dedicated sending against a {target}/day "
                f"target. Free up sender accounts or it will crawl, and every vertical "
                f"inside it will sit unjudged.")

    # The gap between email 1 and email 2 comes from the template and EmailBison exposes
    # no API to change it (every route 404s, probed 2026-09-18). So verify it and say so
    # loudly, rather than sending on whatever cadence the template happened to carry.
    try:
        waits = emailbison.sequence_step_waits(campaign_id)
        want = settings.step_wait_days
        wrong = [f"step {i + 1} waits {w}d" for i, w in enumerate(waits[:2]) if w != want]
        if wrong:
            db.log(batch["run_id"], "stage7",
                   f"campaign {campaign_id} cadence is NOT {want}d: {', '.join(wrong)} "
                   f"-- fix template {model_id} in the UI", level="warn")
            slack.cadence_wrong(batch["vertical"], campaign_id, waits, want,
                                model_id,
                                db.vertical_thread(batch["vertical"]))
        elif waits:
            db.log(batch["run_id"], "stage7", f"campaign {campaign_id} cadence ok: {waits}")
    except Exception as exc:
        db.log(batch["run_id"], "stage7",
               f"could not verify the cadence on {campaign_id}: {exc}", level="warn")

    senders = emailbison.campaign_sender_emails(campaign_id)
    if not senders:
        db.log(batch["run_id"], "stage7",
               f"campaign {campaign_id} has NO sender accounts attached -- leads will "
               f"load but nothing sends until accounts are aligned", level="warn")
    slack.senders_aligned(batch["vertical"], planned_name, len(senders),
                          aligned.get("daily_capacity", 0), target,
                          db.vertical_thread(batch["vertical"]))
    if is_new or pooled:
        # A pooled campaign is registered under db.POOL_VERTICAL, not under the vertical
        # that happened to open it. The row's `vertical` is what the review scores, and
        # a pool holds several -- naming it after the first one would have the loop
        # judge every later vertical's replies as that one's. Which verticals are inside
        # comes from db.verticals_in_campaign(), off the contacts' own labels.
        row_vertical = db.POOL_VERTICAL if pooled else batch["vertical"]
        with db.cursor() as cur:
            # pushed_lead_count starts at 0, not len(contacts): this runs BEFORE the
            # push below, using the pre-split count, and the push can still fail
            # (BulkLoadUnavailable returns early). The one place that increments it now
            # is db.increment_pushed_lead_count(), unconditionally, only after
            # EmailBison has actually accepted the load -- one counter, one writer, no
            # double count here and no overcount on a failed push.
            cur.execute(
                """INSERT INTO ve_campaigns (campaign_id, campaign_name, vertical,
                                             batch_id, run_id, pushed_lead_count,
                                             seed_industry, vertical_config)
                   VALUES (%s,%s,%s,%s,%s,0,%s,%s)
                   ON CONFLICT (campaign_id) DO NOTHING""",
                (campaign_id, planned_name, row_vertical, batch_id,
                 batch["run_id"],
                 (batch.get("summary") or {}).get("vertical", {}).get("seed_industry"),
                 json.dumps((batch.get("summary") or {}).get("vertical"), default=str)),
            )

    # Idempotent: contacts already attached to THIS campaign (a cut-off earlier push)
    # are skipped; progress is written per chunk so the next cut-off resumes too.
    already, contacts = split_for_push(contacts, campaign_id)
    if already:
        db.log(batch["run_id"], "stage7",
               f"{batch_id}: {len(already)} leads already in campaign {campaign_id}, "
               f"loading the remaining {len(contacts)}")
    try:
        if contacts:
            result = emailbison.push_leads(
                campaign_id, contacts, dry_run=False,
                on_upserted=db.record_lead_ids,
                on_attached=lambda emails: db.mark_attached(emails, campaign_id))
        else:
            result = {"dry_run": False, "campaign_id": campaign_id, "lead_count": 0,
                      "attached": 0, "id_by_email": {}}
    except emailbison.BulkLoadUnavailable as exc:
        # No CSV fallback: the file landed on a container disk that a restart wipes, and
        # nobody could act on it (2026-09-14). Say exactly what EmailBison refused, in
        # the vertical's thread and the channel, and leave the batch 'approved' so the
        # same button or command retries -- idempotently -- once the cause is fixed.
        # The records stay 'approved', not 'pushed': nothing (more) has been loaded.
        reason = str(exc)[:600]
        slack.push_failed(batch["vertical"], batch_id, campaign_id, reason,
                          db.vertical_thread(batch["vertical"]))
        db.log(batch["run_id"], "stage7", f"lead load refused for {batch_id}: {reason}",
               level="warn")
        return {"batch_id": batch_id, "error": reason, "lead_count": len(contacts),
                "campaign_id": campaign_id}

    # Store each contact's EmailBison lead id: stop-on-reply needs it to pull siblings.
    id_by_email = result.get("id_by_email") or {}
    if id_by_email:
        with db.cursor() as cur:
            cur.executemany(
                "UPDATE contacts SET eb_lead_id=%s, eb_campaign_id=%s WHERE email=%s",
                [(lead_id, campaign_id, email) for email, lead_id in id_by_email.items()],
            )

    # Tag the campaign with the arm it carries, so the reviewer can tell an arm from
    # an audience and does not file "copy-test a-sleep-test" as a dead market.
    try:
        summary = batch.get("summary") or {}
        db.record_campaign_variant(campaign_id, summary.get("copy_variant"),
                                   summary.get("experiment_id"))
    except Exception as exc:
        db.log(batch["run_id"], "stage7", f"variant tag not recorded: {exc}", level="warn")

    moved = db.mark_pushed(batch_id)
    loaded = len(contacts) + len(already)
    # UNCONDITIONAL, unlike the is_new/pooled insert above -- that one only ever fires
    # for a brand-new or pooled campaign, so every ordinary top-up push (the common
    # case) never incremented this at all. `contacts` here already excludes `already`
    # (split_for_push), so this is exactly what just got newly loaded, not a re-count.
    db.increment_pushed_lead_count(campaign_id, len(contacts))
    db.log(batch["run_id"], "stage7",
           f"pushed {loaded} leads for {batch_id} into campaign {campaign_id}",
           rows_marked_pushed=moved)
    # Start it, so approve + push means sending begins in the next schedule window.
    started = False
    if settings.auto_start_campaigns:
        try:
            emailbison.start_campaign(campaign_id)
            started = (emailbison.get_campaign(campaign_id).get("status") or "").lower() != "draft"
            db.log(batch["run_id"], "stage7",
                   f"campaign {campaign_id} {'started' if started else 'did NOT leave draft'}",
                   level="info" if started else "warn")
        except Exception as exc:
            db.log(batch["run_id"], "stage7", f"could not start campaign {campaign_id}: {exc}",
                   level="warn")
    # Into the vertical's thread: the last line of its story.
    slack.pushed(batch["vertical"], batch_id, loaded, planned_name,
                 db.vertical_thread(batch["vertical"]), started=started,
                 topup=(batch.get("approved_by") == "system:topup"))
    result["batch_id"] = batch_id
    result["started"] = started
    return result


def confirm_csv(batch_id: str, out_dir: str | None = None) -> str:
    """Mark a manually-loaded batch as pushed, after a CSV fallback.

    NOTE: a batch loaded by hand has no EmailBison lead ids recorded, so stop-on-reply
    cannot pull siblings for it. That is called out loudly rather than silently accepted,
    because 4 contacts per company without sibling suppression is exactly the situation
    multi-contact was only made safe by avoiding.
    """
    moved = db.mark_pushed(batch_id)
    db.log(None, "stage7", f"{batch_id} confirmed manually loaded", rows=moved, level="warn")
    return (f"{batch_id}: {moved} rows marked pushed. WARNING: manually-loaded batches "
            f"have no lead ids, so company-level stop-on-reply will not cover this batch.")
