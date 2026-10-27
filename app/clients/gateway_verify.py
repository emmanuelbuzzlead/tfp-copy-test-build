"""Email verification through the BuzzLead gateway: POST /data/emails/find.

MillionVerifier, TryKitt and BounceBan keys live in ONE place (the gateway), not in every
build. A key rotated or topped up there fixes every expander at once; before this, each
build held its own copy and a rotation left three of them silently rejecting every call.

The gateway runs the same cascade the vendored email_waterfall ran (MV triage, TryKit and
BounceBan on risky addresses, TryKit finder when an input email fails, format guesser
last), and verifies a supplied email first, so it is a drop-in for run_waterfall_batch.
`providers` is pinned to those three vendors on purpose: AI Ark and QuickEnrich are in
the gateway's default chain, and finding is app/finders.py's job. Leaving them on would
spend AI Ark caps on a different chain than the agreed one.

Measured 2026-10-22, 50 role addresses in one call: 37.5s end to end (about 7s fixed
plus 0.6s per address). Batches are 16 per call, run in parallel but kept modest: the
gateway's concurrency ceiling is not documented. A sister build measured 50-person calls
returning empty answers for 92% of people under load (4 valid of 50) against 95%+ valid
at 16, and an empty answer reads as "unknown", so it is re-asked once in small calls and
the leftover is logged. On 2026-10-27 TFP did not reproduce it at 150 people (0 empty).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..config import settings
from .http import VendorError, VendorOutage, request

DEFAULT_BASE = "https://research-615339619342.us-east4.run.app"
PROVIDERS = ["millionverifier", "trykit", "bounceban"]
BATCH = 16
RETRY_BATCH = 8
RETRY_PAUSE = 30
WORKERS = 4
# A 50-person call with finder fallbacks is slow; the shared 90s read timeout cuts it off
# mid-spend, which would bill the credits and discard the answer.
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


def enabled() -> bool:
    """On by default whenever a BuzzLead key exists. VERIFY_VIA_GATEWAY=false goes back to
    the build's own vendor keys. Independent of BUZZLEAD_ENABLED, which only governs
    company sourcing (builds that source from AI Ark keep that off and still verify here)."""
    raw = os.getenv("VERIFY_VIA_GATEWAY")
    on = True if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}
    return on and bool(settings.buzzlead_api_key)


def _base() -> str:
    return (settings.buzzlead_base or DEFAULT_BASE).rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.buzzlead_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post(path: str, body: dict) -> dict:
    try:
        return request("buzzlead-verify", "POST", f"{_base()}{path}", headers=_headers(),
                       json_body=body, timeout=_TIMEOUT) or {}
    except VendorError as e:
        # 401/403: the key is rejected. 402: the gateway's own vendor credit is gone.
        # Either way no later batch can succeed, so stop the stage with its work banked.
        if e.status in (401, 402, 403):
            raise VendorOutage(str(e)) from e
        raise


def _find_chunk(people: list[dict]) -> list[dict]:
    data = _post("/data/emails/find", {"people": people, "providers": PROVIDERS})
    results = data.get("results") or []
    if len(results) == len(people):
        return results
    # Misaligned answer: rebuild positions from the echoed input rather than guess.
    by_key = {_key(r.get("input") or {}): r for r in results}
    return [by_key.get(_key(p), {}) for p in people]


def _key(p: dict) -> tuple:
    return ((p.get("first_name") or "").lower(), (p.get("last_name") or "").lower(),
            (p.get("domain") or "").lower(), (p.get("email") or "").lower())


def _find_chunk_safe(people: list[dict]) -> list[dict]:
    """_find_chunk, but a gateway error (a 504 that outlasted the HTTP layer's four tries) costs only this batch: wait, ask again in halves, and return {} for
    anyone still unanswered so the re-ask in find_and_verify and the leftover count see them. A rejected key (VendorOutage) still stops everything."""
    try:
        return _find_chunk(people)
    except VendorOutage:
        raise
    except VendorError:
        time.sleep(RETRY_PAUSE)
        out: list[dict] = []
        for i in range(0, len(people), RETRY_BATCH):
            try:
                out += _find_chunk(people[i:i + RETRY_BATCH])
            except VendorOutage:
                raise
            except VendorError:
                out += [{} for _ in people[i:i + RETRY_BATCH]]
        return out


def find_and_verify(people: list[dict]) -> list[dict]:
    """people: dicts with first_name, last_name, domain, company_name, title, email,
    linkedin_url. Returns one result per person, in order, each with valid_email, status,
    source and steps. A person the gateway returned nothing for comes back as {}."""
    if not people:
        return []
    chunks = [people[i:i + BATCH] for i in range(0, len(people), BATCH)]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(chunks))) as pool:
        out = list(pool.map(_find_chunk_safe, chunks))
    results = [r for chunk in out for r in chunk]
    # A person with no answer would read as "unknown" downstream, which is a lost contact
    # rather than a verdict. Ask again once, in smaller calls, before accepting that.
    missing = [i for i, r in enumerate(results) if not r]
    if missing:
        again = [missing[i:i + RETRY_BATCH] for i in range(0, len(missing), RETRY_BATCH)]
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(again))) as pool:
            answers = list(pool.map(lambda idx: _find_chunk([people[i] for i in idx]), again))
        for idx, ans in zip(again, answers):
            for i, r in zip(idx, ans):
                if r:
                    results[i] = r
        left = sum(1 for r in results if not r)
        try:
            from .. import db
            db.log(None, "verify", f"gateway gave no answer for {len(missing)} of {len(people)}; {left} still empty after one re-ask",
                   "warning" if left else "info")
        except Exception:
            pass
    return results


def verify_one(email: str, providers: list[str] | None = None) -> str:
    """One address, verification only (POST /data/emails/verify), no finders. Returns
    valid / catch_all / invalid / unknown, or 'unavailable' when the call cannot be made."""
    try:
        data = _post("/data/emails/verify",
                     {"emails": [email], "providers": providers or ["millionverifier"]})
    except (VendorError, VendorOutage):
        return "unavailable"
    rows = data.get("results") or []
    return ((rows[0].get("status") if rows else None) or "unknown").lower()


def verify_many(emails: list[str], providers: list[str] | None = None) -> dict[str, dict]:
    """Verification only (POST /data/emails/verify, 100 per call), no finders.

    Returns {lowercased email: {"status", "steps"}}. An address the gateway could not
    answer for is ABSENT, never defaulted, so a caller that gates on this fails closed.
    A chunk that errors drops only its own addresses, with one chunk's outage not
    hiding what the others returned."""
    emails = [e for e in dict.fromkeys(e for e in emails if e)]
    if not emails:
        return {}
    pv = providers or ["millionverifier"]
    chunks = [emails[i:i + 100] for i in range(0, len(emails), 100)]

    def one(chunk: list[str]) -> list[dict]:
        try:
            return (_post("/data/emails/verify", {"emails": chunk, "providers": pv}) or {}).get("results") or []
        except (VendorError, VendorOutage):
            return []

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(chunks))) as pool:
        rows = [r for part in pool.map(one, chunks) for r in part]
    return {(r.get("email") or "").lower(): {"status": (r.get("status") or "unknown").lower(),
                                              "steps": r.get("steps") or []}
            for r in rows if r.get("email")}
