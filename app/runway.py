"""One compact status for a scale-up: how many leads, how many contacts, how many companies, how much runway is left, and what to do next.

Emmanuel, 2026-10-16: "a compact way of saying how many contacts we have, how much runway we have left, how many companies in total."
Used three ways: the Slack action `runway`, the page /runway (adds a per-vertical table), and the scale-up job posts it after every copy cycle.
Everything is counted from the database; nothing here changes anything.
"""
from __future__ import annotations

import tomllib
from datetime import datetime, timezone

from . import client, db

ASSUMED_COPY_PASS = 0.85      # share of banked contacts that survive copy now that the skip rule is off and the caps are 550 and 700


def _n(sql: str, params: tuple = ()) -> int:
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(next(iter(row.values())) or 0) if row else 0


def _targets() -> dict[str, int]:
    """Per-angle fill target from the client's copy_test.toml, and the campaign of each angle."""
    prof = client.profile()
    try:
        cfg = tomllib.loads((prof.prompts_dir.parent / "copy_test.toml").read_text(encoding="utf-8"))
        target = int((cfg.get("fill") or {}).get("target") or 0)
    except Exception:
        target = 0
    with db.cursor() as cur:
        cur.execute("SELECT angle, campaign_id FROM copy_test_campaigns WHERE is_primary ORDER BY angle")
        rows = cur.fetchall()
    return {"target": target, "angles": {r["angle"]: r["campaign_id"] for r in rows}}


def numbers() -> dict:
    t = _targets()
    angles = t["angles"]
    loaded_by = {}
    for a, cid in angles.items():
        loaded_by[a] = _n("SELECT count(*) FROM contacts WHERE status='pushed' AND eb_campaign_id=%s", (cid,))
    loaded = sum(loaded_by.values())
    goal = t["target"] * max(len(angles), 1)
    per_hour = _n("SELECT count(*) FROM contacts WHERE status='pushed' AND eb_campaign_id = ANY(%s) AND updated_at > now() - interval '60 minutes'",
                  (list(angles.values()),))
    bank = _n("""SELECT count(*) FROM contacts c LEFT JOIN company_funnel f ON f.domain = c.company_domain
                 WHERE c.status='verified' AND c.eb_campaign_id IS NULL AND coalesce(c.first_name,'') <> '' AND coalesce(c.personalization_1,'') = ''
                   AND coalesce(f.fit,'') NOT IN ('LOW','NOT_FIT')""")
    held = _n("SELECT count(*) FROM contacts WHERE status='needs_review'")
    sourced = _n("SELECT count(*) FROM staged_companies WHERE verdict='KEEP'")
    searched = _n("SELECT count(*) FROM staged_companies WHERE verdict='KEEP' AND found_at IS NOT NULL")
    with_contacts = _n("SELECT count(DISTINCT company_domain) FROM contacts WHERE company_domain IN (SELECT domain FROM staged_companies)")
    contact_rows = _n("SELECT count(*) FROM contacts WHERE company_domain IN (SELECT domain FROM staged_companies)")
    fit_failed = _n("""SELECT count(*) FROM staged_companies s JOIN company_funnel f ON f.domain = s.domain WHERE f.fit IN ('LOW','NOT_FIT')""")
    left = max(sourced - searched, 0)
    cps = contact_rows / searched if searched else 0.5            # contacts per searched company, measured
    pool_contacts = bank + left * cps
    potential = loaded + pool_contacts * ASSUMED_COPY_PASS
    return dict(goal=goal, target=t["target"], loaded=loaded, loaded_by=loaded_by, per_hour=per_hour, bank=bank, held=held, sourced=sourced,
                searched=searched, left=left, with_contacts=with_contacts, contact_rows=contact_rows, fit_failed=fit_failed, cps=cps,
                potential=potential)


def recommendation(n: dict) -> str:
    gap = n["goal"] - n["potential"]
    if gap <= 0:
        pool = "The companies already sourced are enough for the goal."
    else:
        need = int(gap / max(n["cps"] * ASSUMED_COPY_PASS, 0.05))
        pool = f"The sourced pool tops out near {int(n['potential']):,} leads; about {need:,} more companies are needed. The loop sources the next, bigger round by itself when this pool is used."
    if n["bank"] > 300:
        bottle = f"Copy is the slower side ({n['bank']:,} contacts waiting)."
    elif n["bank"] < 60 and n["left"] > 0:
        bottle = "Contact finding is the slower side (the bank is nearly empty)."
    else:
        bottle = "Contact finding and copy are in balance."
    return pool + " " + bottle


def report() -> str:
    n = numbers()
    pct = lambda a, b: f"{round(100 * a / b)}%" if b else "0%"
    angles = ", ".join(f"{a} {v:,}" for a, v in n["loaded_by"].items())
    eta = ""
    if n["per_hour"] > 0 and n["goal"] > n["loaded"]:
        hours = (min(n["potential"], n["goal"]) - n["loaded"]) / n["per_hour"]
        eta = f" At the last hour's pace ({n['per_hour']:,}/h) the pool runs out in about {hours:.1f} h."
    return "\n".join([
        f"*TFP runway* ({datetime.now(timezone.utc).strftime('%H:%M')} UTC)",
        f"*Leads:* {n['loaded']:,} of {n['goal']:,} ({pct(n['loaded'], n['goal'])}) | {angles} | target {n['target']:,} each | last hour +{n['per_hour']:,}",
        f"*Contacts:* {n['bank']:,} ready for copy | {n['held']:,} held | {n['loaded']:,} loaded",
        f"*Companies:* {n['sourced']:,} sourced | {n['searched']:,} searched ({pct(n['searched'], n['sourced'])} used) | {n['left']:,} left | "
        f"{n['with_contacts']:,} gave contacts ({pct(n['with_contacts'], n['searched'])}) | {n['fit_failed']:,} failed the fit check",
        f"*Runway:* {n['cps']:.1f} contacts per searched company; pool potential about {int(n['potential']):,} leads ({pct(n['potential'], n['goal'])} of goal).{eta}",
        f"*Next:* {recommendation(n)}",
    ])


def by_vertical() -> str:
    with db.cursor() as cur:
        cur.execute("""SELECT s.vertical v, count(DISTINCT s.domain) sourced, count(DISTINCT s.domain) FILTER (WHERE s.found_at IS NOT NULL) searched,
                              count(DISTINCT c.company_domain) with_contacts
                       FROM staged_companies s LEFT JOIN contacts c ON c.company_domain = s.domain
                       WHERE s.verdict='KEEP' GROUP BY 1 ORDER BY 2 DESC""")
        rows = cur.fetchall()
    lines = [f"{'vertical':<46}{'sourced':>8}{'searched':>9}{'left':>7}{'w/ contacts':>12}"]
    for r in rows:
        lines.append(f"{(r['v'] or '(none)')[:45]:<46}{r['sourced']:>8,}{r['searched']:>9,}{r['sourced'] - r['searched']:>7,}{r['with_contacts']:>12,}")
    return "\n".join(lines)
