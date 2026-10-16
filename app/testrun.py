"""The first test run, executed BY THE WEB SERVICE (Emmanuel, 2026-10-15: "it should run on web, not locally").

Three steps, each resumable, each posting to the client's Slack channel:
  1. source   find NEW companies for every declared vertical (modes.run_source, companies only: QuickEnrich and Lead Ocean search, then the fit judge)
  2. contacts find and verify people at the fit companies, in chunks, until the bank holds TESTRUN_BANK_TARGET contacts
  3. fill     wave1_fill.fill(): research and copy for the bank, then load up to [fill] target leads into each angle's DRAFT campaign (no senders, nothing sends)
State lives in the testrun_state table, so a redeploy or crash continues where it stopped. It starts at boot only when TESTRUN_ENABLED=true and the
pipeline kill switch (PIPELINE_ENABLED) is on. Nothing here attaches senders or starts a campaign.
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
    return (row["state"] if row and isinstance(row["state"], dict) else json.loads(row["state"])) if row else {}


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


def run() -> dict:
    from . import client, modes, scaling, wave1_fill
    from .runner import _run_id

    holder = f"testrun-{int(time.time())}"
    if not _lock(holder):
        return {"status": "already_running"}
    try:
        prof = client.profile()
        per_v = int(os.getenv("TESTRUN_COMPANIES_PER_VERTICAL", "60"))
        bank_target = int(os.getenv("TESTRUN_BANK_TARGET", "60"))
        st = _load()
        if st.get("done"):
            return {"status": "done"}
        st.setdefault("sourced", [])
        verticals = [v for v in prof.verticals]
        todo = [v for v in verticals if v not in st["sourced"]]
        if todo:
            _say(f"{prof.display_name} test run: sourcing new companies for {len(todo)} of {len(verticals)} verticals ({per_v} contacts' worth each), fit judged by gpt-oss.")
        for i, v in enumerate(todo, 1):
            _beat(holder)
            try:
                res = modes.run_source(prof, v, per_v, companies_only=True)
                st["sourced"].append(v)
                st.setdefault("by_vertical", {})[v] = res.get("companies", 0)
            except Exception as exc:
                st.setdefault("errors", []).append(f"{v}: {str(exc)[:120]}")
            _save(st)
            if i % 4 == 0 or i == len(todo):
                _say(f"sourcing {len(st['sourced'])}/{len(verticals)} verticals done, {sum(st.get('by_vertical', {}).values())} fit companies so far.")
        # contacts
        run_id = _run_id()
        db.start_run(run_id)
        chunks = 0
        while _bank() < bank_target and chunks < 15:
            _beat(holder)
            companies = scaling.pick_companies(verticals, 150)
            if not companies:
                break
            # The real fit check (website text, same judge as the fill) BEFORE paying for contact finding: the sourcing gate reads only the vendor
            # description and kept about a third of companies that fail on their own site (measured 2026-10-15: 26 of 84 contacts).
            from . import company_fit
            verdicts = company_fit.check([{"domain": c["domain"], "name": c.get("name") or ""} for c in companies], workers=6)
            companies = [c for c in companies if (verdicts.get(c["domain"]) or {}).get("fit") in company_fit.PASS]
            if not companies:
                continue
            scaling.source_contacts(companies, run_id)
            chunks += 1
            st["contact_chunks"] = st.get("contact_chunks", 0) + 1
            _save(st)
            _say(f"contact finding: chunk {chunks} done ({len(companies)} companies searched), {_bank()} contacts in the bank (target {bank_target}).")
        st["bank"] = _bank()
        _save(st)
        if st["bank"] == 0:
            _say("no contacts were found at the sourced companies; stopping before the fill. Needs more companies or another contact source.")
            return {"status": "no_contacts", "state": st}
        # fill (copy + load into the draft campaigns)
        _beat(holder)
        _say(f"bank has {st['bank']} contacts. Writing copy and loading up to the per-angle target into the draft campaigns.")
        out = wave1_fill.fill()
        st["fill"] = {k: v for k, v in (out or {}).items() if k in ("status", "counts", "pulled")}
        st["done"] = True
        _save(st)
        _say(f"test run finished: fill {st['fill']}")
        return {"status": "done", "state": st}
    finally:
        _release(holder)


def run_safe() -> None:
    try:
        out = run()
        print(f"[testrun] {out.get('status')}", flush=True)
    except Exception as exc:
        print(f"[testrun] failed: {exc}", flush=True)
        try:
            slack.post(f":warning: test run stopped: {str(exc)[:200]}. It resumes on the next deploy or restart.")
        except Exception:
            pass
