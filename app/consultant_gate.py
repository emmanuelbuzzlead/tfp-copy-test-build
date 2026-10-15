"""Shared-pool rules for ProQ and MasterNow, who both target US consultancies.

Opt-in. A client takes part only if its client.toml sets `history_file` and/or the
`partition_lo` / `partition_hi` pair. Every other client (IDPR, ProductEVO) has neither, so
`active()` is False and nothing here changes what they do.

Three rules, decided by Troy on 2026-10-08:

1. ONE FIRM, ONE CLIENT. Each firm belongs to whichever client owns its hash bucket.
   MasterNow owns buckets 0-29 (30%), ProQ owns 30-99 (70%). The hash is of the
   normalised domain, so it never changes and the two clients need no coordination: a firm
   in MasterNow's range is blocked for ProQ and the other way round.

2. REPLIED = OFF LIMITS FOR EVER. A firm where anyone replied, showed interest or
   unsubscribed in ProQ's history is blocked for both clients, permanently. A firm loaded in
   a campaign that is still sending but not yet emailed (`in_flight`) is held too, until
   that campaign finishes and the history is rebuilt.

3. EMAILED, NO REPLY = BACK IN ON A LADDER. Never contact a firm emailed under 60 days
   ago. Past that, fresh firms go first, then firms last emailed 120+ days ago, then 90+,
   then 60+ (see `age_rank`).

The history is a CSV (domain, status, last_emailed) built from the client's EmailBison send log.
Age is recomputed from `last_emailed` on every call, so a firm moves down the ladder as
the days pass without the file being rebuilt.

A gated client whose history file is missing FAILS LOUDLY. Silently skipping it would
email people who replied.
"""

from __future__ import annotations

import csv
import hashlib
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

from . import client

DEFAULT_MIN_DAYS = 60


def min_days() -> int:
    """No contact inside this many days. The client's own recontact_days (Lumion: 90)."""
    p = _profile()
    return int(getattr(p, "recontact_days", None) or DEFAULT_MIN_DAYS) if p else DEFAULT_MIN_DAYS


def ladder_days() -> tuple[int, int, int]:
    """Re-entry ladder, oldest first. For the default 60 this is the original 120 / 90 / 60."""
    m = min_days()
    return (m + 60, m + 30, m)

PERMANENT = {"blocked_replied", "blocked_unsubscribed"}


def _profile():
    try:
        return client.profile()
    except Exception:
        return None


def active() -> bool:
    p = _profile()
    return bool(p and (p.history_file or p.partition_hi is not None))


def norm(domain: str | None) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0].strip()


def bucket(domain: str | None) -> int:
    """0-99, stable for a given domain."""
    return int(hashlib.md5(norm(domain).encode()).hexdigest(), 16) % 100


def owns(domain: str | None) -> bool:
    p = _profile()
    if not p or p.partition_hi is None:
        return True
    return int(p.partition_lo or 0) <= bucket(domain) < int(p.partition_hi)


@lru_cache(maxsize=4)
def _load(path_str: str, mtime: float) -> dict[str, tuple[str, date | None]]:
    out: dict[str, tuple[str, date | None]] = {}
    with open(path_str, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            last = row.get("last_emailed") or ""
            out[norm(row["domain"])] = (
                row["status"], date.fromisoformat(last) if last else None)
    return out


def history() -> dict[str, tuple[str, date | None]]:
    p = _profile()
    if not p or not p.history_file:
        return {}
    path = client.CLIENTS_DIR / p.name / p.history_file
    if not path.exists():
        raise RuntimeError(
            f"{p.name}: history_file {p.history_file!r} not found at {path}. Refusing to "
            f"run without it: it holds the firms that replied and must never be emailed.")
    return _load(str(path.resolve()), path.stat().st_mtime)


def days_since(domain: str, today: date | None = None) -> int | None:
    """Days since this firm was last emailed with no reply, or None if not applicable."""
    rec = history().get(norm(domain))
    if not rec or rec[0] != "emailed_no_response" or rec[1] is None:
        return None
    return ((today or date.today()) - rec[1]).days


def blocked(domains, today: date | None = None) -> dict[str, str]:
    """{domain: reason} for firms this client must not touch right now."""
    if not active():
        return {}
    hist = history()
    out: dict[str, str] = {}
    for d in domains:
        if not owns(d):
            out[d] = "belongs to another client (partition split)"
            continue
        rec = hist.get(norm(d))
        if not rec:
            continue
        status, last = rec
        if status in PERMANENT:
            out[d] = ("replied, showed interest or unsubscribed in past outreach "
                      "(permanent)")
        elif status == "in_flight":
            out[d] = "loaded in a campaign that is still sending, not yet emailed"
        elif status == "emailed_no_response" and last is not None:
            age = ((today or date.today()) - last).days
            if age < min_days():
                out[d] = f"emailed {age} days ago, needs {min_days()}"
    return out


def age_rank(domain: str, today: date | None = None) -> int | None:
    """Sort key, lower goes first. None when the gate is off for this client.

    0 never emailed (or loaded but never sent), 1 last emailed 120+ days ago,
    2 90+, 3 60+, 9 under 60 (blocked anyway).
    """
    if not active():
        return None
    age = days_since(domain, today)
    if age is None:
        return 0
    for rank, floor in enumerate(ladder_days(), start=1):
        if age >= floor:
            return rank
    return 9
