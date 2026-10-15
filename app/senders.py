"""Sender-account alignment: give a new campaign the accounts it needs to send.

Troy, 2026-09-13/14, the rules Ivan works to:
  - align all available ACTIVE accounts (EmailBison status "Connected");
  - an account can be on at most 3 ACTIVE campaigns at once -- archived and completed
    campaigns do not count, which is also why "take from archived campaigns first,
    then campaigns 90%+ complete" needs no detaching: those accounts are simply free.
    An account's daily limit is split across its campaigns, not added, so a shared
    account counts for limit / (campaigns + 1) (share_of). Ranking: idle accounts
    first, then those on one campaign, then two (Troy, 2026-10-01);
  - capacity scales with the list: 1,000 sends/day at 3,000 leads, +200/day for each
    further 1,000 leads (target_for). Re-aligned on every push, top-ups included.

A campaign duplicated from the model inherits NO sender accounts (verified 2026-09-07),
so until this ran, every VE campaign loaded its leads and sent nothing until a human
attached accounts by hand. Verified 2026-09-14: POST /api/campaigns/{id}/attach-
sender-emails with {"sender_email_ids": [...]} attaches; it refuses ids already on the
campaign, so re-running is safe. There is no detach route, and this module never
touches another campaign's accounts.

Workspace shape, measured 2026-09-14: 2,658 connected accounts, ~12,300 sends/day in
total; most accounts allow 3/day, ~380 allow 10-20. Highest limit first means a campaign
reaches 1,000/day on ~80-100 accounts instead of ~330.
"""

from __future__ import annotations

import re
from collections import Counter

from . import db
from .clients import emailbison
from .config import settings

# Campaign statuses that hold an account's "active" slot. Draft campaigns are not
# sending; the freshly duplicated campaign is a draft until its leads load.
ACTIVE_STATUSES = {"active", "paused"}
ATTACH_CHUNK = 50


# Tags that disqualify an account from sending for us. Troy, 2026-09-22, after two
# IDPR campaigns launched on them: "we only want active emails without warming up or
# burnt tags." Measured the same day: the first 225 accounts in the workspace were ALL
# tagged "Warming Up", all had warmup_enabled true, and all had a daily limit of 3. The
# selector only asked whether the account was Connected, so every one of them qualified
# -- filling a campaign's slots with accounts that can send three emails a day.
BLOCKED_TAGS = ("warming up", "warming", "warm up", "warmup",
                "burnt", "burned", "burn", "paused", "do not use", "dnu")

# The operator's explicit green light. Measured 2026-09-22: 1,507 accounts carry
# "\N{LARGE GREEN CIRCLE} Active", NOT ONE of them also carries "Warming Up", and every
# wu- cohort stamp they still hold is 80-90 days old. They are graduated accounts whose
# warmup stamp was never cleared, and they are sending -- median 95 emails of lifetime
# volume each. The wu- rule below read those stale stamps as "currently warming" and hid
# 1,255 accounts worth 3,769 sends/day from the selector, which is why stage 7 held a
# batch for want of 600/day while they sat there idle.
#
# Normalized to letters and digits so the emoji and any spacing fall away; the match is
# exact, so a future "Inactive" tag cannot be read as permission. A BLOCKED tag still
# wins over this: if an account is ever marked both Active and Warming Up, the cautious
# word is the one we obey.
ACTIVE_TAGS = ("active",)


def _tag_names(sender: dict) -> list[str]:
    out = []
    for t in (sender.get("tags") or []):
        name = (t or {}).get("name") if isinstance(t, dict) else t
        if name:
            out.append(str(name).strip().lower())
    return out


def _normalized_tags(names: list[str]) -> set[str]:
    return {re.sub(r"[^a-z0-9]+", " ", n).strip() for n in names}


def operator_active(sender: dict) -> bool:
    """True when a human has explicitly tagged this account as active."""
    return bool(_normalized_tags(_tag_names(sender)) & set(ACTIVE_TAGS))


def _warmup_stamp_age_days(names: list[str]) -> int | None:
    """Days since the OLDEST wu-YYYY-MM-DD cohort stamp, or None if there is none.

    Oldest, not newest. Troy, 2026-09-30: most Active accounts carry two stamps --
    e.g. wu-2026-09-15 AND wu-2026-09-23 -- and the older one is when the warm-up
    actually started; the later one is a second cohort label on the same account,
    not a restart. Reading the newest stamp treated 1,098 Active accounts as seven
    days into a fresh warm-up and held 3,294 sends/day back until late October. By
    the oldest stamp and the 14-day warm-up that is really run, they were ready on
    2026-09-29.
    """
    from datetime import date
    today, oldest = date.today(), None
    for t in names:
        if not t.startswith("wu-"):
            continue
        try:
            d = date.fromisoformat(t[3:])
        except ValueError:
            continue
        age = (today - d).days
        if oldest is None or age > oldest:
            oldest = age
    return oldest


def warming_now(sender: dict) -> bool:
    """A warmup stamp young enough to still mean something.

    The Active tag is allowed to overrule a STALE stamp, because the workspace stamps a
    cohort and never clears it: all 1,255 Active accounts carrying one on 2026-09-22
    were stamped 80-90 days ago and had been sending for months. It must NOT overrule a
    FRESH one -- an account stamped this week is warming whatever else it is tagged, and
    the tag is the likelier mistake.

    This is the narrow form of the check. The blunt version (refuse any account with
    warmup_enabled and a daily limit of 5 or less) reads as the same safeguard but is
    not: measured 2026-09-22 it would refuse 1,292 of the 1,507 Active accounts, worth
    3,876 sends/day, because a graduated account here keeps warmup running as
    maintenance and sits at 3/day until someone raises it. That is most of the
    workspace's real capacity, not a safety margin.
    """
    age = _warmup_stamp_age_days(_tag_names(sender))
    return age is not None and age < settings.warmup_stamp_fresh_days


def sendable(sender: dict) -> bool:
    """Connected, out of warmup, and not tagged out of service.

    A tag is the operator's own word on the account and beats any inferred signal, so
    it is checked first. warmup_enabled alone is NOT disqualifying -- plenty of healthy
    accounts leave warmup running as maintenance -- but combined with a warming tag or
    a starter daily limit it is what a warming account looks like.

    That inference is a guess made in the absence of a human's word. Where the human HAS
    spoken -- an explicit Active tag -- the guess must yield to it, or the engine
    overrules the operator on the strength of a stale timestamp. Only a blocked tag
    outranks Active.
    """
    if (sender.get("status") or "").lower() != "connected":
        return False
    names = _tag_names(sender)
    for tag in names:
        if any(b in tag for b in BLOCKED_TAGS):
            return False
    # A stamp from the last few weeks beats the Active tag: an account stamped this week
    # is warming whatever else it is tagged, and the tag is the likelier mistake.
    if warming_now(sender):
        return False
    if operator_active(sender):
        return True
    # wu-YYYY-MM-DD is how this workspace stamps a warmup cohort.
    if any(t.startswith("wu-") for t in names):
        return False
    if sender.get("warmup_enabled") and int(sender.get("daily_limit") or 0) <= 5:
        return False
    return True


def choose(senders: list[dict], active_counts: dict[int, int], already: set[int],
           target_daily: int, max_active: int) -> list[dict]:
    """Pick accounts for one campaign. Pure, so the rule is testable.

    Eligible: Connected, on fewer than max_active active campaigns, not already on this
    one. Order: fewest active campaigns first (0, then 1, then 2), highest daily limit
    within each rank. Stops once the summed SHARE reaches target_daily.

    SHARE, not limit. Troy, 2026-10-01: "ranked by no campaigns attached, then 1
    campaign, then 2 campaigns, and we share sender emails from that ranking."
    EmailBison splits an account's daily limit across the campaigns it is on, so an
    account on n other campaigns gives this one about limit / (n + 1). Counting the
    whole limit made a "1,000/day" campaign real only when every pick was idle.
    """
    eligible = [
        s for s in senders
        if sendable(s)
        and int(s.get("id") or 0) not in already
        and active_counts.get(int(s.get("id") or 0), 0) < max_active
        and int(s.get("daily_limit") or 0) > 0
    ]
    # DEDICATED accounts first. 2026-09-15: highest-limit-first picked exactly the
    # accounts the older campaigns already use -- every one of the 464 attached to our
    # six campaigns was shared -- and an account's daily limit goes to the campaign it
    # joined first, so ours sent 0. Free accounts (on no active campaign) send for us
    # alone; only when those run out do shared ones get added, and the caller reports
    # how much of the capacity is real.
    eligible.sort(key=lambda s: (active_counts.get(int(s.get("id") or 0), 0),
                                 -int(s.get("daily_limit") or 0),
                                 int(s.get("id") or 0)))
    picked, capacity = [], 0
    for s in eligible:
        if capacity >= target_daily:
            break
        picked.append(s)
        capacity += share_of(s, active_counts)
    return picked


def share_of(sender: dict, active_counts: dict[int, int]) -> int:
    """What this account can send for ONE more campaign: limit / (campaigns + 1)."""
    n = active_counts.get(int(sender.get("id") or 0), 0)
    return int(sender.get("daily_limit") or 0) // (n + 1)


def target_for(leads: int) -> int:
    """Daily send target for a campaign holding this many leads.

    Troy, 2026-09-14: 3,000 leads -> 1,000/day, 4,000 -> 1,200/day. Linear above the
    base, never below it, rounded to the nearest 50: 10,000 leads -> 2,400/day.
    """
    extra = max(int(leads) - settings.sender_base_leads, 0)
    raw = settings.sender_target_daily + settings.sender_per_extra_1000 * extra / 1000
    return int(round(raw / 50.0) * 50)


def active_campaign_counts(exclude_campaign_id: int | None = None) -> dict[int, int]:
    """How many active campaigns each account is on right now."""
    counts: Counter = Counter()
    for c in emailbison.list_campaigns():
        if (c.get("status") or "").lower() not in ACTIVE_STATUSES:
            continue
        if exclude_campaign_id and c.get("id") == exclude_campaign_id:
            continue
        for s in emailbison.campaign_sender_emails(c["id"]):
            if s.get("id") is not None:
                counts[int(s["id"])] += 1
    return dict(counts)


def archive_completed_campaigns(run_id: str | None = None) -> list[dict]:
    """Archive every COMPLETED campaign so its sender accounts come back to the pool.

    Troy, 2026-09-30: "archiving frees accounts." A completed campaign still holds
    every account it launched with -- "New Healthcare 2" finished in August and was
    still holding 177 -- and a held account is a slot no new launch can use. Pausing
    does not release them; only /archive does (see emailbison.archive_campaign). The
    model campaign and anything in never_archive_campaign_ids are left alone.
    """
    out: list[dict] = []
    keep = set(settings.never_archive_campaign_ids) | {int(settings.model_campaign_id or 0)}
    for c in emailbison.list_campaigns():
        if (c.get("status") or "").lower() != "completed":
            continue
        cid = int(c.get("id") or 0)
        if not cid or cid in keep:
            continue
        # "COMPLETED" ONLY MEANS "OUT OF LOADED LEADS". Funded Proptech, 2026-09-30:
        # campaign 1147 was pending a verdict at 1,718 of 2,000 sends, ran through
        # what it had loaded, EmailBison called it completed, and this archived it --
        # after which 638 top-up leads were loaded into a campaign that can never be
        # resumed. A VE campaign is archived here only when its vertical is finished:
        # dropped, or genuinely out of supply. Anything still pending or scaling is
        # left for the top-up loop to refill and restart. Pools are never archived here.
        ve = db.get_ve_campaign(cid)
        if ve:
            if (ve.get("vertical") or "") == db.POOL_VERTICAL:
                continue
            outcome = db.vertical_outcome(ve.get("vertical") or "") or {}
            finished = outcome.get("verdict") == "drop" or bool(ve.get("scale_exhausted_at"))
            if not finished:
                db.log(run_id, "senders", f"campaign {cid} ({c.get('name')}) is completed "
                       f"but its vertical is {outcome.get('verdict') or 'unjudged'}, not "
                       f"finished; left for top-up, not archived")
                continue
        try:
            res = emailbison.archive_campaign(cid) or {}
        except Exception as exc:
            db.log(run_id, "senders", f"archive of completed campaign {cid} failed: {exc}",
                   level="warn")
            continue
        freed = res.get("senders_freed")
        out.append({"campaign_id": cid, "name": c.get("name"), "senders_freed": freed})
        db.log(run_id, "senders", f"archived completed campaign {cid} ({c.get('name')}): "
               f"{freed if freed is not None else '?'} sender accounts released")
    if out:
        try:
            from .clients import slack
            slack.post(":open_file_folder: *Archived " + str(len(out)) +
                       " completed campaign(s)* to free their sender accounts: " +
                       ", ".join(f"`{o['name']}`" for o in out))
        except Exception:
            pass
    return out


def free_capacity() -> dict:
    """Accounts on NO active campaign, and the sends/day they add up to.

    This is the workspace's real headroom for a new campaign. Shared accounts count
    for nothing here: their limit is already spoken for by the campaign that had them
    first. Measured 2026-09-15: 3,000 connected, 1,023 on active campaigns, 2,332 free
    for 7,190/day -- almost all 3/day accounts.
    """
    senders = emailbison.list_sender_emails(limit=10000)
    counts = active_campaign_counts()
    # Troy, 2026-09-30: "an account can be on 3 campaigns." EmailBison splits an
    # account's daily limit across the campaigns it is on, so an account on n live
    # campaigns can give a NEW one about limit / (n + 1). Idle accounts (n = 0) give
    # their whole limit and are still reported separately as `dedicated`, because
    # that is the figure that cannot be diluted by anything else launching later.
    limit_n = max(settings.sender_max_active_campaigns, 1)
    free = [s for s in senders if sendable(s)
            and counts.get(int(s.get("id") or 0), 0) < limit_n
            and int(s.get("daily_limit") or 0) > 0]
    daily = 0
    dedicated = 0
    for s in free:
        n = counts.get(int(s.get("id") or 0), 0)
        share = int(s.get("daily_limit") or 0) // (n + 1)
        daily += share
        if n == 0:
            dedicated += share
    return {"accounts": len(free), "daily": daily, "dedicated": dedicated,
            "senders": senders, "counts": counts}


def dedicated_daily(campaign_id: int, counts: dict[int, int] | None = None) -> int:
    """Sends/day this campaign gets from accounts it does not share with a live campaign."""
    counts = counts if counts is not None else active_campaign_counts(exclude_campaign_id=campaign_id)
    have = emailbison.campaign_sender_emails(campaign_id)
    return sum(int(s.get("daily_limit") or 0) for s in have
               if counts.get(int(s.get("id") or 0), 0) == 0)


def current_daily_capacity(campaign_id: int, counts: dict[int, int] | None = None) -> int:
    """This campaign's REAL current capacity from whatever accounts it already holds,
    counting shared accounts at their true fair share -- the same figure align() has
    always computed internally as `have_daily`, exposed standalone so a cap-only pass
    (fair_cap_all_active) does not need to run the attach step too."""
    counts = (counts if counts is not None
             else active_campaign_counts(exclude_campaign_id=campaign_id))
    have = emailbison.campaign_sender_emails(campaign_id)
    return sum(share_of(s, counts) for s in have)


def align(campaign_id: int, run_id: str | None = None, dry_run: bool = False,
          target_daily: int | None = None) -> dict:
    """Attach accounts until the campaign has target_daily sends/day of DEDICATED capacity."""
    target = target_daily or settings.sender_target_daily
    senders = emailbison.list_sender_emails(limit=10000)
    counts = active_campaign_counts(exclude_campaign_id=campaign_id)
    have = emailbison.campaign_sender_emails(campaign_id)
    already = {int(s["id"]) for s in have if s.get("id") is not None}
    # `counts` excludes this campaign, so share_of() reads each attached account's
    # share for THIS campaign.
    have_daily = current_daily_capacity(campaign_id, counts)
    if have_daily >= target:
        return {"campaign_id": campaign_id, "attached": 0, "accounts": len(have),
                "daily_capacity": have_daily, "target": target, "note": "already at target"}

    picked = choose(senders, counts, already, target - have_daily,
                    settings.sender_max_active_campaigns)
    # SHARED ACCOUNTS ARE ATTACHED, in choose()'s ranking. Until 2026-10-01 this
    # line read `picked = dedicated or picked`: if even ONE idle account was picked,
    # every shared pick was thrown away. The Healthcare rerun (campaign 1226) asked
    # for 1,000/day, choose() found 146 accounts worth exactly that, and align()
    # attached the 14 idle ones -- 64/day -- while 1,345 Active accounts with
    # 5,875/day of headroom sat on one or two other campaigns. Troy: "ranked by no
    # campaigns attached, then 1, then 2, and we share from that ranking."
    picked_daily = sum(share_of(s, counts) for s in picked)

    result = {"campaign_id": campaign_id, "target": target, "dry_run": dry_run,
              "eligible_pool": sum(1 for s in senders
                                   if sendable(s)
                                   and counts.get(int(s.get("id") or 0), 0)
                                   < settings.sender_max_active_campaigns),
              "picked": len(picked), "picked_daily": picked_daily}
    if dry_run or not picked:
        result.update({"attached": 0, "accounts": len(have),
                       "daily_capacity": have_daily + (picked_daily if dry_run else 0)})
        return result

    attached = 0
    ids = [int(s["id"]) for s in picked]
    for i in range(0, len(ids), ATTACH_CHUNK):
        chunk = ids[i:i + ATTACH_CHUNK]
        try:
            emailbison.attach_sender_emails(campaign_id, chunk)
            attached += len(chunk)
        except Exception as exc:
            db.log(run_id, "senders", f"attach failed for {len(chunk)} accounts on "
                   f"campaign {campaign_id}: {exc}", level="warn")

    # Trust the read-back, not the request: it is what will actually send -- and only
    # the accounts this campaign does not share are counted.
    now = emailbison.campaign_sender_emails(campaign_id)
    daily = sum(share_of(s, counts) for s in now)
    result.update({"attached": attached, "accounts": len(now), "daily_capacity": daily})
    db.log(run_id, "senders",
           f"campaign {campaign_id}: {len(now)} accounts, {daily}/day of {target} target "
           f"({attached} attached this pass)", **{k: v for k, v in result.items()
                                                    if k not in ("campaign_id",)})
    return result


def apply_fair_cap(campaign_id: int, daily_capacity: int, run_id: str | None = None,
                   dry_run: bool = False) -> dict:
    """Set a campaign's OWN send ceiling to its real fair share, instead of leaving it
    at whatever generic cap (1,500/2,400 on every VE campaign measured 2026-10-06) lets
    it monopolize a shared account.

    EmailBison's own documentation: "we don't have automatic distribution per
    campaign. A campaign that gets processed first may take all of the sending
    volume... If you want to evenly distribute the traffic, set the max daily emails
    value in each campaign." share_of()'s daily_limit/(n+1) split was never something
    EmailBison enforces on its own -- it is a FAIR-SHARE TARGET that only becomes real
    once every campaign sharing that pool caps itself to it. Attaching more accounts
    without this is necessary but not sufficient: the account can still go entirely to
    whichever campaign happens to process first that day.

    Only LOWERS the cap, never raises it past whatever is already configured -- a human
    may have set max_emails_per_day deliberately for reasons this function cannot see
    (Healthcare's 500/day cap, 2026-10-05), and raising a cap is a different, separate
    decision from the one this function exists to make. The existing new-leads-to-total
    ratio is preserved rather than invented fresh, so a campaign that already spent its
    quota mostly on follow-ups keeps that balance at the new, smaller scale.
    """
    current = emailbison.get_campaign(campaign_id) or {}
    cur_new = int(current.get("max_new_leads_per_day") or 0)
    cur_total = int(current.get("max_emails_per_day") or 0)
    if cur_total <= 0 or daily_capacity <= 0 or daily_capacity >= cur_total:
        return {"campaign_id": campaign_id, "skipped": True,
               "reason": "no current caps, no capacity, or fair share already covered"}
    ratio = daily_capacity / cur_total
    new_total = max(daily_capacity, 1)
    new_new_leads = max(int(round(cur_new * ratio)), 1)
    if dry_run:
        return {"campaign_id": campaign_id, "dry_run": True, "old_total": cur_total,
               "new_total": new_total, "old_new_leads": cur_new,
               "new_new_leads": new_new_leads}
    try:
        emailbison.update_campaign_limits(campaign_id, new_new_leads, new_total)
    except Exception as exc:
        db.log(run_id, "senders",
               f"fair-cap update failed for campaign {campaign_id}: {exc}", level="warn")
        return {"campaign_id": campaign_id, "error": str(exc)[:200]}
    db.log(run_id, "senders",
           f"campaign {campaign_id}: cap lowered to its real fair share -- "
           f"max_emails_per_day {cur_total} -> {new_total}, max_new_leads_per_day "
           f"{cur_new} -> {new_new_leads} (EmailBison does not auto-split a shared "
           f"account; each sharer must cap itself or the one processed first takes it "
           f"all)")
    return {"campaign_id": campaign_id, "old_total": cur_total, "new_total": new_total,
           "old_new_leads": cur_new, "new_new_leads": new_new_leads}


def rebalance_winners(run_id: str | None = None, dry_run: bool = False) -> list[dict]:
    """Daily, additive-only pass: give every live winner a chance at sender_target_daily,
    BEST INTERESTED-REPLY PERFORMER FIRST.

    Troy, 2026-10-06: 12+ VE campaigns sharing a ~9,000/day pool cannot all reach the
    1,000/day target at once -- the math does not allow it -- and before this, which
    campaign ended up thin was pure accident of launch timing, not performance. One
    campaign measured at 347/day on 327 accounts the same week another held 1,194/day
    on 435, with no relationship to which one actually converts replies.

    This does not invent a new weighted-share formula. Every winner still asks align()
    for the SAME target (unchanged contract) -- the only thing that changes is ORDER.
    Because the shared pool runs dry as better performers go first, whatever capacity
    is left for the rest is exactly what "best performers get priority" means in
    practice: the top of vertical_outcomes.objective_rate (interested + booked events
    per send -- the same number the nightly review already uses to judge SCALE vs
    DROP, not a new metric) gets first claim each day, and a weaker winner simply stops
    GAINING once today's pool is spent. It never LOSES ground: align() only ever
    attaches, so an account a weaker campaign already holds is never pulled back to
    hand to a stronger one. Run once a day (the nightly review, alongside
    report.post_daily()) rather than weekly, because capacity frees at unpredictable
    moments -- an archive, a batch of warmups graduating -- and a week of lag between
    that happening and winners actually getting it is a week of idle accounts.

    Pools (db.POOL_VERTICAL) and already-exhausted campaigns are skipped: a pool
    serves several verticals at once and has no single objective_rate to rank on, and
    an exhausted campaign has nowhere to put more capacity until a human reopens it
    (see scale_vertical's own exhaustion check).
    """
    with db.cursor() as cur:
        cur.execute(
            """SELECT v.campaign_id, v.vertical, o.objective_rate
                 FROM ve_campaigns v
                 JOIN vertical_outcomes o ON o.vertical = v.vertical
                WHERE v.vertical <> %s
                  AND o.verdict = 'scale'
                  AND v.scale_exhausted_at IS NULL
                ORDER BY o.objective_rate DESC""",
            (db.POOL_VERTICAL,))
        winners = [dict(r) for r in cur.fetchall()]
    if not winners:
        return []
    out = []
    for w in winners:
        try:
            r = align(w["campaign_id"], run_id=run_id, dry_run=dry_run,
                      target_daily=settings.sender_target_daily)
        except Exception as exc:
            r = {"campaign_id": w["campaign_id"], "attached": 0, "accounts": 0,
                 "daily_capacity": 0, "error": str(exc)[:200]}
        # ATTACHING ACCOUNTS IS NOT ENOUGH. EmailBison does not split a shared account
        # between the campaigns on it -- whichever one it processes first can take the
        # whole thing. align()'s daily_capacity is a fair-share TARGET, not something
        # EmailBison enforces on its own; it only becomes real once the campaign's own
        # ceiling is capped to it, which is what the whole fair-share math was for.
        try:
            cap = apply_fair_cap(w["campaign_id"], r.get("daily_capacity") or 0,
                                 run_id=run_id, dry_run=dry_run)
            r["cap"] = cap
        except Exception as exc:
            r["cap"] = {"campaign_id": w["campaign_id"], "error": str(exc)[:200]}
        r["vertical"] = w["vertical"]
        r["objective_rate"] = w["objective_rate"]
        out.append(r)
    gained = [r for r in out if r.get("attached")]
    if gained:
        lines = ", ".join(f"{r['vertical']} (+{r['attached']}, "
                          f"{r['objective_rate']:.1%})" for r in gained)
        db.log(run_id, "senders",
               f"daily rebalance: {len(gained)} of {len(out)} winner(s) gained "
               f"accounts, best interested-rate first: {lines}")
    capped = [r for r in out if (r.get("cap") or {}).get("new_total")]
    if capped:
        lines = ", ".join(f"{r['vertical']} ({r['cap']['old_total']}->"
                          f"{r['cap']['new_total']})" for r in capped)
        db.log(run_id, "senders",
               f"daily rebalance: {len(capped)} winner(s) had their own cap lowered "
               f"to their real fair share: {lines}")
    return out


def fair_cap_all_active(run_id: str | None = None, dry_run: bool = False) -> list[dict]:
    """Fair-cap EVERY active VE campaign -- standalone or pooled, any verdict, not
    just proven winners -- using whatever capacity it currently holds.

    Troy, 2026-10-07: summed every active campaign's max_emails_per_day and got
    24,386/day against a real ~8,000-9,000/day account ceiling, nearly 3x oversubscribed
    at the cap level alone. rebalance_winners() only ever fair-capped verdict='scale'
    campaigns, so everything else -- both pools (no single objective_rate to rank on,
    deliberately excluded there) and pre-fix test-seat launches (Specialty Legal,
    Commercial Real Estate, Specialized Management Consulting all launched 2026-10-05,
    a day before the new-launch fair-cap fix existed) -- sat at the generic 2,400/day
    default. EmailBison does not split a shared account between the campaigns on it,
    so any ONE uncapped neighbor on a shared account can still out-eat a correctly
    capped winner right next to it; fair-capping only some campaigns protects nothing.

    Unlike rebalance_winners(), this attaches NO accounts -- current_daily_capacity()
    reads whatever each campaign already holds, cap-only, every active campaign, same
    lower-only safety property as apply_fair_cap() itself.
    """
    with db.cursor() as cur:
        cur.execute("SELECT campaign_id, vertical FROM ve_campaigns ORDER BY campaign_id")
        camps = [dict(r) for r in cur.fetchall()]
    out = []
    for c in camps:
        try:
            capacity = current_daily_capacity(c["campaign_id"])
            cap = apply_fair_cap(c["campaign_id"], capacity, run_id=run_id, dry_run=dry_run)
        except Exception as exc:
            cap = {"campaign_id": c["campaign_id"], "error": str(exc)[:200]}
            capacity = 0
        cap["vertical"] = c["vertical"]
        cap["daily_capacity"] = capacity
        out.append(cap)
    changed = [r for r in out if r.get("new_total")]
    if changed:
        lines = ", ".join(f"{r['vertical']} ({r['old_total']}->{r['new_total']})"
                          for r in changed)
        db.log(run_id, "senders",
               f"fair-cap backfill: {len(changed)} of {len(out)} active campaign(s) "
               f"corrected: {lines}")
    return out
