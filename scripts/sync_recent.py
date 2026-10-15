#!/usr/bin/env python3
"""Index every company contacted in the last N days, from EmailBison itself.

Troy, 2026-09-11: a company IDPR has emailed before is not automatically off limits.
What matters is whether it appears in a RECENT campaign. Anything not touched in the
last 90 days can be approached again -- reusing the contacts already in EmailBison
rather than re-sourcing people, re-verifying their addresses and writing fresh copy.

The backfill cannot answer this. It stored ingestion time, not send time, so every one
of its 223,863 contacts looks like it was contacted the day we imported it. The only
truthful source of "when was this company last emailed" is the campaigns themselves:
their created_at, and the leads attached to them.

Scale, measured 2026-09-11: 21 campaigns inside 90 days holding 83,502 lead slots,
which is ~5,566 pages at the API's fixed 15 per page. Roughly eight minutes at 16-wide.
The other 59 campaigns hold 273,806 slots and are the pool this opens up.

Usage:
    python scripts/sync_recent.py            # default 90 days
    python scripts/sync_recent.py --days 60
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import db  # noqa: E402
from app.clients import emailbison as eb  # noqa: E402
from app.config import settings  # noqa: E402

WORKERS = 16
PAGE_SIZE = 15  # the API's fixed page size; per_page is ignored
PAGE_RETRIES = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS recent_contacts (
    company_domain TEXT NOT NULL,
    email          TEXT NOT NULL,
    campaign_id    BIGINT NOT NULL,
    campaign_name  TEXT,
    campaign_date  DATE,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_domain, email, campaign_id)
);
CREATE INDEX IF NOT EXISTS recent_contacts_domain_idx ON recent_contacts (company_domain);
CREATE INDEX IF NOT EXISTS recent_contacts_date_idx   ON recent_contacts (campaign_date);
"""

_HEADERS = {
    "Authorization": f"Bearer {settings.emailbison_api_key}",
    "Accept": "application/json",
    "X-Workspace-Id": str(settings.workspace_id),
}


def _campaign_date(campaign: dict) -> datetime | None:
    for field in ("created_at", "updated_at"):
        raw = campaign.get(field)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _page(args: tuple[int, int]) -> list[dict]:
    """One page of one campaign's leads, retried.

    A dropped page is not a cosmetic loss. Every lead it would have carried reads as
    never-contacted afterwards, so the company becomes eligible for a second approach
    inside the window the index exists to enforce -- the failure is silent and it is in
    the unsafe direction. The first unretried build lost 135 leads this way.
    """
    campaign_id, page = args
    for attempt in range(PAGE_RETRIES):
        try:
            r = httpx.get(
                f"{settings.emailbison_base}/api/campaigns/{campaign_id}/leads",
                headers=_HEADERS, params={"page": page}, timeout=30,
            )
            if r.status_code == 200:
                return (r.json() or {}).get("data") or []
            if r.status_code in (404, 403):
                return []          # genuinely absent, not worth retrying
        except Exception:
            pass
        time.sleep(0.4 * (attempt + 1))
    return []


def sync(days: int, only: set[int] | None = None) -> dict:
    with db.cursor() as cur:
        cur.execute(SCHEMA)

    now = datetime.now(timezone.utc)
    recent = []
    for c in eb.list_campaigns():
        d = _campaign_date(c)
        if not d or (c.get("total_leads") or 0) <= 0:
            continue
        if only and c["id"] not in only:
            continue
        if only or (now - d).days <= days:
            recent.append((c, d))
    print(f"{len(recent)} campaigns inside {days} days "
          f"({sum((c.get('total_leads') or 0) for c, _ in recent):,} lead slots)\n", flush=True)

    totals = {"campaigns": 0, "rows": 0, "domains": set()}
    started = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for campaign, when in sorted(recent, key=lambda x: x[1], reverse=True):
            cid = campaign["id"]
            expected = campaign.get("total_leads") or 0
            pages = max((expected + PAGE_SIZE - 1) // PAGE_SIZE, 1)
            rows: list[dict] = []
            for chunk_start in range(0, pages, WORKERS * 4):
                chunk = range(chunk_start + 1, min(chunk_start + WORKERS * 4, pages) + 1)
                for got in pool.map(_page, [(cid, p) for p in chunk]):
                    rows.extend(got)

            payload = []
            for lead in rows:
                email = (lead.get("email") or "").strip().lower()
                if not email or "@" not in email:
                    continue
                domain = db.normalize_domain(email.rsplit("@", 1)[1])
                if not domain:
                    continue
                payload.append((domain, email, cid, campaign.get("name"), when.date()))
                totals["domains"].add(domain)
            if payload:
                with db.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO recent_contacts "
                        " (company_domain, email, campaign_id, campaign_name, campaign_date) "
                        " VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", payload)
            totals["campaigns"] += 1
            totals["rows"] += len(payload)
            print(f"  {str(campaign.get('name'))[:44]:46} {len(payload):>6}/{expected:<6} "
                  f"({(now - when).days}d ago)", flush=True)

    totals["elapsed"] = time.time() - started
    totals["distinct_domains"] = len(totals.pop("domains"))
    return totals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--campaign", type=int, action="append",
                    help="re-sync only these campaign ids (repeatable)")
    args = ap.parse_args()
    t = sync(args.days, set(args.campaign or []) or None)
    with db.cursor() as cur:
        cur.execute("SELECT count(DISTINCT company_domain) AS d, count(*) AS r FROM recent_contacts")
        row = cur.fetchone()
    print(f"\ncampaigns synced : {t['campaigns']}")
    print(f"rows written     : {t['rows']:,}")
    print(f"index now holds  : {row['d']:,} companies / {row['r']:,} contacts")
    print(f"elapsed          : {t['elapsed']:,.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
