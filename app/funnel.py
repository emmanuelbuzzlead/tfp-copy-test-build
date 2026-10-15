"""Keep company_funnel (the tier table) current after sourcing.

ProductEVO builds company_funnel with two root scripts and nobody calls them from the app: someone runs
funnel_dedupe.py after a sourcing run, then history_backfill2.py to tier everything. Until then a newly sourced company
has no funnel row, so scaling.pick_companies (which joins staged_companies to company_funnel on tier = 1) cannot see it.

This is the ONE addition to the ProductEVO system in the Lumion build: it runs those same two scripts, unchanged, from the
app, right after sourcing, so "find companies we have never contacted" feeds the tier table without a person in the loop.
funnel_dedupe labels every sourced company never_contacted, previously_contacted or suppressed; history_backfill2 then sets
tier 1 for never_contacted, tiers 2 to 5 by days since contact, and adds companies that exist only in the send history.
"""
from __future__ import annotations

import os
import runpy
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("funnel_dedupe.py", "history_backfill2.py")
MIN_INTERVAL_S = int(os.getenv("FUNNEL_REFRESH_MIN_INTERVAL_S", "300"))

_lock = threading.Lock()
_last = 0.0


def refresh(run_id: str | None = None, force: bool = False) -> dict:
    """Run both scripts in order. Returns {script: 'ok' or the error}. Skips if it ran in the last few minutes."""
    global _last
    with _lock:
        if not force and time.monotonic() - _last < MIN_INTERVAL_S:
            return {"status": "recent"}
        try:
            Path("/app/logs").mkdir(parents=True, exist_ok=True)     # history_backfill2 logs there
        except OSError:
            pass
        out: dict[str, str] = {}
        for name in SCRIPTS:
            try:
                runpy.run_path(str(ROOT / name), run_name="__main__")
                out[name] = "ok"
            except SystemExit:
                out[name] = "ok"
            except Exception as exc:                                  # report which step failed, never swallow it
                out[name] = f"failed: {str(exc)[:160]}"
                break
        _last = time.monotonic()
    try:
        from . import db
        db.log(run_id, "funnel", f"refresh: {out}")
    except Exception:
        pass
    return out
