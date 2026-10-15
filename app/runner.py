"""Orchestrator + CLI.

  python -m app.runner preflight            check env, DB, and vendor reachability
  python -m app.runner initdb               create tables
  python -m app.runner run [--dry-run]      the weekly pipeline, stages 1-6
  python -m app.runner push <batch_id> [--live] [--campaign N]
  python -m app.runner status               pending batches + last run
  python -m app.runner expire               auto-kill batches past their window

The weekly cron calls `run`. It stops at Stage 6 by design: the approval gate is the
end of the automated path, and pushing is always a separate, deliberate command.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import math
import threading
import sys
from concurrent.futures import ThreadPoolExecutor
import traceback
from datetime import datetime, timezone

from . import db, prompts
from .clients import emailbison
from .config import settings
from .clients.http import VendorOutage
from .clients import slack
from .stages import (
    s1_performance,
    s2_expansion,
    s2b_freshness,
    s3_companies,
    s4_people,
    s5_copy,
    s6_approval,
    s7_push,
    s8_review,
    stop_on_reply,
)


# A copy test writes this many per arm before stopping, unless told otherwise.
DEFAULT_SAMPLE = int(os.getenv("DEFAULT_SAMPLE", "20"))


def _run_id() -> str:
    return f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"


def _intake_size(run_id: str | None = None, ignore_seat_ceiling: bool = False) -> int:
    """How many NEW verticals this week's run should take on: exactly as many TEST
    SEATS are empty, never a flat guess regardless of what else is running.

    Troy, 2026-09-22's "2 a week, 3 if under 5 campaigns" and the flat-3 version before
    it both gated on the wrong thing: a campaign only ever left the count by being
    DROPPED, and almost nothing drops (median vertical runs 8.0% against a 6% bar), so
    the step-up condition could never actually fire and a flat number launched into
    whatever the workspace already looked like -- 18 active campaigns sharing a pool
    built for 6 on 2026-10-05, every one of them crawling.
    A test seat is a reserved, modest allocation (sender_min_daily) held by ONE
    vertical from the moment it launches until its FIRST verdict, scale or drop either
    way -- then the seat is free again (db.occupied_test_seats()). Winners never hold a
    seat once they graduate, so a crowded winners pool can no longer starve new
    testing, and new testing can no longer starve winners: each draws from its own
    side of the line. See app/stages/s7_push.py for the matching modest allocation a
    brand-new campaign actually launches with (sender_min_daily, not the full
    lead-scaled target) and the launch gate checking DEDICATED capacity, not the
    diluted shared figure.

    ignore_seat_ceiling: Troy, 2026-10-08, on the midweek catch-up: "if we're slacking
    wednesday should launch regardless." The seat ceiling is a steady-state PREFERENCE
    (don't overcrowd judging) -- it is not the thing that protects winners from
    dilution; the dedicated-capacity check below is, and that stays in force either
    way. A week already behind its 15,000 quota is a more urgent signal than "don't
    start a 4th thing while 3 are still being judged," so midweek_intake_if_short()
    passes True here and sizes against verticals_per_run_max instead of open seats.
    Monday's regular call leaves this False.
    """
    occupied = db.occupied_test_seats()
    open_seats = max(settings.test_seats - occupied, 0)
    if ignore_seat_ceiling:
        desired = max(settings.verticals_per_run_max, settings.min_weekly_launches)
    else:
        # THE FLOOR. Troy, 2026-10-07: "ideally i would like some launches every week
        # regardless of the seat. some new activity is always good." A vertical still
        # under 7 days old has not failed anything -- it just has not had time to earn
        # a verdict -- and a run of young launches could otherwise leave every seat
        # occupied for a week with nothing new started. min_weekly_launches guarantees
        # at least that many get a shot even then; it raises what is DESIRED, it never
        # bypasses the capacity check below, which is the actual thing that protects
        # winners from dilution.
        desired = max(open_seats, settings.min_weekly_launches)
    if desired == 0:
        db.log(run_id, "stage2b", "intake 0: no open seats and the weekly floor is 0")
        return 0
    # A real check against DEDICATED (idle) capacity, not a step-up gate: seats (and the
    # floor) decide how many verticals SHOULD be tested, this decides how many can
    # actually afford to launch right now without sitting HELD at the push gate.
    try:
        from . import senders
        dedicated = senders.free_capacity()["dedicated"]
    except Exception as exc:
        db.log(run_id, "stage2b",
               f"intake: capacity probe failed ({exc}); using the {desired} desired "
               f"launch(es) as-is", level="warn")
        return desired
    affordable = dedicated // settings.sender_min_daily
    if affordable < desired:
        db.log(run_id, "stage2b",
               f"intake {affordable}: wanted {desired} (seats open={open_seats}, "
               f"floor={settings.min_weekly_launches}) but only {dedicated}/day "
               f"dedicated free, {settings.sender_min_daily}/day needed per launch")
        return max(affordable, 0)
    if ignore_seat_ceiling and desired > open_seats:
        db.log(run_id, "stage2b",
               f"intake {desired}: the week is behind pace, so the seat ceiling is "
               f"skipped ({occupied} of {settings.test_seats} normally would cap this "
               f"at {open_seats}) -- {dedicated}/day dedicated covers {desired}")
    elif open_seats < settings.min_weekly_launches:
        db.log(run_id, "stage2b",
               f"intake {desired}: only {open_seats} test seat(s) open ({occupied} of "
               f"{settings.test_seats} occupied) but the {settings.min_weekly_launches}"
               f"/week floor still gets a launch -- {dedicated}/day dedicated covers it")
    else:
        db.log(run_id, "stage2b",
               f"intake {desired}: {occupied} of {settings.test_seats} test seat(s) "
               f"occupied, {dedicated}/day dedicated free covers the rest")
    return desired


def sync_bison_leads(run_id: str | None = None) -> list[dict]:
    """Stamp every active campaign's REAL current lead count from EmailBison itself,
    once a day.

    Troy, 2026-10-08: "the live page should be synced daily with bison, the numbers
    are not tallying with how many leads are in bison." pushed_lead_count is our own
    push history -- it only ever grows, and never reflects what EmailBison later
    removes on its own: Venture Capital alone showed 1,810 stopped leads (sender
    pulled, domain blacklisted, manual stop) that none of our bookkeeping knew about,
    and it only ever gets WIDER as a campaign ages, not narrower. This does not try to
    reconcile or correct pushed_lead_count -- that number still means what it always
    meant, and least-scaled-first ordering still reads it. bison_total_leads is a
    second, honest number alongside it: what EmailBison says is actually there right
    now, refreshed daily, so a human looking at the dashboard sees both instead of
    trusting a number that only ever moves one direction.
    """
    from .clients import emailbison
    with db.cursor() as cur:
        cur.execute("SELECT campaign_id, vertical FROM ve_campaigns ORDER BY campaign_id")
        camps = [dict(r) for r in cur.fetchall()]
    out = []
    for c in camps:
        try:
            live = emailbison.get_campaign(c["campaign_id"]) or {}
            total = int(live.get("total_leads") or 0)
            db.record_bison_sync(c["campaign_id"], total)
            out.append({"campaign_id": c["campaign_id"], "vertical": c["vertical"],
                       "bison_total_leads": total})
        except Exception as exc:
            db.log(run_id, "stage8",
                   f"bison lead sync failed for campaign {c['campaign_id']}: {exc}",
                   level="warn")
    return out


def preflight(verbose: bool = True) -> bool:
    """Fail loudly before spending anything. Never prints a secret value."""
    ok = True
    info = settings.describe()
    if verbose:
        print("workspace:", info["workspace"])
        print("winner gate:", info["winner_gate"])
        print("pipeline_enabled:", info["pipeline_enabled"])
        for name, state in info["secrets"].items():
            print(f"  {name:42} {state}")

    missing = settings.missing_for_live_run()
    if missing:
        print(f"\nMISSING required for a live run: {missing}")
        ok = False

    try:
        version = db.ping().split(",")[0]
        print(f"postgres OK: {version}")
    except Exception as exc:
        print(f"postgres FAILED: {exc}")
        ok = False

    try:
        for loader in (prompts.research, prompts.email1, prompts.email2):
            p = loader()
            print(f"prompt OK: {p.name} ({p.model}, {len(p.system)} chars)")
    except Exception as exc:
        print(f"prompts FAILED: {exc}")
        ok = False

    try:
        workspaces = emailbison.list_workspaces()
        names = [f"{w.get('name')} (id={w.get('id')})" for w in workspaces]
        print(f"emailbison OK: {names}")
        if not any(w.get("id") == settings.workspace_id for w in workspaces):
            print(f"  WARNING: configured WORKSPACE_ID={settings.workspace_id} not in that list")
            ok = False
    except Exception as exc:
        print(f"emailbison FAILED: {exc}")
        ok = False

    print("\nPREFLIGHT", "PASS" if ok else "FAIL")
    return ok


def copy_intake(room: int, pass_rate: float, headroom: float = 0.25) -> int:
    """How many contacts to put INTO copy to get `room` sendable OUT.

    room / pass_rate, capped at 25% above room so a wrongly low rate cannot double
    the spend. Pure, so the arithmetic is tested.
    """
    if room <= 0:
        return 0
    rate = max(0.6, min(1.0, pass_rate or 1.0))
    return int(min(math.ceil(room / rate), math.floor(room * (1 + headroom))))


def _work_vertical(vertical, run_id: str, dry_run: bool, summary: dict,
                   max_new: int | None = None) -> None:
    """Stages 3-6 for one vertical. Raises VendorOutage if any vendor is dry.

    max_new caps how many new contacts THIS call may add, on top of whatever is
    already banked. Troy, 2026-10-02: auto top-up's job is to nudge a thin vertical,
    not refill it to the cap in one pass -- see auto_topup_increment in config.py.
    Monday/midweek intake pass None and size to the full cap as before.
    """
    # One Slack message per vertical; everything after replies in its thread. Reuse the
    # thread if a resumed vertical already has one.
    thread = db.vertical_thread(vertical.name)
    if not thread:
        kept_now = len(db.staged(vertical.name, judged=True))
        thread = slack.vertical_started(vertical.name, kept_now, settings.target_contacts)
        db.set_vertical_thread(vertical.name, thread)
    # Resume before re-sourcing. A halted run leaves verified contacts behind
    # with no copy; going back through stages 3-4 would spend the same
    # verification credits again for the same people.
    # Cap what goes into copy against EVERYTHING the vertical already holds, not just
    # against the unwritten contacts. Troy, 2026-09-14: "we're writing copy for the
    # verticals even after they've passed 3k contacts written, why?" The cap was
    # applied to verified_uncopied alone, so Biotech with 1,900 contacts already in
    # its pending batch wrote copy for 3,000 more when it needed 1,100 -- about
    # $30 of copy per vertical for drafts no batch would carry. The room left is the
    # cap minus what the pending batch holds minus finished drafts no batch has
    # picked up yet; those banked drafts go into the batch FIRST, before any new copy
    # is written, and a vertical with no room writes nothing.
    pending = db.pending_batch_for(vertical.name)
    in_batch = int(pending["contact_count"]) if pending else 0
    banked_copy = db.orphan_drafts(vertical.name)
    room_total = max(settings.contact_cap_per_vertical - in_batch - len(banked_copy), 0)
    if max_new is not None and max_new < room_total:
        db.log(run_id, "stage5", f"{vertical.name}: auto top-up capped at {max_new} "
               f"this round (room for {room_total} total) -- a nudge, not a refill")
        room_total = max_new
    # Oversample for the copy QA that will fail. 3,000 in gave 2,986 out on Venture
    # Capital (2026-09-14) and nothing backfilled the gap; feeding room / pass_rate in
    # lands the cap OUT. The rate is the vertical's own measured copy_pass.
    pass_rate = db.copy_pass_rate(vertical.name)
    copy_target = copy_intake(room_total, pass_rate)
    if copy_target > room_total:
        db.log(run_id, "stage5", f"{vertical.name}: copy pass rate {pass_rate:.0%}, so "
               f"{copy_target} contacts go into copy for {room_total} sendable")
    leftover = db.verified_uncopied(vertical.name)[:copy_target]
    unfound = db.unfound_kept(vertical.name) if copy_target > len(leftover) else []
    short = len(leftover) < copy_target
    if room_total <= 0:
        db.log(run_id, "stage5", f"{vertical.name}: {in_batch} in the pending batch and "
               f"{len(banked_copy)} finished drafts banked already cover the "
               f"{settings.contact_cap_per_vertical} cap; no copy to write")
        contacts, companies = [], []
        stats3 = {"companies_kept": 0, "resumed": True}
        stats4 = {"contacts_found": 0}
        ready_banked, stats5a, pending_copy = [], {}, []
    elif leftover and unfound and short:
        room = max(copy_target - len(leftover), 0)
        db.log(run_id, "stage4", f"{vertical.name}: {len(leftover)} verified banked, "
               f"{len(unfound)} kept companies not yet searched; writing copy for the "
               f"banked ones WHILE the finders work")
        # Copy the already-banked contacts at the same time as the finders search for
        # more. Troy, 2026-09-14: "if we have contacts awaiting copy for the first 3
        # why not resume copy even at stage 4?" They were sequential, so 949 contacts
        # ready to write sat idle for half an hour while the finders ran. The two do
        # not compete: finders spend QuickEnrich, AI Ark and the verification
        # waterfall; copy spends the LLM. A VendorOutage in either still surfaces on
        # .result() and pauses the vertical with everything banked.
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_copy = pool.submit(s5_copy.run, leftover, run_id)
            fut_find = pool.submit(s4_people.build, unfound, run_id)
            ready_banked, stats5a = fut_copy.result()
            more, stats4 = fut_find.result()
        contacts = leftover + more[:room]
        companies = [{"domain": d, "name": None, "vertical": vertical.name}
                     for d in {c["company_domain"] for c in contacts}]
        stats3 = {"companies_kept": len(companies), "resumed": True}
        stats4 = {**stats4, "contacts_found": stats4.get("contacts_found", 0) + len(leftover)}
        pending_copy = more[:room]          # the banked ones are already written
    elif len(leftover) >= settings.resume_min_contacts:
        db.log(run_id, "stage4", f"{vertical.name}: resuming with "
               f"{len(leftover)} verified contacts left by an earlier run")
        contacts = leftover
        seen_c: dict[str, dict] = {}
        for c in contacts:
            seen_c.setdefault(c["company_domain"], {
                "domain": c["company_domain"], "name": c.get("company_name"),
                "vertical": vertical.name})
        companies = list(seen_c.values())
        stats3 = {"companies_kept": len(companies), "resumed": True}
        stats4 = {"contacts_found": len(contacts)}
        ready_banked, stats5a, pending_copy = [], {}, contacts
    else:
        companies, stats3 = s3_companies.find(vertical, run_id,
                                               target_contacts=copy_target)
        if not companies:
            db.log(run_id, "stage3", f"{vertical.name}: no companies survived",
                   level="warn")
            return

        from . import client as _cl
        if _cl.profile().research_fit_gate:
            from .stages import s5_copy as _s5
            companies = _s5.gate_companies(companies, vertical.name, run_id)
            if not companies:
                db.log(run_id, "stage3b", f"{vertical.name}: the research gate dropped every company", level="warn")
                return
        contacts, stats4 = s4_people.build(companies, run_id)
        contacts = contacts[:copy_target]
        if not contacts:
            db.log(run_id, "stage4", f"{vertical.name}: no contacts built",
                   level="warn")
            return
        # Persist what verification just paid for, before anything else can
        # fail. Status 'verified' blocks nothing and is promoted downstream.
        try:
            db.reserve_contacts(contacts, run_id, None, status="verified")
        except Exception as exc:
            db.log(run_id, "stage4", f"verified contacts not persisted: {exc}",
                   level="warn")
        ready_banked, stats5a, pending_copy = [], {}, contacts

    # Say so in Slack, on EVERY path into copy. The ping used to sit inside the
    # fresh-stage-4 branch only, so a vertical that resumed -- which all three did on
    # 2026-09-13 -- reached 3,000 contacts in silence. Troy: "why didn't I get a contact
    # update on slack".
    slack.contacts_found(vertical.name, len(companies), stats4, len(contacts),
                         surplus=sum(db.surplus_count(vertical.name).values()),
                         thread_ts=thread)

    if dry_run:
        # Stop before paid copy generation; report what would have been built.
        db.log(run_id, "stage5",
               f"{vertical.name}: DRY RUN, skipping copy for "
               f"{len(contacts)} contacts")
        summary["batches"].append({
            "vertical": vertical.name, "companies": len(companies),
            "contacts": len(contacts), "batch_id": None, **stats3, **stats4,
        })
        return

    ready_new, stats5 = s5_copy.run(pending_copy, run_id) if pending_copy else ([], {})
    # Banked drafts first: they were paid for in an earlier run and no batch has them.
    ready = banked_copy + ready_banked + ready_new
    known = {c.get("domain") for c in companies}
    for c in banked_copy:
        if c.get("company_domain") and c["company_domain"] not in known:
            known.add(c["company_domain"])
            companies.append({"domain": c["company_domain"], "name": c.get("company_name"),
                              "vertical": vertical.name})
    stats5 = {k: (stats5.get(k, 0) or 0) + (stats5a.get(k, 0) or 0)
              for k in set(stats5) | set(stats5a)}
    merged = {**stats3, **stats4, **stats5, "contacts_verified": len(contacts)}
    # Top up the batch this vertical already has awaiting approval, rather than
    # opening a second one. A second batch means a second EmailBison campaign for one
    # vertical, and the 90-day scoring reads per-campaign stats. Only when there is no
    # pending batch is a new one opened.
    # The cap limits how much copy is WRITTEN, not how much is sent. Troy, 2026-09-14:
    # send every contact copy was written for -- it is paid for, a dropped vertical
    # is dropped forever, and a scaled one would send to them next anyway.
    existing = db.pending_batch_for(vertical.name)
    if existing:
        added = db.add_to_batch(existing["batch_id"], run_id, ready, companies,
                                vertical.name)
        batch_id = existing["batch_id"]
        db.log(run_id, "stage6", f"{vertical.name}: added {added} contacts to the "
               f"pending batch {batch_id} (was {existing['contact_count']})")
        # Batch small top-ups into one notification instead of pinging Slack every
        # time -- Troy, 2026-09-25: "rather than having to push a couple hundred leads
        # everytime". Quiet in the log either way; only worth a Slack post once the
        # running total actually crosses the threshold.
        running_total = db.record_batch_addition(batch_id, added)
        if running_total >= settings.notify_added_threshold:
            slack.contacts_found(vertical.name, len(companies), merged, running_total,
                                 surplus=sum(db.surplus_count(vertical.name).values()),
                                 thread_ts=thread)
            db.reset_batch_notification(batch_id)
    else:
        batch_id = s6_approval.open_batch(run_id, vertical, companies, ready, merged)
    # AUTO-APPROVE A TOP-UP. Troy, 2026-09-30: "first launch of a vertical needs a
    # human; top-ups into an already-approved campaign auto-approve." The copy
    # strategy was approved when the vertical first launched; a top-up is the same
    # copy for more people, and making a human re-approve every 246-contact addition
    # was the single biggest thing standing between the pipeline and running itself.
    # The push then happens on the next two-hourly tick via push_held_batches.
    try:
        s6_approval.auto_approve_if_topup(batch_id, vertical.name, run_id)
    except Exception as exc:
        db.log(run_id, "stage6", f"{vertical.name}: auto-approve check failed: {exc}",
               level="warn")
    try:
        db.record_yield(vertical.name, run_id, merged)
    except Exception as exc:
        db.log(run_id, "stage6", f"yield not recorded: {exc}", level="warn")
    db.record_vertical(vertical.name, run_id)
    db.finish_backlog(vertical.name)
    # The companies this batch used are consumed; everything else the gate kept stays
    # banked as surplus, vertical-fit and IDPR-fit alike, for a top-up if the campaign
    # performs. Unjudged staging is dropped.
    used = {c.get("company_domain") for c in ready if c.get("company_domain")}
    db.mark_consumed(vertical.name, used)
    db.clear_staged(vertical.name)
    surplus = db.surplus_count(vertical.name)
    db.log(run_id, "stage6", f"{vertical.name}: {sum(surplus.values())} surplus companies "
           f"banked for top-up", **surplus)
    summary["batches"].append({
        "vertical": vertical.name, "batch_id": batch_id,
        "companies": len(companies), "contacts": len(ready), **merged,
    })


def seed_declared_backlog(run_id: str, only_new: bool = True, names: list[str] | None = None) -> list[dict]:
    """Probe the client's DECLARED verticals through the waterfall, queue the viable ones, best supply first.

    This replaces "find a winner, then expand" for a client with declared_first set (ProQ, MasterNow): the
    declared list is the starting point, ranked by projected usable companies (fresh x fit), and the normal
    intake then claims from the top. Verticals that already ran are left alone.
    """
    from . import client as _cl, modes as _modes
    from .stages import s2c_waterfall_probe as s2c_probe
    prof = _cl.profile()
    done = {x.lower() for x in db.verticals_already_run()}
    items: list[dict] = []
    configs: dict[str, dict] = {}
    for name in (names or list(prof.verticals)):
        if name.lower() in done:
            continue
        if only_new and db.backlog_config(name):
            continue
        v = _modes.vertical_from_description(name, run_id, prof)
        v.name = name
        item = s2c_probe.probe(v, run_id)
        items.append(item)
        configs[name] = v.as_dict()
        db.log(run_id, "stage2c", f"probed {name}: {item['verdict']} | universe {item['universe']}"
               f"{'+' if item['capped'] else ''}, fresh {item['fresh']}, keep {item['keep_rate']}, "
               f"projected usable {item['projected']}")
    db.enqueue_verticals(items, configs)
    return sorted(items, key=lambda x: -x["projected"])


def vendors_ready() -> tuple[bool, str]:
    """Probe every vendor copy needs, before resuming anything.

    Troy, 2026-09-14: copy must pause on an outage and resume on its own. Resuming
    while a vendor is still dry would restart the stage only to pause again, so each
    dependency is checked first and the one that is out is named.

    The LLM probe is deliberately REAL-SIZED: measured 2026-09-11, a 10-token call
    succeeded on an almost-empty Concentrate balance while every 1,500-token call got
    402. Serper is probed because it feeds the research prompt -- when it ran dry on
    2026-09-14 it took 3,058 contacts with it.
    """
    from .clients import llm, serper
    import app.clients.llm as L
    saved = L._UNAVAILABLE_RETRIES
    L._UNAVAILABLE_RETRIES = 0
    try:
        for chain in ("precopy", "research", "copy"):
            try:
                llm.complete("anthropic/claude-haiku-4.5", "You summarise.",
                             "Summarise in one sentence: " + ("company profile text. " * 120),
                             0.0, 1200, provider=chain)
            except Exception as exc:
                return False, f"LLM chain {chain!r}: {str(exc)[:90]}"
        try:
            serper.news("Stryker")
        except Exception as exc:
            return False, f"serper: {str(exc)[:90]}"
    finally:
        L._UNAVAILABLE_RETRIES = saved
    return True, "all vendors answering"


def llm_probe_ok() -> bool:
    return vendors_ready()[0]


_COPY_TEST_TICK_LOCK = "copy_test_scale_tick"
_COPY_TEST_TICK_STALE_MIN = 15   # tick_scale_ups does a bank check + maybe one feed_campaign call; 15 min is generous


def _tick_copy_test_scaling() -> None:
    """Troy, 2026-10-08: "we need to fix such error so it doesnt happen again, ever."

    Found 2026-10-08: tick_scale_ups used to run INSIDE resume(), after the run_alive()
    gate below -- which blocks every resume() call while ANY prior one is still "alive"
    (a 10-minute heartbeat). A resume() tick that got stuck for 6+ hours deep in the
    unrelated general-vertical pipeline (retrying copy for a couple of stubborn contacts
    over and over) kept its heartbeat alive that whole time, so every later tick saw
    "already running" and never reached tick_scale_ups at all -- MMM's scale-up sat
    frozen since 2:40 AM while that one tick was still "running" at 6:48 PM.

    This now runs FIRST, before that gate, under its own short separate lock -- so a
    stuck general-pipeline run can structurally never block copy-test scaling again,
    no matter how long it runs or what it gets stuck on.
    """
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS job_locks (name TEXT PRIMARY KEY, holder TEXT,
                       heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""INSERT INTO job_locks (name, holder, heartbeat_at) VALUES (%s, %s, now())
                       ON CONFLICT (name) DO UPDATE SET holder=EXCLUDED.holder, heartbeat_at=now()
                       WHERE job_locks.heartbeat_at < now() - make_interval(mins => %s)
                       RETURNING holder""",
                    (_COPY_TEST_TICK_LOCK, "tick-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f"),
                     _COPY_TEST_TICK_STALE_MIN))
        got = cur.fetchone() is not None
    if not got:
        return
    try:
        from . import testgrid as _testgrid_scale
        tick = _testgrid_scale.tick_scale_ups("copy-test-tick-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
        if tick.get("status") == "ticked":
            print(f"[resume] scale-up tick: {tick}", flush=True)
    except Exception as exc:
        print(f"[warn] scale-up tick failed: {exc}", flush=True)
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM job_locks WHERE name=%s", (_COPY_TEST_TICK_LOCK,))


def resume() -> dict:
    """Finish verticals a halted run left paused. Cron-driven, every two hours.

    Troy, 2026-09-13: "self check every 2 hours if topped up to complete process."
    Probes the gateway with a real-sized call; if it answers, each paused vertical
    picks up exactly where it stopped -- staged companies get judged, kept ones get
    people found and verified, verified ones get copy, and a batch posts to Slack.
    """
    # Same schema step the weekly run takes: a resume that starts on a freshly deployed
    # column without it fails every vertical on the first query (2026-09-13, slack_ts).
    db.init_schema()
    # Copy-test scaling first, independent of everything below -- see _tick_copy_test_scaling.
    _tick_copy_test_scaling()
    # Close 'running' rows no worker can still own, so they stop accumulating.
    closed = db.close_abandoned_runs()
    if closed:
        print(f"[resume] closed {closed} abandoned run(s)", flush=True)
    live = db.run_alive(within_minutes=10)
    if live:
        return {"status": "already_running", "run_id": live}

    # Register THIS tick as a real run before doing any work, not only once paused
    # verticals are found below. finish_banked_work() and resume_interrupted_scale_ups()
    # can each run for HOURS (a vertical's own contact-finding is slow), and until now
    # neither ever got a runs row unless paused verticals ALSO existed this tick -- so a
    # tick that only continued a scale-up (the common case) stayed invisible to
    # run_alive(), the /live page's "right now" banner, and interrupted_scale_ups()'s own
    # liveness check, which all read the runs table. That let a LATER tick treat the same
    # in-progress scale-up as "interrupted" and start a second, overlapping attempt at it.
    # Troy, 2026-09-25: Venture Capital was legitimately still finding real companies from
    # a 10:19 attempt when a second attempt, still holding a stale pre-exhaustion snapshot
    # from earlier in that SAME overlap, ran out of its own narrower slice and incorrectly
    # marked the whole vertical exhausted at 12:17 -- while the first attempt went on to
    # find 2,925 more real contacts that afternoon.
    run_id = f"resume-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    db.start_run(run_id)

    # THE TICK'S OWN CLOCK. Railway replaces this cron container when the next
    # 2-hourly schedule fires, so a tick still working at the 2-hour mark is killed
    # mid-slice -- and its successor sees the fresh-looking run row and backs off
    # "already running". Consumer Edtech's continuation died at exactly its tick's
    # 2-hour mark twice (2026-10-01, 2026-10-04). Every heavy phase below checks the
    # minutes left and either fits inside them or hands off to the next tick cleanly.
    import time as _time
    tick_deadline = _time.monotonic() + settings.resume_tick_budget_minutes * 60

    def _mins_left() -> int:
        return max(int((tick_deadline - _time.monotonic()) / 60), 0)

    # Winner scale-ups now tick at the very top of resume(), via _tick_copy_test_scaling(),
    # before the run_alive() gate -- see that function's docstring for why.

    # Wave 1 campaign fill (ProductEVO, Troy 2026-09-27): keep the angle campaigns topped
    # up to target, so a stall -- vendor out of credit, container restart -- resumes on the
    # next tick instead of waiting for someone to notice.
    try:
        from . import wave1_fill
        if wave1_fill.needed():
            wf = wave1_fill.fill()
            print(f"[resume] wave 1 fill: {wf}", flush=True)
            if wf.get("status") in ("complete", "partial", "already_running"):
                db.finish_run(run_id, "complete", f"wave 1 fill {wf}")
                return {**wf, "run_id": run_id}
    except Exception as exc:
        print(f"[warn] wave 1 fill failed: {exc}", flush=True)

    # Queued cost-reduction tests (Troy, 2026-09-28): pre-approved, capped, unattended.
    # Silent no-op until Concentrate has credit again; runs once, posts one Slack summary,
    # and never scales anything on its own -- that stays gated on Troy's reply.
    try:
        from . import cost_tests, testgrid as _testgrid
        if _testgrid.load_config() is not None:
            # Troy, 2026-10-01: he renames clones of a stalled campaign with a similar
            # name ("Stopped Leads PEVO_W1_MMM"); fold any new ones into their angle's
            # score every tick, so scoring never silently misses a clone's replies.
            dup = _testgrid.sync_duplicate_campaigns(run_id)
            if dup.get("found"):
                print(f"[resume] folded in renamed duplicate campaigns: {dup['found']}", flush=True)
            ct = cost_tests.run_pending(run_id)
            if ct.get("status") not in ("all_done",):
                print(f"[resume] cost tests: {ct}", flush=True)
            # Troy, 2026-10-05: a winner's scale-up now runs the moment the day-10
            # decision names it, with no wait for a reply -- testgrid.evaluate_and_record
            # (the daily review) calls auto_expand_champion directly and Slack is told,
            # not asked. Nothing left for this tick to do here.
    except Exception as exc:
        print(f"[warn] cost tests / expansion failed: {exc}", flush=True)

    # A copy test left half written is resumed before anything else: its contacts are
    # already paid for and its arms are already assigned.
    try:
        from . import modes
        ct = modes.resume_copy_test()
        if ct.get("status") == "resumed_copy_test":
            print(f"[resume] copy test: {ct}", flush=True)
            db.finish_run(run_id, "complete", "resumed a copy test")
            return {**ct, "run_id": run_id}
    except Exception as exc:
        print(f"[warn] copy-test resume failed: {exc}", flush=True)

    # NEEDS_REVIEW IS A DEAD END on its own -- it is a BLOCKING status, so nothing ever
    # retries one. Troy, 2026-09-25: "imagine how many needs review we've lost, how can
    # we save them?" (2,889 all-time). A modest, capped requeue every tick means this
    # keeps recovering on its own instead of needing someone to remember to run
    # `/ve requeue`. 300 is deliberately small: each one costs a real research + copy
    # call, and requeue_needs_review() already skips anything a retry cannot fix.
    try:
        requeued = db.requeue_needs_review(limit=300)
        if requeued:
            print(f"[resume] requeued {requeued} needs_review contact(s) for a fresh "
                  f"copy attempt", flush=True)
    except Exception as exc:
        print(f"[warn] requeuing needs_review failed: {exc}", flush=True)

    # A dead SCALE-UP is not in vertical_backlog -- scale_vertical never claims a row
    # there -- so paused_verticals() below cannot see it and its banked contacts used to
    # wait up to 24 hours for the next daily review. Troy, 2026-09-23: "if scaling is
    # paused because of an outage does it check every two hours to complete like with
    # monday runs?" It did not. It does now: finishing banked work is copy only, safe to
    # re-run, and already filtered to this client's own verticals.
    try:
        from .stages import s8_review as _s8
        done = _s8.finish_banked_work(run_id)
        if done:
            print(f"[resume] finished banked work for {len(done)} vertical(s): "
                  + ", ".join(f"{d['vertical']} +{d.get('finished', 0)}" for d in done),
                  flush=True)
    except Exception as exc:
        print(f"[warn] finishing banked work failed: {exc}", flush=True)

    # A SCALE-UP KILLED MID-SOURCING is a different case from banked work above: it has
    # nothing verified yet to write copy for, so finish_banked_work has nothing to do
    # for it. Troy, 2026-09-24, after a deploy killed Medical Device and Venture
    # Capital's scale-ups mid-BuzzLead-discovery, before either had produced a single
    # contact: "this is not good. it makes us fall behind schedule. runs should also
    # resume if they were failed right?" Until now they waited up to 24 hours for the
    # next daily review to re-decide the same winner. Now the two-hourly tick continues
    # them directly.
    try:
        resumed = _s8.resume_interrupted_scale_ups(run_id, budget_minutes=_mins_left())
        if resumed:
            print(f"[resume] continued {len(resumed)} interrupted scale-up(s): "
                  + ", ".join(f"{r['vertical']} +{r.get('scaled', 0)}" for r in resumed),
                  flush=True)
    except Exception as exc:
        print(f"[warn] resuming interrupted scale-ups failed: {exc}", flush=True)

    # A WINNER THAT NEVER GOT A LANE is a third case, distinct from both above: it has
    # no scaling_run_id stamp at all, because the review's 3-lanes-at-a-time queue ran
    # out of process before a freed lane could reach it. Troy, 2026-09-25: Product
    # Design won the 2026-09-24 review as the 5th of 5 winners and sat completely
    # untouched for a full day when the process died right as a lane should have opened
    # for it.
    try:
        started = _s8.resume_unscaled_winners(run_id, budget_minutes=_mins_left())
        if started:
            print(f"[resume] started {len(started)} winner(s) that never got a lane: "
                  + ", ".join(f"{r['vertical']} +{r.get('scaled', 0)}" for r in started),
                  flush=True)
    except Exception as exc:
        print(f"[warn] starting unscaled winners failed: {exc}", flush=True)

    # ARCHIVE COMPLETED CAMPAIGNS. Troy, 2026-09-30: "archiving frees accounts." A
    # finished campaign still holds every account it launched with; releasing them
    # here, before the held-push retry and the top-ups below, is what turns sendable
    # capacity into launchable capacity.
    try:
        from . import senders as _senders
        archived = _senders.archive_completed_campaigns(run_id)
        if archived:
            print(f"[resume] archived {len(archived)} completed campaign(s)", flush=True)
            # The accounts just released are exactly what a HELD launch is waiting
            # for. 2026-09-30 07:31-07:44: six pushes were retried against 279/day
            # and held, then the archive freed the accounts two minutes later --
            # and the pushes had to wait another tick. Retry them now, in order.
            try:
                from .stages import s6_approval as _s6
                held = _s6.push_held_batches(run_id)
                if held:
                    print("[resume] held pushes after archiving:",
                          json.dumps(held, default=str)[:300], flush=True)
            except Exception as exc:
                print(f"[warn] held-push retry after archiving failed: {exc}", flush=True)
    except Exception as exc:
        print(f"[warn] archiving completed campaigns failed: {exc}", flush=True)

    # EXPIRY WARNING. A pending batch is paid sourcing and copy; it expires at 72h
    # (the clock restarts whenever a top-up grows it). A human gets one warning a day
    # before it goes, naming the batch, rather than finding it gone.
    try:
        for b in db.batches_expiring_soon(24):
            slack.post(f":alarm_clock: *{b['vertical']}* batch `{b['batch_id']}` "
                       f"({int(b['contact_count'] or 0):,} contacts) expires "
                       f"{b['expires_at']:%a %d %b %H:%M} UTC. "
                       f"`/ve approve {b['batch_id']}` or `/ve kill {b['batch_id']} <reason>`.")
            db.mark_expiry_warned(b["batch_id"])
    except Exception as exc:
        print(f"[warn] expiry warnings failed: {exc}", flush=True)

    # VENDOR CREDIT ALERT. Troy, 2026-09-30: "alert before, not after." The ledger
    # records every refused call; the first refusal in a day is announced once, with
    # the count, so an out-of-credit vendor is a Slack message, not a 402 found in a
    # log three days later. The finders' own breakers already stop the retry storm.
    try:
        refused = db.vendor_refusals_since(hours=2)
        for vendor, n in refused.items():
            if db.recently_logged("vendors", f"{vendor} refused", hours=24):
                continue
            msg = (f"{vendor} refused {n} call(s) in the last 2 hours -- out of credit "
                   f"or unauthorised. Its breaker is tripped; companies that missed it "
                   f"are remembered and re-searched once it is back.")
            db.log(run_id, "vendors", msg, level="warn")
            slack.post(f":credit_card: *{vendor} is out of credit* -- {n} refused "
                       f"call(s) in the last 2 hours. Top it up; the pipeline reopens "
                       f"the companies that missed it automatically.")
    except Exception as exc:
        print(f"[warn] vendor credit alert failed: {exc}", flush=True)

    # VENDOR CREDIT WATCH. Troy, 2026-10-05: "prevent rather than report" -- Serper
    # ran dry mid-Monday and blocked a tick before anyone knew the balance was low.
    # MillionVerifier exposes a balance endpoint, so it is probed and warned on at the
    # floor; vendors with no balance API (Serper) get a LOUD alert the moment a
    # not-enough-credits refusal appears, instead of a note in a blocked run row.
    try:
        vendor_credit_watch(run_id)
    except Exception as exc:
        print(f"[warn] vendor credit watch failed: {exc}", flush=True)

    # VENDOR RECOVERY. Troy, 2026-09-30: "how do companies already searched get a
    # chance to get more contacts by going through previous vendors that were emptied
    # but are now topped up." Every searched company remembers which vendors were OUT
    # at the time; when one is back, those companies (with a seat still empty) are put
    # in front of the finders again, and the top-up loop below carries them.
    try:
        reopened = reopen_companies_for_recovered_vendors(run_id)
        if reopened:
            print("[resume] vendor recovery reopened " +
                  ", ".join(f"{n} for {v}" for v, n in reopened.items()), flush=True)
    except Exception as exc:
        print(f"[warn] vendor recovery sweep failed: {exc}", flush=True)

    # MIDWEEK INTAKE WHEN THE WEEK IS BEHIND. Troy, 2026-09-30: the target is 15,000
    # leads by Sunday, not "whatever Monday produced". If, from Wednesday, the week is
    # tracking under 80% of pace and there are viable verticals queued, start more --
    # once a day, under the same sender-capacity brake the Monday intake uses, so it
    # never builds a batch it cannot launch.
    try:
        if _mins_left() < 45:
            db.log(run_id, "stage2b", f"midweek intake skipped this tick: only "
                   f"{_mins_left()} min of tick budget left; the next tick has a full slice")
            started = []
        else:
            started = midweek_intake_if_short(run_id)
        if started:
            print(f"[resume] midweek intake started {len(started)} vertical(s): "
                  + ", ".join(started), flush=True)
    except Exception as exc:
        print(f"[warn] midweek intake failed: {exc}", flush=True)

    # AUTOMATIC TOP-UP, no human needed. Troy, 2026-09-29, on Monday's intake landing
    # 3 batches short of 3,000 because a vendor was out at the exact moment they
    # needed it: "how can we make this system seamless and without the need of a
    # human." Every 2-hourly tick, not just after a crash: covers a pending batch
    # under the 3,000 cap AND a launched-but-thin campaign under the 2,000 floor
    # that can never earn a verdict on its own. See auto_topup_short_verticals()'s
    # own docstring for why this is retried indefinitely rather than day-bound.
    try:
        topped = auto_topup_short_verticals(run_id, budget_minutes=_mins_left())
        if topped:
            print(f"[resume] auto top-up added to {len(topped)} vertical(s): "
                  + ", ".join(f"{t['vertical']} +{t['added']}" for t in topped),
                  flush=True)
    except Exception as exc:
        print(f"[warn] auto top-up failed: {exc}", flush=True)

    paused = db.paused_verticals()
    if not paused:
        db.finish_run(run_id, "complete", "nothing paused")
        return {"status": "nothing_paused", "run_id": run_id}
    ready, why = vendors_ready()
    if not ready:
        print(f"still blocked ({why}); {len(paused)} verticals stay paused")
        # A credits refusal is a human-actionable outage: say so in the channel once a
        # day, rather than leaving it as a note on a blocked run nobody opens.
        # Serper's dry spell on 2026-10-05 blocked the 10:03 tick with only that note.
        if "credit" in why.lower() or "402" in why:
            try:
                if not db.recently_logged("vendors", "out of credit (blocking)", hours=24):
                    db.log(run_id, "vendors", f"out of credit (blocking): {why}",
                           level="warn")
                    slack.post(f":rotating_light: *A vendor is out of credit and it is "
                               f"blocking the pipeline*: {why}. Paused verticals resume "
                               f"on their own once it is topped up.")
            except Exception:
                pass
        db.finish_run(run_id, "blocked", why)
        return {"status": "still_blocked", "reason": why,
                "paused": [p["vertical"] for p in paused], "run_id": run_id}
    db.log(run_id, "start", f"resume: {why}, finishing {len(paused)} paused verticals")
    summary: dict = {"run_id": run_id, "batches": []}
    verticals = []
    for row in paused:
        cfg = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
        verticals.append(s2_expansion.Vertical.from_json(cfg))
    if _mins_left() < 20:
        db.log(run_id, "start", f"paused verticals wait for the next tick: only "
               f"{_mins_left()} min of tick budget left and their sourcing runs long")
        db.finish_run(run_id, "complete", f"tick budget spent; {len(paused)} paused "
                      f"verticals deferred to the next tick")
        return {"status": "deferred_paused", "paused": [p["vertical"] for p in paused],
                "run_id": run_id}
    done = _work_verticals(verticals, run_id, False, summary)
    db.finish_run(run_id, "complete" if len(done) == len(paused) else "paused_llm",
                  f"resumed {len(done)} of {len(paused)}")
    return {**summary, "status": "resumed", "done": done}


def _work_verticals(verticals, run_id: str, dry_run: bool, summary: dict) -> list[str]:
    """Run several verticals AT THE SAME TIME. Returns the names that completed.

    Troy, 2026-09-13: "is the company sourcing running at the same time for each
    vertical?" It was not: three verticals ran one after another, each through
    sourcing, finders and verification before the next began, so a three-vertical run
    was three verticals long. They share nothing but vendor rate limits and the LLM
    semaphore, so they run concurrently now, with per-vertical worker counts sized so
    three at once do not flood QuickEnrich. A vertical that hits an LLM outage pauses
    itself; the others carry on.
    """
    done: list[str] = []
    lock = threading.Lock()

    def _run(vertical) -> None:
        db.finish_backlog(vertical.name, status="running")
        try:
            _work_vertical(vertical, run_id, dry_run, summary)
            with lock:
                done.append(vertical.name)
        except VendorOutage as exc:
            db.finish_backlog(vertical.name, status="paused_llm")
            db.log(run_id, "stage3", f"{vertical.name}: paused, LLM unavailable "
                   f"({str(exc)[:80]}); banked progress kept", level="warn")
            slack.paused(vertical.name, f"LLM unavailable: {str(exc)[:120]}",
                         db.vertical_thread(vertical.name))
        except Exception as exc:
            db.finish_backlog(vertical.name, status="queued")
            db.log(run_id, "end", f"{vertical.name}: failed: {exc}", level="error")

    with ThreadPoolExecutor(max_workers=max(settings.parallel_verticals, 1)) as pool:
        list(pool.map(_run, verticals))
    return done


def vendor_credit_watch(run_id: str | None) -> None:
    """Warn BEFORE a balance hits zero, where a vendor lets us see it. Once a day."""
    import httpx
    if not settings.millionverifier_api_key:
        return
    if db.recently_logged("vendors", "MillionVerifier balance", hours=24):
        return
    try:
        r = httpx.get("https://api.millionverifier.com/api/v3/credits",
                      params={"api": settings.millionverifier_api_key}, timeout=15)
        credits = int((r.json() or {}).get("credits") or 0) if r.status_code == 200 else None
    except Exception:
        return
    if credits is None:
        return
    db.log(run_id, "vendors", f"MillionVerifier balance: {credits:,} credits")
    if credits < settings.mv_credits_warn:
        slack.post(f":warning: *MillionVerifier is down to {credits:,} credits* "
                   f"(warning floor {settings.mv_credits_warn:,}). Top it up before "
                   f"verification starts failing -- it gates every contact.")


def vendor_is_up(vendor: str) -> bool:
    """One cheap live probe per vendor. True on a 2xx, False on a credit/auth failure
    or anything else that means 'this vendor will refuse the next real call'."""
    import httpx
    try:
        if vendor == "apollo":
            if not settings.apollo_api_key:
                return False
            r = httpx.post("https://api.apollo.io/api/v1/mixed_people/search",
                           headers={"X-Api-Key": settings.apollo_api_key},
                           json={"person_titles": ["CEO"], "per_page": 1}, timeout=20)
            return r.status_code < 400
        if vendor == "aiark":
            if not settings.aiark_api_key:
                return False
            r = httpx.post("https://api.ai-ark.com/api/developer-portal/v1/companies",
                           headers={"X-TOKEN": settings.aiark_api_key,
                                    "Content-Type": "application/json"},
                           json={"account": {"location": {"any": {"include": ["United States"]}},
                                             "employeeSize": {"type": "RANGE",
                                                              "range": [{"start": 11, "end": 500}]}},
                                 "page": 0, "size": 1}, timeout=20)
            return r.status_code < 400
        if vendor == "leadocean":
            key = os.getenv("LEADOCEAN_API_KEY", "")
            if not key:
                return False
            r = httpx.get("https://api.leadocean.io/v1/companies/example.com/people",
                          params={"limit": 1}, headers={"x-api-key": key}, timeout=20)
            return r.status_code in (200, 404)     # 404 = no people, but the account works
    except Exception:
        return False
    return False


RECOVERABLE_VENDORS = ("apollo", "aiark", "leadocean")


def reopen_companies_for_recovered_vendors(run_id: str | None) -> dict[str, int]:
    """For each vendor that is up NOW, reopen the companies that missed it.

    Cheap when nothing changed: one probe per vendor, and the UPDATE matches nothing
    unless some company recorded that vendor as missed. The finders' own breakers are
    reset too, or the very next call would skip the vendor again on a stale flag.
    """
    from . import finders
    out: dict[str, int] = {}
    seats = settings.contacts_per_company
    for vendor in RECOVERABLE_VENDORS:
        if not vendor_is_up(vendor):
            continue
        if vendor == "apollo":
            finders._apollo_dead = False
        elif vendor == "aiark":
            finders._aiark_dead = False
        elif vendor == "leadocean":
            finders._leadocean_dead = False
        n = db.reopen_after_recovery(vendor, seats)
        if n:
            out[vendor] = n
            db.log(run_id, "stage4", f"{vendor} is back: {n} companies that missed it "
                   f"(with a seat still empty) go back through the finders on the "
                   f"next top-up")
    return out


def midweek_intake_if_short(run_id: str | None) -> list[str]:
    """Start extra verticals from the backlog when the week is behind its quota.

    Wednesday onward (WAT), at most once a day, only when the week is under 80% of
    where its pace says it should be, and only as many as free sender capacity can
    launch (the same brake _intake_size applies on Monday). Uses the same
    _work_verticals path as the weekly run, so a started vertical goes through the
    full sourcing -> contacts -> copy -> approval flow and lands in /review like any
    other. Returns the names started.
    """
    from datetime import timedelta as _td
    # ProQ and MasterNow (declared_first) run only what a human asks for until AUTO_INTAKE=true is set on the
    # service: the weekly and midweek intake and the automatic top-ups are sized to IDPR-style quotas.
    from . import client as _cl
    if _cl.profile().declared_first and os.getenv("AUTO_INTAKE", "false").strip().lower() != "true":
        return []
    wat = datetime.now(timezone.utc) + _td(hours=1)
    if wat.weekday() < 2:                              # Mon/Tue: Monday's run is the intake
        return []
    w = db.weekly_progress()
    if w["leads"] >= w["pace_quota"] * 0.8:
        return []
    # once a day: any midweek-* run started today (WAT) means this already fired
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM runs WHERE run_id LIKE 'midweek-%%' "
                    "  AND started_at >= %s LIMIT 1",
                    (wat.replace(hour=0, minute=0, second=0, microsecond=0) - _td(hours=1),))
        if cur.fetchone():
            return []
    if db.run_alive(within_minutes=45):
        return []
    # Troy, 2026-10-08: "if we're slacking wednesday should launch regardless." This IS
    # the slacking signal (the 80% pace check above already fired) -- the seat ceiling
    # is a steady-state preference, not the thing protecting winners from dilution,
    # which is the dedicated-capacity check _intake_size still runs either way.
    n = _intake_size(run_id, ignore_seat_ceiling=True)
    mid_run = f"midweek-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    claimed = db.take_from_backlog(n, mid_run)
    if not claimed:
        db.log(run_id, "stage2b", f"midweek intake: week at {w['leads']:,} of a "
               f"{w['pace_quota']:,} pace but nothing viable is queued")
        return []
    db.start_run(mid_run)
    db.log(mid_run, "start", f"midweek intake: week at {w['leads']:,} leads against a "
           f"{w['pace_quota']:,} pace ({w['quota']:,} quota); starting "
           f"{len(claimed)}: " + ", ".join(c["vertical"] for c in claimed))
    slack.post(f":chart_with_upwards_trend: *Midweek intake* — the week is at "
               f"{w['leads']:,} leads against a {w['pace_quota']:,} pace, so "
               f"{len(claimed)} more vertical(s) are starting now: "
               + ", ".join(c["vertical"] for c in claimed))
    verticals = []
    for c in claimed:
        cfg = c["config"] if isinstance(c["config"], dict) else json.loads(c["config"])
        verticals.append(s2_expansion.Vertical.from_json(cfg))
    summary: dict = {"run_id": mid_run, "batches": []}
    done = _work_verticals(verticals, mid_run, False, summary)
    db.finish_run(mid_run, "complete" if len(done) == len(verticals) else "paused_llm",
                  f"midweek intake finished {len(done)} of {len(verticals)}")
    return [v.name for v in verticals]


# Five straight automatic top-up attempts that add NOTHING pause a vertical for a
# human, rather than burn vendor calls forever on one that will never grow. Same
# shape as s8_review.POOR_RESULT_STREAK_LIMIT, a separate constant because it
# guards a different population (short batches and thin launched campaigns, not
# scale-up label-precision stops).
AUTO_TOPUP_STREAK_LIMIT = 5


def auto_topup_short_verticals(run_id: str | None,
                               budget_minutes: int | None = None) -> list[dict]:
    """Every two hours: top up any vertical under its ceiling, whether or not it
    has launched, instead of waiting for a human to notice and run --topup by hand.

    Troy, 2026-09-29, on Monday's intake landing 3 batches short of 3,000 because a
    vendor was out at the exact moment they needed it: "how can we make this
    system seamless and without the need of a human." Two populations, one loop --

      PRE-LAUNCH: a pending batch under contact_cap_per_vertical (3,000). Exactly
      the Monday gap: nothing previously retried a vertical that already has a
      batch, since the weekly walk only ever moves FORWARD to new verticals.

      POST-LAUNCH, UNJUDGED: a launched campaign under auto_topup_launched_floor
      (2,000) -- too thin to ever clear scale_min_sends and earn a verdict on its
      own, and nothing else tops it up once it has launched (scale_vertical only
      starts AFTER a 'scale' verdict, which needs sends this thin a campaign does
      not have yet).

    Retried INDEFINITELY, not bounded to a day the way resume_interrupted_scale_ups
    is: unlike a stuck scale-up, there is no OTHER daily mechanism waiting to pick
    either of these up, so backing off after one day would mean nothing ever does.
    The only circuit breaker is topup_streak: five consecutive attempts that add
    zero contacts pause the vertical (topup_needs_human_at) until a human widens
    the audience, the same relief valve as a scale-up's poor-result pause.
    """
    # ProQ and MasterNow (declared_first) run only what a human asks for until AUTO_INTAKE=true is set on the
    # service: the weekly and midweek intake and the automatic top-ups are sized to IDPR-style quotas.
    from . import client as _cl
    if _cl.profile().declared_first and os.getenv("AUTO_INTAKE", "false").strip().lower() != "true":
        return []

    done: list[dict] = []
    # STAY OUT OF THE REVIEW'S WAY. 2026-09-29: this loop's 22:04 UTC tick was still
    # sourcing when the 23:00 review fired, and the review yielded to it -- the whole
    # day's scaling was skipped. Two guards: no top-up inside the quiet window before
    # the review, and a wall-clock budget so one tick can never run into the next.
    import time as _time
    now = datetime.now(timezone.utc)
    review_at = now.replace(hour=settings.review_cron_utc_hour, minute=0, second=0,
                            microsecond=0)
    if review_at < now:
        from datetime import timedelta as _td
        review_at += _td(days=1)
    until_review = (review_at - now).total_seconds() / 60
    if until_review <= settings.auto_topup_quiet_before_review_minutes:
        db.log(run_id, "stage8", f"auto top-up skipped this tick: the daily review "
               f"fires in {until_review:.0f} min and must not find this still running")
        return done
    budget = settings.auto_topup_budget_minutes
    if budget_minutes is not None:
        budget = min(budget, budget_minutes)   # never outlive the tick's own clock
    deadline = _time.monotonic() + budget * 60

    candidates = (db.short_pending_batches(settings.contact_cap_per_vertical)
                 + db.short_launched_campaigns(settings.auto_topup_launched_floor))
    seen: set[str] = set()
    for row in candidates:
        name = row["vertical"]
        if name in seen:
            continue
        seen.add(name)
        if _time.monotonic() >= deadline:
            db.log(run_id, "stage8", f"auto top-up budget "
                   f"({settings.auto_topup_budget_minutes} min) reached; {name} and "
                   f"the rest wait for the next tick")
            break
        cfg = db.backlog_config(name)
        if not cfg:
            continue
        vertical = s2_expansion.Vertical.from_json(cfg)
        before = db.pending_batch_for(name)
        before_count = before["contact_count"] if before else 0
        summary: dict = {"run_id": run_id, "batches": []}
        try:
            _work_vertical(vertical, run_id or "auto-topup", False, summary,
                          max_new=settings.auto_topup_increment)
        except VendorOutage as exc:
            db.log(run_id, "stage8", f"{name}: auto top-up paused, vendor outage "
                   f"({str(exc)[:120]}); will try again in 2 hours", level="warn")
            continue
        except Exception as exc:
            db.log(run_id, "stage8", f"{name}: auto top-up failed: {exc}",
                   level="error")
            continue
        after = db.pending_batch_for(name)
        after_count = after["contact_count"] if after else 0
        added = after_count - before_count
        streak = db.record_topup_streak(name, added)
        if added > 0:
            db.log(run_id, "stage8",
                   f"{name}: auto top-up added {added} contacts ({before_count} -> "
                   f"{after_count})")
            done.append({"vertical": name, "added": added})
        elif streak >= AUTO_TOPUP_STREAK_LIMIT:
            db.pause_topup_for_human(name)
            db.log(run_id, "stage8",
                   f"{name}: {streak} straight auto top-up attempts added nothing "
                   f"-- pausing for a human; widen the audience to resume",
                   level="warn")
            slack.post(f":pause_button: *{name}* auto top-up paused -- "
                       f"{streak} straight attempts found nothing new. Widen "
                       f"employee size, geography or titles to resume it.")
        else:
            db.log(run_id, "stage8",
                   f"{name}: auto top-up found nothing this attempt "
                   f"({streak}/{AUTO_TOPUP_STREAK_LIMIT} before pausing for a human)")
    return done


def run_pipeline(dry_run: bool = False) -> dict:
    if not settings.pipeline_enabled:
        print("PIPELINE_ENABLED is false; halting at tick (kill switch).")
        return {"status": "halted", "reason": "kill switch"}

    # THE WEEKLY RUN NEVER SKIPS FOR ANOTHER LIVE RUN. Troy, 2026-10-01: this used to
    # mirror the review's OLD blanket guard -- if ANY run was alive, skip the whole
    # week's launch outright, with no retry until next Monday. That blanket form was
    # wrong for the same reason it was wrong on the review (fixed the same day): the
    # weekly run only ever claims NEW backlog verticals that have never launched,
    # and a 2-hourly resume tick only ever continues or tops up verticals that
    # ALREADY have launched -- disjoint sets, so there is no double-write risk here,
    # only shared vendor rate limits for an hour. And the one real contention case
    # the old comment worried about -- a review-triggered scale-up still running
    # Monday morning -- is already prevented by scale_skip_weekday (s8_review.py):
    # the review that lands at 00:00 WAT Monday judges and splits but does not scale,
    # so there is no hours-long scale-up straddling the 07:05 WAT launch to begin
    # with. The weekly run now always fires on schedule.

    run_id = _run_id()
    db.start_run(run_id)
    # The weekly EXPAND path had no heartbeat, only the two copy-test paths did, so an
    # IDPR run could not be told apart from a dead one -- which is the exact question
    # that cost a day on ProductEVO. Troy, 2026-09-21, looking at the live page.
    from . import modes as _modes
    _modes.heartbeat(run_id)
    db.log(run_id, "start", f"run begins (dry_run={dry_run})")

    expired = db.expire_stale_batches()
    if expired:
        db.log(run_id, "stage6", f"auto-killed {len(expired)} stale batches", batches=expired)

    try:
        with db.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM contacts WHERE source='backfill'")
            n = int(cur.fetchone()["n"] or 0)
        if n < 1000:
            db.log(run_id, "backfill",
                   f"history only has {n} backfilled contacts; 90-day re-engage is "
                   f"blind on pre-pipeline sends. Run: python -m app.runner backfill",
                   level="warn")
    except Exception:
        pass

    # Refresh the 90-day contact index BEFORE anything consults the dedup gate. This is
    # what db.blocked_domains reads to decide whether a company was emailed recently, and
    # a stale index fails in the dangerous direction: companies emailed since the last
    # sync would read as untouched and get a second approach inside the window.
    try:
        from scripts.sync_recent import sync as sync_recent
        t = sync_recent(settings.recontact_days)
        db.log(run_id, "sync", f"recent-contact index refreshed: {t['rows']:,} leads "
                               f"from {t['campaigns']} campaigns", **t)
    except Exception as exc:
        # Do not run on a stale index -- see above for why this fails unsafely.
        msg = f"recent-contact index refresh failed: {exc}"
        db.log(run_id, "sync", msg, level="error")
        db.finish_run(run_id, "failed", msg)
        return {"run_id": run_id, "status": "failed", "reason": msg}

    # Keep the supply map current. Bounded, and a failure here must not stop a run --
    # the map improves proposals, it is not required for them.
    try:
        from . import taxonomy
        taxonomy.refresh(run_id=run_id)
    except Exception as exc:
        db.log(run_id, "taxonomy", f"supply map refresh failed: {exc}", level="warn")

    summary: dict = {"run_id": run_id, "dry_run": dry_run, "batches": []}
    try:
        # Stage 1 -- API aggregates, overridden by any uploaded export.
        # A declared_first client (ProQ, MasterNow) does not look for winners first: its declared, ranked
        # verticals are always the starting point (stage 2c below).
        from . import client as _cl
        declared_first = bool(_cl.profile().declared_first)
        rows = [] if declared_first else s1_performance.current_stats()
        if not rows and not declared_first:
            msg = ("no campaign stats available from the EmailBison API. "
                   "Check the key and workspace, or upload an export to /upload.")
            db.log(run_id, "stage1", msg, level="warn")
            db.finish_run(run_id, "no_data", msg)
            return {**summary, "status": "no_data", "reason": msg}

        themes, stats1 = ([], {}) if declared_first else s1_performance.select_seed_themes(rows)
        winners = [t.as_seed() for t in themes]
        db.log(run_id, "stage1", f"{len(winners)} winning audiences from "
               f"{stats1.get('campaigns', len(rows))} campaigns / "
               f"{stats1.get('themes', 0)} themes", **{
            k: v for k, v in stats1.items() if k != "rejected_sample"
        })
        if not winners and not declared_first:
            msg = (f"no qualified winners: nothing cleared "
                   f">={settings.scale_min_sends} sends, "
                   f">={settings.scale_min_days}d, "
                   f">={settings.scale_rate:.2%} {settings.scale_metric}")
            db.log(run_id, "stage1", msg, level="warn", rejected=stats1["rejected_sample"])
            db.finish_run(run_id, "no_winners", msg, winners=[])
            return {**summary, "status": "no_winners", "reason": msg, "detail": stats1}

        # Stage 2 -- adjacent verticals, but only enough to keep the queue stocked.
        # Proposing every week regardless would re-derive verticals already scored and
        # paid for, and would quietly get a different answer each time.
        depth = db.backlog_depth()
        if declared_first:
            # Stage 2c: probe the declared verticals through the waterfall and queue them, best supply first.
            summary["freshness"] = seed_declared_backlog(run_id)
        elif depth < settings.backlog_floor:
            already = db.recent_verticals(settings.lookback_days)
            outcomes = s8_review.judged_verticals()
            proposed = s2_expansion.propose(winners, already, outcomes)
            db.log(run_id, "stage2",
                   f"backlog at {depth}, proposed {len(proposed)} more",
                   names=[v.name for v in proposed])
            if proposed:
                kept, freshness = s2b_freshness.rank(proposed, run_id)
                summary["freshness"] = freshness
                queued = db.enqueue_verticals(
                    freshness, {v.name: v.as_dict() for v in kept})
                db.log(run_id, "stage2b", f"queued {queued} verticals for later runs")
        else:
            db.log(run_id, "stage2", f"backlog at {depth}, no new proposals needed")

        # Take this run's work off the queue, highest projected first.
        # Re-score what is already queued before choosing. A backlog row keeps the
        # projection it was given when queued, so after the probe improved (union, learned
        # rates) old rows competed on stale numbers: on 2026-09-11 medical device sat at
        # 3,374 from the intersection probe and ranked third behind two verticals scored
        # by the fixed one, though its real universe was 11,241. Like for like, or not.
        try:
            rescored = db.rescore_backlog(
                lambda cfg, dens: (__import__("app.stages.s2c_waterfall_probe", fromlist=["probe"]).probe
                                   if declared_first else s2b_freshness.probe)(
                    s2_expansion.Vertical.from_json(cfg), run_id, known_density=dens))
            if rescored:
                db.log(run_id, "stage2b", f"re-probed {rescored} queued verticals", )
        except Exception as exc:
            db.log(run_id, "stage2b", f"backlog re-score skipped: {exc}", level="warn")
        claimed = db.take_from_backlog(_intake_size(run_id), run_id)
        # Sort by projected supply. take_from_backlog CLAIMS the highest-projected rows,
        # but it does so with UPDATE ... RETURNING, and RETURNING does not preserve the
        # ORDER BY of its subquery -- so the rows come back in whatever order the update
        # touched them. On 2026-09-11 that ran DTC Beauty (projected 1,372) ahead of
        # medical device (3,374), and because the run was interrupted, Beauty was the
        # only thing that finished. Order only matters when a run does not complete,
        # which is precisely when it matters most.
        # On the SAME key take_from_backlog claimed with. Sorting on the floored
        # `projected` alone re-ties the five verticals that MIN_PROJECTED flattened to
        # 2,000, so the run would work them in row order again -- and this sort exists
        # precisely because work order decides what survives a run that dies early.
        claimed = sorted(claimed,
                         key=lambda r: max(int(r.get("projected") or 0),
                                           int(r.get("projected_raw") or 0)),
                         reverse=True)
        verticals = [s2_expansion.Vertical.from_json(
            r["config"] if isinstance(r["config"], dict) else json.loads(r["config"]))
            for r in claimed]
        if not verticals:
            msg = ("nothing workable in the vertical backlog: every queued vertical has "
                   "either been tested already or failed the supply gate")
            db.log(run_id, "stage2b", msg, level="warn")
            db.finish_run(run_id, "no_supply", msg, winners=stats1["winners"])
            slack.nothing_to_do(run_id, msg)
            return {**summary, "status": "no_supply", "reason": msg}
        db.log(run_id, "stage2b",
               f"working {len(verticals)} of {db.backlog_depth() + len(verticals)} queued",
               names=[v.name for v in verticals])
        slack.run_started(run_id, winners,
                          [{"name": r.get("vertical"), "projected": r.get("projected")}
                           for r in claimed])

        summary["winners"] = stats1["winners"]
        summary["verticals"] = [v.name for v in verticals]

        done = _work_verticals(verticals, run_id, dry_run, summary)
        paused = [v.name for v in verticals if v.name not in done]
        if paused:
            # "paused for LLM" was asserted for ANY vertical that did not finish,
            # whatever killed it. Apparel & Footwear died on a NUL byte in a vendor
            # record (2026-09-21) and was filed as an LLM outage, which sends the next
            # person to check credit balances instead of the data.
            msg = (f"{len(paused)} verticals did not finish: {paused}; resume runs "
                   f"every 2h. Read the run log for the actual error -- this is NOT "
                   f"necessarily an LLM outage")
            db.log(run_id, "end", msg, level="warn")
            db.finish_run(run_id, "paused_llm", msg, winners=stats1["winners"])
            return {**summary, "status": "paused_llm", "paused": paused}

        db.finish_run(run_id, "complete", winners=stats1["winners"],
                      verticals=[v.as_dict() for v in verticals])
        summary["status"] = "complete"
        return summary

    except Exception as exc:
        traceback.print_exc()
        db.log(run_id, "error", f"run failed: {exc}", level="error")
        db.finish_run(run_id, "failed", str(exc))
        return {**summary, "status": "failed", "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.runner")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight")
    sub.add_parser("initdb")
    sub.add_parser("status")
    sub.add_parser("expire")
    sub.add_parser("stop-on-reply")
    sub.add_parser("resume")
    sub.add_parser("resume-all", help="resume every client, each in its own process")
    sub.add_parser("review-all", help="judge campaigns for every client")
    sub.add_parser("report-all", help="post the Friday report for every client")
    sub.add_parser("weekly-all", help="the weekly audience run, for clients in expand mode")
    p_conc = sub.add_parser("conclude", help="decide which copy arm won")
    p_conc.add_argument("--client", default=None)
    p_conc.add_argument("--experiment", default=None)
    p_conc.add_argument("--promote", action="store_true",
                        help="make the winning arm this client's copy")
    p_cal = sub.add_parser("calibrate", help="what bar this client's own history suggests")
    p_cal.add_argument("--client", default=None)
    p_booked = sub.add_parser("booked", help="record a booked meeting outcome")
    p_booked.add_argument("email")
    p_booked.add_argument("--by", default="cli")
    p_booked.add_argument("--note", default=None)
    p_review = sub.add_parser("review")
    p_review.add_argument("--no-scale", action="store_true",
                          help="judge and record only; do not source scale-up leads")

    p_run = sub.add_parser("run")
    p_run.add_argument("--dry-run", action="store_true",
                       help="stop before copy generation; no batch is opened")

    p_client = sub.add_parser("client", help="run this pipeline for a client in a mode")
    p_client.add_argument("--client", default=None, help="client profile name")
    p_client.add_argument("--mode", default=None, choices=["source", "copy-test", "expand"])
    p_client.add_argument("--audience", action="append", default=None,
                          help="plain-English audience; repeat for several")
    p_client.add_argument("--verticals", type=int, default=3,
                          help="how many queued audiences to use when none is named")
    p_client.add_argument("--contacts", type=int, default=None, help="contact target")
    p_client.add_argument("--list", action="store_true", help="list clients and stop")
    p_client.add_argument("--plan", action="store_true", help="show the plan and stop")
    p_client.add_argument("--variants", default=None,
                          help="comma-separated copy-test arms; default is the first two")
    p_client.add_argument("--sample", type=int, default=0,
                          help="generate this many per arm, then stop for review")
    p_client.add_argument("--full", action="store_true",
                          help="skip the sample and write the whole run")
    p_client.add_argument("--dry-run", action="store_true",
                          help="resolve the audience, then stop before any spend")
    p_client.add_argument("--companies-only", action="store_true",
                          help="mode=source only: stop after companies, before any "
                               "contact finding")
    p_client.add_argument("--probe", action="store_true",
                          help="declared_first clients: probe and rank the declared verticals through the waterfall, "
                               "queue the viable ones, and stop (searches plus a small fit sample only)")
    p_client.add_argument("--topup", action="append", default=None,
                          help="re-source and top up a NAMED vertical's existing "
                               "pending batch (by exact vertical name), instead of "
                               "walking the backlog forward to new ones; repeat for "
                               "several")
    p_prev = sub.add_parser("preview", help="write one email with the current prompts")
    p_prev.add_argument("--client", default=None)
    p_prev.add_argument("--arm", default=None)
    p_prev.add_argument("--email", default=None)
    p_report = sub.add_parser("report", help="the Friday report as PDF (and post to Slack)")
    p_report.add_argument("--post", action="store_true")
    p_report.add_argument("--out", default=None, help="write the PDF here")
    p_align = sub.add_parser("align-senders", help="attach sender accounts to a campaign")
    p_align.add_argument("campaign_id", type=int)
    p_align.add_argument("--dry-run", action="store_true")
    p_push = sub.add_parser("push")
    p_push.add_argument("batch_id")
    p_push.add_argument("--live", action="store_true", help="actually push (default is dry run)")
    p_push.add_argument("--campaign", type=int, default=None)
    p_push.add_argument("--out-dir", default=".")

    p_confirm = sub.add_parser("confirm-csv")
    p_confirm.add_argument("batch_id")

    p_backfill = sub.add_parser("backfill",
                                help="load EmailBison send history so 90-day re-engage works")
    p_backfill.add_argument("--max-pages", type=int, default=None)
    p_backfill.add_argument("--restart", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "preflight":
        return 0 if preflight() else 1
    if args.command == "initdb":
        db.init_schema()
        print("schema created")
        return 0
    if args.command == "booked":
        ok = db.mark_booked(args.email, actor=args.by, note=args.note)
        print("booked meeting recorded" if ok else "contact not found")
        return 0 if ok else 1
    if args.command == "review":
        if not settings.pipeline_enabled:
            print("PIPELINE_ENABLED is false; skipping review.")
            return 0
        for stale in db.expire_stale_batches():
            # A renewed batch is still pending: bring it back to the channel with its
            # buttons and the new clock, so silence is not how a batch dies.
            try:
                b = db.get_batch(stale)
                if b and b["approval_status"] == "pending":
                    from .stages import s6_approval as _s6
                    _s6._show(stale)
            except Exception as exc:
                print(f"[warn] could not re-post renewed batch {stale}: {exc}", flush=True)
        # Warn a day and a half ahead of expiry -- an approval that lapses silently is
        # a batch of paid-for contacts released for nothing.
        for b in db.batches_expiring(within_hours=36):
            slack.expiring(b["vertical"], b["batch_id"], b["expires_at"], b["contact_count"],
                           db.vertical_thread(b["vertical"]))
            db.mark_expiry_warned(b["batch_id"])
        # REGISTER the review as a run. A SCALE verdict turns this job into a full
        # sourcing + contacts + copy run that takes hours, but until 2026-09-22 it minted
        # a run_id and never wrote a `runs` row. Measured that day: the Biotechnology
        # scale-up ran under run-20260922T120406, died in stage 4 at 13:59 with 2,173
        # verified contacts banked and no copy, and NOTHING noticed -- the two-hourly
        # resume, close_abandoned_runs(), the 45-minute silence alarm and /live all watch
        # the `runs` table, and there was no row to watch. Hours of paid work vanished
        # with no error and no alarm.
        # THE REVIEW NEVER WAITS AND NEVER SKIPS. Troy, 2026-10-01: "the nightly
        # review should take priority." It used to poll up to review_wait_minutes for
        # any other live run, then skip the WHOLE night's judging if that run was
        # still going -- 2026-09-29 and 2026-09-30 both lost a full night's scaling
        # this way, once behind a resume tick's own scale-up continuation and once
        # behind an unrelated midweek-intake sourcing run. The review itself can never
        # be the thing that causes a double-scale (it only ever starts ONE scale_vertical
        # per campaign per run); the actual collision risk is narrower than "any run
        # alive" -- it is specifically a vertical whose campaign a 2-hourly tick is, right
        # now, actively scaling. That one check now lives in
        # s8_review._do_act_on_winner, scoped to the single campaign about to be acted
        # on, right before it would start a duplicate scale_vertical -- so the review
        # runs on schedule every night, judges and scales everything else, and only
        # ever defers the one vertical actually in a live tick's hands.

        rid = _run_id()
        db.start_run(rid)
        # Stamp a NON-SWEEP stage. run_alive() deliberately ignores stage8 and review
        # lines, because stop_on_reply logs against whichever run first created a
        # campaign and those lines say nothing about that run still working. The
        # consequence is that a review judging campaigns logs only under stage8 and is
        # therefore invisible to run_alive -- so the two-hourly resume could start work
        # beside it, the exact race that guard exists to prevent. One 'start' line makes
        # the run visible for the 10-minute window resume checks.
        db.log(rid, "start", "review: judging every VE campaign")
        try:
            for r in s8_review.review_all(run_id=rid, scale=not args.no_scale):
                extra = r.get("scale_result") or {}
                tail = f"  -> scaled +{extra['scaled']}" if extra.get("scaled") else ""
                print(f"  {r['vertical'][:44]:46} {r['verdict'].upper():8} "
                      f"{r['reason']}{tail}")
        except BaseException as exc:
            # BaseException so a container SIGTERM -- the thing that actually killed the
            # scale-up -- still closes the row instead of leaving it open forever.
            db.finish_run(rid, "failed", notes=f"{type(exc).__name__}: {str(exc)[:200]}")
            raise
        db.finish_run(rid, "complete")
        # A kill archives its campaign and frees every mailbox on it. Troy, 2026-09-15:
        # "if a campaign gets killed ... that should free up sender accounts." Launch
        # the next held batch onto them now, not at the next two-hour tick.
        try:
            from .stages import s6_approval as _s6
            held = _s6.push_held_batches(run_id=None)
            if held:
                print("held pushes after review:", json.dumps(held, default=str)[:300])
        except Exception as exc:
            print(f"[warn] held-push retry after review failed: {exc}", flush=True)
        # Every day: give winners a shot at more sender capacity, best interested-reply
        # performer first (Troy, 2026-10-06, after 12+ VE campaigns shared a 9,000/day
        # pool with no relationship between who was thin and who actually converted).
        # Additive only -- see senders.rebalance_winners.
        try:
            from . import senders as _senders
            gained = [r for r in _senders.rebalance_winners(run_id=rid) if r.get("attached")]
            print(f"sender rebalance: {len(gained)} winner(s) gained accounts")
        except Exception as exc:
            print(f"[warn] sender rebalance failed: {exc}", flush=True)
        # Every day: fair-cap EVERY active campaign, not just winners -- pools and
        # pre-fix launches were still sitting at the generic 2,400/day default, summing
        # to 24,386/day of caps against a real ~9,000/day ceiling (Troy, 2026-10-07).
        # Cap-only, attaches nothing; see senders.fair_cap_all_active.
        try:
            corrected = [r for r in _senders.fair_cap_all_active(run_id=rid) if r.get("new_total")]
            print(f"fair-cap backfill: {len(corrected)} campaign(s) corrected")
        except Exception as exc:
            print(f"[warn] fair-cap backfill failed: {exc}", flush=True)
        # Every day: sync every campaign's real lead count from EmailBison itself, so
        # the dashboard can show it alongside our own push history instead of only the
        # number that only ever grows. See sync_bison_leads.
        try:
            synced = sync_bison_leads(run_id=rid)
            print(f"bison lead sync: {len(synced)} campaign(s) synced")
        except Exception as exc:
            print(f"[warn] bison lead sync failed: {exc}", flush=True)
        # Every day: the compact quota check (Troy, 2026-10-05: "daily reports on how
        # close we are to 15,000"). Fridays additionally get the full one-page PDF.
        try:
            from . import report
            print("daily quota check:", report.post_daily())
        except Exception as exc:
            print(f"[warn] daily quota check failed: {exc}", flush=True)
        if datetime.now(timezone.utc).weekday() == 4:
            try:
                from . import report
                print("friday report:", report.post())
            except Exception as exc:
                print(f"[warn] friday report failed: {exc}", flush=True)
        return 0
    if args.command == "client":
        from . import client as _client, modes
        if args.list:
            names = _client.available()
            if not names:
                print("no clients/ profiles found")
                return 0
            for n in names:
                pr = _client.load(n)
                gaps = ("complete" if pr.is_complete_for_sending
                        else "INCOMPLETE: " + ",".join(pr.fell_back))
                print(f"  {n:12} {pr.display_name:24} ws={str(pr.workspace_id):>4} "
                      f"mode={pr.default_mode:10} {gaps}")
            return 0

        profile, mode, ev = modes.resolve(args.client, args.mode)
        print(f"client : {profile.display_name} ({profile.name})")
        print(f"mode   : {mode}" + ("" if args.mode else "   [from the profile]"))
        if ev:
            print(f"history: {ev.campaigns} campaigns, {ev.sends:,} sends, "
                  f"{ev.reply_rate:.2%} reply rate, "
                  f"{ev.interested_of_replies:.1%} of replies interested lifetime, "
                  f"{ev.recent_interested_of_replies:.1%} recently")
            print(f"         detection says {ev.mode}: {ev.why}")
        problems = modes.preflight(profile, mode)
        if problems:
            print("")
            print("BLOCKED:")
            for x in problems:
                print(f"  - {x}")
            return 1

        # MANUAL TOP-UP. Troy, 2026-09-29: Monday's intake landed 3 batches short of
        # the 3,000 cap (missing LEADOCEAN_API_KEY silently starved the contact
        # cascade). The weekly walk only ever moves FORWARD through vertical_backlog's
        # queued rows -- a vertical that already has a batch is never revisited on
        # its own, so there was no way to ask the pipeline to try harder on a NAMED
        # one. This re-loads that vertical's own stored definition and re-enters
        # _work_vertical directly; its existing pending_batch_for/add_to_batch path
        # (see the "Top up the batch this vertical already has" comment below) adds
        # to what is already sitting in review rather than opening a second batch.
        if getattr(args, "probe", False):
            run_id = _run_id()
            db.start_run(run_id)
            items = seed_declared_backlog(run_id, only_new=False)
            db.finish_run(run_id, "complete", f"probed {len(items)} declared verticals")
            print("")
            print("RANKED DECLARED VERTICALS (projected usable = fresh companies x fit keep rate):")
            for n, it in enumerate(items, 1):
                print(f"{n:>2}. {it['vertical']:<48} usable ~{it['projected']:>5}{'+' if it['capped'] else ' '} | universe {it['universe']:>5}, fresh {it['fresh']:>5}, keep {it['keep_rate']}, {it['verdict']}")
            return 0
        if args.topup:
            unknown = [v for v in args.topup if not db.backlog_config(v)]
            if unknown:
                print("no stored definition for: " + ", ".join(unknown))
                print("(a vertical only has one once it has been sourced at least "
                      "once -- check the exact name on /live)")
                return 1
            # vertical_backlog keeps its row forever, so a LAUNCHED vertical is still
            # --topup-able by name long after it scales -- correctly, for a live
            # winner that came up short. What it must not do is silently re-source a
            # vertical this app already closed for real. That gate lives in
            # ve_campaigns, a different table, checked here rather than skipped.
            exhausted = []
            for v in args.topup:
                camp = db.ve_campaign_for_vertical(v)
                if camp and camp.get("scale_exhausted_at"):
                    exhausted.append(
                        f"{v} (closed {camp['scale_exhausted_at']:%d %b} at "
                        f"{camp.get('scale_exhausted_at_leads', 0):,} leads)")
            if exhausted:
                print("refusing -- already marked exhausted, widen the audience "
                      "first:")
                for e in exhausted:
                    print(f"  - {e}")
                return 1
            if args.plan:
                print("")
                print(f"--topup {', '.join(args.topup)} -- would re-source and add "
                      f"to each vertical's existing pending batch, if any.")
                print("--plan given, stopping before any spend.")
                return 0
            run_id = _run_id()
            db.start_run(run_id)
            db.log(run_id, "start", f"manual topup: {', '.join(args.topup)}")
            verticals = [s2_expansion.Vertical.from_json(db.backlog_config(v))
                        for v in args.topup]
            summary: dict = {"run_id": run_id, "batches": []}
            done = _work_verticals(verticals, run_id, args.dry_run, summary)
            db.finish_run(run_id, "complete" if len(done) == len(verticals) else "paused_llm",
                          f"topped up {len(done)} of {len(verticals)}")
            print("")
            print(json.dumps({**summary, "done": done}, default=str)[:600])
            return 0

        if mode == "copy-test":
            print(f"variants: {len(profile.variant_names())}")
            for name, bet in profile.copy_variants:
                print(f"  {name:22} {bet[:76]}")
        target = args.contacts or settings.contact_cap_per_vertical
        # A copy test samples FIRST by default. Its whole premise is that the message is
        # unproven, so writing thousands before anyone reads one is the expensive way to
        # discover a systematic fault -- which is what happened on 2026-09-15. `--full`
        # (or `full` in Slack) is the deliberate way past it. `expand` does not sample:
        # its copy is already proven and the batch gate shows samples before approval.
        if mode == "copy-test" and not args.sample and not args.full:
            args.sample = DEFAULT_SAMPLE
        arms: list[str] = []
        if mode == "copy-test":
            arms = ([v.strip() for v in args.variants.split(",") if v.strip()]
                    if args.variants else list(profile.variant_names())[:2])
            unknown = [a for a in arms if a not in profile.variant_names()]
            if unknown:
                print(f"unknown variant(s): {', '.join(unknown)}")
                print(f"declared: {', '.join(profile.variant_names())}")
                return 1
            if len(arms) < 2:
                print("copy-test needs two arms")
                return 1
            print(f"arms   : {' vs '.join(arms)}")
            n_aud = len(args.audience) if args.audience else args.verticals
            if args.sample:
                print(f"SAMPLE : {args.sample} per arm, then stop so you can read them"
                      + ("" if args.full else "   [default; add --full to write it all]"))
            else:
                print(f"split  : {target:,} contacts over {n_aud} audience(s), "
                      f"about {target // len(arms):,} per arm")
        if mode == "source":
            print(f"target : {target:,} contacts, no copy and no sending")

        if args.plan:
            print("")
            print("--plan given, stopping before any spend.")
            return 0

        if mode == "expand" and args.audience:
            # NAMED AUDIENCES. The weekly flow (run_pipeline) only expands from winning campaigns and ignores
            # --audience, so a client with no winners, or one whose verticals are DECLARED (ProQ, MasterNow),
            # got nothing. Here the named verticals go straight through the same per-vertical pipeline the
            # weekly run uses: waterfall company search -> fit gate -> contacts -> copy -> one batch for the
            # Slack approval. Nothing is pushed until a human approves the batch.
            run_id = _run_id()
            db.start_run(run_id)
            db.log(run_id, "start", f"named audiences: {', '.join(args.audience)}")
            from . import modes as _modes
            dc = __import__("dataclasses")
            verticals = [_modes.vertical_from_description(a, run_id, profile) for a in args.audience]
            db.enqueue_verticals(
                [{"vertical": v.name, "verdict": "ok", "projected": 1, "projected_raw": 1,
                  "density": None, "fresh_rate": None} for v in verticals],
                {v.name: dc.asdict(v) for v in verticals})
            summary = {"run_id": run_id, "batches": []}
            done = _work_verticals(verticals, run_id, args.dry_run, summary)
            db.finish_run(run_id, "complete" if len(done) == len(verticals) else "paused_llm",
                          f"worked {len(done)} of {len(verticals)} named audiences")
            print("")
            print(json.dumps({**summary, "done": done}, default=str)[:600])
            return 0
        if mode == "expand":
            print("")
            print(json.dumps(run_pipeline(dry_run=args.dry_run), default=str)[:600])
            return 0
        # A run launched from Slack has nowhere to print, so a failure has to announce
        # itself. ProductEVO's first run (2026-09-18) died on an empty backlog and left
        # only a 'running' row: eleven minutes of silence and no way to know why.
        try:
            if mode == "source":
                first = args.audience[0] if args.audience else None
                out = modes.run_source(profile, first, target, dry_run=args.dry_run,
                                       companies_only=args.companies_only)
            else:
                out = modes.run_copy_test(profile, args.audience, target, arms,
                                          dry_run=args.dry_run, verticals=args.verticals,
                                          sample=args.sample)
        except BaseException as exc:
            reason = str(exc) or exc.__class__.__name__
            print(f"run failed: {reason}", flush=True)
            try:
                from .clients import slack as _slack
                _slack.post(f":x: *{profile.display_name}* — the `{mode}` run stopped.\n"
                            f"```{reason[:600]}```")
            except Exception:
                pass
            try:
                db.close_abandoned_runs(older_than_hours=0)
            except Exception:
                pass
            raise
        print("")
        print(json.dumps(out, default=str)[:900])
        return 0

    if args.command == "preview":
        from . import client as _client, modes
        from .clients import slack as _slack
        profile = _client.load(args.client) if args.client else _client.profile()
        out = modes.preview(profile, args.arm, args.email)
        print(json.dumps(out, default=str)[:1200])
        if out.get("error") or out.get("rejected"):
            _slack.post(f"Preview for *{profile.display_name}* could not be written: "
                        f"{out.get('error') or out.get('rejected')}")
            return 1
        viol = out.get("rule_violations") or []
        head = (f"*Preview — {profile.display_name}* · arm `{out['arm']}` · prompts "
                f"`{out['prompt_version']}`\n"
                f"{out['company']} · {out['title']} · {out['contact']}\n"
                f"email 1 {out['email1_chars']} chars · email 2 {out['email2_chars']} "
                f"chars · reading grade {out['reading_grade']}")
        if viol:
            head += "\n:warning: " + "; ".join(viol[:3])
        _slack.post(head, blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": head}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": "*Email 1*\n```" + (out["email1"] or "")[:2800] + "```"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": "*Email 2*\n```" + (out["email2"] or "")[:2800] + "```"}},
        ])
        return 0
    if args.command == "report":
        from . import report
        data = report.gather()
        if args.out:
            Path(args.out).write_bytes(report.render_pdf(data))
            print(f"wrote {args.out}")
        if args.post:
            print(json.dumps(report.post(data), default=str))
        if not args.out and not args.post:
            print(report.as_text(data))
        return 0
    if args.command == "align-senders":
        from . import senders
        r = senders.align(args.campaign_id, run_id=None, dry_run=args.dry_run)
        print(json.dumps(r, default=str))
        return 0
    if args.command in ("review-all", "report-all", "weekly-all"):
        # Every scheduled job used to run as one client, and no CLIENT was set on
        # Railway, so all of them ran as IDPR: a second client was sourced and swept but
        # never judged, never reported on, and never expanded (found 2026-09-19).
        import subprocess
        from . import client as _client
        step = {"review-all": ["review"], "report-all": ["report", "--post"],
                "weekly-all": ["run"]}[args.command]
        for name in _client.available():
            profile = _client.load(name)
            if args.command == "weekly-all" and profile.default_mode != "expand":
                print(f"[{args.command}] {name}: mode is {profile.default_mode}, "
                      f"not expand -- a weekly audience run would be the wrong job",
                      flush=True)
                continue
            env = dict(os.environ)
            env["CLIENT"] = name
            env["PYTHONIOENCODING"] = "utf-8"
            print(f"[{args.command}] {name}", flush=True)
            try:
                subprocess.run([sys.executable, "-m", "app.runner", *step],
                               env=env, timeout=14400)
            except Exception as exc:
                print(f"[{args.command}] {name} failed: {exc}", flush=True)
        return 0

    if args.command == "conclude":
        from . import client as _client, conclude as _conclude, db as _db
        from .clients import slack as _slack
        profile = _client.load(args.client) if args.client else _client.profile()
        res = _conclude.evaluate(args.experiment)
        print(json.dumps({"status": res.status, "winner": res.winner,
                          "reason": res.reason, "detail": res.detail}, default=str)[:900])
        if args.promote and res.decided:
            print(_conclude.promote(profile.name, res.winner, by="cli",
                                    evidence=res.reason))
        try:
            _slack.copy_test_result(profile.display_name, profile.name, res)
        except Exception as exc:
            print(f"[warn] result not posted: {exc}", flush=True)
        return 0 if res.status != "no_data" else 1

    if args.command == "calibrate":
        from . import client as _client, modes
        profile = _client.load(args.client) if args.client else _client.profile()
        print(json.dumps(modes.calibrate(profile), default=str))
        return 0

    if args.command == "resume-all":
        # One process per client. The cron container is loaded as a single client, so it
        # cannot resume another's work in-process without pointing one client's prompts
        # at another's database.
        import subprocess
        from . import client as _client
        for name in _client.available():
            env = dict(os.environ)
            env["CLIENT"] = name
            print(f"[resume-all] {name}", flush=True)
            # The reply sweep is per client too: it reads that client's campaigns from
            # that client's database, so running it once for everyone would sweep one
            # workspace and miss the rest.
            for step in ("stop-on-reply", "resume"):
                try:
                    subprocess.run([sys.executable, "-m", "app.runner", step],
                                   env=env, timeout=7200)
                except Exception as exc:
                    print(f"[resume-all] {name} {step} failed: {exc}", flush=True)
        return 0
    if args.command == "resume":
        if not settings.pipeline_enabled:
            print("PIPELINE_ENABLED is false; skipping resume.")
            return 0
        # Approved batches waiting on sender capacity (or a fixed load error) first:
        # campaigns finishing since the last tick may have freed their accounts.
        try:
            from .stages import s6_approval
            held = s6_approval.push_held_batches(run_id=None)
            if held:
                print("held pushes:", json.dumps(held, default=str)[:300])
        except Exception as exc:
            print(f"[warn] held-push retry failed: {exc}", flush=True)
        print(json.dumps(resume(), default=str)[:400])
        return 0
    if args.command == "stop-on-reply":
        for r in stop_on_reply.sweep_all():
            print(f"  campaign {r['campaign_id']}: {r['replied_companies']} replied, "
                  f"{r['siblings_removed']} siblings removed")
        # Troy, 2026-10-01: EmailBison's own 'Meeting Booked' tag counts as a booked call.
        # Rides the same two-hourly tick as the reply sweep above.
        try:
            bk = stop_on_reply.sync_booked_from_tags()
            if bk.get("newly_booked"):
                print(f"  booked-tag sync: {bk}")
        except Exception as exc:
            print(f"[warn] booked-tag sync failed: {exc}", flush=True)
        return 0
    if args.command == "expire":
        print("auto-killed:", db.expire_stale_batches() or "none")
        return 0
    if args.command == "status":
        for batch in db.pending_batches():
            print(f"{batch['batch_id']:24} {batch['vertical']:22} "
                  f"{batch['contact_count']:5} contacts  expires {batch['expires_at']}")
        return 0
    if args.command == "run":
        result = run_pipeline(dry_run=args.dry_run)
        print("\n=== RUN SUMMARY ===")
        print(f"status: {result.get('status')}")
        for batch in result.get("batches", []):
            print(f"  {batch['vertical']:24} companies={batch.get('companies')} "
                  f"contacts={batch.get('contacts')} batch={batch.get('batch_id')}")
        if result.get("reason"):
            print("reason:", result["reason"])
        return 0 if result.get("status") in {"complete", "no_winners", "no_data",
                                             "no_verticals", "no_supply", "halted"} else 1
    if args.command == "push":
        result = s7_push.push(args.batch_id, campaign_id=args.campaign,
                              live=args.live, out_dir=args.out_dir)
        print(result)
        return 0
    if args.command == "confirm-csv":
        print(s7_push.confirm_csv(args.batch_id))
        return 0
    if args.command == "backfill":
        from scripts.backfill_from_api import backfill
        totals = backfill(args.max_pages, 0.05, args.restart)
        print(json.dumps(totals, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
