"""Supply probe for a client that sources through the WATERFALL (QuickEnrich, then Lead Ocean).

The original probe (s2b_freshness.probe) measures a vertical's universe through AI Ark's company search. For
ProQ and MasterNow the waterfall is the main source and AI Ark is the last, so the measurement has to come from
the same providers that will actually supply the companies.

For one declared vertical this does three cheap things:
  1. SUPPLY. Pull up to `cap` companies per provider (US, the client's employee band, the vertical's declared
     industry labels) and merge them by domain. A source that returns exactly `cap` is reported as capped, so
     the number is a floor, never an overstatement.
  2. FRESH. Remove every company this client may not touch right now: already emailed, replied, in flight, or
     owned by the other client. This is the same db.blocked_domains the real run uses.
  3. FIT. Run a small random sample of the fresh companies through the real fit gate (the same Haiku call the
     run uses) to get a keep rate. Projected usable companies = fresh x keep rate.

The ranking key is projected usable companies, highest first: the verticals with the most never-emailed, fit,
owned companies come first. Cost: provider search calls plus about one Haiku call per 20 sampled companies.
"""

from __future__ import annotations

import random
from collections import Counter

from .. import db
from ..clients import leadocean, quickenrich
from ..config import settings
from . import s3_companies

CAP = 1000            # companies pulled per provider per vertical
SAMPLE = 30           # fresh companies sampled through the fit gate
MIN_FRESH = 50        # below this the vertical is too small to carry a batch


def _use_leadocean() -> bool:
    try:
        from .. import client
        return bool(client.profile().use_leadocean)
    except Exception:
        return False


def fetch_universe(vertical, cap: int = CAP) -> tuple[dict[str, dict], dict]:
    """{domain: company} merged across providers, plus per-provider counts and errors."""
    rows: dict[str, dict] = {}
    detail: dict = {"sources": {}, "errors": {}}
    sources = [("quickenrich", quickenrich.find_companies)]
    if _use_leadocean():
        sources.append(("leadocean", leadocean.find_companies))
    for name, fn in sources:
        try:
            found = fn(industries=vertical.industries, employee_min=vertical.employee_min,
                       employee_max=vertical.employee_max, limit=cap) or []
        except Exception as exc:
            detail["errors"][name] = str(exc)[:120]
            continue
        detail["sources"][name] = {"returned": len(found), "capped": len(found) >= cap}
        for c in found:
            d = db.normalize_domain(c.get("domain") or "")
            if d and d not in rows:
                c["domain"] = d
                c["_source_query"] = name
                rows[d] = c
    return rows, detail


def probe(vertical, run_id: str | None = None, known_density=None, cap: int = CAP,
          sample: int = SAMPLE) -> dict:
    """Measure one vertical. Returns the same shape the backlog stores."""
    rows, detail = fetch_universe(vertical, cap)
    universe = len(rows)
    blocked = db.blocked_domains(list(rows)) if rows else {}
    why = Counter()
    for reason in blocked.values():
        why["other client" if "other" in reason else "replied/unsubscribed" if "permanent" in reason
            else "still sending" if "sending" in reason else "emailed recently"] += 1
    fresh = [c for d, c in rows.items() if d not in blocked]
    fresh_rate = (len(fresh) / universe) if universe else 0.0

    keep_rate = None
    if fresh:
        picked = random.Random(vertical.name).sample(fresh, min(sample, len(fresh)))
        verdicts: dict = {}
        for i in range(0, len(picked), s3_companies.ENFORCE_BATCH):
            try:
                verdicts.update(s3_companies._enforce_chunk(vertical, picked[i:i + s3_companies.ENFORCE_BATCH], force_model=True))
            except Exception as exc:
                detail["errors"]["fit_sample"] = str(exc)[:120]
                break
        if verdicts:
            kept = sum(1 for dec, _ in verdicts.values() if dec in ("KEEP", "IDPR"))
            keep_rate = kept / len(verdicts)
    projected = int(len(fresh) * (keep_rate or 0.0))

    if not universe:
        verdict = "no supply returned"
    elif len(fresh) < MIN_FRESH:
        verdict = f"only ~{len(fresh)} fresh companies"
    elif keep_rate is None or projected < 1:
        verdict = "fit sample kept nothing"
    else:
        verdict = "ok"
    capped = any(s.get("capped") for s in detail["sources"].values())
    return {
        "vertical": vertical.name, "verdict": verdict,
        "projected": projected, "projected_raw": projected,
        "density": None, "fresh_rate": round(fresh_rate, 3),
        "universe": universe, "fresh": len(fresh), "keep_rate": None if keep_rate is None else round(keep_rate, 3),
        "capped": capped, "blocked_reasons": dict(why), **detail,
    }
