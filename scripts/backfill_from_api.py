#!/usr/bin/env python3
"""Build the dedup history from EmailBison's workspace-wide lead list.

WHY BULK AND NOT AN ON-DEMAND LOOKUP
------------------------------------
GET /api/leads?search=<term> does work for a term that EXISTS -- an exact email or a
known domain comes back in about 1-4 seconds with a truthful `total`. But measured on
the live workspace 2026-09-08, a term that matches NOTHING behaves like this:

    fake email   -> total=50000, 113.4s
    fake domain  -> total=50000, 118.7s
    fake email   -> ConnectTimeout

A miss does not return zero. It returns a capped 50,000 after roughly two minutes, and
sometimes fails outright. So a lookup-based dedup could not tell "never emailed" from
"already emailed", and would read 50,000 as a hit and silently discard every genuinely
new prospect -- failing in the direction that destroys the list. Misses are also the
normal case in dedup, so the slow path would dominate.

Hence: pull once, index locally, then top up incrementally.

  - PER CAMPAIGN, not workspace-wide. Offset pagination has a depth cap: page 1000 on
    /api/leads is fine, page 5000 returns 422 "You are requesting too many pages. Please
    use the cursor pagination type to traverse large datasets." A workspace-wide pull
    needs 16,013 pages and so dies partway; the largest single campaign needs 853, which
    is comfortably under the cap. Cursor pagination does work (pagination_type=cursor,
    returns next_cursor) but is inherently sequential -- ~6 hours single-threaded --
    whereas per-campaign pages are independent and can be fetched in parallel.
  - per_page is ignored server-side and pinned at 15, so page count is fixed
  - resumable: the last completed page is checkpointed, so an interrupt costs one window
  - idempotent: ON CONFLICT DO NOTHING, so re-running only adds what is missing
  - concurrent: a single page takes ~5s, so serially 16,000 pages is ~22 hours. Pages
    are independent, so they are fetched in parallel windows: 24 workers gets it to
    roughly 4 hours. The window is committed in page order and the checkpoint never
    advances past a page that failed, so a resume re-fetches the gap rather than
    skipping it -- silently skipping pages would leave holes in the dedup index, which
    is the one failure this whole exercise exists to prevent.

Usage:
    python scripts/backfill_from_api.py                 # full pull, resumes if interrupted
    python scripts/backfill_from_api.py --max-pages 20  # smoke test
    python scripts/backfill_from_api.py --restart       # ignore checkpoint, start over
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app import db  # noqa: E402
from app.clients.emailbison import _get  # noqa: E402
from app.clients.emailbison import list_campaigns as eb_list_campaigns  # noqa: E402
from app.config import settings  # noqa: E402

# app.db opens a fresh connection per cursor(), which is right inside Railway where the
# database is a hop away, but this job runs for hours over the public TCP proxy and a
# reconnect per window eventually times out mid-write. So hold one connection here, with
# keepalives, and rebuild it on failure rather than losing the run.
_CONN: "psycopg.Connection | None" = None


def _conn() -> "psycopg.Connection":
    global _CONN
    if _CONN is not None and not _CONN.closed:
        return _CONN
    _CONN = psycopg.connect(
        settings.database_url, row_factory=dict_row, autocommit=True,
        connect_timeout=20, keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )
    return _CONN

CHECKPOINT = Path(__file__).resolve().parent.parent / ".backfill-progress.json"
_EB_HEADERS = {
    "Authorization": f"Bearer {settings.emailbison_api_key}",
    "Accept": "application/json",
    "X-Workspace-Id": str(settings.workspace_id),
}
# Measured on the live workspace 2026-09-08: serial 0.2 pages/s, 12 workers 0.92,
# 24 workers 1.15, 40 workers no better. The server throttles, so past ~24 the extra
# concurrency buys nothing and only raises the error rate.
# 24 workers wins in a short burst (1.15 pages/s, no errors) but does not hold: under
# sustained load the server starts timing pages out and effective throughput FALLS below
# the 12-worker rate. 16 is the point that stays clean over hours. Measured, not guessed.
WORKERS = 16       # parallel page fetches
WINDOW = 48        # pages per committed window


def _load() -> dict:
    if CHECKPOINT.exists():
        try:
            return json.loads(CHECKPOINT.read_text())
        except ValueError:
            pass
    return {"done": []}


def _save(state: dict) -> None:
    CHECKPOINT.write_text(json.dumps(state, indent=1))


def store(rows: list[dict]) -> tuple[int, int]:
    """Write leads and their domains as history.

    Status is 'pushed' / source 'backfill'. That is not a permanent block --
    blocked_contacts ignores backfill timestamps and trusts recent_contacts, so
    anyone last emailed more than recontact_days ago is eligible again.
    """
    contacts, companies = [], {}
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        domain = db.normalize_domain(email.rsplit("@", 1)[1])
        contacts.append((email, domain or None,
                         row.get("first_name"), row.get("last_name"), row.get("title")))
        if domain:
            companies.setdefault(domain, row.get("company"))
    if not contacts:
        return 0, 0
    for attempt in range(4):
        try:
            return _write(contacts, companies)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            global _CONN
            try:
                if _CONN is not None:
                    _CONN.close()
            except Exception:
                pass
            _CONN = None
            if attempt == 3:
                raise
            wait = 2 ** attempt
            print(f"  ! db reconnect after {type(exc).__name__}, retry in {wait}s", flush=True)
            time.sleep(wait)
    return 0, 0


def _write(contacts: list[tuple], companies: dict) -> tuple[int, int]:
    with _conn().cursor() as cur:
        cur.executemany(
            """INSERT INTO contacts (email, company_domain, first_name, last_name, title,
                                     status, source)
               VALUES (%s,%s,%s,%s,%s,'pushed','backfill')
               ON CONFLICT (email) DO NOTHING""",
            contacts,
        )
        cur.executemany(
            """INSERT INTO companies (domain, name, status, source)
               VALUES (%s,%s,'pushed','backfill')
               ON CONFLICT (domain) DO NOTHING""",
            list(companies.items()),
        )
    return len(contacts), len(companies)


def _fetch_page(args: tuple[int, int]) -> tuple[int, list[dict], int | None, str | None]:
    """Fetch one page of one campaign. Returns (page, rows, last_page, error).

    Never raises: one bad page must not abort the window and lose the pages fetched
    alongside it.
    """
    campaign_id, page = args
    # Deliberately NOT app.clients.http.request: its 4 attempts at a 90s read timeout
    # mean one slow page occupies a worker for six minutes, and with every worker stuck
    # on a retry the whole pull stalls. Here a page is cheap and repeatable -- the resume
    # re-fetches anything missed -- so fail fast and move on.
    try:
        r = httpx.get(f"{settings.emailbison_base}/api/campaigns/{campaign_id}/leads",
                      headers=_EB_HEADERS, params={"page": page}, timeout=25)
        if r.status_code != 200:
            return page, [], None, f"{r.status_code}"
        payload = r.json()
        rows = payload.get("data") or []
        last = int((payload.get("meta") or {}).get("last_page") or 1)
        return page, rows, last, None
    except Exception as exc:
        return page, [], None, type(exc).__name__


def _campaign(campaign: dict, pool: ThreadPoolExecutor, totals: dict) -> int:
    """Pull every lead of one campaign, pages fetched in parallel."""
    cid = campaign["id"]
    name = (campaign.get("name") or "")[:40]
    expected = campaign.get("total_leads") or 0

    page, rows, last_page, err = _fetch_page((cid, 1))
    if err:
        totals["errors"] += 1
        print(f"  ! {name}: page 1 failed: {err[:70]}", flush=True)
        return 0
    last_page = last_page or 1
    collected = list(rows)

    if last_page > 1:
        pages = list(range(2, last_page + 1))
        for chunk_start in range(0, len(pages), WINDOW):
            chunk = pages[chunk_start:chunk_start + WINDOW]
            for pnum, prows, _lp, perr in pool.map(_fetch_page, [(cid, p) for p in chunk]):
                if perr:
                    totals["errors"] += 1
                else:
                    collected.extend(prows)

    c, co = store(collected)
    totals["leads"] += len(collected)
    totals["contacts"] += c
    totals["companies"] += co
    print(f"  {name:42} {len(collected):>6}/{expected:<6} leads  "
          f"(+{c} contacts, +{co} domains)", flush=True)
    return len(collected)


def backfill(max_pages: int | None, delay: float, restart: bool) -> dict:
    state = {"done": []} if restart else _load()
    done = set(state.get("done", []))
    started = time.time()
    totals = {"pages": 0, "leads": 0, "contacts": 0, "companies": 0, "errors": 0}

    campaigns = [c for c in eb_list_campaigns() if (c.get("total_leads") or 0) > 0]
    campaigns.sort(key=lambda c: c.get("total_leads") or 0, reverse=True)
    todo = [c for c in campaigns if c["id"] not in done]
    slots = sum(c.get("total_leads") or 0 for c in todo)
    print(f"{len(todo)} campaigns to pull ({slots:,} lead slots)\n", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, campaign in enumerate(todo, 1):
            _campaign(campaign, pool, totals)
            done.add(campaign["id"])
            state["done"] = sorted(done)
            _save(state)
            elapsed = max(time.time() - started, 1)
            eta = (len(todo) - i) * (elapsed / i) / 60
            print(f"     [{i}/{len(todo)}] {totals['leads']:,} leads so far, "
                  f"~{eta:.0f} min left", flush=True)
            if delay:
                time.sleep(delay)

    totals["elapsed"] = time.time() - started
    return totals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages (smoke test)")
    ap.add_argument("--delay", type=float, default=0.05)
    ap.add_argument("--restart", action="store_true", help="ignore the checkpoint and start at page 1")
    args = ap.parse_args()

    totals = backfill(args.max_pages, args.delay, args.restart)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM contacts")
        contacts = cur.fetchone()["n"]
        cur.execute("SELECT count(*) n FROM companies")
        companies = cur.fetchone()["n"]

    print(f"\npages pulled   : {totals['pages']:,} (of {totals.get('last_page')})")
    print(f"leads seen     : {totals['leads']:,}")
    print(f"history holds  : {contacts:,} contacts / {companies:,} domains")
    print(f"elapsed        : {totals['elapsed']:,.0f}s   errors: {totals['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
