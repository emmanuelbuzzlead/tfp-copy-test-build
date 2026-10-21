"""Keep the Wave 1 copy-test campaigns filled to their target -- resumable, cron-driven.

Troy, 2026-09-27: 1,500-2,000 leads per campaign; copy that already passed is kept; the
rest comes from tier 2 (people last emailed 120+ days ago, still in EmailBison). Called by
the 2-hourly `resume` job, so a stall (vendor out of credit, container restart) picks up
on its own. Each pass:
  1. sweep: read every loaded lead back from EmailBison, pull any that break the CURRENT
     copy rules (rules tighten over time; old copy must not ride along)
  2. load saved copy that still passes
  3. write + load tier-2 leads in batches until each campaign reaches the target
  4. attach senders, post to Slack
A DB lock (job_locks) keeps two containers from filling at once. Campaigns stay drafts.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import defaultdict

from . import copy_rules, db, scaling, senders, testgrid, validator
from .clients import emailbison, slack
from .config import settings

TIER2_VERTICAL = "Tier 2 - previously emailed"
PERSONAL_DOMAINS = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com', 'icloud.com', 'msn.com', 'live.com', 'comcast.net',
                    'att.net', 'sbcglobal.net', 'me.com', 'protonmail.com', 'ymail.com', 'verizon.net', 'bellsouth.net', 'mail.com', 'gmx.com'}
LOCK = "wave1_fill"
BATCH = int(os.getenv("WAVE1_BATCH", "600"))
FIT_WORKERS = int(os.getenv("WAVE1_FIT_WORKERS", "10"))
STALE_MIN = 90   # a 600-contact copy batch can outlast 25 minutes


def _name() -> str:
    from . import client
    return client.profile().display_name


def _min_openrouter() -> float:
    try:
        return float(os.getenv("OPENROUTER_MIN_BALANCE_USD", "15"))
    except ValueError:
        return 15.0


def _openrouter_ok() -> tuple[bool, float]:
    """(enough, remaining dollars). A floor of 0 disables the check, and a failed lookup never blocks a fill."""
    floor = _min_openrouter()
    key = os.getenv("OPENROUTER_API_KEY")
    if floor <= 0 or not key:
        return True, -1.0
    try:
        import httpx
        r = httpx.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        d = (r.json() or {}).get("data") or {}
        left = float(d.get("total_credits", 0)) - float(d.get("total_usage", 0))
        return left >= floor, left
    except Exception:
        return True, -1.0


def _seats() -> int:
    """Contacts per company, from the client profile (Lumion: 5, Emmanuel 2026-10-10)."""
    from . import client
    return int(client.profile().contacts_per_company or 3)


def _llm_ready() -> tuple[bool, str]:
    """(ok to keep filling, why not). Follows the REAL waterfall instead of reading one account.

    Copy runs Concentrate first, OpenRouter second; research runs OpenRouter first, Concentrate second. Reading only the OpenRouter
    balance stopped a fill that Concentrate could have served (Emmanuel, 2026-10-10). Concentrate has no balance endpoint, and a tiny
    call passes on a near-empty balance, so this sends one real-size copy-model call through the actual chain and sees who answers:
      - Concentrate answers: fine, the shared OpenRouter account is not touched for copy.
      - OpenRouter answers: fine only if its balance is above the floor (it is shared with other clients).
      - nobody answers: pause until the next 2-hourly tick.
    """
    floor = _min_openrouter()
    if floor <= 0:
        return True, ""
    try:
        from .clients import llm
        _t, provider, _m = llm.complete_meta("anthropic/claude-haiku-4.5", "You write one short sentence.",
                                             "Write a sentence about welding school. " * 60, 0.2, max_tokens=1500, provider="copy")
    except Exception as exc:
        return False, f"no LLM provider answered a real-size copy call ({str(exc)[:120]}). Top up Concentrate or OpenRouter"
    if provider == "openrouter":
        enough, left = _openrouter_ok()
        if not enough:
            return False, (f"only OpenRouter is answering and its shared balance is ${left:,.2f}, under the ${floor:,.0f} floor. "
                           f"Fix Concentrate or top OpenRouter up")
    return True, provider


def _reuse_fit_check() -> bool:
    from . import client
    return bool(client.profile().reuse_fit_check)


def _guess_unlabeled() -> bool:
    from . import client
    return client.profile().name == "productevo"


def _lock(holder: str) -> bool:
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS job_locks (name TEXT PRIMARY KEY, holder TEXT,
                       heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""INSERT INTO job_locks (name, holder, heartbeat_at) VALUES (%s, %s, now())
                       ON CONFLICT (name) DO UPDATE SET holder=EXCLUDED.holder, heartbeat_at=now()
                       WHERE job_locks.heartbeat_at < now() - make_interval(mins => %s)
                       RETURNING holder""", (LOCK, holder, STALE_MIN))
        return cur.fetchone() is not None


def _beat(holder: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE job_locks SET heartbeat_at=now() WHERE name=%s AND holder=%s", (LOCK, holder))


def release_dead_lock() -> bool:
    """Called once at web boot. This service is a single instance, so a booting process means whoever held the fill lock died with the
    old container. Without this a redeploy left the lock held for STALE_MIN (90) minutes and the boot resume answered
    'already_running' while nothing ran (Emmanuel, 2026-10-10)."""
    with db.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name=%s", (LOCK,))
        return (cur.rowcount or 0) > 0


def _release(holder: str) -> None:
    with db.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name=%s AND holder=%s", (LOCK, holder))


def _targets_for(angles: dict, base_target: int) -> dict:
    """Troy, 2026-10-09: "tiered leads... should be the bulk of our sourcing." The flat
    [fill] target used to apply to every angle alike, including a decided winner with a
    real 10,000-lead ceiling or a lookalike's 1,500-lead first stage -- so this engine
    (which SHOULD carry most of the load, since it's reactivating known contacts, not
    paying for fresh discovery) silently stopped contributing to MMM the moment its local
    count crossed 2,000, leaving the entire rest of the climb to the much slower
    fresh-discovery/90-day-reuse cascade in scaling.py. Use the REAL target from
    scale_targets when the angle has one; fall back to the flat test-phase default
    (still right for an angle still in its original 3-way test) otherwise.
    """
    with db.cursor() as cur:
        cur.execute("SELECT angle, target_leads FROM scale_targets")
        overrides = {r["angle"]: int(r["target_leads"]) for r in cur.fetchall()}
    return {a: overrides.get(a, base_target) for a in angles}


def _setup():
    cfg = testgrid.load_config()
    if not cfg or not cfg.get("angle_prompts"):
        return None
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id FROM copy_test_campaigns WHERE is_primary")
        campaign_of = {r["angle"]: int(r["campaign_id"]) for r in cur.fetchall()}
    if len(campaign_of) < len(cfg["angle_prompts"]):
        return None
    base_target = int((cfg.get("fill") or {}).get("target", 1750))
    angles = dict(cfg["angle_prompts"])
    targets = _targets_for(angles, base_target)
    return cfg, angles, campaign_of, targets


def counts(campaign_of: dict) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT eb_campaign_id, count(*) n FROM contacts WHERE eb_campaign_id = ANY(%s) GROUP BY 1",
                    (list(campaign_of.values()),))
        by = {r["eb_campaign_id"]: r["n"] for r in cur.fetchall()}
    return {a: by.get(cid, 0) for a, cid in campaign_of.items()}


def passes(variant: str, first_name: str | None, p1: str, p2: str) -> bool:
    copy_rules.set_context(variant, first_name)
    for text, cap in ((p1, settings.email1_max_chars), (p2, settings.email2_max_chars)):
        if not (text or "").strip() or len(text) > cap or validator._looks_cut_off(text):
            return False
        if [p for p in copy_rules.check(text) if "reading level" not in p]:
            return False
    return True


def _held() -> bool:
    """HOLD_ANGLE_FILL=true: the three angle campaigns stop receiving leads (Emmanuel, 2026-10-21: the bank now feeds the OG-copy campaign 2157)."""
    return os.getenv("HOLD_ANGLE_FILL", "").strip().lower() in ("1", "true", "yes")


def needed() -> bool:
    if _held():
        return False
    s = _setup()
    if not s:
        return False
    _cfg, angles, campaign_of, targets = s
    have = counts(campaign_of)
    return any(have[a] < targets[a] for a in angles)


def sweep(angles: dict, campaign_of: dict, run_id: str) -> dict:
    pulled = {}
    for a, cid in campaign_of.items():
        bad, page = [], 1
        present: set[str] = set()
        complete = False
        while page < 1000:
            rows = (emailbison._get(f"/api/campaigns/{cid}/leads", {"page": page}) or {}).get("data") or []
            if not rows:
                complete = True
                break
            for lead in rows:
                present.add((lead.get("email") or "").lower())
                cv = {str(c.get("name")).lower(): c.get("value") for c in (lead.get("custom_variables") or [])}
                if not passes(angles[a], lead.get("first_name"), cv.get("personalization 1") or "",
                              cv.get("personalization 2") or ""):
                    bad.append(lead)
            page += 1
        if bad:
            emailbison.remove_leads(cid, [int(l["id"]) for l in bad if l.get("id")])
            with db.cursor() as cur:
                cur.executemany("""UPDATE contacts SET status='needs_review', eb_campaign_id=NULL,
                                   drop_reason='failed current copy rules on read-back' WHERE email=%s""",
                                [((l.get("email") or "").lower(),) for l in bad])
        ghosts = 0
        # A page that comes back empty mid-walk (rate limit, a slow answer) used to end the walk early, and every lead after that point looked
        # absent: 359 loaded leads were moved to "held" on 2026-10-16 while EmailBison still held them. Only trust the listing when it covers
        # (almost) every lead EmailBison says the campaign has.
        if complete:
            try:
                total = int((emailbison.get_campaign(cid) or {}).get("total_leads") or 0)
            except Exception:
                total = 0
            if total and len(present) < 0.98 * total:
                db.log(run_id, "wave1_fill", f"sweep {a}: lead listing incomplete ({len(present)} of {total}); ghost check skipped", level="warn")
                complete = False
        if complete:
            # The database must not claim a lead EmailBison does not hold (found 2026-10-09: 9 of 45 were recorded as loaded but were
            # never in the campaign). Put such contacts back so the fill counts the real campaign.
            with db.cursor() as cur:
                cur.execute("""UPDATE contacts SET status='needs_review', eb_campaign_id=NULL,
                               drop_reason='recorded as loaded but not present in the EmailBison campaign'
                               WHERE eb_campaign_id=%s AND status='pushed' AND NOT (lower(email) = ANY(%s))""",
                            (cid, list(present) or [""]))
                ghosts = cur.rowcount
        pulled[a] = len(bad) + ghosts
        db.log(run_id, "wave1_fill", f"sweep {a}: pulled {len(bad)}, {ghosts} recorded-but-absent put back")
    return pulled


STAMP = "previously targeted (tier 2)"


def _unstamp(domains: list[str]) -> None:
    """Tier-2 companies were once waved through as fit without a look (a chocolate maker got
    MMM copy, 2026-09-27). Clear that stamp so the real check judges them."""
    from . import company_fit
    company_fit.ensure_columns()
    with db.cursor() as cur:
        cur.execute("UPDATE company_funnel SET fit=NULL, fit_reason=NULL WHERE domain = ANY(%s) AND fit_reason=%s",
                    (domains, STAMP))


def fit_check_unjudged_reuse_companies(recontact_days: int, run_id: str, batch_size: int = 40) -> int:
    """Troy, 2026-10-09: "the fit gate should run when we need new contacts... go to the
    database tier 2, run fit test on x companies, take the winners." The reuse-eligible
    history pool (tier != 1, genuinely past the recontact window) has a large fraction
    sitting at fit=NULL -- never run through the gate at all, so they're invisible to the
    people-query below even though many would pass. This judges a bounded batch each
    tick (same company_fit.check() the rest of this file already uses, same cost
    profile) so that pool keeps opening up over time instead of sitting unjudged forever.
    """
    if not _reuse_fit_check():
        return 0
    from . import company_fit
    company_fit.ensure_columns()
    with db.cursor() as cur:
        # Troy, 2026-10-09: "fit check starts from unchecked in tier 2 straight down
        # since tier 1 has been checked" -- tier 1 goes through a separate gate
        # (staged_companies.verdict, used by pick_companies for fresh discovery) and is
        # deliberately excluded above; among the tiers THIS gate does cover, work the
        # closest-to-eligible first (tier 2, the oldest/most-settled previously-contacted
        # group) and only reach into tier 5 once the nearer tiers are actually exhausted.
        # A history-only company has no name in the funnel, and a dead site then has nothing to search by. Take the name its
        # leads carry in EmailBison (found 2026-10-09: cybertech-institute.com, really CyberTex Institute, was rejected
        # because its old domain is dead and the name search ran on the domain itself).
        cur.execute("""SELECT f.domain, coalesce(f.name, (SELECT max(h2.company) FROM history_leads h2
                                                          WHERE h2.company_domain = f.domain)) AS name, f.tier FROM company_funnel f
                       WHERE f.tier != 1 AND f.fit IS NULL AND NOT coalesce(f.replied, false)   -- replied companies are never contacted, so never paid for
                         AND coalesce(f.days_since_contact, 0) >= %s
                         AND EXISTS (
                             SELECT 1 FROM history_leads h
                             WHERE h.company_domain = f.domain AND coalesce(h.first_name, '') <> ''
                               AND NOT EXISTS (SELECT 1 FROM history_replies r
                                               WHERE r.email = h.email AND r.bounced_at IS NOT NULL))
                       ORDER BY f.tier ASC, f.domain
                       LIMIT %s""", (recontact_days, batch_size))
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return 0
    v = company_fit.check([{"domain": r["domain"], "name": r["name"]} for r in rows], workers=FIT_WORKERS)
    won = sum(1 for x in v.values() if x.get("fit") in company_fit.PASS)
    db.log(run_id, "wave1_fill", f"reuse fit-check: {len(rows)} companies judged, {won} passed")
    return won


def mx_filter(batch: list[dict], run_id: str) -> list[dict]:
    """TFP hard rule: drop contacts behind Mimecast or Barracuda (by MX record) BEFORE any copy is written for them.
    Uses the engine's own gateway check (app/mx.py, BLOCKED_MAIL_GATEWAYS, cached 30 days). Contacts found through stage 4 were already checked
    at company entry; this covers people who arrive another way (the reuse queue, saved copy, the bank)."""
    from . import client, mx
    if not client.profile().mx_blocklist or not batch:
        return batch
    verdict: dict[str, str | None] = {}

    def gate(c: dict) -> str | None:
        dom = ((c.get("email") or "").split("@")[-1] or c.get("company_domain") or "").lower()
        if dom not in verdict:
            try:
                verdict[dom] = mx.blocked(dom)
            except Exception:
                verdict[dom] = None
        return verdict[dom]

    allowed, blocked = [], []
    for c in batch:
        (blocked if gate(c) else allowed).append(c)
    if blocked:
        with db.cursor() as cur:
            cur.executemany("UPDATE contacts SET status='needs_review', eb_campaign_id=NULL, drop_reason=%s WHERE email=%s",
                            [("behind Mimecast or Barracuda (MX check, hard TFP rule)", c["email"]) for c in blocked])
        db.log(run_id, "wave1_fill", f"MX blocklist: {len(blocked)} of {len(batch)} contacts are behind Mimecast or Barracuda and were dropped")
    return allowed


def fit_filter(batch: list[dict], run_id: str) -> list[dict]:
    """Keep only contacts whose company passes the website fit check."""
    if not _reuse_fit_check():
        return batch
    from . import company_fit
    doms = sorted({c["company_domain"] for c in batch})
    _unstamp(doms)
    names = {c["company_domain"]: c.get("company_name") or "" for c in batch}
    v = company_fit.check([{"domain": d, "name": names[d]} for d in doms], workers=FIT_WORKERS)
    keep = [c for c in batch if (v.get(c["company_domain"]) or {}).get("fit") in company_fit.PASS]
    db.log(run_id, "wave1_fill", f"fit check: {len(keep)} of {len(batch)} contacts at passing companies")
    return keep


def fit_sweep(campaign_of: dict, run_id: str) -> int:
    """Re-judge loaded tier-2 companies that were stamped fit without a check; pull failures."""
    if not _reuse_fit_check():
        return 0
    from . import company_fit
    company_fit.ensure_columns()
    with db.cursor() as cur:
        cur.execute("""SELECT DISTINCT c.company_domain d, c.company_name n FROM contacts c
                       JOIN company_funnel f ON f.domain = c.company_domain
                       WHERE c.eb_campaign_id = ANY(%s) AND (f.fit_reason = %s OR f.fit IS NULL)""",
                    (list(campaign_of.values()), STAMP))
        rows = cur.fetchall()
    if not rows:
        return 0
    _unstamp([r["d"] for r in rows])
    v = company_fit.check([{"domain": r["d"], "name": r["n"]} for r in rows])
    bad = [d for d, x in v.items() if x.get("fit") not in company_fit.PASS and x.get("fit") is not None]
    if not bad:
        return 0
    with db.cursor() as cur:
        cur.execute("""SELECT lower(email) email, eb_campaign_id FROM contacts
                       WHERE company_domain = ANY(%s) AND eb_campaign_id = ANY(%s)""",
                    (bad, list(campaign_of.values())))
        leads = [dict(r) for r in cur.fetchall()]
    by_c = defaultdict(set)
    for l in leads:
        by_c[l["eb_campaign_id"]].add(l["email"])
    for cid, emails in by_c.items():
        ids, page = [], 1
        while page < 1000:
            got = (emailbison._get(f"/api/campaigns/{cid}/leads", {"page": page}) or {}).get("data") or []
            if not got:
                break
            ids += [int(r["id"]) for r in got if (r.get("email") or "").lower() in emails and r.get("id")]
            page += 1
        if ids:
            emailbison.remove_leads(cid, ids)
    with db.cursor() as cur:
        cur.execute("""UPDATE contacts SET status='needs_review', eb_campaign_id=NULL,
                       drop_reason='company failed fit check' WHERE company_domain = ANY(%s)
                       AND eb_campaign_id = ANY(%s)""", (bad, list(campaign_of.values())))
    db.log(run_id, "wave1_fill", f"fit sweep: {len(rows)} companies judged, {len(bad)} failed, {len(leads)} leads pulled")
    return len(leads)


def assign_senders(campaign_of: dict, leads: dict, run_id: str | None = None, dry_run: bool = False) -> dict:
    """Deal the workspace's FREE sender accounts across the Wave 1 campaigns.

    senders.align() cannot be used here: it counts only active/paused campaigns, so while
    the three campaigns are drafts it sees one campaign's accounts as free for the next and
    attaches the same accounts to all three -- and EmailBison has no detach route. This
    gives every free account to exactly one campaign, lowest-capacity campaign first, and
    never attaches an account that is on a live campaign. Safe to call again: it only tops up.
    """
    roster = emailbison.list_sender_emails(limit=10000)
    by_id = {int(s["id"]): s for s in roster if s.get("id") is not None}
    counts = senders.active_campaign_counts()
    on = {a: {int(s["id"]) for s in emailbison.campaign_sender_emails(cid) if s.get("id") is not None}
          for a, cid in campaign_of.items()}
    owner: dict[int, str] = {}
    for a in campaign_of:                       # an account on two of them counts for the first only
        for i in on[a]:
            owner.setdefault(i, a)
    daily = lambda i: int((by_id.get(i) or {}).get("daily_limit") or 0)
    have = {a: sum(daily(i) for i in on[a] if owner[i] == a and counts.get(i, 0) == 0) for a in campaign_of}
    pool = sorted((s for i, s in by_id.items()
                   if i not in owner and senders.sendable(s) and counts.get(i, 0) == 0 and daily(i) > 0),
                  key=lambda s: (-int(s["daily_limit"]), int(s["id"])))
    want = {a: senders.target_for(int(leads.get(a) or 0)) for a in campaign_of}
    picks: dict[str, list[int]] = {a: [] for a in campaign_of}
    for s in pool:
        open_ = [a for a in campaign_of if have[a] < want[a]]
        if not open_:
            break
        a = min(open_, key=lambda x: have[x])
        picks[a].append(int(s["id"]))
        have[a] += int(s["daily_limit"])
    if not dry_run:
        for a, ids in picks.items():
            for i in range(0, len(ids), 50):
                emailbison.attach_sender_emails(campaign_of[a], ids[i:i + 50])
    out = {a: {"accounts": len(on[a]) + len(picks[a]), "attached": len(picks[a]), "daily": have[a],
               "target": want[a]} for a in campaign_of}
    left = sum(daily(int(s["id"])) for s in pool) - sum(daily(i) for ids in picks.values() for i in ids)
    db.log(run_id, "wave1_fill", f"senders{' (dry run)' if dry_run else ''}: {out}; free pool was {len(pool)} "
                                 f"accounts, {left}/day left unassigned")
    return out


def fill(post: bool = True) -> dict:
    """One resumable pass. Returns a summary; safe to call as often as you like."""
    from .stages import s5_copy
    from .stages.s4_people import score_title
    from .clients.quickenrich import Contact

    if _held():
        return {"status": "held"}
    s = _setup()
    if not s:
        return {"status": "no_test"}
    cfg, angles, campaign_of, targets = s
    angle_of_variant = {v: a for a, v in angles.items()}
    run_id = "wave1-fill-" + time.strftime("%Y%m%dT%H%M%S")
    if not _lock(run_id):
        return {"status": "already_running"}
    db.start_run(run_id)
    try:
        from . import modes
        from .clients import slack
        beat_stop = modes.heartbeat(run_id)
        start = counts(campaign_of)
        # Troy, 2026-10-06: "even if the system goes off by morning" -- a fill used to
        # post to Slack only at the end, and only if something changed. A run that stalls
        # for hours (a vendor outage, a slow batch) left total silence, indistinguishable
        # from nothing having started at all. One quiet line up front fixes that.
        if post:
            try:
                slack.post(f":hourglass_flowing_sand: {_name()} copy-test fill starting "
                           f"(targets {targets}): {start}")
            except Exception:
                pass
        pulled = sweep(angles, campaign_of, run_id)
        pulled["fit"] = fit_sweep(campaign_of, run_id)
        _beat(run_id)

        from . import client
        recontact_days = client.profile().recontact_days
        # Troy, 2026-10-09: 40/tick was too slow -- settled on 100/tick ("100 per hour"),
        # which on this job's real ~2-hour cadence averages ~50/hour, ~1,200/day. At
        # ~$0.0045/company (one Sonnet call each) that's ~$5.40/day, clearing the real
        # 14,986-company backlog in under 2 weeks and staying cheap once caught up.
        fit_check_unjudged_reuse_companies(recontact_days, run_id, batch_size=100)
        _beat(run_id)

        def load_for(a: str, rows: list[dict]) -> int:
            room = targets[a] - counts(campaign_of)[a]
            rows = rows[:max(room, 0)]
            return len(scaling.load_leads(campaign_of[a], rows, run_id)) if rows else 0

        # saved copy that still passes (angle read off the copy if it was not stored)
        with db.cursor() as cur:
            cur.execute("""SELECT email, first_name, last_name, title, company_domain, company_name, vertical,
                                  personalization_1, personalization_2, research_report, copy_variant,
                                  experiment_id, prompt_version
                           FROM contacts WHERE eb_campaign_id IS NULL AND status IN ('draft','verified')
                             AND coalesce(personalization_1,'') <> '' AND coalesce(personalization_2,'') <> ''
                             AND updated_at > now() - interval '3 days'""")
            saved = [dict(r) for r in cur.fetchall()]
        by_angle = defaultdict(list)
        for r in saved:
            if r["copy_variant"] not in angle_of_variant:
                # ProductEVO classified unlabeled saved copy by its wording. Another client's copy has no such
                # signature, so an unlabeled row is left alone rather than put under the wrong angle.
                if not _guess_unlabeled():
                    continue
                t = (r["personalization_1"] or "").lower()
                r["copy_variant"] = ("mmm" if "would also buy" in t else
                                     "a-sleep-test" if ("second source" in t or "tariff" in t) else "time-back")
            if passes(r["copy_variant"], r["first_name"], r["personalization_1"], r["personalization_2"]):
                by_angle[angle_of_variant[r["copy_variant"]]].append(r)
        for a, rows in by_angle.items():
            load_for(a, mx_filter(fit_filter(rows, run_id), run_id))

        # tier-2 queue: people EmailBison already holds, one angle per company
        # never-contacted first (Troy's order): tier-1 contacts already found and verified,
        # never written. They are already in contacts; they keep their sourced vertical.
        with db.cursor() as cur:
            cur.execute("""SELECT c.email, c.first_name, c.last_name, c.title, c.company_domain, c.company_name,
                                  c.vertical
                           FROM contacts c LEFT JOIN company_funnel f ON f.domain = c.company_domain
                           -- label contacts_found_not_sent: funnel_dedupe re-labels a sourced company that already has contacts and history_backfill2 sets it to tier 9; those are OUR bank, not suppressed (found 2026-10-16, 597 contacts were stranded)
                           -- f.domain IS NULL: contacts found for newly staged schools, which have no tier row yet (261 sat unused 2026-10-11)
                           -- staged: a contact at a company OUR sourcing found is ours even after the company turns "previously contacted" because
                           -- a sibling was loaded and synced; those siblings were stranded and stopped the scale-up (found 2026-10-19, 107 contacts)
                           WHERE (f.tier = 1 OR f.domain IS NULL OR f.label = 'contacts_found_not_sent' OR c.source = 'reuse_tier2'
                                  OR EXISTS (SELECT 1 FROM staged_companies s WHERE s.domain = c.company_domain))      -- + leads a stopped batch left behind
                             AND NOT coalesce(f.replied, false) AND c.status = 'verified'
                             AND c.eb_campaign_id IS NULL AND coalesce(c.first_name, '') <> ''
                             AND coalesce(c.personalization_1, '') = ''
                             AND coalesce(f.fit, '') NOT IN ('LOW', 'NOT_FIT')""")
            tier1 = [dict(r) for r in cur.fetchall()]
        # A batch is written into contacts BEFORE its copy is generated, so a batch that stops
        # part-way (provider out of credit, restart) leaves leads that the tier-2 query then
        # skips as "already in contacts". 2,632 were stranded that way on 2026-09-27. They are
        # picked up here, companies that already passed the fit check first.
        t1_by_dom = defaultdict(list)
        for r in tier1:
            t1_by_dom[r["company_domain"]].append(r)
        queue = defaultdict(list)
        with db.cursor() as cur:
            cur.execute("SELECT domain FROM company_funnel WHERE domain = ANY(%s) AND fit IN ('HIGH','MEDIUM') "
                        "AND coalesce(fit_reason,'') <> %s", (list(t1_by_dom), STAMP))
            judged_ok = {r["domain"] for r in cur.fetchall()}
        for dom in sorted(t1_by_dom, key=lambda d: (d not in judged_ok, hashlib.sha256(d.encode()).hexdigest())):
            a = list(angles)[int(hashlib.sha256(f"wave1:{dom}".encode()).hexdigest(), 16) % len(angles)]
            for r in t1_by_dom[dom][:_seats()]:
                queue[a].append({**r, "copy_variant": angles[a]})
        db.log(run_id, "wave1_fill", f"never-contacted queued first: {len(tier1)} contacts at {len(t1_by_dom)} companies")

        with db.cursor() as cur:
            # Troy, 2026-10-09: was hardcoded to tier=2 specifically -- tier is a static
            # label assigned whenever a company was last snapshotted, not a live value,
            # and tier 3 (103-119 days since contact, confirmed by real data) is JUST as
            # safely past the recontact window as tier 2 (128-577 days) but was never
            # included. Filtering on the real days_since_contact field directly is both
            # correct today and self-widening: tiers 4/5 (62-89 and 3-54 days -- genuinely
            # NOT yet past the window) join automatically as they age past it, with no
            # further code change and no risk of recontacting someone too soon.
            # Emmanuel, 2026-10-11: "the only criteria is we've not reached out to them in the past 60 days." Each PERSON's own last-touch date
            # decides (EmailBison campaign date), not the company's: the company clock hid 2,555 people whose coworker was emailed recently.
            # Fit checks are off for these (he said so) and there is no title filter; titles only rank who gets the 5 seats. Kept on
            # purpose: company-level reply suppression, bounced people, 'Company team' placeholder first names, people already loaded,
            # and personal-email addresses (their domain is gmail.com, so research would read the wrong site).
            cur.execute("""SELECT DISTINCT ON (h.email) h.email, h.first_name, h.last_name, h.title,
                                  h.company, h.company_domain
                           FROM history_leads h
                           JOIN (SELECT lower(email) e, max(campaign_date) d FROM recent_contacts GROUP BY 1) lc ON lc.e = lower(h.email)
                           WHERE lc.d < current_date - %s::int
                             AND coalesce(h.first_name, '') <> '' AND h.first_name NOT ILIKE '%% team'
                             AND coalesce(h.company_domain, '') <> '' AND NOT (h.company_domain = ANY(%s))
                             AND NOT EXISTS (SELECT 1 FROM contacts c WHERE lower(c.email) = lower(h.email))
                             AND NOT EXISTS (SELECT 1 FROM history_replies r WHERE lower(r.email) = lower(h.email)
                                             AND (r.replied_at IS NOT NULL OR r.bounced_at IS NOT NULL))
                             AND NOT EXISTS (SELECT 1 FROM history_replies r2 WHERE r2.company_domain = h.company_domain
                                             AND r2.replied_at IS NOT NULL)
                             AND NOT EXISTS (SELECT 1 FROM company_funnel f WHERE f.domain = h.company_domain AND coalesce(f.replied, false))""",
                        (recontact_days, list(PERSONAL_DOMAINS)))
            people = [dict(r) for r in cur.fetchall()]
        by_dom = defaultdict(list)
        for p in people:
            by_dom[p["company_domain"]].append(p)
        for dom in sorted(by_dom, key=lambda d: hashlib.sha256(d.encode()).hexdigest()):
            cs = [Contact(first_name=p["first_name"], last_name=p["last_name"] or "", email=p["email"],
                          title=p["title"], linkedin_url=None, company=p["company"], company_domain=dom)
                  for p in by_dom[dom]]
            chosen = sorted(cs, key=lambda c: -score_title(c.title, None))[:_seats()]   # best titles get the seats; none are dropped
            if not chosen:
                continue
            a = list(angles)[int(hashlib.sha256(f"wave1:{dom}".encode()).hexdigest(), 16) % len(angles)]
            for c in chosen:
                queue[a].append({"email": c.email, "first_name": c.first_name, "last_name": c.last_name,
                                 "title": c.title, "company_domain": dom, "company_name": c.company,
                                 "vertical": TIER2_VERTICAL, "copy_variant": angles[a]})

        paused_note = ""
        while True:
            _beat(run_id)
            have = counts(campaign_of)
            open_a = [a for a in angles if have[a] < targets[a] and queue[a]]
            if not open_a:
                break
            # The OpenRouter account is SHARED with other clients, and copy falls back to it when Concentrate answers 402. A big fill
            # must not drain it (Emmanuel, 2026-10-09: balance was $44 against a fill that could cost about $40).
            enough, why = _llm_ready()
            if not enough:
                paused_note = f"Paused: {why}. The next 2-hourly tick continues."
                db.log(run_id, "wave1_fill", paused_note)
                break
            batch = []
            for a in open_a:
                n = min(int((targets[a] - have[a]) * 1.15) + 3, BATCH // len(open_a))
                batch += queue[a][:n]
                queue[a] = queue[a][n:]
            batch = fit_filter(batch, run_id)
            batch = mx_filter(batch, run_id)
            if not batch:
                continue
            # Emmanuel, 2026-10-10: "scrap the reverifier so leads can just flow in." These people were already emailed from this
            # workspace, and EmailBison itself refuses bounced or unsubscribed leads at load time. Re-verifying cost minutes per batch and
            # dropped a third of them (TryKitt is out of credit, so every borderline address came back inconclusive). Off by default;
            # REVERIFY_EMAILS=true restores the Troy 2026-10-09 waterfall check. email_verified_at stays NULL when skipped, so no
            # record claims a check that did not run.
            reverify = os.getenv("REVERIFY_EMAILS", "false").lower() in ("1", "true", "yes")
            if reverify:
                from .clients import waterfall
                verified = waterfall.verify_batch(batch, run_id)
                before_n = len(batch)
                batch = [c for c in verified if waterfall.sendable(c)]
                db.log(run_id, "wave1_fill", f"email re-verify: {len(batch)} of {before_n} confirmed sendable")
            if not batch:
                continue
            with db.cursor() as cur:
                cur.executemany("""INSERT INTO contacts (email, first_name, last_name, title, company_domain,
                                   company_name, vertical, run_id, status, source, email_verified_at)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'verified','reuse_tier2', CASE WHEN %s THEN now() END)
                                   ON CONFLICT (email) DO NOTHING""",
                                [(c["email"], c["first_name"], c["last_name"], c["title"], c["company_domain"],
                                  c["company_name"], c["vertical"], run_id, reverify) for c in batch])
            ready, _ = s5_copy.run(batch, run_id)
            per = defaultdict(list)
            for r in ready:
                per[angle_of_variant[r["copy_variant"]]].append(r)
            added = sum(load_for(a, rows) for a, rows in per.items())
            db.log(run_id, "wave1_fill", f"batch of {len(batch)}: +{added} loaded, now {counts(campaign_of)}")
            if not added and not ready:
                break

        end = counts(campaign_of)
        if settings.skip_sender_attach:
            # EmailBison has no detach route, so an account attached here stays. Lumion's sender plan is not decided yet,
            # so the fill never attaches (SKIP_SENDER_ATTACH=true). The same flag already gates senders.align elsewhere.
            sender_notes = ["not attached (SKIP_SENDER_ATTACH is on): attach senders by hand in EmailBison"]
        else:
            try:
                sres = assign_senders(campaign_of, end, run_id)
                sender_notes = [f"{a}: {r['daily']}/day on {r['accounts']} accounts" for a, r in sres.items()]
            except Exception as exc:
                sender_notes = [f"sender step failed: {str(exc)[:120]}"]
        done = all(end[a] >= targets[a] for a in angles)
        if post and (end != start or any(pulled.values())):
            lines = [f"*{_name()} copy-test fill* ({'complete' if done else 'progress'}, targets {targets})"]
            for a, cid in campaign_of.items():
                lines.append(f"• *{a}* (campaign {cid}): {end[a]} leads"
                             + (f", {pulled[a]} pulled for breaking current rules" if pulled.get(a) else ""))
            if pulled.get("fit"):
                lines.append(f"• {pulled['fit']} leads pulled because their company failed the fit check")
            lines.append("Senders: " + "; ".join(sender_notes))
            lines.append("All drafts. Nothing sends until you press start in EmailBison."
                         + ("" if done else " The 2-hourly job continues this automatically."))
            if paused_note:
                lines.append(":warning: " + paused_note)
            slack.post("\n".join(lines))
        db.finish_run(run_id, "complete", str(end))
        return {"status": "complete" if done else "partial", "counts": end, "pulled": pulled}
    except Exception as exc:
        db.finish_run(run_id, "failed", str(exc)[:200])
        if post:
            try:
                slack.post(f":warning: {_name()} copy-test fill paused: {str(exc)[:250]}. "
                           f"Loaded so far {counts(campaign_of)}; the 2-hourly job retries automatically.")
            except Exception:
                pass
        raise
    finally:
        try:
            beat_stop.set()
        except NameError:
            pass
        _release(run_id)
