"""The test run and the scale-up, executed BY THE WEB SERVICE (Emmanuel, 2026-10-15: "it should run on web, not locally"; 2026-10-16: copy approved,
scale to 1,500 leads today).

One loop, every step resumable, every cycle posted to the client's Slack channel:
  1. source    find NEW companies for every declared vertical (modes.run_source, companies only). With skip_source_gate the vendor pull goes straight on.
  2. fit       the website fit check (gpt-oss, same judge as the fill) on every company BEFORE any contact is searched for
  3. contacts  find and verify people at the fit companies, in chunks, until the bank holds SCALE_BANK_PER_CYCLE usable contacts
  4. fill      wave1_fill.fill(): research and copy for the bank, then load leads into each angle's DRAFT campaign up to the [fill] target
Cycles repeat until SCALE_TARGET_LEADS leads sit in the campaigns, supply runs out, or SCALE_MAX_HOURS passes. When the pool of companies is empty it
sources another, bigger round (SCALE_SOURCE_N contacts' worth per vertical, doubling). State lives in testrun_state, so a redeploy or crash continues.
It starts at boot only when TESTRUN_ENABLED=true and PIPELINE_ENABLED is on. Nothing here attaches senders or starts a campaign.
"""
from __future__ import annotations

import json
import os
import time

from . import db
from .clients import slack

LOCK = "testrun"
STALE_MIN = 90


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
    with db.cursor() as cur:
        cur.execute("""INSERT INTO testrun_state (id, state) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()""",
                    (json.dumps(state),))


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


def _loaded() -> int:
    """Leads sitting in the angle campaigns (this database's own record of what the fill loaded)."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM contacts WHERE status = 'pushed' AND eb_campaign_id IN (SELECT campaign_id FROM copy_test_campaigns)")
        return int(cur.fetchone()["n"])


def _source_round(prof, st: dict, holder: str, n: int) -> int:
    """One sourcing round over every declared vertical. Returns the number of new fit-pending companies staged."""
    from . import modes
    before = _staged()
    _say(f"sourcing round {st.get('source_round', 0) + 1}: about {n} contacts' worth of NEW companies for each of {len(prof.verticals)} verticals.")
    for i, v in enumerate(prof.verticals, 1):
        _beat(holder)
        try:
            modes.run_source(prof, v, n, companies_only=True)
        except Exception as exc:
            st.setdefault("errors", []).append(f"{v}: {str(exc)[:120]}")
        _save(st)
        if i % 6 == 0:
            _say(f"  sourcing {i}/{len(prof.verticals)} verticals, {_staged() - before} new companies so far.")
    st["source_round"] = st.get("source_round", 0) + 1
    added = _staged() - before
    _say(f"sourcing round done: {added} new companies staged.")
    return added


def _staged() -> int:
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM staged_companies WHERE verdict = 'KEEP' AND consumed_at IS NULL")
        return int(cur.fetchone()["n"])


def _contact_chunks(prof, st: dict, holder: str, run_id: str, want: int) -> int:
    """Fit-check then find contacts, chunk by chunk, until `want` usable contacts are banked or no companies are left. Returns chunks run."""
    from . import company_fit, scaling
    chunks = 0
    while _bank() < want:
        _beat(holder)
        companies = scaling.pick_companies(list(prof.verticals), 200)
        if not companies:
            break
        verdicts = company_fit.check([{"domain": c["domain"], "name": c.get("name") or ""} for c in companies], workers=12)
        passing = [c for c in companies if (verdicts.get(c["domain"]) or {}).get("fit") in company_fit.PASS]
        st["fit_checked"] = st.get("fit_checked", 0) + len(companies)
        st["fit_passed"] = st.get("fit_passed", 0) + len(passing)
        if passing:
            scaling.source_contacts(passing, run_id)
            chunks += 1
        _save(st)
        _say(f"fit check on {len(companies)} companies: {len(passing)} passed; contact finding done for them; {_bank()} usable contacts in the bank (cycle goal {want}).")
    return chunks


def run() -> dict:
    from . import client, wave1_fill
    from .runner import _run_id

    holder = f"testrun-{int(time.time())}"
    if not _lock(holder):
        return {"status": "already_running"}
    try:
        prof = client.profile()
        target_total = int(os.getenv("SCALE_TARGET_LEADS", "1500"))
        bank_per_cycle = int(os.getenv("SCALE_BANK_PER_CYCLE", "450"))
        source_n = int(os.getenv("SCALE_SOURCE_N", "150"))
        deadline = time.time() + float(os.getenv("SCALE_MAX_HOURS", "14")) * 3600
        st = _load()
        if st.get("done"):
            return {"status": "done"}
        st.setdefault("source_round", 1 if st.get("sourced") else 0)
        run_id = _run_id()
        db.start_run(run_id)
        _say(f"{prof.display_name} scale-up started: goal {target_total} leads across the draft campaigns, {_loaded()} loaded so far, {_bank()} usable contacts banked.")
        stalls = 0
        while time.time() < deadline:
            _beat(holder)
            loaded = _loaded()
            if loaded >= target_total:
                break
            if _contact_chunks(prof, st, holder, run_id, bank_per_cycle) == 0 and _bank() == 0:
                # no companies left to search: source another, bigger round
                n = source_n * (2 ** max(st["source_round"] - 1, 0)) if st["source_round"] > 1 else source_n
                added = _source_round(prof, st, holder, n)
                if added < 150:
                    stalls += 1
                    if stalls >= 2:
                        _say(f"supply is running out: {loaded} leads loaded, the last sourcing rounds added only {added} companies.")
                        break
                continue
            _beat(holder)
            _say(f"cycle: {_bank()} usable contacts banked, {loaded} of {target_total} leads loaded. Writing copy and loading drafts.")
            out = wave1_fill.fill()
            if (out or {}).get("status") == "already_running":     # the 2-hourly resume holds the fill lock: wait, do not count it as a stall
                time.sleep(60)
                continue
            st["fill"] = {k: v for k, v in (out or {}).items() if k in ("status", "counts", "pulled")}
            now = _loaded()
            st["cycles"] = st.get("cycles", 0) + 1
            _save(st)
            _say(f"cycle {st['cycles']} done: {now} of {target_total} leads loaded ({now - loaded} added). Per campaign: {st['fill'].get('counts')}.")
            if now - loaded < 10:
                stalls += 1
                if stalls >= 3:
                    _say(f"progress has stalled at {now} leads (three cycles added almost nothing). Stopping so you can look.")
                    break
            else:
                stalls = 0
        final = _loaded()
        st["done"] = final >= target_total
        st["final_loaded"] = final
        _save(st)
        _say(f"scale-up {'reached the goal' if st['done'] else 'stopped'}: {final} of {target_total} leads loaded into the draft campaigns. No senders attached; nothing sends.")
        return {"status": "done" if st["done"] else "stopped", "state": st}
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
