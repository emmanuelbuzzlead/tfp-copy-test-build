"""Decide which copy arm won, and say so.

Troy, 2026-09-19, on probing the ProductEVO build: the machine could run the experiment
but never conclude it. Two campaigns went out and nothing compared them; the one screen
built for the comparison asked for a hardcoded experiment id that the copy test never
wrote; and even a clear result had no way to become the client's copy.

This module closes that. It reads the exposures each arm actually received, compares
them, and either names a winner or says plainly that the arms have not separated yet.

WHY REPLY RATE IS THE PRIMARY READ.  Interested replies are far too sparse to separate
two arms at one test's volume: three thousand contacts per arm at ProductEVO's rates is
roughly thirty replies and one or two interested, and two is not distinguishable from
one. Reply rate is the same signal a hundred times denser, and it is the thing the
opening line actually controls. Interested rate is reported alongside and governs when
it is strong enough to, because a variant that wins on replies while losing on interest
has bought attention it cannot convert -- which is exactly the risk with a fear-led
angle.

A verdict is deliberately conservative: an arm wins only when its interval clears the
other's entirely. "Not yet" is a real answer and the honest one most of the time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import db

# Below this many replies an arm cannot be judged at all; the interval is wider than
# any difference worth acting on.
MIN_REPLIES_PER_ARM = 30
# The share of the interested signal that has to be present before it can overrule a
# reply-rate result. Below it, interest is reported but does not decide.
MIN_INTERESTED_TO_GOVERN = 8


@dataclass(frozen=True)
class Arm:
    name: str
    delivered: int
    replied: int
    interested: int
    booked: int

    @property
    def reply_rate(self) -> float:
        return self.replied / self.delivered if self.delivered else 0.0

    @property
    def interested_of_replies(self) -> float:
        return self.interested / self.replied if self.replied else 0.0

    @property
    def interested_of_delivered(self) -> float:
        return self.interested / self.delivered if self.delivered else 0.0


@dataclass(frozen=True)
class Result:
    experiment_id: str
    arms: tuple[Arm, ...] = field(default=())
    winner: str | None = None
    status: str = "pending"          # pending | decided | no_data
    reason: str = ""
    detail: str = ""

    @property
    def decided(self) -> bool:
        return self.status == "decided"


def wilson(successes: int, total: int, z: float = 1.959964) -> tuple[float, float]:
    """Two-sided 95% Wilson interval. No scipy; this repo pins its dependencies."""
    if total <= 0:
        return 0.0, 1.0
    successes = max(0, min(successes, total))
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - spread) / denom), min(1.0, (centre + spread) / denom)


def _separated(best: Arm, rest: list[Arm], metric: str) -> bool:
    """True when the best arm's interval clears every other arm's entirely."""
    if metric == "reply":
        b_lo, _ = wilson(best.replied, best.delivered)
        for other in rest:
            _, o_hi = wilson(other.replied, other.delivered)
            if b_lo <= o_hi:
                return False
        return True
    b_lo, _ = wilson(best.interested, best.replied)
    for other in rest:
        _, o_hi = wilson(other.interested, other.replied)
        if b_lo <= o_hi:
            return False
    return True


def evaluate(experiment_id: str | None = None) -> Result:
    """Compare the arms of one experiment and decide, or explain why not yet."""
    if not experiment_id:
        seen = db.experiments_seen()
        if not seen:
            return Result("", status="no_data",
                          reason="no sends have been recorded against any experiment yet")
        experiment_id = seen[0]

    rows = db.experiment_performance(experiment_id)
    arms = tuple(Arm(r["variant_id"], int(r["delivered"] or 0), int(r["replied"] or 0),
                     int(r["interested"] or 0), int(r["booked"] or 0))
                 for r in rows if r.get("variant_id"))
    if len(arms) < 2:
        return Result(experiment_id, arms, status="no_data",
                      reason=f"only {len(arms)} arm has exposures; a comparison needs two")

    thin = [a for a in arms if a.replied < MIN_REPLIES_PER_ARM]
    if thin:
        short = ", ".join(f"{a.name} has {a.replied}" for a in thin)
        return Result(experiment_id, arms, status="pending",
                      reason=f"needs {MIN_REPLIES_PER_ARM} replies per arm before the "
                             f"difference means anything: {short}")

    # Interest governs only when there is enough of it to carry the decision.
    total_interested = sum(a.interested for a in arms)
    metric = "interested" if total_interested >= MIN_INTERESTED_TO_GOVERN else "reply"
    key = (lambda a: a.interested_of_replies) if metric == "interested" \
        else (lambda a: a.reply_rate)

    ranked = sorted(arms, key=key, reverse=True)
    best, rest = ranked[0], list(ranked[1:])
    detail = " · ".join(
        f"{a.name}: {a.delivered:,} delivered, {a.replied} replied "
        f"({a.reply_rate:.2%}), {a.interested} interested "
        f"({a.interested_of_replies:.1%} of replies)" for a in ranked)

    if not _separated(best, rest, metric):
        return Result(experiment_id, arms, status="pending",
                      reason=f"the arms have not separated on {metric} rate yet; "
                             f"keep sending or accept there is no real difference",
                      detail=detail)

    # A win on replies that loses badly on interest is not a win.
    if metric == "reply" and total_interested:
        worse = [a for a in rest
                 if a.interested_of_replies > best.interested_of_replies * 1.5]
        if worse:
            return Result(experiment_id, arms, status="pending",
                          reason=(f"{best.name} wins on replies but {worse[0].name} "
                                  f"converts them far better "
                                  f"({worse[0].interested_of_replies:.1%} against "
                                  f"{best.interested_of_replies:.1%}); read the replies "
                                  f"before calling this"),
                          detail=detail)

    return Result(experiment_id, arms, winner=best.name, status="decided",
                  reason=(f"{best.name} wins on {metric} rate and its interval clears "
                          f"the other arm entirely"),
                  detail=detail)


# How much of a lead is worth calling out as "one arm is losing, stop paying for it"
# before the arms have formally separated. Not a verdict -- a warning that the cheaper
# decision is available early.
EARLY_STOP_RATIO = 1.8
EARLY_STOP_MIN_REPLIES = 12


def check_and_report(profile, run_id: str | None = None) -> dict:
    """Judge the open copy test and tell Slack, once, when there is something to say.

    Troy, 2026-09-22: "would it not be better if it gives me the outcome and I use a
    button to scale it?" The comparison and the Promote button already existed; nothing
    ever TRIGGERED them, so a finished test sat there until somebody remembered to type
    /ve conclude. This runs in the daily review.

    Deliberately quiet. Too few replies says nothing at all, because a daily "not yet"
    is noise that teaches you to ignore the channel. It speaks when a winner exists,
    when the arms have been given a fair run and refuse to separate, or when one arm is
    losing badly enough that stopping it early saves real money.
    """
    from . import db
    from .clients import slack

    test = db.unfinished_copy_test(profile.name)
    if not test:
        return {"status": "no_open_test"}

    res = evaluate(test.get("experiment_id") or None)
    arms = res.arms
    if not arms:
        return {"status": "no_exposures", "run_id": test["run_id"]}

    total_replies = sum(a.replied for a in arms)
    say = None
    if res.decided:
        say = "winner"
    elif total_replies and min(a.replied for a in arms) >= MIN_REPLIES_PER_ARM:
        say = "no_separation"          # a fair run, and they will not separate
    elif total_replies >= EARLY_STOP_MIN_REPLIES:
        ranked = sorted(arms, key=lambda a: a.reply_rate, reverse=True)
        best, worst = ranked[0], ranked[-1]
        if worst.reply_rate and best.reply_rate / worst.reply_rate >= EARLY_STOP_RATIO:
            say = "early_gap"

    if not say:
        return {"status": "too_early", "replies": total_replies}

    try:
        slack.copy_test_result(profile.display_name, profile.name, res)
        if say == "early_gap":
            ranked = sorted(arms, key=lambda a: a.reply_rate, reverse=True)
            slack.post(
                f"{profile.display_name}: one arm is well behind",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text":
                         f"*{ranked[0].name}* is replying at {ranked[0].reply_rate:.2%} "
                         f"against *{ranked[-1].name}* at {ranked[-1].reply_rate:.2%}, "
                         f"on {total_replies} replies. Not decided yet -- "
                         f"{MIN_REPLIES_PER_ARM} replies per arm are needed for that -- "
                         f"but you can stop the weaker arm now rather than pay to "
                         f"finish it."}}])
    except Exception as exc:
        return {"status": "post_failed", "said": say, "error": str(exc)[:160]}
    return {"status": say, "winner": res.winner, "replies": total_replies}


def promote(client_name: str, variant: str, by: str = "system",
            evidence: str = "") -> str:
    """Make a winning arm the client's copy from now on.

    Stored in the database rather than written back into the repo: the running service
    has an ephemeral filesystem, so a file it wrote would vanish on the next deploy.
    prompts._dir() consults this whenever no variant is named, which is every ordinary
    run once the test is over.
    """
    db.set_active_variant(client_name, variant, by=by, evidence=evidence)
    return (f"{variant} is now {client_name}'s copy. Ordinary runs use it from here; "
            f"the losing arm's prompts stay in the repo for reference.")
