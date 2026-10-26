"""The test run and the scale-up, executed BY THE WEB SERVICE (Emmanuel, 2026-10-15: "it should run on web, not locally"; 2026-10-16: copy approved,
scale to 1,500 leads PER ANGLE today, and copy must run at the same time as contact finding).

Two workers run side by side inside the service, both posting to the client's Slack channel:
  PRODUCER  source companies (modes.run_source, companies only) -> find contacts at them (stage 4) -> the website fit check on the companies that HAVE
            contacts, before any email is verified (fit_before_verify) -> verified contacts land in the bank. It keeps the bank topped up to SCALE_BANK_CAP.
  CONSUMER  whenever the bank holds a batch, wave1_fill.fill() writes research and copy and loads the leads into each angle's DRAFT campaign.
They stop when SCALE_TARGET_LEADS leads sit in the campaigns, supply runs out, progress stalls, or SCALE_MAX_HOURS passes. State lives in testrun_state, so a
redeploy or crash continues. It starts at boot only when TESTRUN_ENABLED=true and PIPELINE_ENABLED is on. Nothing here attaches senders or starts a campaign.
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import db
from .clients import slack

LOCK = "testrun"
STALE_MIN = 90
_st_lock = threading.Lock()


def _ensure() -> None:
    with db.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS testrun_state (id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1), state JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT now())")


def _load() -> dict:
    _ensure()
    with db.cursor() as cur:
        cur.execute("SELECT state FROM testrun_state WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return {}
    return row["state"] if isinstance(row["state"], dict) else json.loads(row["state"])


def _save(state: dict) -> None:
    with _st_lock:
        payload = json.dumps(state)
    with db.cursor() as cur:
        cur.execute("""INSERT INTO testrun_state (id, state) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()""", (payload,))


def _bump(state: dict, **inc: int) -> None:
    with _st_lock:
        for k, v in inc.items():
            state[k] = state.get(k, 0) + v


def _set(state: dict, **kv) -> None:
    with _st_lock:
        state.update(kv)


def _lock(holder: str) -> bool:
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS job_locks (name TEXT PRIMARY KEY, holder TEXT, heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""INSERT INTO job_locks (name, holder, heartbeat_at) VALUES (%s, %s, now())
                       ON CONFLICT (name) DO UPDATE SET holder = EXCLUDED.holder, heartbeat_at = now()
                       WHERE job_locks.heartbeat_at < now() - make_interval(mins => %s) RETURNING holder""", (LOCK, holder, STALE_MIN))
        return cur.fetchone() is not None


def _beat(holder: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE job_locks SET heartbeat_at = now() WHERE name = %s AND holder = %s", (LOCK, holder))


def _release(holder: str) -> None:
    with db.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name = %s AND holder = %s", (LOCK, holder))


def _say(text: str) -> None:
    print(f"[testrun] {text}", flush=True)
    try:
        slack.post(f":gear: {text}")
    except Exception:
        pass


def _bank() -> int:
    with db.cursor() as cur:
        # Contacts at companies the website fit check rejected are not supply: the fill will never use them.
        cur.execute("""SELECT count(*) n FROM contacts c LEFT JOIN company_funnel f ON f.domain = c.company_domain
                       WHERE c.status = 'verified' AND c.eb_campaign_id IS NULL AND coalesce(c.first_name, '') <> ''
                         AND coalesce(c.personalization_1, '') = '' AND coalesce(f.fit, '') NOT IN ('LOW', 'NOT_FIT')""")
        return int(cur.fetchone()["n"])


def sourcing_only() -> bool:
    return os.getenv("SOURCING_ONLY", "").strip().lower() in ("1", "true", "yes")


def sourcing_target() -> int:
    return int(os.getenv("SOURCING_TARGET", "1000"))


def _loaded() -> int:
    """Leads sitting in the angle campaigns (this database's own record of what the fill loaded)."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM contacts WHERE status = 'pushed' AND eb_campaign_id IN (SELECT campaign_id FROM copy_test_campaigns)")
        return int(cur.fetchone()["n"])


def _staged() -> int:
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM staged_companies WHERE verdict = 'KEEP' AND consumed_at IS NULL AND found_at IS NULL")
        return int(cur.fetchone()["n"])


def _source_round(prof, st: dict, n: int) -> int:
    """One sourcing round over every declared vertical. Returns the number of new companies staged."""
    from . import modes
    before = _staged()
    rnd = st.get("source_round", 0) + 1
    _say(f"sourcing round {rnd}: about {n} contacts' worth of NEW companies for each of {len(prof.verticals)} verticals.")
    for i, v in enumerate(prof.verticals, 1):
        try:
            modes.run_source(prof, v, n, companies_only=True)
        except Exception as exc:
            with _st_lock:
                st.setdefault("errors", []).append(f"{v}: {str(exc)[:120]}")
        _save(st)
        if i % 6 == 0:
            _say(f"  sourcing {i}/{len(prof.verticals)} verticals, {_staged() - before} new companies so far.")
    _set(st, source_round=rnd)
    # Every run_source call tiers new companies, but funnel.refresh skips itself inside a five-minute window, so companies staged late in a round never got a
    # tier row and the contact search could not see them: 10,792 of 14,881 stored companies on 2026-10-22. One forced pass closes the round.
    try:
        from . import funnel
        funnel.refresh(force=True)
    except Exception as exc:
        _say(f"tiering the new companies failed: {str(exc)[:140]}; they will not be searchable until it runs.")
    added = _staged() - before
    _say(f"sourcing round {rnd} done: {added} new companies staged.")
    return added


_claimed: set[str] = set()
_claim_lock = threading.Lock()


def _claim(prof, n: int) -> list[dict]:
    """Companies for one chunk. A company is only marked searched when its chunk finishes, so several chunks running side by side would pick the same
    ones and pay twice; claimed domains are kept here until their chunk is done."""
    from . import scaling
    with _claim_lock:
        picked = scaling.pick_companies(list(prof.verticals), n + len(_claimed))
        fresh = [c for c in picked if c["domain"] not in _claimed][:n]
        _claimed.update(c["domain"] for c in fresh)
    return fresh


def _claim_peek(prof) -> bool:
    """True when at least one unclaimed company is waiting to be searched."""
    from . import scaling
    with _claim_lock:
        return any(c["domain"] not in _claimed for c in scaling.pick_companies(list(prof.verticals), 1 + len(_claimed)))


_trips = {"count": 0, "until": 0.0}
_verifier_state = {"alerted": False}


def _verifier_blocked() -> bool:
    """Contact search waits while email verification is down (MillionVerifier out of credits, 2026-10-26), instead of searching companies it cannot verify.
    One Slack line when it goes down and one when it is back; the check is cached for five minutes."""
    from . import sourcing_health
    ok, text = sourcing_health.verifier_ok()
    if ok:
        if _verifier_state["alerted"]:
            _verifier_state["alerted"] = False
            _say("Email verification is working again; contact search resumes.")
        return False
    if not _verifier_state["alerted"]:
        _verifier_state["alerted"] = True
        _say(f":warning: Email verification is down: {text}. Contact search is paused so no companies are used up. It resumes by itself once verification works.")
    return True


def _paused() -> bool:
    return time.time() < _trips["until"]


def _trip(companies: list[dict], found: int, floor: float) -> None:
    """A chunk found far fewer contacts than normal. Un-mark its companies that have no contacts so they are searched again, pause the producer
    (15 minutes, doubling each time up to 4 hours), and alert Slack with the provider probe."""
    domains = [c["domain"] for c in companies]
    with db.cursor() as cur:
        cur.execute("""UPDATE staged_companies s SET found_at = NULL WHERE s.domain = ANY(%s)
                       AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.company_domain = s.domain)""", (domains,))
    with _claim_lock:
        if _paused():
            return                       # a sibling chunk already tripped the breaker and alerted
        _trips["count"] += 1
        minutes = min(15 * 2 ** (_trips["count"] - 1), 240)
        _trips["until"] = time.time() + minutes * 60
        count = _trips["count"]
    try:
        from . import sourcing_health
        probe = sourcing_health.probe(force=True)
    except Exception as exc:
        probe = f"probe failed: {str(exc)[:100]}"
    _say(f":rotating_light: Contact search yield collapsed: {found} of {len(companies)} companies gave contacts, where the floor is {floor:.0%}. The companies were put back, "
         f"so nothing is lost. Paused {minutes} minutes (trip {count}). Provider check: {probe}")


def _one_chunk(prof, st: dict, run_id: str, n: int = 200) -> int:
    """Search up to n staged companies for contacts. Fit-before-verify runs inside stage 4. Returns how many companies were searched (0 = none left,
    -1 = the yield breaker tripped and the chunk was put back)."""
    from . import company_fit, scaling
    companies = _claim(prof, n)
    if not companies:
        return 0
    try:
        people = scaling.source_contacts(companies, run_id)
        found = {(p.get("company_domain") or p.get("domain")) for p in people}
        with_contacts = [c for c in companies if c["domain"] in found]
        verdicts = company_fit.check([{"domain": c["domain"], "name": c.get("name") or ""} for c in with_contacts], workers=12) if with_contacts else {}
        passing = [c for c in with_contacts if (verdicts.get(c["domain"]) or {}).get("fit") in company_fit.PASS]
    finally:
        with _claim_lock:
            _claimed.difference_update(c["domain"] for c in companies)
    # The breaker. 2026-10-23 21:00 to 10-24 10:00 UTC roughly 11,000 companies were marked searched with almost no contacts found (5 per 1,500) because a finder
    # answered with nothing and the other one's key was rejected, and no alarm fired. A chunk that yields far below the normal rate is a provider problem, not
    # 250 companies with no contacts: put the companies back, stop, say so, and try again later.
    floor = float(os.getenv("SCALE_MIN_YIELD", "0.04"))
    if len(companies) >= 100 and len(with_contacts) / len(companies) < floor:
        _trip(companies, len(with_contacts), floor)
        return -1
    _trips["count"] = 0
    _bump(st, searched=len(companies), with_contacts=len(with_contacts), fit_passed=len(passing), chunks=1)
    _save(st)
    line = (f"contacts: {len(companies)} companies searched, {len(with_contacts)} had contacts that passed the fit check (judged before verification); "
            f"{_bank()} usable contacts in the bank.")
    if st.get("chunks", 0) % 4 == 0:
        _say(line)              # several chunks run at once now: Slack gets every fourth, the log gets all
    else:
        print(f"[testrun] {line}", flush=True)
    return len(companies)


def run() -> dict:
    from . import client, materials_fill, wave1_fill
    from .runner import _run_id

    # MATERIALS_CAMPAIGN_ID set: the same producer keeps the bank full, but the consumer loads the OG-copy campaign (no research, no email writing)
    # instead of writing copy for the three angle campaigns. Goal and done flag are the campaign's own.
    # SOURCING_ONLY=true: no consumer at all. The producer fills the bank to SOURCING_TARGET ready contacts and the job stops (Emmanuel, 2026-10-26: the
    # sourcing stage should work for any client, with or without a copy stage attached).
    only = sourcing_only()
    mat = materials_fill.enabled() and not only
    done_key = "sourcing_done" if only else ("materials_done" if mat else "done")
    loaded_now = _bank if only else (materials_fill.loaded if mat else _loaded)

    holder = f"testrun-{int(time.time())}"
    if not _lock(holder):
        return {"status": "already_running"}
    try:
        prof = client.profile()
        fixed_target = int(os.getenv("SCALE_TARGET_LEADS", "4500"))
        target_now = sourcing_target if only else (materials_fill.target if mat else (lambda: fixed_target))      # a Slack button can raise a campaign's goal mid-run
        target_total = target_now()
        bank_cap = sourcing_target() if only else int(os.getenv("SCALE_BANK_CAP", "900"))
        min_batch = int(os.getenv("SCALE_MIN_BATCH", "60"))
        source_n = int(os.getenv("SCALE_SOURCE_N", "150"))
        deadline = time.time() + float(os.getenv("SCALE_MAX_HOURS", "20")) * 3600
        st = _load()
        if (_bank() >= sourcing_target() if only else (materials_fill.all_done() if mat else st.get(done_key))):
            return {"status": "done"}
        st.setdefault("source_round", 1 if st.get("sourced") else 0)
        run_id = _run_id()
        db.start_run(run_id)
        stop = threading.Event()
        flags = {"supply_done": False}
        if only:
            _say(f"{prof.display_name} sourcing started: goal {target_total:,} ready contacts in the bank, {_bank():,} there now. Nothing is loaded into any campaign and no copy is written.")
        elif mat:
            _say(f"{prof.display_name} OG-copy fill started: goal {target_total} leads across campaign(s) {materials_fill.label()}, {loaded_now()} loaded so far, {_bank()} usable contacts banked. "
                 f"Contact finding keeps the bank full; each batch gets its COMPANY and MATERIALS_HAULED values and loads.")
        else:
            _say(f"{prof.display_name} scale-up started: goal {target_total} leads across the draft campaigns, {_loaded()} loaded so far, {_bank()} usable contacts banked. "
                 f"Contact finding and copy now run side by side.")

        # Emmanuel, 2026-10-22: "we can search more companies faster... send more companies through." One chunk of 200 took about 18 minutes, so contact
        # finding was the slow side. SCALE_PRODUCER_THREADS chunks of SCALE_CHUNK companies now run side by side (defaults 4 x 250).
        threads_n = max(int(os.getenv("SCALE_PRODUCER_THREADS", "4")), 1)
        chunk_n = int(os.getenv("SCALE_CHUNK", "250"))
        src_lock = threading.Lock()
        shared = {"stalls": 0, "alive": threads_n}

        def producer() -> None:
            try:
                while not stop.is_set() and time.time() < deadline and loaded_now() < target_now():
                    if _bank() >= bank_cap or _paused():
                        time.sleep(20)
                        continue
                    if _verifier_blocked():
                        time.sleep(60)
                        continue
                    if _one_chunk(prof, st, run_id, chunk_n) != 0:
                        continue
                    # no companies left to search: one worker at a time sources another, bigger round
                    with src_lock:
                        if _claim_peek(prof):
                            continue                       # another worker sourced while this one waited
                        rnd = st.get("source_round", 1)
                        n = source_n * (2 ** max(rnd - 1, 0)) if rnd > 1 else source_n
                        added = _source_round(prof, st, n)
                        if added < 150:
                            shared["stalls"] += 1
                            if shared["stalls"] >= 2:
                                _say(f"supply is running out: the last sourcing rounds added only {added} companies.")
                                break
                        else:
                            shared["stalls"] = 0
            except Exception as exc:
                _say(f"contact finding stopped on an error: {str(exc)[:160]}. Loading keeps working through the bank.")
            finally:
                with src_lock:
                    shared["alive"] -= 1
                    if shared["alive"] <= 0:
                        flags["supply_done"] = True

        workers = [threading.Thread(target=producer, daemon=True) for _ in range(threads_n)]
        for w in workers:
            w.start()
        t = workers[0]

        low_cycles = 0
        while time.time() < deadline:
            _beat(holder)
            loaded = loaded_now()
            if loaded >= target_now():
                break
            bank = _bank()
            if only:
                if flags["supply_done"]:
                    break
                time.sleep(30)
                continue
            if bank >= min_batch or (flags["supply_done"] and bank > 0):
                out = materials_fill.run_pass() if mat else wave1_fill.fill()
                if (out or {}).get("status") == "already_running":     # the 2-hourly resume holds the fill lock: wait, do not count it
                    time.sleep(60)
                    continue
                now = loaded_now()
                _set(st, fill={k: v for k, v in (out or {}).items() if k in ("status", "counts", "pulled", "loaded_now", "held_unclear", "held_standard", "unclear_pct")})
                _bump(st, cycles=1)
                _save(st)
                try:
                    from . import runway
                    if mat:
                        _say(f"load cycle {st.get('cycles')} done: +{now - loaded}, {now:,} of {target_now():,} across campaign(s) {materials_fill.label()}. Held this pass: "
                             f"{(out or {}).get('held_unclear', 0)} unclear, {(out or {}).get('held_standard', 0)} failed the field standards. Bank {_bank():,}.\n"
                             "Samples (company | hauls): " + "; ".join(f"{a} | {b}" for a, b in ((out or {}).get("samples") or [])[:6]))
                    else:
                        _say(f"copy cycle {st.get('cycles')} done (+{now - loaded}).\n" + runway.report())
                except Exception:
                    _say(f"copy cycle {st.get('cycles')}: {now} of {target_total} leads loaded ({now - loaded} added). Bank: {_bank()}.")
                low_cycles = low_cycles + 1 if now - loaded < 10 else 0
                if low_cycles >= 4:
                    # It used to STOP here (2026-10-16, 20:39 UTC) and nobody restarted it. Now it says so, waits, and keeps trying: the producer keeps
                    # finding contacts, so a stall that clears by itself (a provider back, a fix deployed) is picked up without anyone restarting anything.
                    _say(f"progress is slow: {now} leads loaded, four copy cycles added almost nothing. Bank {_bank()}. Still running; trying again in 10 minutes.")
                    time.sleep(600)
                    low_cycles = 2
            elif flags["supply_done"]:
                break          # nothing banked and nothing left to find
            else:
                time.sleep(30)
        stop.set()
        for w in workers:
            w.join(timeout=60)
        target_total = target_now()
        final = loaded_now()
        _set(st, **{done_key: final >= target_total, "final_loaded": final})
        _save(st)
        if only:
            _say(f"sourcing {'reached the goal' if st[done_key] else 'stopped'}: {final:,} of {target_total:,} ready contacts in the bank.")
        else:
            _say(f"scale-up {'reached the goal' if st[done_key] else 'stopped'}: {final} of {target_total} leads loaded into the draft campaign(s). No senders attached; nothing sends.")
        return {"status": "done" if st[done_key] else "stopped", "state": st}
    finally:
        _release(holder)


def run_safe() -> None:
    try:
        out = run()
        print(f"[testrun] {out.get('status')}", flush=True)
    except Exception as exc:
        print(f"[testrun] failed: {exc}", flush=True)
        try:
            slack.post(f":warning: scale-up stopped: {str(exc)[:200]}. It resumes on the next deploy or restart.")
        except Exception:
            pass


_thread: threading.Thread | None = None


def ensure_running() -> str:
    """Watchdog: start the scale-up if it is not running and not finished. Called at boot and every few minutes by the web service, so a stop (a stall
    guard, a crash, a restart) never leaves the system idle with companies still waiting."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return "running"
    try:
        st = _load()
    except Exception:
        return "state unreadable"
    from . import materials_fill
    if (_bank() >= sourcing_target() if sourcing_only() else (materials_fill.all_done() if materials_fill.enabled() else st.get("done"))):
        return "done"
    _thread = threading.Thread(target=run_safe, daemon=True)
    _thread.start()
    return "started"
