"""Deterministic experiments and evidence-aware traffic allocation.

The LLM may propose copy, but it never gets to choose its own treatment.  Assignment is
stable from the contact email, recorded on the contact and exposure, and kept independent
of model output.  That makes later outcome differences attributable rather than anecdotal.
"""

from __future__ import annotations

import hashlib
import math
import threading


COPY_EXPERIMENT = "copy-angle-v1"
CONTROL = "control"
MOMENTUM = "momentum-led"
VARIANTS = (CONTROL, MOMENTUM)

_cache: dict[str, dict[str, float]] = {}
_lock = threading.Lock()


def title_segment(title: str | None) -> str:
    value = (title or "").lower()
    if any(x in value for x in ("marketing", "brand", "digital")):
        return "marketing"
    if any(x in value for x in ("communication", "public relation", "media relation", "pr ")):
        return "communications_pr"
    if any(x in value for x in ("founder", "owner", "chief executive", "ceo", "president")):
        return "founder_executive"
    return "other"


def _default_weights() -> dict[str, float]:
    return {CONTROL: 0.5, MOMENTUM: 0.5}


def _wilson_lower(successes: int, total: int, z: float = 1.2816) -> float:
    """One-sided ~90% lower bound, used only to rank mature variants."""
    if total <= 0:
        return 0.0
    p = successes / total
    d = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - spread) / d)


def weights(experiment_id: str = COPY_EXPERIMENT, refresh: bool = False) -> dict[str, float]:
    """50/50 while learning; 80/20 winner/exploration once both arms have evidence.

    `delivered` from db.experiment_performance is intentionally the seven-day-matured,
    non-bounced intent-to-treat population. Newly loaded leads cannot choose a winner
    before they have had time to reply.
    """
    with _lock:
        if not refresh and experiment_id in _cache:
            return dict(_cache[experiment_id])
    result = _default_weights()
    try:
        from . import db
        rows = {r["variant_id"]: r for r in db.experiment_performance(experiment_id)}
        if all(int(rows.get(v, {}).get("delivered") or 0) >= 300 for v in VARIANTS):
            def score(v: str) -> tuple[float, float]:
                row = rows[v]
                delivered = int(row.get("delivered") or 0)
                interested = int(row.get("interested") or 0)
                booked = int(row.get("booked") or 0)
                # Bookings are the stronger outcome. They add three value events while
                # remaining visible separately in every report.
                value = interested + 3 * booked
                return _wilson_lower(value, delivered), value / delivered
            winner = max(VARIANTS, key=score)
            loser = next(v for v in VARIANTS if v != winner)
            result = {winner: 0.8, loser: 0.2}
    except Exception:
        # Cold starts and local unit tests have no database. Balanced assignment is safe.
        result = _default_weights()
    with _lock:
        _cache[experiment_id] = dict(result)
    return result


def assign(email: str, experiment_id: str = COPY_EXPERIMENT) -> str:
    """Stable weighted assignment; retries and resumes always retain the same arm."""
    digest = hashlib.sha256(f"{experiment_id}:{email.strip().lower()}".encode()).digest()
    point = int.from_bytes(digest[:8], "big") / 2**64
    running = 0.0
    chosen = VARIANTS[-1]
    for variant, share in weights(experiment_id).items():
        running += share
        if point < running:
            chosen = variant
            break
    return chosen


def copy_instruction(variant: str) -> str:
    if variant != MOMENTUM:
        return ""
    return (
        "\n\nEXPERIMENT TREATMENT: Keep every format, length, tone, and CTA rule above. "
        "For the opening angle, lead with a concrete positive momentum signal from the "
        "research (growth, launch, expansion, customer proof, or timely expertise). Do not "
        "frame the company as having a weakness or a media gap."
    )
