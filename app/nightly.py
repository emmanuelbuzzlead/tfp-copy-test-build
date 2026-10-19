"""The nightly copy-test check, run by the web service (Emmanuel, 2026-10-19: "i also want the nightly schedule turned on, say go, no need for slack
approval before pausing").

Once a day at NIGHTLY_UTC_HOUR:MINUTE (default 14:30 UTC) it:
  1. runs testgrid.evaluate_and_record(): day 7 to 10 it scales whichever angle is ahead (only if someone has a positive reply); at day 10, and only once every
     angle has SENT min_sends_per_angle emails, it names the winner, PAUSES the losers on its own (no Slack approval) and raises the winner's target.
  2. checks any lookalike angle that is 7 days old.
  3. posts a short plain-language status to Slack: sends, replies, interested per angle, and what the rules will do next.
It records the date it ran in nightly_state, so a redeploy near the hour does not post twice.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from . import db
from .clients import slack


def _once_today() -> bool:
    """True the first time it is called on a given UTC date (and records it)."""
    today = datetime.now(timezone.utc).date().isoformat()
    with db.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS nightly_state (name TEXT PRIMARY KEY, last_date TEXT NOT NULL)")
        cur.execute("""INSERT INTO nightly_state (name, last_date) VALUES ('copy_test', %s)
                       ON CONFLICT (name) DO UPDATE SET last_date = EXCLUDED.last_date WHERE nightly_state.last_date <> EXCLUDED.last_date
                       RETURNING 1""", (today,))
        return cur.fetchone() is not None


def status_text(result: dict) -> str:
    by = result.get("by_angle") or {}
    lines = [f":crystal_ball: *Copy test, nightly check* ({result.get('status', 'unknown')})"]
    for a, row in sorted(by.items()):
        sent = row.get("sent")
        sent_txt = f"{sent:,} sent, " if sent is not None else ""
        lines.append(f"  {a}: {sent_txt}{row.get('leads', 0):,} loaded, {row.get('replied', 0)} replies, {row.get('interested', 0)} interested, {row.get('booked', 0)} booked")
    if result.get("reason"):
        lines.append(f"  {result['reason']}")
    if result.get("paused"):
        lines.append(f"  Paused automatically: {', '.join(result['paused'])}.")
    return "\n".join(lines)


def run_once() -> dict:
    from . import angle_derivation, testgrid
    out: dict = {}
    try:
        out = testgrid.evaluate_and_record()
    except Exception as exc:
        slack.post(f":rotating_light: *Copy test check failed to run tonight*: {str(exc)[:250]}. Nothing was decided or paused; it tries again tomorrow.")
        return {"status": "error", "error": str(exc)}
    try:
        out["derived"] = angle_derivation.check_derived_angles()
    except Exception as exc:
        db.log(None, "nightly", f"derived-angle check failed: {exc}", level="warn")
    try:
        slack.post(status_text(out))
    except Exception:
        pass
    return out


def _seconds_until(hour: int, minute: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = datetime.fromtimestamp(target.timestamp() + 86400, tz=timezone.utc)
    return (target - now).total_seconds()


def loop() -> None:
    hour = int(os.getenv("NIGHTLY_UTC_HOUR", "14"))
    minute = int(os.getenv("NIGHTLY_UTC_MINUTE", "30"))
    while True:
        time.sleep(max(_seconds_until(hour, minute), 1))
        try:
            if _once_today():
                run_once()
        except Exception as exc:
            print(f"[nightly] failed: {exc}", flush=True)
        time.sleep(90)       # step past the minute so one hour:minute cannot fire twice


def start() -> threading.Thread:
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
