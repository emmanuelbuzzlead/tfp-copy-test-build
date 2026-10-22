"""ProductEVO copy test: 3 angle campaigns, each holding every segment, read per segment.

Design (Troy, 2026-09-26): one campaign per angle, each carrying 1,000 contacts from every
segment, each contact tagged with its vertical. The angle is read across the whole
campaign (3x the volume of a 9-cell grid) and the segment is read afterwards from the tag.

Three jobs, all read-only against EmailBison and additive to the database:

  deal()       split contacts across the angle campaigns at random, balanced per segment,
               so the three audiences are the same by construction
  read_grid()  per (angle, segment) counts straight from outcome_events -- exact per lead,
               no apportioning of campaign totals
  decide()     the pre-declared rules in clients/productevo/copy_test.toml; names a
               champion only when the rules are met, otherwise says why not yet

champion_campaign() is what s7_push consults so later batches join the winner instead of
opening a new campaign. Nothing here sends; the approval gate is untouched.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

CLIENTS_DIR = Path(__file__).resolve().parent.parent / "clients"
# Kept for callers that name ProductEVO's file on purpose.
CONFIG_PATH = CLIENTS_DIR / "productevo" / "copy_test.toml"

_TABLES = """
CREATE TABLE IF NOT EXISTS copy_test_campaigns (
    angle       TEXT NOT NULL,
    campaign_id BIGINT NOT NULL,
    launched_at TIMESTAMPTZ,
    is_primary  BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (angle, campaign_id)
);
ALTER TABLE copy_test_campaigns ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT TRUE;
CREATE TABLE IF NOT EXISTS copy_test_champion (
    id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    angle       TEXT NOT NULL,
    campaign_id BIGINT NOT NULL,
    separated   BOOLEAN NOT NULL,
    reason      TEXT,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def load_config(path: Path | None = None) -> dict | None:
    """The ACTIVE client's copy_test.toml, or None. It used to be ProductEVO's file for every
    client, so IDPR's nightly review could have run ProductEVO's test and auto-expansion
    (found 2026-09-27)."""
    if path is None:
        from .client import current_name
        path = CLIENTS_DIR / current_name() / "copy_test.toml"
    if not path.exists():
        return None
    with path.open("rb") as fh:
        return tomllib.load(fh)


def ensure_tables() -> None:
    with db.cursor() as cur:
        cur.execute(_TABLES)
        # Migration, 2026-10-01: the table used to allow exactly one campaign per angle.
        # Troy now clones a stalled campaign under a new name (e.g. "Stopped Leads
        # PEVO_W1_MMM") to keep delivering, and the clone needs to count toward the SAME
        # angle's score. Drop the old angle-only primary key if it is still there.
        cur.execute("""SELECT conname, array_length(conkey, 1) AS ncols FROM pg_constraint
                       WHERE conrelid = 'copy_test_campaigns'::regclass AND contype = 'p'""")
        row = cur.fetchone()
        if row and row["ncols"] == 1:
            cur.execute(f'ALTER TABLE copy_test_campaigns DROP CONSTRAINT "{row["conname"]}"')
            cur.execute("""ALTER TABLE copy_test_campaigns
                           ADD PRIMARY KEY (angle, campaign_id)""")


def _register_for_reply_sweep(angle: str, campaign_id: int) -> None:
    """Make stop_on_reply.sweep_all() see this campaign too.

    Found 2026-10-01: ve_campaigns (the table that sweep reads) was completely empty.
    Every Wave 1 copy-test campaign was registered in copy_test_campaigns for SCORING,
    but never here, so real replies sitting in EmailBison were never pulled into
    contacts.replied_at / outcome_events -- the day-7 decision would have read zero
    signal from campaigns that actually had dozens of replies. copy_test_campaigns and
    ve_campaigns are two separate registries; a campaign now needs both.
    """
    from . import db as _db
    with _db.cursor() as cur:
        cur.execute(
            """INSERT INTO ve_campaigns (campaign_id, campaign_name, vertical, batch_id,
                                         run_id, pushed_lead_count)
               VALUES (%s,%s,%s,%s,%s,0)
               ON CONFLICT (campaign_id) DO NOTHING""",
            (campaign_id, f"copy-test-{angle}-{campaign_id}", _db.POOL_VERTICAL,
             f"copy-test-{angle}", f"copy-test-{angle}"))


def register_campaign(angle: str, campaign_id: int, launched_at: datetime | None = None) -> None:
    """Registers the PRIMARY campaign for an angle -- the one wave1_fill loads fresh
    leads into and auto_expand_champion scales. See sync_duplicate_campaigns() for
    Troy's renamed clones, which count toward scoring but are never the load target."""
    ensure_tables()
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO copy_test_campaigns (angle, campaign_id, launched_at, is_primary)
               VALUES (%s,%s,%s,TRUE)
               ON CONFLICT (angle, campaign_id) DO UPDATE
                  SET is_primary = TRUE,
                      launched_at = COALESCE(EXCLUDED.launched_at, copy_test_campaigns.launched_at)""",
            (angle, campaign_id, launched_at))
    _register_for_reply_sweep(angle, campaign_id)


def sync_duplicate_campaigns(run_id: str | None = None) -> dict:
    """Find EmailBison campaigns Troy cloned off a registered one by name, and fold them
    into that angle's score.

    Troy, 2026-10-01: "I kept the naming similar with just suffixes" -- when a campaign
    stalls (EmailBison marks it finished with most leads never contacted, usually a
    sender-capacity wall) he clones it under a new name, e.g. base "PEVO_W1_MMM" ->
    "Stopped Leads PEVO_W1_MMM", to keep delivering to the same audience. The clone's
    replies are really that angle's replies and must count, but it is never itself the
    load target or the champion -- only the ORIGINAL registered row (is_primary) is.
    Matches case-insensitively on the primary campaign's own base name appearing
    anywhere in a candidate's name, so a prefix or suffix either side is fine.
    """
    from .clients import emailbison
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id FROM copy_test_campaigns WHERE is_primary")
        primaries = {r["angle"]: int(r["campaign_id"]) for r in cur.fetchall()}
    if not primaries:
        return {"found": {}}
    all_campaigns = emailbison.list_campaigns()
    by_id = {int(c["id"]): c for c in all_campaigns}
    found: dict[str, list[int]] = {}
    with db.cursor() as cur:
        for angle, primary_id in primaries.items():
            base_name = (by_id.get(primary_id) or {}).get("name") or ""
            if not base_name.strip():
                continue
            base_lower = base_name.strip().lower()
            matches = [c for c in all_campaigns
                      if int(c["id"]) != primary_id
                      and base_lower in (c.get("name") or "").strip().lower()]
            new_ids = []
            for c in matches:
                cur.execute(
                    """INSERT INTO copy_test_campaigns (angle, campaign_id, is_primary)
                       VALUES (%s,%s,FALSE)
                       ON CONFLICT (angle, campaign_id) DO NOTHING""",
                    (angle, int(c["id"])))
                if cur.rowcount:
                    new_ids.append(int(c["id"]))
                    _register_for_reply_sweep(angle, int(c["id"]))
            if new_ids:
                found[angle] = new_ids
    if found:
        db.log(run_id, "testgrid", f"sync_duplicate_campaigns: newly folded in {found}")
    _flip_stale_primaries(primaries, all_campaigns, run_id)
    return {"found": found}


def segment_of(vertical: str | None, cfg: dict) -> str | None:
    for seg, verticals in cfg["segments"].items():
        if vertical in verticals:
            return seg
    return None


def deal(contacts: list[dict], cfg: dict) -> dict[str, list[dict]]:
    """Deal contacts to angles at random, exactly balanced within each segment.

    Ordering is by a seeded hash of the email, not list position, so no angle gets the
    top of a ranked list; then cards go round the angles in turn, so the counts differ by
    at most one. Contacts outside every segment are left out, never guessed into one.
    """
    angles = list(cfg["angles"])
    seed = cfg["rules"]["random_deal_seed"]
    out: dict[str, list[dict]] = {a: [] for a in angles}
    by_seg: dict[str, list[dict]] = {}
    for c in contacts:
        seg = segment_of(c.get("vertical"), cfg)
        if seg:
            by_seg.setdefault(seg, []).append(c)
    default_cap = int(cfg["rules"]["per_segment_per_campaign"])
    caps = cfg.get("segment_caps") or {}
    for seg, rows in by_seg.items():
        cap = int(caps.get(seg, default_cap))
        rows.sort(key=lambda c: hashlib.sha256(f"{seed}:{c['email'].strip().lower()}".encode()).hexdigest())
        rows = rows[: cap * len(angles)]
        for i, c in enumerate(rows):
            out[angles[i % len(angles)]].append({**c, "segment": seg})
    return out


def wilson(successes: int, total: int, z: float = 1.959964) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    d = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - spread) / d), min(1.0, (centre + spread) / d)


def read_grid() -> list[dict]:
    """One row per (angle, vertical): leads, replied, interested, booked, positive.

    Sums across every campaign registered under an angle, primary AND any name-matched
    duplicate (sync_duplicate_campaigns) -- a clone Troy made to keep delivering to a
    stalled campaign's audience is still that angle's traffic for scoring purposes.
    """
    ensure_tables()
    with db.cursor() as cur:
        cur.execute(
            """SELECT tc.angle, ct.vertical,
                      count(DISTINCT ct.email) AS leads,
                      count(DISTINCT ct.email) FILTER (WHERE oe.event_type='replied')    AS replied,
                      count(DISTINCT ct.email) FILTER (WHERE oe.event_type='interested') AS interested,
                      count(DISTINCT ct.email) FILTER (WHERE oe.event_type='booked')     AS booked,
                      count(DISTINCT ct.email) FILTER (WHERE oe.event_type IN ('interested','booked')) AS positive
                 FROM copy_test_campaigns tc
                 JOIN contacts ct ON ct.eb_campaign_id = tc.campaign_id
                 LEFT JOIN outcome_events oe
                        ON oe.email = ct.email AND oe.campaign_id = tc.campaign_id
                WHERE ct.vertical IS NOT NULL
                GROUP BY tc.angle, ct.vertical""")
        return [dict(r) for r in cur.fetchall()]


def rollup(grid: list[dict], cfg: dict) -> dict:
    """{'by_angle': {...}, 'by_segment': {seg: {angle: {...}}}} from read_grid() rows.

    Found 2026-10-05 (Troy: "tariff has 1 interested lead" when by_angle showed 0): a
    row whose vertical did not exactly match one of the declared [segments] names used
    to be dropped from by_angle too, not just by_segment -- 1,027 of the leads loaded
    so far carry a vertical outside the original 7 segments (new verticals the weekly
    job has already sourced into, or a research-tagged vertical with a slightly longer
    name), and every one of their replies was invisible to scoring. by_angle, which
    decide() actually uses, now counts every row regardless of segment; only the
    by_segment breakdown (a secondary view) still needs a segment to bucket into, with
    anything unmapped kept visible under "(other)" instead of silently vanishing.
    """
    zero = {"leads": 0, "replied": 0, "interested": 0, "booked": 0, "positive": 0}
    by_angle = {a: dict(zero) for a in cfg["angles"]}
    by_seg: dict[str, dict[str, dict]] = {}
    for r in grid:
        if r["angle"] not in by_angle:
            continue
        for k in zero:
            by_angle[r["angle"]][k] += int(r[k] or 0)
        seg = segment_of(r["vertical"], cfg) or "(other)"
        cell = by_seg.setdefault(seg, {a: dict(zero) for a in cfg["angles"]})[r["angle"]]
        for k in zero:
            cell[k] += int(r[k] or 0)
    return {"by_angle": by_angle, "by_segment": by_seg}


def _value(row: dict, booked_weight: int) -> int:
    return int(row["interested"]) + booked_weight * int(row["booked"])


def _score(row: dict, booked_weight: int) -> tuple[float, float]:
    """(positive value, replies) per email SENT when the row carries sends, else per lead loaded (Emmanuel, 2026-10-19: leads sit loaded for days
    before a mailbox reaches them, so per-lead rewards the angle that happened to send least)."""
    base = max(int(row["sent"]), 1) if row.get("sent") is not None else max(int(row["leads"]), 1)
    return _value(row, booked_weight) / base, int(row.get("replied") or 0) / base


def decide(by_angle: dict[str, dict], launched_at: datetime, cfg: dict,
           now: datetime | None = None) -> dict:
    """Troy, 2026-10-07: "to not complicate this... after 10 days, when we find our
    winner, we scale our winner... and kill the rest." Replaces the Wilson tie-break
    (2026-10-05): no more "too close to call, scale all three." Always force exactly
    one winner at day 10, by raw rate, and kill both of the others outright.

      - day 7 (freeze_days): frozen, nothing judged.
      - day 7 to day 10 (decision_day): monitoring, no action, just watching.
      - day 10: FORCED decision, whatever the volume. Highest (interested +
        booked_weight*booked) per lead wins, no exceptions; the other two are paused.

    Returns status: frozen | monitoring | decided | no_data.
    """
    r = cfg["rules"]
    now = now or datetime.now(timezone.utc)
    if launched_at.tzinfo is None:
        launched_at = launched_at.replace(tzinfo=timezone.utc)
    freeze_end = launched_at + timedelta(days=int(r["freeze_days"]))
    decision_at = launched_at + timedelta(days=int(r.get("decision_day", 10)))
    w = int(r["booked_weight"])
    positives = sum(int(a["positive"]) for a in by_angle.values())
    booked = sum(int(a["booked"]) for a in by_angle.values())
    base = {"positives": positives, "booked": booked, "freeze_ends": freeze_end, "decision_at": decision_at}
    if now < freeze_end:
        return {**base, "status": "frozen",
                "reason": f"inside the {r['freeze_days']}-day freeze (ends {freeze_end:%d %b}); nothing is judged or edited"}
    if now < decision_at:
        return {**base, "status": "monitoring",
                "reason": f"past the {r['freeze_days']}-day freeze, watching until the day-{r.get('decision_day', 10)} "
                          f"decision on {decision_at:%d %b}; {positives} interested, {booked} booked so far"}
    # Troy, 2026-10-08: launching NEXT_SEASON_LINEUP added a 4th entry to cfg["angles"]
    # (needed so rollup() scores it at all) -- without this filter, a derived/lookalike
    # angle would silently join the ORIGINAL day-10 test's own ranking every night this
    # runs again, even though that decision is already recorded and TIME/TARIFF are
    # already paused. Derived angles are judged separately, by check_derived_angles'
    # flat day-7 bar, never by this 3-way ranking.
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS angle_derivations (
            angle TEXT PRIMARY KEY, parent_angle TEXT NOT NULL, checked_60pct BOOLEAN NOT NULL DEFAULT FALSE)""")
        cur.execute("SELECT angle FROM angle_derivations")
        derived = {r["angle"] for r in cur.fetchall()}
    by_angle = {a: row for a, row in by_angle.items() if a not in derived}

    if len(by_angle) < 3:
        return {**base, "status": "monitoring", "reason": "fewer than 3 angles still have data; nothing to rank"}

    # Emmanuel, 2026-10-19: "i want 700 minimum." No angle is judged until each has SENT this many emails; day 10 alone is not enough.
    min_sends = int(r.get("min_sends_per_angle", 0) or 0)
    short = {a: int(row["sent"]) for a, row in by_angle.items() if row.get("sent") is not None and int(row["sent"]) < min_sends}
    if short:
        return {**base, "status": "monitoring", "waiting_for_volume": short,
                "reason": f"day {r.get('decision_day', 10)} has passed but every angle needs {min_sends} emails sent before a call; still short: "
                          + ", ".join(f"{a} {n}" for a, n in sorted(short.items()))}
    ranked = sorted(by_angle.items(), key=lambda kv: _score(kv[1], w), reverse=True)
    winner, winner_row = ranked[0]
    if _value(winner_row, w) <= 0:
        return {**base, "status": "monitoring",
                "reason": f"day {r.get('decision_day', 10)} has passed but no angle has a single interested or booked reply yet, so there is nothing to rank; "
                          f"nobody is paused on a tie"}
    losers = [a for a, _ in ranked[1:]]
    unit = "per email sent" if winner_row.get("sent") is not None else "per lead"
    detail = ", ".join(f"{a} {_score(rw, w)[0]:.2%}" for a, rw in ranked)
    return {**base, "status": "decided", "winner": winner, "losers": losers,
            "reason": f"{winner} leads {unit} ({detail}); scaling {winner} only, killing {' and '.join(losers)}"}


def record_champion(angle: str, campaign_id: int, separated: bool, reason: str) -> None:
    ensure_tables()
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO copy_test_champion (id, angle, campaign_id, separated, reason)
               VALUES (1,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE
                  SET angle=EXCLUDED.angle, campaign_id=EXCLUDED.campaign_id,
                      separated=EXCLUDED.separated, reason=EXCLUDED.reason, decided_at=now()""",
            (angle, campaign_id, separated, reason))


def champion_campaign() -> int | None:
    """The campaign new batches should join, or None while the test is still open."""
    if load_config() is None:
        return None
    try:
        ensure_tables()
        with db.cursor() as cur:
            cur.execute("SELECT campaign_id FROM copy_test_champion WHERE id = 1")
            row = cur.fetchone()
        return int(row["campaign_id"]) if row else None
    except Exception:
        return None


def _flip_stale_primaries(primaries: dict, all_campaigns: list, run_id: str | None) -> None:
    """Catch the exact bug found 2026-10-05: MMM's primary (1180) had been paused by
    EmailBison -- "finished", having sent only 485 of 1,971 leads -- after Troy cloned it
    into 1231 and kept running that instead. Nothing noticed; the day's scale-up started
    spending into the dead campaign before a human caught it. This checks every primary
    every night: if it is paused/archived while a same-angle sibling is still live, flip
    is_primary to the live one and say so on Slack, instead of staying silently wrong.
    """
    from .clients import slack
    by_id = {int(c["id"]): c for c in all_campaigns}
    DEAD = {"paused", "archived"}
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id FROM copy_test_campaigns WHERE NOT is_primary")
        siblings: dict[str, list[int]] = {}
        for r in cur.fetchall():
            siblings.setdefault(r["angle"], []).append(int(r["campaign_id"]))
    for angle, primary_id in primaries.items():
        primary = by_id.get(primary_id)
        if not primary or (primary.get("status") or "").lower() not in DEAD:
            continue
        live = [cid for cid in siblings.get(angle, [])
               if (by_id.get(cid) or {}).get("status", "").lower() not in DEAD]
        if not live:
            continue
        new_primary = live[0]
        with db.cursor() as cur:
            cur.execute("SELECT launched_at FROM copy_test_campaigns WHERE angle=%s AND campaign_id=%s",
                        (angle, primary_id))
            old_launch = (cur.fetchone() or {}).get("launched_at")
            cur.execute("UPDATE copy_test_campaigns SET is_primary=FALSE WHERE angle=%s AND campaign_id=%s",
                        (angle, primary_id))
            # Keep the SAME day-7/day-10 clock -- it tracks the angle's audience, not
            # whichever campaign happens to hold it this week.
            cur.execute("UPDATE copy_test_campaigns SET is_primary=TRUE, "
                        "launched_at = COALESCE(launched_at, %s) WHERE angle=%s AND campaign_id=%s",
                        (old_launch, angle, new_primary))
        slack.post(f":warning: *{angle}'s primary campaign ({primary_id}) is {primary.get('status')} by "
                   f"EmailBison* -- switched the primary to its live sibling ({new_primary}) automatically. "
                   f"New leads and scale-ups now go there.")
        db.log(run_id, "testgrid", f"{angle}: flipped primary {primary_id} -> {new_primary} ({primary.get('status')})")


def unused_verticals(cfg: dict, n: int) -> list[str]:
    """Declared verticals not already feeding any Wave 1 segment, most-recently-declared last.

    Medical Devices is dropped from this test outright (Troy, 2026-09-27): never offer it
    here even as a "new vertical to try" expansion target.
    """
    from . import client
    used = {v for verts in cfg["segments"].values() for v in verts}
    try:
        declared = list(client.profile().verticals)
    except Exception:
        return []
    return [v for v in declared if v not in used and v != "Medical Devices"][:n]


CHAMPION_TARGET_LEADS = 10_000   # Troy, 2026-10-08: "once we get a winner like MMM we should scale to 10k"
LOOKALIKE_AUTOSCALE_TARGET = 5_000   # a passed lookalike auto-scales to here, then waits for a human
LOOKALIKE_FULL_TARGET = 10_000       # ...to press the button that raises it the rest of the way


def _scale_targets_schema(cur) -> None:
    cur.execute("""CREATE TABLE IF NOT EXISTS scale_targets (
        angle TEXT PRIMARY KEY, campaign_id BIGINT NOT NULL, variant TEXT NOT NULL,
        target_leads INTEGER NOT NULL, new_verticals JSONB NOT NULL DEFAULT '[]',
        done BOOLEAN NOT NULL DEFAULT FALSE, requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
        approval_pending BOOLEAN NOT NULL DEFAULT FALSE)""")
    # Additive migrations for a table that predates these columns/default (built 2026-10-06).
    cur.execute("ALTER TABLE scale_targets ALTER COLUMN new_verticals SET DEFAULT '[]'::jsonb")
    cur.execute("ALTER TABLE scale_targets ADD COLUMN IF NOT EXISTS requires_approval BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE scale_targets ADD COLUMN IF NOT EXISTS approval_pending BOOLEAN NOT NULL DEFAULT FALSE")


def auto_expand_champion(winner: str, run_id: str | None = None, target_leads: int | None = None,
                         requires_approval: bool = False) -> dict:
    """Sets (or raises) an angle's scale target; tick_scale_ups does the real, repeated
    feeding and inventory refill every 2 hours.

    Troy, 2026-10-08: "once we get a winner like MMM we should scale to 10k. for lookalike
    winners we scale from 1k to 5k and when it gets there it pings slack with the present
    stats [and] a button to scale to 10 that the human presses." Defaults to the 10k
    ceiling (the decided day-10 winner); callers pass a smaller target + requires_approval
    for a lookalike's first-stage 5k cap.

    This can only RAISE a target, never re-trigger or lower one -- found 2026-10-08: the
    old version guarded on "has this angle EVER been scaled," which is exactly what broke
    MMM at the real day-10 decision: the day 7-10 monitoring phase had already bumped it
    once, so the guard refused the real decision's push to 10k, silently, no error.
    """
    from .clients import emailbison, slack
    cfg = load_config()
    ensure_tables()
    target = int(target_leads or CHAMPION_TARGET_LEADS)
    with db.cursor() as cur:
        cur.execute("SELECT campaign_id FROM copy_test_campaigns WHERE angle=%s AND is_primary", (winner,))
        row = cur.fetchone()
    if not row:
        return {"target_set": False, "reason": f"no campaign registered for {winner}"}
    champion = int(row["campaign_id"])
    variant = (cfg.get("angle_prompts") or {}).get(winner)
    if not variant:
        return {"target_set": False, "reason": f"no prompt folder for {winner} in [angle_prompts]"}
    with db.cursor() as cur:
        _scale_targets_schema(cur)
        cur.execute("SELECT target_leads FROM scale_targets WHERE angle=%s", (winner,))
        existing = cur.fetchone()
    if existing and int(existing["target_leads"]) >= target:
        return {"target_set": False,
                "reason": f"{winner} already has a target of {existing['target_leads']:,}, not lowering to {target:,}"}

    starting = int((emailbison.get_campaign(champion) or {}).get("total_leads") or 0)
    with db.cursor() as cur:
        cur.execute("""INSERT INTO scale_targets
                           (angle, campaign_id, variant, target_leads, requires_approval, done, approval_pending)
                       VALUES (%s,%s,%s,%s,%s,FALSE,FALSE)
                       ON CONFLICT (angle) DO UPDATE SET
                           target_leads=EXCLUDED.target_leads, requires_approval=EXCLUDED.requires_approval,
                           done=FALSE, approval_pending=FALSE""",
                    (winner, champion, variant, target, requires_approval))
    slack.post(f":seedling: *{winner}* -- scaling to {target:,} leads (currently {starting:,}). "
               f"Ticks every 2 hours until done, sourcing across all approved verticals as needed.")
    run_id = run_id or "scale-winner-" + __import__("time").strftime("%Y%m%dT%H%M%S")
    db.log(run_id, "testgrid", f"{winner} target set: {starting:,} -> {target:,}")
    return {"winner": winner, "target_set": True, "starting": starting, "target": target}


def _angle_stats_line(angle: str) -> str:
    cfg = load_config()
    roll = rollup(read_grid(), cfg)
    a = roll["by_angle"].get(angle, {"leads": 0, "replied": 0, "interested": 0, "booked": 0})
    pct = (a["interested"] / a["replied"] * 100) if a["replied"] else 0.0
    return (f"{a['leads']:,} leads, {a['replied']} replied, {a['interested']} interested "
           f"({pct:.1f}% of replies), {a['booked']} booked")


def approve_lookalike_scale_to_10k(angle: str, user: str | None = None) -> dict:
    """The human half of the 5k gate: a Slack button calls this to raise an already-
    5k-scaled lookalike the rest of the way to 10k."""
    result = auto_expand_champion(angle, None, target_leads=LOOKALIKE_FULL_TARGET, requires_approval=False)
    db.log(None, "testgrid", f"{angle} approved for 10k by {user or 'unknown'}: {result}")
    return result


def all_approved_verticals() -> list[str]:
    """Every vertical ProductEVO has approved, not just the 10 the original 3-angle test
    was scoped to. Troy, 2026-10-07: "the scale up should go with all evo's approved
    verticals" -- the vertical restriction existed to keep the TEST fair (identical
    audiences across angles); once an angle is a decided winner or a passed lookalike,
    that restriction no longer serves any purpose, it only caps growth. Medical Devices
    stays excluded (Troy, 2026-09-27: dropped from this test outright)."""
    from . import client
    try:
        return [v for v in client.profile().verticals if v != "Medical Devices"]
    except Exception:
        return []


def _available_keep_count(verticals: list[str]) -> int:
    if not verticals:
        return 0
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) n FROM staged_companies s JOIN company_funnel f ON f.domain = s.domain
                       WHERE s.verdict = 'KEEP' AND s.vertical = ANY(%s)
                         AND s.found_at IS NULL AND s.consumed_at IS NULL
                         AND f.tier = 1 AND NOT coalesce(f.replied, false)""", (verticals,))
        return cur.fetchone()["n"]


# Troy, 2026-10-07: "no need to run the pool refill every 2 hours" -- moved off the
# 2-hourly resume() tick entirely, into the once-nightly review instead, gated to these
# two weekdays (Monday=0, Thursday=3; roughly twice a week, spaced evenly). The cooldown
# below is just a same-day double-fire guard, not the real cadence control.
POOL_REFILL_WEEKDAYS = (0, 3)
POOL_REFILL_COOLDOWN_HOURS = 20


def refill_pool(run_id: str | None = None) -> dict:
    """Troy, 2026-10-07: "it would be nice to have a loop that searches for new never-
    contacted companies that would keep feeding our never-contacted [pool]." Call from
    the nightly review (see stages/s8_review.py), twice a week -- across ALL approved
    verticals, so the bank stays ahead of demand instead of only refilling reactively
    after it already hit zero -- without re-spending on company search every 2 hours.

    Found 2026-10-07: the company-search tool's own "companies found" count does not
    reliably reflect what actually lands in the database -- a reactive refill once
    reported +1,660 while the real available pool grew by 7 (most of the "finds" were
    companies already known and staged, silently absorbed by the dedup, or previously
    judged DROP and not re-opened). This measures the real pool size before and after
    and reports THAT, not the tool's self-reported count -- and now also reports the
    real dollar spend, measured off each call's actual token usage (Troy, 2026-10-07:
    "should let me know how many were found and spend").
    """
    from . import client, modes
    from .clients import llm, slack
    # Lumion: sourcing NEW (never contacted) companies is deferred until Emmanuel asks for it (2026-10-09). It spends on
    # company search and fit checks for every approved vertical, so it stays off until POOL_REFILL_ENABLED=true.
    if os.getenv("POOL_REFILL_ENABLED", "false").strip().lower() not in ("1", "true", "yes"):
        return {"status": "disabled"}
    if datetime.now(timezone.utc).weekday() not in POOL_REFILL_WEEKDAYS:
        return {"status": "not_scheduled_today"}
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS pool_refills (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1), last_refill_at TIMESTAMPTZ)""")
        cur.execute("SELECT last_refill_at FROM pool_refills WHERE id = 1")
        row = cur.fetchone()
    if row and row["last_refill_at"] and row["last_refill_at"] > datetime.now(timezone.utc) - timedelta(hours=POOL_REFILL_COOLDOWN_HOURS):
        return {"status": "cooldown"}
    verticals = all_approved_verticals()
    if not verticals:
        return {"status": "no_verticals"}
    before = _available_keep_count(verticals)
    cost_mark = llm.usage_mark()
    profile = client.profile()
    errors = []
    for v in verticals:
        try:
            modes.run_source(profile, v, 250, companies_only=True)
        except Exception as exc:
            errors.append(f"{v}: {str(exc)[:100]}")
    try:
        from . import funnel
        funnel.refresh(run_id, force=True)     # the sourced companies must be tiered before the pool is measured
    except Exception as exc:
        errors.append(f"funnel refresh: {str(exc)[:100]}")
    spend = llm.usage_cost_since(cost_mark)
    after = _available_keep_count(verticals)
    gained = after - before
    with db.cursor() as cur:
        cur.execute("""INSERT INTO pool_refills (id, last_refill_at) VALUES (1, now())
                       ON CONFLICT (id) DO UPDATE SET last_refill_at = now()""")
    cost_note = (f"${spend['usd']:.2f} spent" if not spend["unpriced_models"]
                 else f"${spend['usd']:.2f}+ spent ({len(spend['unpriced_models'])} model(s) unpriced)")
    slack.post(f":mag: *Never-contacted pool refill* -- searched all {len(verticals)} approved "
               f"verticals, the real available pool went {before:,} -> {after:,} (+{gained:,}). "
               f"{cost_note}."
               + (f" {len(errors)} vertical(s) errored." if errors else ""))
    db.log(run_id, "testgrid", f"refill_pool: {before} -> {after} (+{gained}), spend={spend}, errors={errors}")
    return {"status": "ran", "before": before, "after": after, "gained": gained,
            "spend_usd": spend["usd"], "errors": errors}


def tick_scale_ups(run_id: str | None = None) -> dict:
    """Call every 2 hours (same cadence as wave1_fill) to keep feeding any scale-up that
    hasn't yet reached its real target. Draws from ALL approved verticals (see
    all_approved_verticals), not just the original test's 10 -- refill_pool keeps that
    wider bank topped up on its own schedule, independent of any specific angle stalling.
    """
    from . import scaling
    from .clients import emailbison, slack
    with db.cursor() as cur:
        _scale_targets_schema(cur)
        cur.execute("SELECT * FROM scale_targets WHERE NOT done")
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return {"status": "nothing_pending"}
    verticals = all_approved_verticals()
    ticked = []
    for r in rows:
        angle = r["angle"]
        campaign_id, variant, target = int(r["campaign_id"]), r["variant"], int(r["target_leads"])
        try:
            # A derived angle's prompt files live only on disk; a deploy since it
            # launched can wipe them mid-scale-up (found 2026-10-08 twice over, once as
            # a leaked-comment bug, once as an approved angle with nothing left to
            # launch). Self-heal before feeding it, same fix launch_new_test_angle uses.
            from . import angle_derivation
            angle_derivation._restore_prompt_files_if_missing(variant)
        except Exception as exc:
            db.log(run_id, "testgrid", f"prompt-file restore check failed for {angle}: {exc}", level="warn")
        current = int((emailbison.get_campaign(campaign_id) or {}).get("total_leads") or 0)
        need = target - current
        if need <= 0:
            with db.cursor() as cur:
                cur.execute("UPDATE scale_targets SET done = TRUE WHERE angle = %s", (angle,))
            if r.get("requires_approval") and not r.get("approval_pending"):
                # Troy, 2026-10-08: a lookalike that clears its 5k auto-scale stops and
                # waits for a human -- post the real stats plus a button, not a silent
                # "done." The button's click calls approve_lookalike_scale_to_10k, which
                # raises this same row's target to 10k and clears `done` so tick_scale_ups
                # picks it back up on the next tick.
                with db.cursor() as cur:
                    cur.execute("UPDATE scale_targets SET approval_pending = TRUE WHERE angle = %s", (angle,))
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn",
                        "text": f":bar_chart: *{angle}* hit its {target:,}-lead auto-scale cap.\n"
                                f"{_angle_stats_line(angle)}"}},
                    {"type": "actions", "elements": [{
                        "type": "button", "style": "primary",
                        "text": {"type": "plain_text", "text": "Scale to 10,000"},
                        "action_id": "scale_lookalike_10k", "value": angle,
                    }]},
                ]
                slack.post(f"{angle} hit {target:,} leads -- approve scaling to 10,000?", blocks=blocks)
            else:
                slack.post(f":white_check_mark: *{angle} fully scaled* -- {current:,} leads, target reached.")
            ticked.append({"angle": angle, "status": "done", "total": current})
            continue
        result = scaling.feed_campaign(campaign_id, variant, verticals, need, run_id or "tick-scale-up")
        db.log(run_id, "testgrid", f"tick_scale_up {angle}: +{result.get('loaded', 0)} toward {target} (was {current})")
        ticked.append({"angle": angle, "status": "ticked", "loaded": result.get("loaded", 0),
                      "now": current + result.get("loaded", 0), "target": target})
    return {"status": "ticked", "results": ticked}


_DECISION_LOCK = "copy_test_decision"
_DECISION_LOCK_STALE_MIN = 20   # a full pass (read + maybe a scale-up) should never take this long to even START


def _acquire_decision_lock(holder: str) -> bool:
    """Same job_locks pattern wave1_fill uses. Troy, 2026-10-05: without this, the nightly
    review and any manual or retried call could both decide/scale at once -- two processes
    both seeing a winner not yet marked scaled, both spending on it. auto_expand_champion's
    per-angle DB guard closes the gap between "check" and "insert" only once one write wins;
    this stops the second process from starting its (expensive) read-and-decide work at all."""
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS job_locks (name TEXT PRIMARY KEY, holder TEXT,
                       heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""INSERT INTO job_locks (name, holder, heartbeat_at) VALUES (%s, %s, now())
                       ON CONFLICT (name) DO UPDATE SET holder=EXCLUDED.holder, heartbeat_at=now()
                       WHERE job_locks.heartbeat_at < now() - make_interval(mins => %s)
                       RETURNING holder""", (_DECISION_LOCK, holder, _DECISION_LOCK_STALE_MIN))
        return cur.fetchone() is not None


def _release_decision_lock(holder: str) -> None:
    with db.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name=%s AND holder=%s", (_DECISION_LOCK, holder))


def evaluate_and_record() -> dict:
    """Daily entry point: read the grid, apply the rules, record a champion if decided."""
    import time as _time
    holder = "eval-" + _time.strftime("%Y%m%dT%H%M%S")
    if not _acquire_decision_lock(holder):
        return {"status": "already_running"}
    try:
        return _evaluate_and_record_locked()
    finally:
        _release_decision_lock(holder)


def _attach_sends(by_angle: dict, camps: dict) -> None:
    """Add 'sent' (emails EmailBison reports sent, summed over the angle's campaigns) to each angle row. Left off if EmailBison cannot be read, so
    the rules fall back to per-lead instead of judging on a half-read number."""
    from .clients import emailbison
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id FROM copy_test_campaigns")
        ids: dict[str, list[int]] = {}
        for r in cur.fetchall():
            ids.setdefault(r["angle"], []).append(int(r["campaign_id"]))
    for angle, row in by_angle.items():
        try:
            live = [emailbison.get_campaign(cid) or {} for cid in ids.get(angle, [])]
            row["sent"] = sum(int(c.get("emails_sent") or 0) for c in live)
            # EmailBison's own counters, not only our outcome_events (that table is filled by the reply sweep, which this build does not run on a
            # timer: it was empty on 2026-10-19 while EmailBison showed 18 replies). The larger of the two wins, so neither source can hide a reply.
            row["replied"] = max(int(row.get("replied") or 0), sum(int(c.get("unique_replies") or 0) for c in live))
            row["interested"] = max(int(row.get("interested") or 0), sum(int(c.get("interested") or 0) for c in live))
            row["positive"] = int(row["interested"]) + int(row.get("booked") or 0)
        except Exception as exc:
            db.log(None, "testgrid", f"could not read sends for {angle}: {exc}", level="warn")
            for other in by_angle.values():
                other.pop("sent", None)
            return


def _evaluate_and_record_locked() -> dict:
    cfg = load_config()
    if cfg is None:
        return {"status": "no_config"}
    ensure_tables()
    try:
        sync_duplicate_campaigns()
    except Exception as exc:
        db.log(None, "testgrid", f"sync_duplicate_campaigns failed: {exc}", level="warn")
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id, launched_at FROM copy_test_campaigns WHERE is_primary")
        camps = {r["angle"]: r for r in cur.fetchall()}
    if len(camps) < len(cfg["angles"]) or any(c["launched_at"] is None for c in camps.values()):
        return {"status": "no_data", "reason": "test campaigns are not all registered and launched"}
    launched = min(c["launched_at"] for c in camps.values())
    roll = rollup(read_grid(), cfg)
    _attach_sends(roll["by_angle"], camps)
    verdict = decide(roll["by_angle"], launched, cfg)

    if verdict["status"] == "decided" and not _decision_already_recorded():
        record_decision(verdict)
        from .clients import slack
        winner, losers = verdict["winner"], verdict["losers"]
        paused = []
        for loser in losers:
            try:
                pause_angle(loser, int(camps[loser]["campaign_id"]))
                paused.append(loser)
            except Exception as exc:
                db.log(None, "testgrid", f"could not pause {loser}: {exc}", level="warn")
        verdict["paused"] = paused
        # Emmanuel, 2026-10-22: the winner had one interested lead, so it does not jump to 10k. [scale_gate] stage1_target (TFP: 3,500) first; five days later
        # scale_review reads the results and Slack offers the next step with a button. A client with no [scale_gate] keeps the old straight-to-ceiling rule.
        stage1 = int((cfg.get("scale_gate") or {}).get("stage1_target") or 0) or None
        slack.post(f":axe: *Copy test decided* -- day-10 winner: *{winner}*. {verdict['reason']}\n"
                   f"{' and '.join(losers)} paused. "
                   + (f"Scaling {winner} to {stage1:,} first. Five days later Slack will say whether to take it further, with a button." if stage1
                      else f"Scaling {winner} only, hard."))
        verdict["scaled"] = auto_expand_champion(winner, None, target_leads=stage1)
        if stage1:
            try:
                from . import scale_review
                scale_review.schedule(f"angle:{winner}", "angle", winner, int(camps[winner]["campaign_id"]), stage1, angle=winner)
            except Exception as exc:
                db.log(None, "testgrid", f"could not start the scale review for {winner}: {exc}", level="warn")
        # Troy, 2026-10-07: "we keep testing out those angles [lookalikes]" -- this was
        # described as part of the rule but never actually wired to anything; propose_new_
        # test_angle existed only for the manual/60%-checkpoint path. Fire it automatically
        # the moment a winner is named, so a lookalike shows up in Slack the same night,
        # not only when someone remembers to ask for one.
        try:
            from . import angle_derivation
            verdict["proposed_angle"] = angle_derivation.propose_new_test_angle(winner, None)
        except Exception as exc:
            db.log(None, "testgrid", f"could not propose a lookalike angle off {winner}: {exc}", level="warn")

    elif verdict["status"] == "monitoring" and len(roll["by_angle"]) == 3:
        # Troy, 2026-10-05: "whats wrong with deciding now, at least scaling the winning
        # and then by day 10 dropping the loser" -- scaling is cheap to be wrong about
        # (worst case, some spend on an angle that does not end up winning); PAUSING is
        # not, so the kill above still waits for day 10's statistical test. Every night
        # between day 7 and day 10, scale whichever angle is CURRENTLY ahead by raw rate
        # (no separation required), once. auto_expand_champion's per-angle guard makes
        # this safe to call every night: it only spends on a given angle once this test.
        w = int(r["booked_weight"]) if (r := cfg["rules"]) else 3
        leader = max(roll["by_angle"], key=lambda a: _score(roll["by_angle"][a], w))
        if _value(roll["by_angle"][leader], w) <= 0:
            return {**verdict, **roll}          # nobody has a single positive reply yet: there is no leader to scale
        with db.cursor() as cur:
            _scale_targets_schema(cur)
            # Found 2026-10-08: this used to check copy_test_expansions, a table
            # auto_expand_champion no longer writes to since the rewrite that let a
            # target be RAISED instead of only set once -- "already" was permanently
            # False, which would have fired this branch (and its Slack post) every
            # single night between day 7 and 10 instead of once. scale_targets, the
            # thing auto_expand_champion actually writes, is the real source of truth.
            cur.execute("SELECT 1 FROM scale_targets WHERE angle=%s LIMIT 1", (leader,))
            already = cur.fetchone() is not None
        if not already:
            from .clients import emailbison, slack
            starting = int((emailbison.get_campaign(int(camps[leader]["campaign_id"])) or {}).get("total_leads") or 0)
            early_target = max(starting * 2, starting + 500)
            stage1 = int((cfg.get("scale_gate") or {}).get("stage1_target") or 0)
            if stage1:
                early_target = min(early_target, stage1)
            slack.post(f":chart_with_upwards_trend: *Copy test, day {(datetime.now(timezone.utc) - launched).days}*: "
                       f"{leader} is ahead on today's numbers (not yet statistically separated -- the day-10 cut "
                       f"still waits for that). Scaling {leader} now while the others keep running untouched.")
            verdict["early_scale"] = auto_expand_champion(leader, None, target_leads=early_target)

    return {**verdict, **roll}


def _decision_already_recorded() -> bool:
    """Found 2026-10-07, the first night day-10 was ever actually reached: this guard
    ran BEFORE record_decision() -- the only place that used to create
    copy_test_decisions -- so the very first "decided" verdict crashed on
    'relation "copy_test_decisions" does not exist' before anything else ran. That one
    exception killed the whole nightly pass: MMM never got scaled, TIME/TARIFF never got
    paused, and the lookalike-angle proposal never fired, because none of that code was
    ever reached. The table is now created here too, not only in record_decision()."""
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS copy_test_decisions (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1), status TEXT NOT NULL,
            winner TEXT, losers JSONB NOT NULL DEFAULT '[]', reason TEXT,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("SELECT 1 FROM copy_test_decisions WHERE id = 1")
        return cur.fetchone() is not None


def record_decision(verdict: dict) -> None:
    """One-time record of the day-10 call. Troy, 2026-10-07: always a single winner now,
    so this stores exactly that, not the old multi-winner shape (2026-10-05, retired)."""
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS copy_test_decisions (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1), status TEXT NOT NULL,
            winner TEXT, losers JSONB NOT NULL DEFAULT '[]', reason TEXT,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("ALTER TABLE copy_test_decisions ADD COLUMN IF NOT EXISTS winner TEXT")
        cur.execute("ALTER TABLE copy_test_decisions ADD COLUMN IF NOT EXISTS losers JSONB NOT NULL DEFAULT '[]'")
        cur.execute(
            """INSERT INTO copy_test_decisions (id, status, winner, losers, reason)
               VALUES (1,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (verdict["status"], verdict["winner"], json.dumps(verdict["losers"]), verdict["reason"]))


def pause_angle(angle: str, campaign_id: int) -> None:
    """Stop the lone loser from sending any more. One-way, like archiving -- EmailBison
    has no unpause-by-API path back to active that this codebase has confirmed works.

    Troy, 2026-10-08: "the expander still lists tariff and time as in the scale on slack
    even though theyve been killed." Archiving in EmailBison was already correct and
    confirmed -- the actual bug was never un-registering the campaign from ve_campaigns,
    the SAME table the unrelated general-vertical pipeline reads for its own "pooled
    campaign" review every night. That review doesn't know or care about a copy-test
    decision; it just reports on whatever's still registered, which is why a killed
    angle kept showing up in the nightly Vertical Review Slack digest ("[pooled in
    campaign 1181, ...]") and kept burning a real EmailBison API call every 2 hours in
    stop_on_reply.sweep_all(), forever, with nothing to show for either. Removing the
    registration here stops both at the source instead of patching either symptom.
    """
    from .clients import emailbison
    emailbison.archive_campaign(campaign_id)
    with db.cursor() as cur:
        cur.execute("DELETE FROM ve_campaigns WHERE campaign_id = %s", (campaign_id,))
    db.log(None, "testgrid", f"paused the day-10 loser: {angle} (campaign {campaign_id}), "
           f"unregistered from ve_campaigns")
