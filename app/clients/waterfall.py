"""Email verification via the vendored email_waterfall: MV -> TryKit -> BounceBan.

This replaces Hunter entirely. Hunter could not resolve catch-all domains -- it returned
`accept_all` and the contact was dropped -- which on 2026-09-09 emptied the whole legal
vertical. Verified the same day on a contact Hunter had rejected:

    Joanna Arnason, Director of Marketing, Porter Wright (catch-all domain)
      Hunter    -> accept_all, dropped
      waterfall -> jarnason@porterwright.com, status "valid", source trykit

Division of labour: FINDING is app/finders.py (QuickEnrich -> AI.ARK -> Apollo). The
waterfall ships its own finders too, and if it sourced contacts we would silently be
running a different chain than the agreed one. So each contact is handed in WITH an
email where we have one, and the waterfall's job is to verify it.

Two behaviours of the vendored module worth knowing:
  - Given an input email it verifies first, and only runs its own finders if that email
    FAILS verification. That fallback is recovery, not re-targeting, so it is left on.
  - Its format-guesser fires last when everything else came back unknown. It costs only
    MV credits and it is what rescues contacts no finder could resolve.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..config import settings

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor"))

# 'valid' is sendable. 'catch_all' means the domain accepts everything and BounceBan
# could not resolve the individual mailbox -- deliverable in practice but unproven, and
# the spec says never push unverified, so it is excluded here and surfaced separately.
SENDABLE = {"valid"}


def _api_keys() -> dict:
    return {
        "millionverifier": settings.millionverifier_api_key,
        "bounceban": settings.bounceban_api_key,
        "trykit": settings.trykitt_api_key,
        "leadmagic": os.getenv("LEADMAGIC_API_KEY"),
        "icypeas": os.getenv("ICYPEAS_API_KEY"),
        "icypeas_secret": os.getenv("ICYPEAS_API_SECRET") or os.getenv("ICYPEAS_SECRET_KEY"),
        "hunter": settings.hunter_api_key,
    }


def available() -> tuple[bool, str]:
    if not settings.millionverifier_api_key:
        return False, "MILLIONVERIFIER_API_KEY not set (the waterfall's only hard requirement)"
    return True, "ok"


def verify_batch(contacts: list[dict], run_id: str | None = None) -> list[dict]:
    """Verify a batch. Each input dict needs first_name, last_name, domain[, email].

    Returns the same dicts with `valid_email`, `email_status` and `email_source` filled
    in. Nothing is dropped here -- the caller decides, so the drop reasons stay in one
    place and can be counted.
    """
    if not contacts:
        return []

    from vendor import email_waterfall as ew

    # Preflight probes every provider on every call, which is wasted latency inside a
    # run we have already health-checked.
    os.environ.setdefault("WATERFALL_PREFLIGHT", "false")

    payload = [
        {
            "first_name": c.get("first_name") or "",
            "last_name": c.get("last_name") or "",
            "domain": c.get("company_domain") or "",
            "company_name": c.get("company_name") or "",
            "title": c.get("title") or "",
            "email": c.get("email") or "",
            "linkedin_url": c.get("linkedin_url") or "",
        }
        for c in contacts
    ]

    results = ew.run_waterfall_batch(payload, _api_keys(),
                                     max_workers=settings.waterfall_workers)

    by_person: dict[tuple, dict] = {}
    for r in results:
        d = r.__dict__ if hasattr(r, "__dict__") else dict(r)
        key = ((d.get("first_name") or "").lower(), (d.get("last_name") or "").lower(),
               (d.get("domain") or "").lower())
        by_person[key] = d

    out = []
    for c in contacts:
        key = ((c.get("first_name") or "").lower(), (c.get("last_name") or "").lower(),
               (c.get("company_domain") or "").lower())
        r = by_person.get(key, {})
        enriched = dict(c)
        enriched["valid_email"] = r.get("valid_email") or None
        enriched["email_status"] = r.get("status") or "unknown"
        enriched["email_source"] = r.get("email_source") or None
        enriched["waterfall_steps"] = r.get("steps_run")
        out.append(enriched)
    return out


def sendable(contact: dict) -> bool:
    return bool(contact.get("valid_email")) and contact.get("email_status") in SENDABLE
