"""One evidence-aware outcome contract for seed selection and scale/drop.

The primary automatic outcome is interested replies per send. Booked meetings are
recorded manually when there is no CRM and receive an explicit bonus. Share-of-replies
remains diagnostic; using it as the objective can prefer fewer interested prospects.

A campaign that has not yet reached the send floor or the week-long wait is
PENDING, never a loser -- judging on 200 sends poisons the memory with noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .config import settings


@dataclass(frozen=True)
class Verdict:
    status: str          # pending | winner | loser
    reason: str
    rate: float
    of_replies: float
    of_leads: float
    of_sends: float
    confidence_low: float = 0.0
    confidence_high: float = 0.0

    @property
    def is_winner(self) -> bool:
        return self.status == "winner"


def rates(*, leads: int = 0, sends: int = 0, replies: int = 0,
          interested: int = 0) -> tuple[float, float, float]:
    of_replies = (interested / replies) if replies else 0.0
    of_leads = (interested / leads) if leads else 0.0
    of_sends = (interested / sends) if sends else 0.0
    return of_replies, of_leads, of_sends


def governing_rate(of_replies: float, of_leads: float, of_sends: float) -> float:
    metric = (settings.scale_metric or "of_sends").strip().lower()
    if metric == "of_leads":
        return of_leads
    if metric == "of_sends":
        return of_sends
    return of_replies


def wilson(successes: int, total: int, z: float = 1.644854) -> tuple[float, float]:
    """Two-sided 90% Wilson interval without a scipy dependency."""
    if total <= 0:
        return 0.0, 1.0
    successes = max(0, min(successes, total))
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - spread) / denom), min(1.0, (centre + spread) / denom)


def age_days(launched) -> float | None:
    """Days since launch, or None when we have no clock (lifetime historical campaigns)."""
    if launched is None:
        return None
    if isinstance(launched, str):
        # EmailBison sends "2026-09-14T20:09:43.000000Z", which fromisoformat rejects
        # before Python 3.11 and which this returned None for. The review happens to
        # pass ve_campaigns.created_at, already a datetime, so it never bit -- but None
        # here now means "treat as overdue", which SKIPS both the age gate and the
        # progress gate. A parse failure must not be the thing that lets a campaign be
        # judged early on partial delivery.
        try:
            launched = datetime.fromisoformat(launched.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(launched, datetime):
        start = launched if launched.tzinfo else launched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - start).total_seconds() / 86400
    return None


def judge(*, sends: int = 0, replies: int = 0, interested: int = 0,
          booked: int = 0, leads: int = 0, launched=None, progress=None) -> Verdict:
    """Apply the single winner contract.

    Gates, in order:
      1. age >= SCALE_MIN_DAYS when a launch timestamp exists (default 7)
      1b. delivery >= SCALE_MIN_PROGRESS percent when the campaign reports it (default
          60), as an AND with the age gate. The send floor below is absolute and so
          stops discriminating once campaigns grow by top-up; percent delivered is the
          only ripeness signal that scales with the campaign.
      2. sends >= SCALE_MIN_SENDS (default 1,000)
      3. compute uncertainty around interested + booking-value events per send
      4. decide early only when the interval clears/fails the bar; at the mature sample,
         use the point estimate so an ambiguous campaign cannot stay pending forever
    """
    of_replies, of_leads, of_sends = rates(
        leads=leads or 0, sends=sends or 0, replies=replies or 0,
        interested=interested or 0)
    rate = governing_rate(of_replies, of_leads, of_sends)
    metric = (settings.scale_metric or "of_sends").strip().lower()
    min_sends = settings.scale_min_sends
    min_days = settings.scale_min_days
    min_replies = settings.scale_min_replies
    bar = settings.scale_rate
    value_events = int(interested or 0) + settings.booking_bonus * int(booked or 0)
    if metric == "of_sends":
        rate = value_events / sends if sends else 0.0
        low, high = wilson(value_events, sends)
    else:
        # Compatibility for an explicitly configured legacy denominator.
        low, high = wilson(int(interested or 0), replies if metric == "of_replies" else leads)

    age = age_days(launched)
    if age is not None and age < min_days:
        return Verdict("pending",
                       f"launched {age:.1f} days ago, needs {min_days} days for replies to land",
                       rate, of_replies, of_leads, of_sends, low, high)

    # Ripeness by delivery, not by clock or by absolute count. Only applied when the
    # campaign actually reports a figure: a missing or unparseable completion_percentage
    # must not silently freeze every verdict as pending.
    # ...and it lapses at scale_max_days, so a campaign starved of senders cannot sit
    # unjudged forever, holding the very accounts that would unstarve the others.
    # `age is None` means no usable created_at. Such a campaign already skips the
    # min_days gate above, so if it could never become overdue the progress gate would
    # apply to it with NO lapse -- the exact self-feeding deadlock scale_max_days exists
    # to prevent, and the one case where we cannot even see how long it has been stuck.
    # Unknown age is treated as overdue: judge it on what it has.
    max_days = int(getattr(settings, "scale_max_days", 0) or 0)
    overdue = max_days > 0 and (age is None or age >= max_days)
    min_progress = float(getattr(settings, "scale_min_progress", 0) or 0)
    if overdue:
        min_progress = 0.0
    if min_progress > 0 and progress is not None:
        try:
            done = float(progress)
        except (TypeError, ValueError):
            done = None
        if done is not None and done < min_progress:
            return Verdict("pending",
                           f"only {done:.1f}% of the sequence delivered, needs "
                           f"{min_progress:.0f}% before the reply picture is real",
                           rate, of_replies, of_leads, of_sends, low, high)

    if (sends or 0) < min_sends:
        return Verdict("pending",
                       f"only {sends or 0} sends, needs {min_sends} for a fair read",
                       rate, of_replies, of_leads, of_sends, low, high)

    if metric == "of_replies" and (replies or 0) < min_replies:
        return Verdict("pending",
                       f"only {replies or 0} replies, needs {min_replies} before a "
                       f"share-of-replies rate means anything",
                       rate, of_replies, of_leads, of_sends, low, high)

    mature = sends >= settings.scale_decision_sends
    if low >= bar or (mature and rate >= bar):
        return Verdict("winner",
                       f"{rate:.2%} outcome value per send ({interested} interested, "
                       f"{booked} booked) clears {bar:.2%}",
                       rate, of_replies, of_leads, of_sends, low, high)
    if high < bar or mature:
        return Verdict("loser",
                       f"{rate:.2%} outcome value per send ({interested} interested, "
                       f"{booked} booked) is below {bar:.2%}",
                       rate, of_replies, of_leads, of_sends, low, high)
    return Verdict("pending",
                   f"{rate:.2%} outcome value per send is inconclusive "
                   f"({low:.2%}-{high:.2%}); continue to {settings.scale_decision_sends} sends",
                   rate, of_replies, of_leads, of_sends, low, high)
