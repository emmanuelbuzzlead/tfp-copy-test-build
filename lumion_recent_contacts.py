"""Fill recent_contacts for Lumion with EXACT last-send dates, from EmailBison's send log.

ProductEVO's tiers are "days since we last contacted this company". The stock sync script dates a lead by its
campaign's creation day, which understates how recently a long campaign was still sending. Lumion's campaigns run for
weeks, so this reads each campaign's send log (/api/campaigns/{id}/scheduled-emails) and dates every lead by the newest
email actually SENT to it. A lead with an email still scheduled counts as contacted today (it is in flight). A lead that
was loaded but never reached falls back to the campaign's creation day. Read-only against EmailBison.

Run on Railway from the app root, after the schema exists:
    python lumion_recent_contacts.py              new campaigns, plus every active or paused one (refresh)
    python lumion_recent_contacts.py --all        every campaign again
    python lumion_recent_contacts.py --campaigns 1651,1888

Resumable: a campaign is recorded in recent_contacts_sync only when ALL its pages were read. Log: stdout.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from app import db
from app.clients import emailbison

FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "msn.com", "live.com",
            "me.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "protonmail.com"}
WORKERS = 6


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _page(args):
    cid, p = args
    for attempt in range(4):
        try:
            return p, (emailbison._get(f"/api/campaigns/{cid}/scheduled-emails", {"page": p}) or {})
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return p, None


def _domain(lead: dict) -> str:
    cv = {str(v.get("name", "")).lower(): (v.get("value") or "") for v in (lead.get("custom_variables") or [])}
    for name in ("domain", "website"):
        d = db.normalize_domain(cv.get(name, "") or "")
        if d and d not in FREEMAIL:
            return d
    email = (lead.get("email") or "").lower()
    host = db.normalize_domain(email.rsplit("@", 1)[1]) if "@" in email else ""
    return "" if host in FREEMAIL else host


def read_campaign(cid: int, created: date | None) -> tuple[list[tuple], bool]:
    first = emailbison._get(f"/api/campaigns/{cid}/scheduled-emails", {"page": 1}) or {}
    last_page = int((first.get("meta") or {}).get("last_page") or 1)
    pages = {1: first}
    ok = True
    if last_page > 1:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for p, got in pool.map(_page, [(cid, p) for p in range(2, last_page + 1)]):
                if got is None:
                    ok = False
                else:
                    pages[p] = got
    leads: dict[str, dict] = {}
    for body in pages.values():
        for row in body.get("data") or []:
            lead = row.get("lead") or {}
            email = (lead.get("email") or "").strip().lower()
            if "@" not in email:
                continue
            rec = leads.setdefault(email, {"domain": _domain(lead), "last": None, "pending": False})
            sent = row.get("sent_at")
            if sent:
                d = datetime.fromisoformat(str(sent).replace("Z", "+00:00")).date()
                if rec["last"] is None or d > rec["last"]:
                    rec["last"] = d
            elif (row.get("status") or "").lower() == "scheduled":
                rec["pending"] = True
    today = date.today()
    out = []
    for email, rec in leads.items():
        if not rec["domain"]:
            continue
        when = today if rec["pending"] else (rec["last"] or created or today)
        out.append((rec["domain"], email, cid, when))
    return out, ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--campaigns", default="")
    a = ap.parse_args()
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS recent_contacts_sync (
            campaign_id BIGINT PRIMARY KEY, synced_at TIMESTAMPTZ NOT NULL DEFAULT now(), rows INTEGER)""")
        cur.execute("SELECT campaign_id FROM recent_contacts_sync")
        done = {r["campaign_id"] for r in cur.fetchall()}
    camps = emailbison.list_campaigns()
    wanted = {int(x) for x in a.campaigns.split(",") if x.strip()}
    todo = []
    for c in camps:
        cid = int(c["id"])
        if not (c.get("emails_sent") or c.get("total_leads")):
            continue
        live = str(c.get("status") or "").lower() in ("active", "paused", "launching", "queued")
        if wanted and cid not in wanted:
            continue
        if wanted or a.all or cid not in done or live:
            todo.append(c)
    todo.sort(key=lambda c: -int(c["id"]))
    log(f"{len(camps)} campaigns in the workspace, {len(todo)} to read ({len(done)} already recorded)")
    failed = 0
    for c in todo:
        cid = int(c["id"])
        created = None
        try:
            created = datetime.fromisoformat(str(c.get("created_at")).replace("Z", "+00:00")).date()
        except Exception:
            pass
        rows, ok = read_campaign(cid, created)
        name = (c.get("name") or "").strip()
        if rows:
            with db.cursor() as cur:
                cur.executemany("""INSERT INTO recent_contacts (company_domain, email, campaign_id, campaign_name, campaign_date)
                                   VALUES (%s,%s,%s,%s,%s)
                                   ON CONFLICT (company_domain, email, campaign_id) DO UPDATE
                                   SET campaign_date = EXCLUDED.campaign_date, campaign_name = EXCLUDED.campaign_name,
                                       synced_at = now()""",
                                [(d, e, cid, name, w) for d, e, cid, w in rows])
        if ok:
            with db.cursor() as cur:
                cur.execute("""INSERT INTO recent_contacts_sync (campaign_id, rows) VALUES (%s,%s)
                               ON CONFLICT (campaign_id) DO UPDATE SET synced_at = now(), rows = EXCLUDED.rows""",
                            (cid, len(rows)))
        else:
            failed += 1
        log(f"campaign {cid} {name[:44]!r}: {len(rows)} leads {'ok' if ok else 'INCOMPLETE, will retry next run'}")
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n, count(DISTINCT company_domain) d, max(campaign_date) newest FROM recent_contacts")
        r = cur.fetchone()
    log(f"RECENT_DONE: {r['n']} lead rows at {r['d']} companies, newest send {r['newest']}, incomplete campaigns {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
