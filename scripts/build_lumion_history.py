#!/usr/bin/env python3
"""Build clients/lumion/history_domains.csv from the Lumion EmailBison send-log export.

Input: the "all" CSV written by scripts/lumion/emailbison_export.py in the BuzzLead repo
(prospects/lumion-outreach-audit/<run>/lumion-outreach-all-*.csv). One row per lead per campaign.

Output columns match what app/consultant_gate.py reads: domain, company, status, tier, last_emailed,
days_since, owner. One row per company domain, strongest status wins:

    blocked_replied       anyone at the company replied, showed interest, or the lead status is replied
    blocked_unsubscribed  anyone unsubscribed
    in_flight             someone is still in a sequence or has an email scheduled
    emailed_no_response   emailed, nobody replied; last_emailed is the newest send date

Usage:
    python scripts/build_lumion_history.py <older.csv> [<newer.csv> ...] [--out clients/lumion/history_domains.csv] [--today 2026-10-08]

Several files are layered oldest first. Rows from an older file are dropped for any campaign_id that appears in a newer
file, so a refresh of recent campaigns replaces their stale state without throwing away the rest of the history.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "msn.com",
            "live.com", "me.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "protonmail.com"}
RANK = {"blocked_replied": 0, "blocked_unsubscribed": 1, "in_flight": 2, "emailed_no_response": 3}


def norm(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0].strip()


def company_domain(row: dict) -> str:
    d = norm(row.get("domain", ""))
    if d:
        return d
    host = norm((row.get("email") or "").split("@")[-1]) if "@" in (row.get("email") or "") else ""
    return "" if host in FREEMAIL else host


def status_of(row: dict) -> str | None:
    lead = (row.get("campaign_lead_status") or "").strip().lower()
    if (row.get("replied") or "").lower() == "yes" or (row.get("interested") or "").lower() == "yes" or lead == "replied":
        return "blocked_replied"
    if lead == "unsubscribed" or (row.get("deliverability") or "").lower() == "unsubscribed":
        return "blocked_unsubscribed"
    try:
        scheduled = int(row.get("emails_still_scheduled") or 0)
    except ValueError:
        scheduled = 0
    if lead == "in_sequence" or scheduled > 0:
        return "in_flight"
    if (row.get("last_touch_date") or "").strip():
        return "emailed_no_response"
    return None


def tier(status: str, last: date | None, today: date, floor: int = 90) -> str:
    if status != "emailed_no_response" or last is None:
        return status
    age = (today - last).days
    if age < floor:
        return f"blocked_under_{floor}"
    if age >= floor + 60:
        return f"{floor + 60}_plus"
    if age >= floor + 30:
        return f"{floor + 30}_plus"
    return f"{floor}_plus"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export_csv", nargs="+")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "clients" / "lumion" / "history_domains.csv"))
    ap.add_argument("--today", default=date.today().isoformat())
    a = ap.parse_args()
    today = date.fromisoformat(a.today)

    files = []
    for path in a.export_csv:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            files.append(list(csv.DictReader(fh)))
    superseded = [set() for _ in files]
    for i in range(len(files)):
        for later in files[i + 1:]:
            superseded[i] |= {str(r.get("campaign_id")) for r in later}
    rows_in = [r for i, rows in enumerate(files) for r in rows if str(r.get("campaign_id")) not in superseded[i]]
    best: dict[str, dict] = {}
    seen = skipped = 0
    if True:
        for row in rows_in:
            seen += 1
            dom = company_domain(row)
            st = status_of(row)
            if not dom or not st:
                skipped += 1
                continue
            try:
                last = date.fromisoformat((row.get("last_touch_date") or "")[:10]) if row.get("last_touch_date") else None
            except ValueError:
                last = None
            cur = best.get(dom)
            if cur is None:
                best[dom] = {"company": row.get("company") or "", "status": st, "last": last}
                continue
            if RANK[st] < RANK[cur["status"]]:
                cur["status"] = st
            if last and (cur["last"] is None or last > cur["last"]):
                cur["last"] = last
            if not cur["company"] and row.get("company"):
                cur["company"] = row["company"]

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["domain", "company", "status", "tier", "last_emailed", "days_since", "owner"])
        for dom in sorted(best):
            r = best[dom]
            last = r["last"]
            t = tier(r["status"], last, today)
            counts[(r["status"], t if r["status"] == "emailed_no_response" else "")] += 1
            w.writerow([dom, r["company"], r["status"], t, last.isoformat() if last else "",
                        (today - last).days if last else "", "lumion"])
    print(f"rows read {seen}, unusable {skipped}, companies {len(best)} -> {out}")
    for (st, t), n in sorted(counts.items()):
        print(f"  {st:22} {t:18} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
