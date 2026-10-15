#!/usr/bin/env python3
"""Lean refresh of Lumion's send history straight from EmailBison (read-only GETs).

Why this exists: scripts/lumion/emailbison_export.py in the BuzzLead repo walks the leads endpoint and
runs DNS checks per email domain. A run took more than 13 hours for 9 campaigns. History only needs, per
lead and campaign: who replied or showed interest, who is still in a sequence, and the newest send date.
That comes from two endpoints, so this takes minutes.

  /api/campaigns/{id}/scheduled-emails   the send log (15 rows a page, filters ignored, walk every page)
  /api/campaigns/{id}/replies?folder=inbox   human replies (automated replies are skipped)

Output has the same columns scripts/build_lumion_history.py reads. Run it for the campaigns created since the
last full export, then pass BOTH files to the builder: rows from the older file are dropped for any campaign
that appears in the newer one.

Usage:
    python scripts/collect_lumion_history.py --campaigns 1411,1412,1504 --out refresh.csv
    python scripts/collect_lumion_history.py --since 2026-08-21 --out refresh.csv     (every campaign created since)

The API key is read from LUMION_EMAIL_BISON_API_KEY (root .env or the environment) and never printed.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

BASE = "https://personal.buzzlead.io"
WORKSPACE = 37
WORKERS = 6
FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "msn.com",
            "live.com", "me.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "protonmail.com"}
COLUMNS = ["campaign_id", "lead_id", "email", "company", "domain", "replied", "interested",
           "campaign_lead_status", "deliverability", "emails_still_scheduled", "last_touch_date"]


def _key() -> str:
    key = os.getenv("LUMION_EMAIL_BISON_API_KEY")
    if not key:
        for env in (Path(__file__).resolve().parents[1] / ".env", Path("C:/Claude_Code_GTM_Training/.env")):
            if env.exists():
                for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("LUMION_EMAIL_BISON_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if key:
                break
    if not key:
        sys.exit("LUMION_EMAIL_BISON_API_KEY is not set")
    return key


class Api:
    def __init__(self, key: str):
        self.client = httpx.Client(base_url=BASE, timeout=60, headers={
            "Authorization": f"Bearer {key}", "Accept": "application/json", "X-Workspace-Id": str(WORKSPACE)})

    def get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(6):
            try:
                r = self.client.get(path, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError:
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 30))
        return {}

    def pages(self, path: str, extra: dict | None = None) -> list[dict]:
        first = self.get(path, {"page": 1, **(extra or {})})
        rows = list(first.get("data") or [])
        last = int((first.get("meta") or {}).get("last_page") or 1)
        if last <= 1:
            return rows

        def grab(p: int) -> list[dict]:
            try:
                return self.get(path, {"page": p, **(extra or {})}).get("data") or []
            except Exception as exc:                      # report, never hide a gap
                print(f"    ! {path} page {p} failed: {str(exc)[:80]}", flush=True)
                self.failed += 1
                return []

        self.failed = getattr(self, "failed", 0)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for fut in as_completed([pool.submit(grab, p) for p in range(2, last + 1)]):
                rows.extend(fut.result())
        return rows


def domain_of(lead: dict) -> str:
    cv = {str(v.get("name", "")).lower(): (v.get("value") or "") for v in (lead.get("custom_variables") or [])}
    for name in ("domain", "website"):
        d = re.sub(r"^https?://", "", cv.get(name, "").strip().lower())
        d = re.sub(r"^www\.", "", d).split("/")[0]
        if d:
            return d
    email = lead.get("email") or ""
    host = email.split("@")[-1].lower() if "@" in email else ""
    return "" if host in FREEMAIL else host


def collect(api: Api, cid: int, cname: str) -> list[dict]:
    print(f"  campaign {cid} {cname[:50]!r}", flush=True)
    sends = api.pages(f"/api/campaigns/{cid}/scheduled-emails")
    replies = api.pages(f"/api/campaigns/{cid}/replies", {"folder": "inbox"})
    print(f"    {len(sends)} send rows, {len(replies)} inbox replies", flush=True)

    leads: dict[int, dict] = {}
    for s in sends:
        lead = s.get("lead") or {}
        lid = lead.get("id") or s.get("lead_id")
        if lid is None:
            continue
        rec = leads.setdefault(lid, {
            "campaign_id": cid, "lead_id": lid, "email": lead.get("email", ""), "company": lead.get("company") or "",
            "domain": domain_of(lead), "replied": "no", "interested": "no", "campaign_lead_status": "sequence_finished",
            "deliverability": "unverified", "scheduled": 0, "last": ""})
        if (lead.get("status") or "").lower() == "unsubscribed":
            rec["deliverability"] = "unsubscribed"
            rec["campaign_lead_status"] = "unsubscribed"
        if s.get("sent_at"):
            rec["last"] = max(rec["last"], str(s["sent_at"])[:10])
        elif (s.get("status") or "").lower() == "scheduled":
            rec["scheduled"] += 1
        if (s.get("replies") or 0) > 0:
            rec["replied"] = "yes"
        if s.get("interested"):
            rec["interested"] = "yes"
    for r in replies:
        if r.get("automated_reply"):
            continue
        lead = r.get("lead") or {}
        lid = r.get("lead_id") or lead.get("id")
        rec = leads.get(lid)
        if rec is None:
            rec = leads.setdefault(lid, {
                "campaign_id": cid, "lead_id": lid, "email": lead.get("email") or r.get("from_email_address") or "",
                "company": lead.get("company") or "", "domain": domain_of(lead), "replied": "no", "interested": "no",
                "campaign_lead_status": "sequence_finished", "deliverability": "unverified", "scheduled": 0, "last": ""})
        rec["replied"] = "yes"
        rec["campaign_lead_status"] = "replied"
        if r.get("interested"):
            rec["interested"] = "yes"
    out = []
    for rec in leads.values():
        if rec["scheduled"] > 0 and rec["campaign_lead_status"] == "sequence_finished":
            rec["campaign_lead_status"] = "in_sequence"
        out.append({"campaign_id": rec["campaign_id"], "lead_id": rec["lead_id"], "email": rec["email"],
                    "company": rec["company"], "domain": rec["domain"], "replied": rec["replied"],
                    "interested": rec["interested"], "campaign_lead_status": rec["campaign_lead_status"],
                    "deliverability": rec["deliverability"], "emails_still_scheduled": rec["scheduled"],
                    "last_touch_date": rec["last"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaigns", help="comma separated campaign ids")
    ap.add_argument("--since", help="every campaign created on or after YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    api = Api(_key())

    camps = api.pages("/api/campaigns")
    by_id = {int(c["id"]): c for c in camps}
    if a.campaigns:
        wanted = [int(x) for x in a.campaigns.split(",") if x.strip()]
    elif a.since:
        wanted = [i for i, c in by_id.items() if str(c.get("created_at", ""))[:10] >= a.since]
    else:
        sys.exit("give --campaigns or --since")
    wanted = [i for i in sorted(wanted, reverse=True) if i in by_id and (by_id[i].get("emails_sent") or 0) > 0]
    print(f"{len(camps)} campaigns in the workspace, collecting {len(wanted)}: {wanted}", flush=True)

    rows: list[dict] = []
    for cid in wanted:
        rows.extend(collect(api, cid, by_id[cid].get("name") or ""))
        with open(a.out, "w", newline="", encoding="utf-8") as fh:        # write after EVERY campaign: a kill loses nothing
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)
    print(f"done: {len(rows)} lead-campaign rows -> {a.out}; failed pages: {getattr(api, 'failed', 0)}")
    return 0 if not getattr(api, "failed", 0) else 2


if __name__ == "__main__":
    sys.exit(main())
