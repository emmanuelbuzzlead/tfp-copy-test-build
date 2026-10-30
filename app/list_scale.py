"""The quick-run lists (campaigns 2210 BM, 2211 HG, 2212 CN, 2213 BR) scaled to their lead targets, inside the web service (Emmanuel, 2026-10-27).

Leads only, no copy to write: source contacts at companies in each list's verticals (employee floor is the client's 100), enrich each company (app/list_enrich.py:
city, product, plant count), and load the usable ones, up to 4 people per company. The job is resumable (a watchdog restarts it after a deploy; every contact is saved the
moment it is found), pauses while email verification is down, and stops a list when its yield collapses or its stored companies run out and new sourcing finds none.
Progress shows on the live page ("Quick-run lists") and in Slack.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import db, list_enrich
from .clients import slack

LOCK = "list_scale"
STALE_MIN = 10
CHUNK = int(os.getenv("LIST_SCALE_CHUNK", "100"))                 # companies per search round
WORKERS = int(os.getenv("LIST_SCALE_WORKERS", "2"))                # search rounds running at once per list
MAX_PER_COMPANY = 4
PASS_RATE = 0.85                                                  # share of held contacts that end up loaded (measured 0.85 to 0.9)
MIN_YIELD = float(os.getenv("SCALE_MIN_YIELD", "0.04"))           # companies with contacts per company searched, as for the main scale-up
NEW_COMPANIES = int(os.getenv("LIST_SCALE_NEW_COMPANIES", "2500"))   # new companies asked for in one top-up
FALLBACK_BELOW = 150                                              # a list with fallback verticals widens when its own have fewer companies than this (the tail is weak: 1 in 76 gave contacts)
TOP_UPS = 2                                                       # rounds of new-company sourcing per list before it is declared out of companies
LISTS = {
    "HG": {"campaign": 2211, "name": "Heavy goods", "verticals": ["Machinery and Equipment Manufacturing", "General Manufacturing and Industrial Products",
                                                                  "Automotive and Transportation Equipment"]},
    # Building materials: QuickEnrich holds few more of them (a 2,500-company top-up found 172, 2026-10-27), so once they are used up it takes the adjacent distribution
    # verticals (Emmanuel, 2026-10-27: yes). The broad list may take the same ones; one company still goes to one campaign.
    # 2026-10-30 (Emmanuel: options 1 and 2): new companies from the closest labels never pulled, and from its own labels at 50-99 staff.
    "BM": {"campaign": 2210, "name": "Building materials", "verticals": ["Building Materials and Products", "Building Products Adjacent", "Building Materials 50-99"],
           "source": ["Building Products Adjacent", "Building Materials 50-99"],
           "fallback": ["Wholesale and Distribution", "Other Industrial Shippers"]},
    "CN": {"campaign": 2212, "name": "Construction", "verticals": ["Construction and Infrastructure"]},
    # The broad angle test: its own verticals are logistics, trucking and plastics, and new companies are sourced for all three in turn (Emmanuel, 2026-10-28:
    # "trucking, logistics, plastics"); while they are low it also takes the other shipping verticals' stored companies (2026-10-27: "if nothing is found for
    # the broad angle we can use other verticals since this is broad angle"). The other three lists keep their own verticals.
    "BR": {"campaign": 2213, "name": "Warehousing", "verticals": ["Logistics, Warehousing and 3PL", "Trucking and Freight Carriers", "Plastics Manufacturing"],
           "source": ["Logistics, Warehousing and 3PL", "Trucking and Freight Carriers", "Plastics Manufacturing"],
           "fallback": ["Wholesale and Distribution", "Other Industrial Shippers", "Food and Beverage Production",
                        "Consumer and Medical Products Manufacturing", "Defense and Aerospace Manufacturing", "Mining, Oil, Gas and Utilities", "Agriculture and Forestry"]},
}
DEFAULT_TARGET = 1000

_claim_lock = threading.Lock()
_claimed: set[str] = set()
_state_lock = threading.Lock()
_paused_until = {"alerted": False}                                  # verification down: every list waits
_yield_pause = {code: {"t": 0.0, "trips": 0} for code in LISTS}       # a yield collapse pauses only the list that tripped
_topped = {code: 0 for code in LISTS}
_top_lock = {code: threading.Lock() for code in LISTS}
_thread: threading.Thread | None = None


def _say(text: str) -> None:
    print(f"[list_scale] {text}", flush=True)
    try:
        slack.post(f":mag: {text}")
    except Exception:
        pass


def ensure_tables() -> None:
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS list_scale_targets (code TEXT PRIMARY KEY, campaign_id BIGINT NOT NULL, target INTEGER NOT NULL,
                       stuck_reason TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        for code, spec in LISTS.items():
            cur.execute("INSERT INTO list_scale_targets (code, campaign_id, target) VALUES (%s, %s, %s) ON CONFLICT (code) DO NOTHING", (code, spec["campaign"], DEFAULT_TARGET))
    list_enrich.ensure_table()


def targets() -> dict[str, dict]:
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("SELECT code, campaign_id, target, stuck_reason FROM list_scale_targets")
        return {r["code"]: dict(r) for r in cur.fetchall()}


def _left_in(verticals: list[str]) -> int:
    with db.cursor() as cur:
        cur.execute("""SELECT count(DISTINCT s.domain) n FROM staged_companies s JOIN company_funnel f ON f.domain = s.domain
                       WHERE s.verdict = 'KEEP' AND s.vertical = ANY(%s) AND s.found_at IS NULL AND s.consumed_at IS NULL AND f.tier = 1
                         AND NOT coalesce(f.replied, false) AND coalesce(f.fit, '') NOT IN ('LOW', 'NOT_FIT')""", (verticals,))
        return int(cur.fetchone()["n"])


def verticals_for(code: str) -> list[str]:
    """The list's own verticals; plus its fallback verticals once its own are nearly used up (only the broad angle has any)."""
    spec = LISTS[code]
    primary = spec["verticals"]
    fb = spec.get("fallback") or []
    return primary + fb if fb and _left_in(primary) < FALLBACK_BELOW else primary


def n_loaded(code: str) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM contacts WHERE status = 'pushed' AND eb_campaign_id = %s", (LISTS[code]["campaign"],))
        return int(cur.fetchone()["n"])


def n_held(code: str) -> int:
    """Contacts waiting to be enriched or loaded, leaving out companies already judged unusable."""
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) n FROM contacts c WHERE c.status = 'quickrun_hold' AND c.source = %s
                       AND NOT EXISTS (SELECT 1 FROM list_enrichment e WHERE e.domain = c.company_domain AND e.list_code = %s AND NOT e.gate_pass)""", (f"quickrun-{code}", code))
        return int(cur.fetchone()["n"])


def companies_left(code: str) -> int:
    return _left_in(verticals_for(code))


def work_left() -> bool:
    try:
        t = targets()
        return any(not v["stuck_reason"] and n_loaded(c) < v["target"] for c, v in t.items())
    except Exception:
        return False


def set_target(code: str, target: int) -> None:
    """Raise or lower a list's goal; clears a 'stuck' note so the watchdog starts it again."""
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("UPDATE list_scale_targets SET target = %s, stuck_reason = NULL, updated_at = now() WHERE code = %s", (int(target), code))


def _stuck(code: str, why: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE list_scale_targets SET stuck_reason = %s, updated_at = now() WHERE code = %s", (why, code))
    _say(f"{LISTS[code]['name']} stopped at {n_loaded(code)} leads: {why}")


# ---- lock -------------------------------------------------------------------------------------------------------------------------------------------------

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


# ---- sourcing ----------------------------------------------------------------------------------------------------------------------------------------------

def tag_bank(code: str) -> int:
    """Verified contacts already banked for this list's verticals (its fallback ones too, once it uses them) (no copy needed): hold them for this list, 4 per company, none contacted in the last 60 days,
    none at a company that already has people in one of these campaigns."""
    camps = [v["campaign"] for v in LISTS.values()]
    with db.cursor() as cur:
        cur.execute("""SELECT c.email, c.company_domain d FROM contacts c
                       JOIN (SELECT DISTINCT ON (domain) domain, vertical FROM staged_companies WHERE verdict = 'KEEP') s ON s.domain = c.company_domain
                       LEFT JOIN company_funnel f ON f.domain = c.company_domain
                       WHERE c.status = 'verified' AND c.eb_campaign_id IS NULL AND coalesce(c.first_name, '') <> '' AND coalesce(c.personalization_1, '') = ''
                         AND s.vertical = ANY(%s) AND coalesce(f.fit, '') NOT IN ('LOW', 'NOT_FIT')
                         AND NOT EXISTS (SELECT 1 FROM recent_contacts r WHERE r.company_domain = c.company_domain AND r.campaign_date > now() - interval '60 days')
                         AND NOT EXISTS (SELECT 1 FROM contacts o WHERE o.company_domain = c.company_domain AND o.eb_campaign_id = ANY(%s) AND o.status = 'pushed')
                       ORDER BY c.company_domain, c.email""", (verticals_for(code), camps))
        per: dict[str, int] = {}
        keep = []
        for r in cur.fetchall():
            per[r["d"]] = per.get(r["d"], 0) + 1
            if per[r["d"]] <= MAX_PER_COMPANY:
                keep.append(r["email"].lower())
        if keep:
            cur.execute("UPDATE contacts SET status = 'quickrun_hold', source = %s, updated_at = now() WHERE lower(email) = ANY(%s) AND status = 'verified'", (f"quickrun-{code}", keep))
    return len(keep)


def _verifier_wait() -> None:
    """Search waits while email verification is down (the 23-24 Oct burn: companies were marked searched with nothing verified). One Slack line down, one back."""
    from . import sourcing_health
    while True:
        ok, text = sourcing_health.verifier_ok()
        if ok:
            if _paused_until["alerted"]:
                _paused_until["alerted"] = False
                _say("Email verification is working again; the quick-run lists resume.")
            return
        if not _paused_until["alerted"]:
            _paused_until["alerted"] = True
            _say(f":warning: Email verification is down: {text}. The quick-run lists are paused so no companies are used up; they resume by themselves.")
        time.sleep(120)


def _source_round(code: str) -> tuple[int, int] | None:
    """One chunk: search CHUNK companies for people, hold them. None when no company is waiting."""
    from . import scaling
    verticals = verticals_for(code)
    with _claim_lock:
        picked = scaling.pick_companies(verticals, CHUNK + len(_claimed) + 40)
        with db.cursor() as cur:       # one company, one campaign: skip any that already has people in one of the four
            cur.execute("SELECT DISTINCT company_domain d FROM contacts WHERE status = 'pushed' AND eb_campaign_id = ANY(%s) AND company_domain = ANY(%s)",
                        ([v["campaign"] for v in LISTS.values()], [c["domain"] for c in picked]))
            taken = {r["d"] for r in cur.fetchall()}
        fresh = [c for c in picked if c["domain"] not in _claimed and c["domain"] not in taken][:CHUNK]
        _claimed.update(c["domain"] for c in fresh)
    if not fresh:
        return None
    try:
        people = scaling.source_contacts(fresh, f"list-scale-{code}-{uuid.uuid4().hex[:6]}")
    finally:
        with _claim_lock:
            _claimed.difference_update(c["domain"] for c in fresh)
    by_dom: dict[str, list[str]] = {}
    for p in people:
        by_dom.setdefault((p.get("company_domain") or p.get("domain") or "").lower(), []).append(p["email"].lower())
    keep = [e for emails in by_dom.values() for e in emails[:MAX_PER_COMPANY]]
    if keep:
        with db.cursor() as cur:
            cur.execute("UPDATE contacts SET status = 'quickrun_hold', source = %s, updated_at = now() WHERE lower(email) = ANY(%s) AND status = 'verified'", (f"quickrun-{code}", keep))
    if len(fresh) >= 50 and len(by_dom) / len(fresh) < MIN_YIELD:
        # The yield breaker: a chunk that found almost nobody is a provider problem. Put its companies back, pause, say so.
        domains = [c["domain"] for c in fresh]
        with db.cursor() as cur:
            cur.execute("""UPDATE staged_companies s SET found_at = NULL WHERE s.domain = ANY(%s)
                           AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.company_domain = s.domain)""", (domains,))
        with _state_lock:
            _yield_pause[code]["trips"] += 1
            minutes = min(15 * 2 ** (_yield_pause[code]["trips"] - 1), 240)
            _yield_pause[code]["t"] = time.time() + minutes * 60
        _say(f":rotating_light: Quick-run yield collapsed on {LISTS[code]['name']}: {len(by_dom)} of {len(fresh)} companies gave contacts (floor {MIN_YIELD:.0%}). "
             f"The companies were put back. Paused {minutes} minutes.")
        return len(fresh), 0
    _yield_pause[code]["trips"] = 0
    return len(fresh), len(keep)


def _sources(code: str) -> list[str]:
    """The verticals new companies are sourced for, in turn: the list's 'source' ones, else its main vertical."""
    return LISTS[code].get("source") or LISTS[code]["verticals"][:1]


def _needs_new(code: str) -> bool:
    """A list with its own source verticals sources new companies as soon as those run low (the broad angle should grow in trucking, logistics and
    plastics, not only drain the fallback); the others only when everything they can search is used up."""
    if LISTS[code].get("source"):
        return _left_in(LISTS[code]["verticals"]) < FALLBACK_BELOW
    return companies_left(code) < 40


def _top_up(code: str) -> bool:
    """Source new companies for the list's source verticals (one per call, in turn) when the stored ones run low. True when companies are ready to search."""
    from . import client, funnel, modes
    with _top_lock[code]:
        src = _sources(code)
        limit = len(src) if LISTS[code].get("source") else (1 if LISTS[code].get("fallback") else TOP_UPS)
        if _topped[code] >= limit or not _needs_new(code):
            return companies_left(code) >= 40
        v = src[_topped[code] % len(src)]
        _topped[code] += 1
        # run_source sizes its search from the contacts asked for (1.2 contacts a company) and REUSES stored companies first, so a small ask finds nothing new
        # (2026-10-27: 700 and 1,500 added zero for Construction and Building materials). Ask for more than the stored companies would give, so QuickEnrich is searched.
        with db.cursor() as cur:
            cur.execute("SELECT count(DISTINCT domain) n FROM staged_companies WHERE verdict = 'KEEP' AND vertical = %s", (v,))
            stored = int(cur.fetchone()["n"])
        ask = int((stored + NEW_COMPANIES) * 1.3)
        before = stored
        _say(f"{LISTS[code]['name']}: stored companies are running out ({stored:,} stored), sourcing about {NEW_COMPANIES:,} new ones for {v}.")
        try:
            modes.run_source(client.profile(), v, ask, companies_only=True)
            funnel.refresh(force=True)
        except Exception as exc:
            _say(f"{LISTS[code]['name']}: sourcing new companies failed ({type(exc).__name__}: {str(exc)[:100]}).")
        with db.cursor() as cur:
            cur.execute("SELECT count(DISTINCT domain) n FROM staged_companies WHERE verdict = 'KEEP' AND vertical = %s", (v,))
            _say(f"{LISTS[code]['name']}: {int(cur.fetchone()['n']) - before:,} new companies found; {companies_left(code):,} are ready to search.")
        return companies_left(code) >= 40


def _source_worker(code: str, target: int, stop: threading.Event) -> str:
    """Search rounds until the held pile projects past the goal ("enough"), the stored companies and new sourcing run dry ("exhausted"), or too many rounds fail."""
    failures = 0
    while not stop.is_set():
        if n_loaded(code) + n_held(code) * PASS_RATE >= target:
            return "enough"
        wait = _yield_pause[code]["t"] - time.time()
        if wait > 0:
            time.sleep(min(wait, 60)); continue
        _verifier_wait()
        if _needs_new(code) and not _top_up(code) and companies_left(code) == 0:
            return "exhausted"
        try:
            r = _source_round(code)
        except Exception as exc:
            failures += 1
            print(f"[list_scale] {code} search chunk failed ({failures}): {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            if failures >= 6:
                return "failures"
            time.sleep(30); continue
        if r is None and not _top_up(code):
            return "exhausted"
    return "stopped"


def _finish(code: str) -> str:
    a = list_enrich.run(code, workers=8)
    b = list_enrich.fill_cities(code)
    c = list_enrich.load(code, dry_run=False)
    return (f"enriched {a['enriched']} ({a['passed']} usable), cities {b['city_found']}, loaded {c.get('sent')} "
            f"(attached {c.get('attached')}, refused {len(c.get('refused') or {})})")


def _list_loop(code: str, target: int, holder: str) -> None:
    """One list: search workers fill the held pile, this loop enriches and loads it every minute until the goal is met or nothing more can be found.
    Workers that stopped because the pile projected past the goal are started again when the loaded count still falls short."""
    stop = threading.Event()
    print(f"[list_scale] {code}: {tag_bank(code)} banked contacts held", flush=True)
    ex = ThreadPoolExecutor(WORKERS + 2)
    futures = [ex.submit(_source_worker, code, target, stop) for _ in range(WORKERS)]
    last_say = 0.0
    idle = 0
    while True:
        _beat(holder)
        done = all(f.done() for f in futures)
        before = n_loaded(code)
        if n_held(code) >= 25 or done:
            try:
                print(f"[list_scale] {code}: {_finish(code)}; in campaign {n_loaded(code)}", flush=True)
            except Exception as exc:
                print(f"[list_scale] {code}: finish failed {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        if n_loaded(code) >= target:
            _say(f"{LISTS[code]['name']} reached {n_loaded(code)} leads (goal {target}).")
            break
        if time.time() - last_say > 900:
            last_say = time.time()
            _say(f"{LISTS[code]['name']}: {n_loaded(code)} of {target} leads loaded, {n_held(code)} contacts waiting, {companies_left(code)} companies left to search.")
        if done:
            reasons = {f.result() for f in futures if not f.exception()}
            idle = idle + 1 if n_loaded(code) <= before else 0
            if reasons <= {"exhausted", "failures"} and (n_held(code) == 0 or idle >= 2):
                _stuck(code, f"no more companies or contacts found (stored companies left: {companies_left(code)})")
                break
            if idle >= 3:
                _stuck(code, "contacts are waiting but none can be loaded")
                break
            if "enough" in reasons:
                futures = [ex.submit(_source_worker, code, target, stop) for _ in range(WORKERS)]
        time.sleep(60)
    stop.set()
    ex.shutdown(wait=False)


_crash_said: set[str] = set()


def _list_loop_safe(code: str, target: int, holder: str) -> None:
    """A list that crashes is restarted within 30s by run(); say why, or it restarts and crashes unseen (BM and BR, 2026-10-28). Slack once per list."""
    try:
        _list_loop(code, target, holder)
    except Exception as exc:
        import traceback
        print(f"[list_scale] {code} crashed: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}", flush=True)
        if code not in _crash_said:
            _crash_said.add(code)
            _say(f":warning: {LISTS[code]['name']} crashed ({type(exc).__name__}: {str(exc)[:150]}); retrying every 30 seconds.")
        time.sleep(30)


def run() -> dict:
    """Runs every list that is short of its goal and not stopped, and keeps checking: a list whose goal is raised or whose stop note is cleared starts within a minute."""
    holder = uuid.uuid4().hex[:10]
    ensure_tables()
    if not _lock(holder):
        return {"status": "another instance holds the lock"}
    running: dict[str, object] = {}
    started_any = False
    try:
        with ThreadPoolExecutor(len(LISTS)) as pool:
            while True:
                _beat(holder)
                idle = {c for c in LISTS if c not in running or running[c].done()}        # read before the targets, so a list that just finished is seen as finished
                t = targets()
                new = {c: v["target"] for c, v in t.items() if c in idle and not v["stuck_reason"] and n_loaded(c) < v["target"]}
                if new:
                    if not started_any:
                        _say("Quick-run lists started: " + ", ".join(f"{LISTS[c]['name']} {n_loaded(c)}/{tg}" for c, tg in new.items()) + ".")
                        started_any = True
                    for c, tg in new.items():
                        running[c] = pool.submit(_list_loop_safe, c, tg, holder)
                if not any(not f.done() for f in running.values()) and not new:
                    break
                time.sleep(30)
        _say("Quick-run lists finished: " + ", ".join(f"{LISTS[c]['name']} {n_loaded(c)}" for c in LISTS) + ".")
        return {"status": "finished"}
    finally:
        _release(holder)


def run_safe() -> None:
    try:
        print(f"[list_scale] {run().get('status')}", flush=True)
    except Exception as exc:
        print(f"[list_scale] failed: {exc}", flush=True)
        try:
            slack.post(f":warning: quick-run lists stopped: {str(exc)[:200]}. They resume on the next deploy or restart.")
        except Exception:
            pass


def ensure_running() -> str:
    """Watchdog: start the job when it is not running and a list is short of its goal and not stopped. Called at boot and every few minutes."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return "running"
    if not work_left():
        return "done"
    _thread = threading.Thread(target=run_safe, daemon=True)
    _thread.start()
    return "started"


def release_stale_lock() -> None:
    """A booting process means the previous container's job died with it (single instance)."""
    with db.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name = %s", (LOCK,))


def rows() -> list[dict]:
    """For the live page: each list's campaign, loaded, goal, waiting contacts, companies left, and why it stopped if it did."""
    t = targets()
    out = []
    for code, spec in LISTS.items():
        v = t.get(code) or {}
        out.append({"code": code, "name": spec["name"], "campaign": spec["campaign"], "loaded": n_loaded(code), "target": int(v.get("target") or DEFAULT_TARGET),
                    "held": n_held(code), "companies_left": companies_left(code), "stuck": v.get("stuck_reason")})
    return out
