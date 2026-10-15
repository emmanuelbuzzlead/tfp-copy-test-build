"""Hunter email finder + verifier.

Decision #12: Hunter is NOT called for every contact. Quick Enrich already returns an
email plus an email_verification_date, so Hunter runs only when QE gave us no email,
or when its verification is older than HUNTER_REVERIFY_DAYS (default 30). That gate
is the difference between paying Hunter once per contact and paying it only on the
minority that actually need it.

Only "deliverable" survives. Risky/unknown/undeliverable are dropped, never pushed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import settings
from .http import VendorError, request

BASE = "https://api.hunter.io/v2"
DELIVERABLE = "deliverable"

# Hunter answers with BOTH a `result` and a `status`, in two different vocabularies:
#   result: deliverable | undeliverable | risky | unknown
#   status: valid | invalid | accept_all | webmail | disposable | unknown
# Reading `status` and comparing it to "deliverable" never matches, so a perfectly good
# address ("status": "valid", "result": "deliverable", "score": 100) was rejected and
# EVERY contact was dropped. Read `result` first, and accept `status == valid` as the
# equivalent verdict when only that is present.
GOOD_RESULTS = {"deliverable"}
GOOD_STATUSES = {"valid"}

# Hunter returns HTTP 222 for "we failed to verify for reasons outside of our control,
# retry later". That is a statement about Hunter, not about the address, so it must not
# be recorded as undeliverable -- the contact is simply unresolved this run.
UNAVAILABLE = "hunter_unavailable"


def needs_hunter(email: str | None, verified_at: datetime | None) -> bool:
    if not email:
        return True
    if verified_at is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.hunter_reverify_days)
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    return verified_at < cutoff


def _key() -> str:
    if not settings.hunter_api_key:
        raise RuntimeError("HUNTER_API_KEY is not set")
    return settings.hunter_api_key


def find_email(domain: str, first_name: str, last_name: str) -> tuple[str | None, str | None]:
    """Returns (email, status). Status is Hunter's verification verdict when present."""
    try:
        payload = request(
            "hunter", "GET", f"{BASE}/email-finder",
            params={"domain": domain, "first_name": first_name,
                    "last_name": last_name, "api_key": _key()},
        )
    except VendorError:
        return None, None
    data = (payload or {}).get("data") or {}
    verification = (data.get("verification") or {})
    verdict = (verification.get("result") or verification.get("status") or "").lower()
    if verdict in GOOD_RESULTS or verdict in GOOD_STATUSES:
        verdict = DELIVERABLE
    return data.get("email"), (verdict or None)


def verify(email: str) -> str | None:
    """Normalised verdict: 'deliverable', another Hunter verdict, or UNAVAILABLE/None."""
    try:
        payload = request("hunter", "GET", f"{BASE}/email-verifier",
                          params={"email": email, "api_key": _key()})
    except VendorError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("errors"):
        # HTTP 222 and friends: Hunter could not check, so we learned nothing.
        return UNAVAILABLE
    data = payload.get("data") or {}
    result = (data.get("result") or "").lower()
    status = (data.get("status") or "").lower()
    if result in GOOD_RESULTS or status in GOOD_STATUSES:
        return DELIVERABLE
    return result or status or None


def resolve(email: str | None, verified_at: datetime | None, domain: str,
            first_name: str, last_name: str) -> tuple[str | None, str]:
    """Return (deliverable_email_or_None, reason).

    Skips Hunter entirely when Quick Enrich's email is fresh enough (decision #12).
    """
    if not needs_hunter(email, verified_at):
        return email, "quickenrich verified within window"

    if email:
        status = verify(email)
        if status == DELIVERABLE:
            return email, "hunter verified"
        if status == UNAVAILABLE:
            # Hunter could not reach a verdict. Falling through to email-finder would
            # spend another call to learn the same nothing, so stop here and mark the
            # contact for a later retry rather than burning it as undeliverable.
            return None, "hunter could not verify this run (retry later)"
        # QE's address failed verification; try to find a better one before giving up.
    found, status = find_email(domain, first_name, last_name)
    if found and (status == DELIVERABLE or (status is None and verify(found) == DELIVERABLE)):
        return found, "hunter found + verified"
    return None, f"no deliverable email (hunter status: {status or 'none'})"
