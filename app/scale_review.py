"""Scale in two steps, with a person pressing the second one (Emmanuel, 2026-10-22: "the winner has just 1 interested, we should not scale to 10k immediately. Scale to
3500, and if it does well in the next 5 days, suggest in Slack taking it to 5k or more, then I pick a button and it scales").

  step 1  a copy-test winner scales to [scale_gate] stage1_target (3,500). A campaign filled by materials_fill (2174 and later ones) starts its clock when its first email goes out.
  review  review_after_days (5) later, tonight's nightly check reads the campaign's own EmailBison counters and judges them against the bar below.
  step 2  met the bar: Slack posts the numbers with one button per next target (5,000 and 10,000). Pressing one raises the goal. Missed the bar: Slack posts the numbers and
          says it is not recommending more, and looks again after another review period (three looks at most). Nothing scales further without a button.

The bar is in clients/<client>/copy_test.toml [scale_gate]; the defaults are below. Counters are EmailBison's, not this database's.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db

DEFAULTS = {"stage1_target": 3500, "review_after_days": 5, "min_sends": 1500, "min_interested": 3, "min_interested_per_1000": 1.0, "next_targets": [5000, 10000],
            "max_looks": 3}


def gate() -> dict:
    try:
        from . import testgrid
        return {**DEFAULTS, **((testgrid.load_config() or {}).get("scale_gate") or {})}
    except Exception:
        return dict(DEFAULTS)


def ensure_tables() -> None:
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS scale_reviews (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, campaign_id BIGINT NOT NULL, angle TEXT, current_target INTEGER NOT NULL,
            review_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', looks INTEGER NOT NULL DEFAULT 0, detail JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def schedule(key: str, kind: str, label: str, campaign_id: int, current_target: int, angle: str | None = None, days: int | None = None) -> bool:
    """Start the review clock for a campaign. Returns False when one already exists (it never resets a clock)."""
    ensure_tables()
    review_at = datetime.now(timezone.utc) + timedelta(days=int(days if days is not None else gate()["review_after_days"]))
    with db.cursor() as cur:
        cur.execute("""INSERT INTO scale_reviews (key, kind, label, campaign_id, angle, current_target, review_at) VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (key) DO NOTHING RETURNING key""", (key, kind, label, campaign_id, angle, current_target, review_at))
        return cur.fetchone() is not None


def stats(campaign_id: int) -> dict:
    from .clients import emailbison
    c = emailbison.get_campaign(campaign_id) or {}
    return {"sent": int(c.get("emails_sent") or 0), "replied": int(c.get("unique_replies") or 0), "interested": int(c.get("interested") or 0),
            "leads": int(c.get("total_leads") or 0), "name": c.get("name")}


def judge(st: dict, g: dict | None = None) -> tuple[bool, str]:
    """(met the bar, a plain sentence about why)."""
    g = g or gate()
    per_k = 1000 * st["interested"] / st["sent"] if st["sent"] else 0.0
    if st["sent"] < int(g["min_sends"]):
        return False, f"only {st['sent']:,} emails sent so far, and the bar needs {int(g['min_sends']):,} before the numbers mean anything"
    if st["interested"] < int(g["min_interested"]):
        return False, f"{st['interested']} interested, and the bar is {int(g['min_interested'])}"
    if per_k < float(g["min_interested_per_1000"]):
        return False, f"{per_k:.2f} interested per 1,000 emails sent, and the bar is {float(g['min_interested_per_1000']):.2f}"
    return True, f"{st['interested']} interested from {st['sent']:,} sent ({per_k:.2f} per 1,000), above the bar of {float(g['min_interested_per_1000']):.2f} per 1,000"


def discover_fill_campaigns() -> list[str]:
    """Start the clock for each campaign materials_fill loads once its first email has gone out."""
    from . import materials_fill
    started = []
    for x in materials_fill.specs():
        try:
            st = stats(x["campaign"])
        except Exception:
            continue
        if st["sent"] > 0 and schedule(f"fill:{x['campaign']}", "fill", st["name"] or f"campaign {x['campaign']}", x["campaign"], x["target"]):
            started.append(f"fill:{x['campaign']}")
    return started


def _post(row: dict, st: dict, ok: bool, why: str, g: dict) -> None:
    from .clients import slack
    head = f":bar_chart: *{row['label']}* (campaign {row['campaign_id']}), {g['review_after_days']}-day check. {st['sent']:,} sent, {st['replied']} replies, {st['interested']} interested. "
    if not ok:
        looks = int(row["looks"]) + 1
        tail = (f"Not recommending more yet: {why}. I will look again in {g['review_after_days']} days." if looks < int(g["max_looks"])
                else f"Not recommending more: {why}. That was the last automatic look; tell me if you want to push it anyway.")
        slack.post(head + tail)
        return
    targets = [t for t in g["next_targets"] if int(t) > int(row["current_target"])]
    elements = [{"type": "button", "style": "primary", "text": {"type": "plain_text", "text": f"Scale to {int(t):,}"},
                 "action_id": f"scale_review_{int(t)}", "value": f"{row['key']}|{int(t)}"} for t in targets]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head + f"Doing well: {why}. Take it from {int(row['current_target']):,} to more?"}}]
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    slack.post(f"{row['label']} is doing well. Scale it?", blocks=blocks)


def check_due() -> list[dict]:
    """Review every campaign whose clock has run out. Called by the nightly job."""
    ensure_tables()
    g = gate()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM scale_reviews WHERE status IN ('waiting', 'looking') AND review_at <= now()")
        rows = [dict(r) for r in cur.fetchall()]
    done = []
    for row in rows:
        try:
            st = stats(int(row["campaign_id"]))
        except Exception as exc:
            db.log(None, "scale_review", f"could not read {row['key']}: {exc}", level="warn")
            continue
        ok, why = judge(st, g)
        _post(row, st, ok, why, g)
        if ok:
            new_status, next_at = "posted", row["review_at"]
        else:
            looks = int(row["looks"]) + 1
            new_status = "looking" if looks < int(g["max_looks"]) else "not_recommended"
            next_at = datetime.now(timezone.utc) + timedelta(days=int(g["review_after_days"]))
        with db.cursor() as cur:
            cur.execute("UPDATE scale_reviews SET status=%s, looks=looks+1, review_at=%s, detail=%s, updated_at=now() WHERE key=%s",
                        (new_status, next_at, json.dumps({**st, "ok": ok, "why": why}), row["key"]))
        done.append({"key": row["key"], "ok": ok, "why": why})
    return done


def approve(key: str, new_target: int, user: str | None = None) -> dict:
    """A Slack button: raise the campaign's goal to new_target."""
    ensure_tables()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM scale_reviews WHERE key = %s", (key,))
        row = cur.fetchone()
    if not row:
        return {"ok": False, "reason": f"no review found for {key}"}
    row = dict(row)
    if int(new_target) <= int(row["current_target"]):
        return {"ok": False, "reason": f"{row['label']} already has a goal of {int(row['current_target']):,}"}
    if row["kind"] == "angle":
        from . import testgrid
        result = testgrid.auto_expand_champion(row["angle"], None, target_leads=int(new_target), requires_approval=False)
        ok = bool(result.get("target_set"))
    else:
        from . import materials_fill
        materials_fill.raise_target(int(row["campaign_id"]), int(new_target))
        result, ok = {"target": int(new_target)}, True
    if ok:
        with db.cursor() as cur:
            cur.execute("UPDATE scale_reviews SET status='approved', current_target=%s, updated_at=now() WHERE key=%s", (int(new_target), key))
    db.log(None, "scale_review", f"{key} -> {new_target} approved by {user or 'unknown'}: {result}")
    return {"ok": ok, "label": row["label"], "target": int(new_target), "result": result}
