"""Stage 6 -- Slack approval gate.

Nothing reaches EmailBison without an explicit approval. The batch's companies and
contacts are written as 'reserved' at the moment the batch is posted, so the next
weekly run skips them while a human decides (decision #9) instead of re-finding and
re-enriching the same list.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from .. import db
from ..clients import slack
from ..config import settings


def new_batch_id(vertical: str) -> str:
    slug = "".join(ch for ch in vertical.lower() if ch.isalnum())[:12] or "batch"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def estimate_credits(stats: dict) -> str:
    """Rough per-run spend, reported in the Slack summary (spec section 7)."""
    return (
        f"~{stats.get('companies_found', 0)} AI Ark results, "
        f"~{stats.get('companies_kept', 0)} enforcement calls, "
        f"~{stats.get('contacts_found', 0)} Quick Enrich lookups, "
        f"{stats.get('hunter_calls', 0)} Hunter calls "
        f"({stats.get('hunter_skipped', 0)} skipped as already fresh), "
        f"~{stats.get('copy_generated', 0) * 3} OpenRouter calls"
    )


def open_batch(run_id: str, vertical, companies: list[dict], contacts: list[dict],
               stats: dict) -> str | None:
    """Reserve the batch's records, post the summary, return the batch_id.

    Returns None when there is nothing to approve -- an empty batch should not consume
    a human's attention or a 72h timer.
    """
    if not contacts:
        db.log(run_id, "stage6", f"{vertical.name}: no sendable contacts, no batch opened",
               level="warn")
        return None

    batch_id = new_batch_id(vertical.name)
    summary = dict(stats)
    summary["credit_estimate"] = estimate_credits(stats)
    summary["vertical"] = vertical.as_dict()

    db.create_batch(batch_id, run_id, vertical.name, summary, len(companies), len(contacts))
    db.reserve_companies(companies, run_id, batch_id, vertical.name)
    db.reserve_contacts(contacts, run_id, batch_id)

    # Samples in the same order the dropdown will resolve them: db.batch_contacts.
    blocks = slack.batch_summary_blocks(
        batch_id, vertical.name, vertical.rationale, summary, db.batch_contacts(batch_id)[:3]
    )
    # In the vertical's thread AND broadcast to the channel: it needs a human.
    ts = slack.post(
        f"Batch {batch_id} ready for approval: {vertical.name}, "
        f"{len(contacts)} contacts at {len(companies)} companies.",
        blocks=blocks, thread_ts=db.vertical_thread(vertical.name), broadcast=True,
    )
    if ts:
        with db.cursor() as cur:
            cur.execute("UPDATE batches SET slack_ts=%s WHERE batch_id=%s", (ts, batch_id))

    db.log(run_id, "stage6",
           f"batch {batch_id} opened for {vertical.name}",
           companies=len(companies), contacts=len(contacts),
           expires_in_hours=settings.approval_timeout_hours)
    return batch_id


def _show(batch_id: str) -> str:
    """Re-post a batch's samples. The Slack channel's history is cleared on a retention
    policy, so a posted batch can vanish before it is reviewed -- the batch itself lives
    in Postgres, and this summons it back rather than losing the approval to a timer."""
    batch = db.get_batch(batch_id)
    if not batch:
        return f"No batch `{batch_id}` found."
    contacts = db.batch_contacts(batch_id)
    summary = dict(batch.get("summary") or {})
    # Live counts, not the ones frozen when the batch first opened: a topped-up batch
    # read "1,849 sendable" while it held 3,000 (Troy, 2026-09-14).
    summary["contacts_sendable"] = batch["contact_count"]
    summary["companies_kept"] = batch["company_count"]
    vert = summary.get("vertical") or {}
    blocks = slack.batch_summary_blocks(
        batch_id, batch["vertical"], vert.get("rationale") or "", summary, contacts[:3])
    slack.post(f"Batch {batch_id} ({batch['approval_status']}): {batch['vertical']}, "
               f"{batch['contact_count']} contacts.", blocks=blocks)
    return f"Re-posted `{batch_id}`. Full copy: {settings.public_url}/review/{batch_id}"


def _deploycheck() -> str:
    """Plainly: safe to deploy, or name exactly what a deploy would kill right now."""
    problems: list[str] = []
    live = db.run_alive(within_minutes=45)
    if live:
        age = None
        with db.cursor() as cur:
            cur.execute("SELECT max(ts) AS t FROM logs WHERE run_id=%s", (live,))
            row = cur.fetchone()
        if row and row["t"]:
            from datetime import datetime, timezone
            age = int((datetime.now(timezone.utc) - row["t"]).total_seconds() / 60)
        working = []
        with db.cursor() as cur:
            cur.execute("SELECT vertical FROM vertical_backlog WHERE status='running' LIMIT 6")
            working = [r["vertical"] for r in cur.fetchall()]
        problems.append(f"`{live}` is working" +
                        (f" (last step {age} min ago)" if age is not None else "") +
                        (", on: " + ", ".join(working) if working else ""))
    with db.cursor() as cur:
        cur.execute("SELECT campaign_id, vertical, scaling_run_id FROM ve_campaigns "
                    " WHERE scaling_run_id IS NOT NULL")
        for r in cur.fetchall():
            if db.run_is_truly_alive(r["scaling_run_id"]):
                problems.append(f"*{r['vertical']}* is mid-scale-up under "
                                f"`{r['scaling_run_id']}` (campaign {r['campaign_id']})")
    if not problems:
        return (":white_check_mark: *Safe to deploy.* Nothing is running -- no live "
                "tick, no scale-up in flight. Banked state means even a mistimed "
                "deploy is recoverable, but right now nothing would even be touched.")
    return (":no_entry: *A deploy right now kills live work:*\n" +
            "\n".join(f"• {p}" for p in problems) +
            "\n\nEverything is banked, so it recovers -- but the slice in flight is "
            "lost and the next tick spends time re-fetching. Wait for it to finish, "
            "or deploy knowingly.")


def _brief() -> str:
    """Plain-English answers to the four things Troy keeps asking for on demand:
    what just ran, which vendors are out, whether the last check added anything
    (and why not), and the single biggest lever to raise output right now. Every
    number here comes from the database or a live EmailBison call -- nothing reads
    an in-process flag, since this runs in whichever service answered the Slack
    command, not necessarily the one that ran the last tick.
    """
    with db.cursor() as cur:
        cur.execute("SELECT run_id, started_at, finished_at, status, notes FROM runs "
                    " WHERE run_id LIKE 'resume-%%' ORDER BY started_at DESC LIMIT 1")
        last_tick = cur.fetchone()

    lines = []

    # 1. WHAT JUST RAN
    if not last_tick:
        lines.append("1. *What ran:* nothing yet -- no two-hourly check has fired.")
    else:
        t = dict(last_tick)
        ago_min = int((_now() - t["started_at"]).total_seconds() / 60)
        done = (f"finished {int((t['finished_at'] - t['started_at']).total_seconds() / 60)} "
                f"min later" if t["finished_at"] else "is still running")
        note = t["notes"] or "no summary logged"
        lines.append(f"1. *What ran:* the two-hourly check started {ago_min} min ago and "
                      f"{done} ({t['status']}). In plain terms: {note}.")

    # 2. VENDORS CURRENTLY OUT (persisted ledger, not an in-process breaker flag)
    refused = db.vendor_refusals_since(hours=24)
    if refused:
        named = ", ".join(f"*{v}* ({n} refused call{'s' if n != 1 else ''})"
                          for v, n in refused.items())
        lines.append(f"2. *Vendors out:* {named} in the last 24h. Top up and the "
                      f"pipeline reopens anything it missed automatically.")
    else:
        lines.append("2. *Vendors out:* none -- every vendor answered normally in "
                      "the last 24h.")

    # 3. DID THE LAST CHECK ADD ANYTHING, AND IF NOT, WHY
    if last_tick and last_tick["status"] == "nothing_paused":
        lines.append("3. *Did it add anything:* no -- nothing was paused or short, "
                      "so there was nothing for it to do. That is the system working "
                      "correctly, not a stall.")
    elif last_tick and last_tick["status"] == "still_blocked":
        lines.append(f"3. *Did it add anything:* no -- it was blocked: "
                      f"{last_tick['notes'] or 'reason not logged'}.")
    elif last_tick:
        lines.append(f"3. *Did it add anything:* see line 1 -- {last_tick['notes'] or 'no detail logged'}.")
    else:
        lines.append("3. *Did it add anything:* no check has run yet to answer this.")

    # 4. THE SINGLE BIGGEST LEVER, in priority order: a vendor actually out >
    # a vertical parked for a human > sender capacity actually tight > nothing to do.
    paused_humans = db.paused_for_human()
    week = db.weekly_progress()
    if refused:
        top_vendor = max(refused, key=refused.get)
        lines.append(f"4. *Biggest lever:* get *{top_vendor}* topped up with credit -- "
                      f"it refused {refused[top_vendor]} call(s) in the last 24h and "
                      f"every one of those is a company that found nothing.")
    elif paused_humans:
        p = paused_humans[0]
        lines.append(f"4. *Biggest lever:* *{p['vertical']}* is parked waiting on a "
                      f"human ({p['kind']}-pause since {p['since']:%d %b}) -- widen its "
                      f"audience (employee size, geography or titles) or leave it "
                      f"paused on purpose.")
    else:
        try:
            from .. import senders
            cap = senders.free_capacity()
            tight = cap["daily"] < 600
        except Exception:
            cap, tight = None, False
        if tight:
            lines.append(f"4. *Biggest lever:* sender capacity -- only "
                         f"{cap['daily']:,}/day free across {cap['accounts']} accounts. "
                         f"Archive more finished campaigns or connect more senders.")
        elif week["leads"] < week["pace_quota"] * 0.8:
            lines.append(f"4. *Biggest lever:* nothing is broken, but the week is "
                         f"behind pace ({week['leads']:,} of a {week['pace_quota']:,} "
                         f"target so far) -- the midweek intake should pick this up on "
                         f"its own; `/ve topup <client> <vertical>` forces it sooner.")
        else:
            lines.append("4. *Biggest lever:* none right now -- nothing is blocked and "
                         "the week is on pace. The system is running at its natural rate.")

    return "\n".join(lines)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _open_batches() -> list[dict]:
    """Pending and approved batches -- everything a human can still act on."""
    with db.cursor() as cur:
        cur.execute("SELECT batch_id, vertical, contact_count, expires_at, approval_status "
                    "  FROM batches WHERE approval_status IN ('pending','approved') "
                    " ORDER BY created_at")
        return cur.fetchall()


def _resolve(token: str) -> tuple[dict | None, str | None]:
    """A batch from a number, an id, or the start of a vertical name."""
    if token.isdigit():
        pending = _open_batches()
        n = int(token)
        if not 1 <= n <= len(pending):
            return None, (f"There is no pending batch #{n}. `/ve status` lists "
                          f"{len(pending)} pending.")
        return db.get_batch(pending[n - 1]["batch_id"]), None
    batch = db.get_batch(token)
    if batch:
        return batch, None
    low = token.lower()
    hits = [b for b in _open_batches()
            if b["batch_id"].lower().startswith(low) or b["vertical"].lower().startswith(low)]
    if len(hits) == 1:
        return db.get_batch(hits[0]["batch_id"]), None
    if len(hits) > 1:
        names = ", ".join(f"`{b['batch_id']}`" for b in hits)
        return None, f"`{token}` matches several pending batches: {names}. Use the number."
    return None, f"No batch `{token}` found. `/ve status` lists the pending ones."


# Batches with a live push in flight in this process, so a double click cannot load
# the same leads twice.
_pushing: set[str] = set()
_push_lock = __import__("threading").Lock()
_live_push_serial = __import__("threading").Lock()


def push_button(batch_id: str) -> dict:
    """The 'Push to EmailBison' button shown once a batch is approved."""
    return {
        "type": "actions", "block_id": f"push:{batch_id}",
        "elements": [{
            "type": "button", "action_id": "push_batch", "style": "primary",
            "value": batch_id,
            "text": {"type": "plain_text", "text": "Push to EmailBison"},
            "confirm": {
                "title": {"type": "plain_text", "text": "Send this batch?"},
                "text": {"type": "mrkdwn",
                         "text": (f"`{batch_id}`: a new campaign is created from the model, "
                                  f"sender accounts are attached and the leads are loaded. "
                                  f"This is the step that leads to email going out.")},
                "confirm": {"type": "plain_text", "text": "Push live"},
                "deny": {"type": "plain_text", "text": "Cancel"}}}],
    }


def _push(batch: dict, user: str, live: bool) -> str | dict:
    """`/ve push <n>` (dry run) and `/ve push <n> live`. Troy, 2026-09-14: the last
    manual step was a laptop command; now it runs from Slack, dry run first, and the
    live push needs its own explicit word or button."""
    from . import s7_push
    batch_id = batch["batch_id"]
    if batch["approval_status"] == "pushed":
        return f"`{batch_id}` was already pushed."
    if batch["approval_status"] != "approved":
        return (f"`{batch_id}` is {batch['approval_status']}, not approved. Approve it "
                f"first: `/ve confirm {batch_id}`.")
    if not live:
        r = s7_push.push(batch_id, live=False)
        text = (f"Dry run for `{batch_id}` ({batch['vertical']}): {r.get('lead_count', 0):,} "
                f"leads ready, campaign to create `{r.get('campaign_to_create')}`. "
                f"Nothing sent.")
        if r.get("invalid"):
            text += f" {len(r['invalid'])} leads would be rejected."
        return {"text": text + f"\nPush live with `/ve push {batch_id} live` or the button.",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                           push_button(batch_id)]}
    with _push_lock:
        if batch_id in _pushing:
            return f"`{batch_id}` is already being pushed. Watch the vertical's thread."
        _pushing.add(batch_id)
    try:
        # One live push at a time. Three at once on 2026-09-14 got two of them
        # throttled at the lead load; the campaigns existed with senders and no leads.
        with _live_push_serial:
            r = s7_push.push(batch_id, live=True)
    except Exception as exc:
        return f"Push of `{batch_id}` FAILED: {str(exc)[:300]}"
    finally:
        with _push_lock:
            _pushing.discard(batch_id)
    if r.get("held"):
        return (f"`{batch_id}` is approved but held: only {r['free_daily']:,} sends/day are "
                f"free right now. It launches on its own when running campaigns finish.")
    if r.get("error"):
        return (f"EmailBison refused the load for `{batch_id}`; the exact reason is in the "
                f"vertical's thread. The batch stays approved -- push again once fixed.")
    started = " Campaign started." if r.get("started") else ""
    return (f"Pushed `{batch_id}`: {r.get('lead_count', 0):,} leads loaded by {user}.{started} "
            f"Details in the vertical's thread.")


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent.parent


def _launch(client_name: str, mode: str, opts: dict, user: str) -> str:
    """Start a run for a client as a SEPARATE PROCESS.

    A subprocess, not a thread, and deliberately. The web service is loaded as one
    client: its settings are read once at import and its database connections are held
    per thread, so switching client inside the live process would leave a half-switched
    world -- one client's prompts against another's database is the exact failure the
    profiles exist to prevent. A fresh process with CLIENT set is unambiguous.

    The run then reports into that client's own Slack channel, so a ProductEVO run does
    not narrate itself into IDPR's channel.
    """
    import os
    import subprocess
    import sys

    argv = [sys.executable, "-m", "app.runner", "client",
            "--client", client_name, "--mode", mode]
    for flag in ("sample", "verticals", "contacts"):
        if opts.get(flag):
            argv += [f"--{flag}", str(opts[flag])]
    if opts.get("variants"):
        argv += ["--variants", opts["variants"]]
    if opts.get("full"):
        argv += ["--full"]
    if opts.get("probe"):
        argv += ["--probe"]
    for aud in opts.get("audience") or []:
        argv += ["--audience", aud]

    env = dict(os.environ)
    env["CLIENT"] = client_name
    env["PYTHONIOENCODING"] = "utf-8"
    if opts.get("contacts") and mode == "expand" and opts.get("audience"):
        # The weekly flow sizes every vertical from these two settings (read once, when the run's
        # process starts), so a named-audience run of N contacts sets both to N.
        env["TARGET_CONTACTS"] = str(int(opts["contacts"]))
        env["CONTACT_CAP"] = str(int(opts["contacts"]))
    try:
        subprocess.Popen(argv, cwd=str(REPO_ROOT), env=env,
                         stdout=_run_log(client_name)[1],
                         stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as exc:
        return f"Could not start the run: {str(exc)[:200]}"
    extra = " ".join(f"{k}={v}" for k, v in opts.items() if v and k != "audience")
    note = ""
    if opts.get("sample"):
        note = (f"\nThis is a SAMPLE of {opts['sample']} per arm, about a dollar. Read "
                f"them, then `/ve run {client_name} full` to write the whole run.")
    return (f"Started *{mode}* for *{client_name}* by {user}. {extra}{note}\n"
            f"Progress lands in that client's channel; `/ve status` shows batches when "
            f"they open. A redeploy of this service would kill it.")


RUN_LOG_DIR = Path(tempfile.gettempdir()) / "ve-runs"


def _run_log(client_name: str):
    """A file for a launched run's output.

    It used to go to DEVNULL. A run that is KILLED rather than raising -- the kernel's
    out-of-memory killer, or the container going away -- never reaches the crash
    handler, so nothing is posted, nothing is written to the database, and the run row
    stays 'running' forever. That is exactly what ProductEVO's 2026-09-20 16:09 run did:
    two log lines, then silence, and no way at all to find out why. A file costs
    nothing and is the only record that survives a kill.
    """
    try:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = RUN_LOG_DIR / f"{client_name}.log"
        return path, open(path, "ab", buffering=0)
    except Exception:
        return None, subprocess.DEVNULL


def tail_run_log(client_name: str, lines: int = 12) -> str:
    """The last lines a launched run printed before it stopped."""
    try:
        path = RUN_LOG_DIR / f"{client_name}.log"
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return ""


def _launch_step(client_name: str, step: list[str], user: str, ack: str) -> str:
    """Run a runner step for one client, in its own process."""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env["CLIENT"] = client_name
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.Popen([sys.executable, "-m", "app.runner", *step, "--client",
                          client_name] if step[0] in ("conclude", "calibrate")
                         else [sys.executable, "-m", "app.runner", *step],
                         cwd=str(REPO_ROOT), env=env,
                         stdout=_run_log(client_name)[1],
                         stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as exc:
        return f"Could not start: {str(exc)[:160]}"
    return ack


def _parse_run(parts: list[str]) -> tuple[str | None, str | None, dict, str | None]:
    """`/ve run <client> <mode> [arms a,b] [sample N] [verticals N] [contacts N]`"""
    from .. import client as _client, modes
    if len(parts) < 2:
        return None, None, {}, (
            "Usage: `/ve run <client> <mode> [arms a,b] [sample 20] [verticals 3] "
            "[contacts 3000]`\n"
            f"clients: {', '.join(_client.available()) or 'none'} · "
            f"modes: {', '.join(modes.MODES)}")
    name = parts[1].strip().lower()
    if name not in _client.available():
        return None, None, {}, (f"Unknown client {name!r}. "
                                f"Known: {', '.join(_client.available())}")
    profile = _client.load(name)
    mode = profile.default_mode
    opts: dict = {"audience": []}
    rest = parts[2:]
    i = 0
    while i < len(rest):
        tok = rest[i].strip().lower()
        if tok in modes.MODES:
            mode = tok
            i += 1
        elif tok == "full":
            opts["full"] = True
            i += 1
        elif tok == "probe":
            opts["probe"] = True
            i += 1
        elif tok in ("sample", "verticals", "contacts") and i + 1 < len(rest):
            try:
                opts[tok] = int(rest[i + 1])
            except ValueError:
                return None, None, {}, f"`{tok}` needs a number, got {rest[i + 1]!r}"
            i += 2
        elif tok in ("arms", "arm") and i + 1 < len(rest):
            opts["variants"] = rest[i + 1].strip()
            i += 2
        elif tok == "audience" and i + 1 < len(rest):
            opts["audience"].append(" ".join(rest[i + 1:]))
            break
        else:
            return None, None, {}, f"Did not understand {rest[i]!r}"
        continue
    # A copy test samples first unless the operator says `full`, so a plain
    # `/ve run productevo` costs about a dollar rather than a few hundred.
    if mode == "copy-test" and not opts.get("sample") and not opts.get("full"):
        opts["sample"] = 20
    if opts.get("variants"):
        known = profile.variant_names()
        asked = [v.strip() for v in opts["variants"].split(",") if v.strip()]
        unknown = [v for v in asked if v not in known]
        if unknown:
            return None, None, {}, (f"No arm {unknown[0]!r} for {name}. "
                                    f"Arms: {', '.join(known)}")
        if len(asked) < 2:
            return None, None, {}, "A copy test needs two arms to compare."
        opts["variants"] = ",".join(asked)
    return name, mode, opts, None


def _add_arm(raw: str, user: str) -> str:
    """`/ve addarm <client> <name>` followed by the angle, in plain English.

    An arm handed over this way is stored in the client's database, not written into
    the repo. Two reasons, both hard: the service's filesystem is wiped on every
    deploy, so a file it wrote would not survive; and a file in the repo needs a commit
    and a deploy before it can be tested, which is the exact bottleneck this removes.

    The angle is INSTRUCTIONS, not an email. It is appended to the client's approved
    prompts, so the arm inherits the research brief, the structure, the length limits
    and the banned-word rules, and differs only in the argument it makes.
    """
    import re

    from .. import client as _client, db as _db

    head = raw.split(None, 3)
    if len(head) < 3:
        return ("Usage: `/ve addarm <client> <short-name>` then the angle on the next "
                "line.\n\nExample:\n```/ve addarm productevo cost-question\n"
                "Open by asking what they paid in duty last quarter, as a question they "
                "can answer from memory. No fear, no tariff news, no predictions. The "
                "bet is that a question they can answer beats a claim they must "
                "judge.```")

    name = head[1].lower()
    if name not in _client.available():
        return f"No client `{name}`. Clients: {', '.join(_client.available())}"
    arm = head[2].strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}", arm):
        return (f"`{arm}` is not a usable name. Use lowercase letters, numbers and "
                f"hyphens, 2 to 39 characters, like `cost-question`.")
    angle = (head[3] if len(head) > 3 else "").strip()

    # "bet: ..." on its own line states what the arm is testing, for the result readout.
    bet = ""
    kept = []
    for line in angle.splitlines():
        if line.strip().lower().startswith("bet:") and not bet:
            bet = line.split(":", 1)[1].strip()
        else:
            kept.append(line)
    angle = "\n".join(kept).strip()

    if len(angle) < 40:
        return ("That angle is too short to be a different arm. Say what the opening "
                "should argue and what it must not do, in a sentence or three. The "
                "engine already knows the client, the offer and the format.")
    if len(angle) > 4000:
        return ("That is longer than an instruction; it reads like a whole prompt. "
                "Put a full prompt set in the repo instead, as "
                f"`clients/{name}/prompts/{arm}/`, or send me the shorter version.")

    pr = _client.load(name)
    if arm in pr.discovered_variants() and arm not in pr.handed_arms():
        return (f"`{arm}` already exists in the repo for {name}. Pick another name so "
                f"the two do not get confused.")
    existing = pr.handed_arms()
    replacing = arm in existing

    try:
        with _db.for_client(name):
            _db.init_schema()
            _db.add_arm(name, arm, angle, bet=bet, by=user)
    except Exception as exc:
        return f"Could not save it: {str(exc)[:200]}"
    _client.forget_arms()

    base = pr.base_variant or (pr.variant_names()[0] if pr.variant_names() else "?")
    verb = "Replaced" if replacing else "Added"
    # What to compare it against: the promoted winner if there is one, else the base.
    against = base
    try:
        with _db.for_client(name):
            live = _db.active_variant(name)
        if live:
            against = live
    except Exception:
        pass
    return (f"{verb} arm `{arm}` for *{pr.display_name}*."
            f"{chr(10)}It inherits everything from `{base}` — the research brief, "
            f"email 1, email 2, the length limits and the banned words — and changes "
            f"only the argument."
            f"{chr(10)}{chr(10)}See one before you spend: `/ve preview {name} {arm}`"
            f"{chr(10)}Then test it against the current best:"
            f"{chr(10)}`/ve run {name} copy-test arms {against},{arm} sample 20`"
            f"{chr(10)}_Name the arms. A run left to itself takes the first two in the "
            f"list, which would leave this one out._")


HELP = """*Everything `/ve` does.*

*Run something*
• `/ve clients` — who is set up, and whether they are ready to send
• `/ve run <client> [mode] [arms a,b] [sample 20]` — start a run
• `/ve topup <client> <vertical>` — re-source a named vertical and add to its
  existing pending batch, when it landed short instead of opening a new one
• `/ve status` — what is waiting on you right now
• `/ve brief` — plain-English: what just ran, which vendors are out, whether the
  last two-hourly check added anything (and why not), and the biggest lever right now
• `/ve deploycheck` — is anything running that a deploy would kill right now?
• `/ve campaigns` — each fill campaign with progress and Add 500 / Add 1,000 buttons
• `/ve add 500 to 2174` — add leads to a fill campaign; `/ve register 2190 1500 company` starts filling another
• `/ve progress <client>` — is that client's run alive, and where is it
• `/ve show <number>|all` — the copy for a batch, before you approve it

*Decide*
• `/ve confirm <number> [note]` — approve a batch
• `/ve kill <number> <reason>` — reject it; the reason is kept and learned from
• `/ve push <number> [live]` — send it to EmailBison
• `/ve booked <email> [note]` — record a meeting, so it counts

*Copy*
• `/ve requeue [vertical]` — give needs_review contacts another shot at copy (skips
  ones no retry can fix: no first name, or already declined by EmailBison)
• `/ve arms <client>` — the copy being tested, and where each one came from
• `/ve addarm <client> <name>` then the angle — hand over copy to test
• `/ve droparm <client> <name>` — stop using an arm you handed over
• `/ve preview <client> [arm]` — one real email, before you spend anything
• `/ve conclude <client>` — compare the arms and name a winner
• `/ve promote <client> <arm>` — make that arm the client's copy from now on
"""


def handle_command(text: str, user: str) -> str | dict:
    """Handle `/ve approve <id> [note]` / `/ve kill <id> <reason>` / `/ve status`.

    A kill REQUIRES a reason and an approval takes an optional note. The decision is
    the most informative signal the system receives -- a human looked at real copy for
    a real vertical and said no -- and until 2026-09-14 only the decider's name was
    kept. The reason is stored on the batch and stamped on every suppressed record.
    """
    raw = (text or "").strip()
    parts = raw.split()
    if not parts:
        return HELP
    action = parts[0].lower()
    if action in ("help", "commands", "?"):
        return HELP

    if action == "booked":
        if len(parts) < 2 or "@" not in parts[1]:
            return "Usage: `/ve booked person@company.com [optional note]`"
        email = parts[1].strip().lower()
        note = " ".join(parts[2:]).strip() or None
        if not db.mark_booked(email, actor=user, note=note):
            return f"No pushed pipeline contact found for `{email}`."
        return f"Booked meeting recorded for `{email}`. It now counts in the learning loop."

    if action == "topup":
        # /ve topup <client> <vertical name...>
        #
        # Troy, 2026-09-29: Monday's intake landed 3 batches short of the 3,000 cap
        # (a missing LEADOCEAN_API_KEY silently starved the contact cascade). Fixing
        # the cause does not refill what already landed short, and the weekly walk
        # only ever moves FORWARD through the backlog -- a vertical that already has
        # a batch is never revisited on its own. This is the durable answer to "can
        # we make this run again if it happens again": re-source a NAMED vertical and
        # add to its existing pending batch (app.runner's --topup), launched the same
        # way `/ve run` launches anything -- as a real process inside this service,
        # not a local script that cannot reach the database at all.
        if len(parts) < 3:
            return ("Usage: `/ve topup <client> <vertical name>` -- re-source that "
                    "vertical and add to its existing pending batch, rather than "
                    "opening a new one or waiting for the weekly walk to reach it.")
        from .. import client as _client, db as _db
        name = parts[1].strip().lower()
        if name not in _client.available():
            return f"Unknown client {name!r}. Known: {', '.join(_client.available())}"
        vertical = " ".join(parts[2:]).strip()
        try:
            with _db.for_client(name):
                live = _db.run_alive(within_minutes=10)
        except Exception:
            live = None
        if live:
            return (f"*{name}* already has a run working (`{live}`). Wait for it, "
                    f"or check `/ve progress {name}`.")
        return _launch_step(
            name, ["client", "--client", name, "--mode", "expand", "--topup", vertical],
            user,
            f"Topping up *{vertical}* for *{name}* -- re-sourcing and adding to its "
            f"existing pending batch, if any. Check `/ve progress {name}`.")

    if action == "requeue":
        # /ve requeue [vertical words...]
        vertical = " ".join(parts[1:]).strip() or None
        n = db.requeue_needs_review(vertical=vertical)
        scope = f"*{vertical}*" if vertical else "all verticals"
        return (f"Requeued {n} needs_review contact(s) for {scope} for a fresh copy "
                f"attempt on the next resume tick -- skipped anything with no first "
                f"name on record, already declined by EmailBison, or already requeued "
                f"twice before.")

    if action == "clients":
        from .. import client as _client
        rows = []
        for n in _client.available():
            pr = _client.load(n)
            state = "ready" if pr.is_complete_for_sending else "INCOMPLETE"
            rows.append(f"• *{n}* — {pr.display_name}, workspace {pr.workspace_id}, "
                        f"default mode `{pr.default_mode}`, {state}")
        return ("Clients:\n" + "\n".join(rows) +
                "\n\nStart one with `/ve run <client> [mode] [arms a,b] [sample 20]`") if rows \
            else "No client profiles found."

    if action == "run":
        name, mode, opts, problem = _parse_run(parts)
        if problem:
            return problem
        from .. import client as _client, db as _db, modes
        profile = _client.load(name)
        blockers = modes.preflight(profile, mode)
        warnings = []
        try:
            warnings = modes.degraded(mode)
        except Exception:
            warnings = []
        if blockers:
            return ("Cannot run *" + name + "* in `" + mode + "`:\n"
                    + "\n".join(f"• {b}" for b in blockers))
        # THIS client's runs. The check read whichever database the web process booted
        # against, so IDPR's weekly run blocked a ProductEVO one -- two clients with
        # separate databases, separate mailboxes and separate vendors, and one could
        # stop the other for no reason (found 2026-09-21, after IDPR's Monday run held
        # ProductEVO off all afternoon).
        try:
            with _db.for_client(name):
                live = _db.run_alive(within_minutes=10)
        except Exception:
            live = None
        if live:
            return (f"*{name}* already has a run working (`{live}`). Wait for it, or "
                    f"check `/ve progress {name}`.")
        out = _launch(name, mode, opts, user)
        if warnings:
            out += "\n\n:warning: " + "\n:warning: ".join(w[:220] for w in warnings)
        return out

    if action == "preview":
        # /ve preview <client> [arm] [email]
        from .. import client as _client
        if len(parts) < 2 or parts[1].lower() not in _client.available():
            return (f"Usage: `/ve preview <client> [arm]`\n"
                    f"clients: {', '.join(_client.available())}")
        name = parts[1].lower()
        pr = _client.load(name)
        arm = None
        addr = None
        for tok in parts[2:]:
            if "@" in tok:
                addr = tok.strip("<>").split("|")[0].replace("mailto:", "")
            elif tok in pr.variant_names():
                arm = tok
        import os
        import subprocess
        import sys
        argv = [sys.executable, "-m", "app.runner", "preview", "--client", name]
        if arm:
            argv += ["--arm", arm]
        if addr:
            argv += ["--email", addr]
        env = dict(os.environ)
        env["CLIENT"] = name
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            subprocess.Popen(argv, cwd=str(REPO_ROOT), env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception as exc:
            return f"Could not start the preview: {str(exc)[:160]}"
        return (f"Writing one {name} email with the prompts as they stand"
                + (f" on arm `{arm}`" if arm else "")
                + ". It posts here in about a minute.")

    if action in ("conclude", "result"):
        from .. import client as _client
        if len(parts) < 2 or parts[1].lower() not in _client.available():
            return f"Usage: `/ve conclude <client>` · {', '.join(_client.available())}"
        return _launch_step(parts[1].lower(), ["conclude"], user,
                            "Comparing the copy arms; the result posts here shortly.")

    if action == "promote":
        from .. import client as _client, conclude as _conclude
        if len(parts) < 3 or parts[1].lower() not in _client.available():
            return "Usage: `/ve promote <client> <arm>`"
        name, arm = parts[1].lower(), parts[2]
        pr = _client.load(name)
        if arm not in pr.variant_names():
            return (f"{name} has no arm {arm!r}. Arms: {', '.join(pr.variant_names())}")
        from .. import db as _db
        try:
            with _db.for_client(name):
                return _conclude.promote(name, arm, by=user,
                                         evidence="promoted from Slack")
        except Exception as exc:
            return f"Could not promote: {str(exc)[:200]}"

    if action in ("addarm", "arm+", "newarm"):
        return _add_arm(raw, user)

    if action in ("droparm", "rmarm", "arm-"):
        from .. import client as _client, db as _db
        if len(parts) < 3 or parts[1].lower() not in _client.available():
            return "Usage: `/ve droparm <client> <arm>`"
        name, arm = parts[1].lower(), parts[2]
        pr = _client.load(name)
        if arm not in pr.handed_arms():
            return (f"`{arm}` was not handed over from Slack, so it cannot be dropped "
                    f"here. Arms in the repo are removed by editing the repo.")
        with _db.for_client(name):
            _db.retire_arm(name, arm)
        _client.forget_arms()
        return (f"Retired `{arm}`. It will not be used in new runs. Anything already "
                f"sent under it stays, and its result stays comparable.")

    if action == "arms":
        from .. import client as _client
        if len(parts) < 2 or parts[1].lower() not in _client.available():
            return f"Usage: `/ve arms <client>` · {', '.join(_client.available())}"
        pr = _client.load(parts[1].lower())
        handed = pr.handed_arms()
        lines = []
        for v in pr.variant_names():
            if v in handed:
                who = handed[v].get("created_by") or "slack"
                where = f"handed over in Slack by {who}"
            elif pr.prompts_for(v) != pr.prompts_dir:
                where = "its own prompts, in the repo"
            else:
                where = "an angle in the repo"
            lines.append(f"• *{v}* — {pr.bet_for(v)[:90]}\n   _{where}_")
        return (f"*{pr.display_name}* arms:\n" + "\n".join(lines) +
                "\n\nHand over another right here: `/ve addarm " + pr.name +
                " <name>` then the angle on the next line.")

    if action in ("progress", "running"):
        # `/ve status` reads whatever database THIS process was started against, which
        # is one client. A second client's run was therefore invisible from Slack: when
        # ProductEVO's first run died on an empty backlog (2026-09-18) nothing showed
        # it, and the only signal was silence. This reads the named client's own
        # database. Read-only and button-free on purpose -- a button carries a batch id
        # that the next click would resolve against the wrong database.
        from .. import client as _client, db as _db
        if len(parts) < 2 or parts[1].lower() not in _client.available():
            return f"Usage: `/ve progress <client>` · {', '.join(_client.available())}"
        name = parts[1].lower()
        pr = _client.load(name)
        try:
            with _db.for_client(name):
                with _db.cursor() as cur:
                    cur.execute("SELECT run_id, started_at, finished_at, status "
                                "  FROM runs ORDER BY started_at DESC LIMIT 1")
                    run = cur.fetchone()
                    if not run:
                        return f"*{pr.display_name}* has never run."
                    cur.execute("SELECT stage, message, ts FROM logs "
                                " WHERE run_id=%s ORDER BY id DESC LIMIT 6",
                                (run["run_id"],))
                    tail = cur.fetchall()
                    cur.execute("SELECT count(*) AS n FROM contacts "
                                " WHERE run_id=%s", (run["run_id"],))
                    people = cur.fetchone()["n"]
                    cur.execute("SELECT count(*) AS n FROM batches "
                                " WHERE run_id=%s", (run["run_id"],))
                    batches = cur.fetchone()["n"]
        except Exception as exc:
            return f"Could not read {name}'s database: {str(exc)[:200]}"

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        age = int((now - (tail[0]["ts"] if tail else run["started_at"]))
                  .total_seconds() // 60)
        if run["finished_at"]:
            state = f"finished {run['status']}"
        elif age > 45:
            state = (f":warning: nothing logged for {age} min — it may have died. "
                     f"A redeploy of the web service kills a run in progress.")
        else:
            state = f"working, last step {age} min ago"
        lines = [f"*{pr.display_name}* — run `{run['run_id']}`",
                 f"started {run['started_at']:%a %d %b %H:%M} UTC · {state}",
                 f"{people:,} contacts · {batches} batch(es) opened"]
        if tail:
            lines.append("")
            for r in reversed(tail):
                lines.append(f"`{r['ts']:%H:%M}` {r['stage']}: {str(r['message'])[:140]}")
        printed = tail_run_log(name, 10)
        if printed:
            lines.append("\nWhat it last printed:\n```" + printed[-1200:] + "```")
        return "\n".join(lines)

    if action == "brief":
        # /ve brief -- an on-demand, plain-English status check. Troy, 2026-10-01:
        # "I need to be able to ask the Slack app... what ran, this vendor is out,
        # the two hour check did not produce anything, what is going to raise the
        # lever." Four questions, every time, no jargon:
        #   1. What just ran (the last 2-hourly tick)
        #   2. Which vendors are currently out
        #   3. Did that last check actually add anything, and if not, why
        #   4. The single biggest lever to raise output right now
        return _brief()

    if action in ("deploycheck", "deploy"):
        # /ve deploycheck -- Troy, 2026-10-05: "is anything running right now that
        # this deploy would kill?" A deploy replaces the container instantly: it
        # killed the nightly review mid-scale on 2026-10-01 and orphaned a manual
        # tick that then blocked everything for 11 hours. This answers before, not
        # after. It changes nothing; it only looks.
        return _deploycheck()

    if action == "runway":
        # /<command> runway -- the compact one-screen count of leads, contacts, companies and what is left (app/runway.py)
        from .. import runway
        return runway.report()

    if action in ("campaigns", "controls"):
        # /<command> campaigns -- each fill campaign with its progress and Add 500 / Add 1,000 buttons (app/materials_fill.py)
        from .. import materials_fill
        return {"text": "Fill campaigns", "blocks": materials_fill.controls_blocks()}

    if action == "add":
        # /<command> add 500 to 2174
        from .. import materials_fill
        digits = [p for p in parts[1:] if p.isdigit()]
        if len(digits) != 2:
            return "Usage: `/<command> add <number of leads> to <campaign id>`, for example `add 500 to 2174`."
        n, cid = int(digits[0]), int(digits[1])
        r = materials_fill.add_leads(cid, n, user)
        return (f"Adding {r['added']:,} leads to campaign {r['campaign']}: it holds {r['was']:,}, the goal is now {r['target']:,}. {r['note']}" if r.get("ok") else r["reason"])

    if action == "register":
        # /<command> register 2190 1500 company|materials -- start filling another campaign, no deploy needed
        from .. import materials_fill
        digits = [p for p in parts[1:] if p.isdigit()]
        if len(digits) != 2:
            return "Usage: `/<command> register <campaign id> <target leads> company|materials`."
        r = materials_fill.register(int(digits[0]), int(digits[1]), materials=("materials" in [p.lower() for p in parts[1:]]), user=user)
        return (f"Registered *{r['name']}* (campaign {r['campaign']}): {r['loaded']:,} loaded, goal {r['target']:,}, "
                f"{'with materials hauled' if r['materials'] else 'company name only'}. Loading starts now." if r.get("ok") else r["reason"])

    if action == "status":
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        pending = _open_batches()
        lines = []
        for n, b in enumerate(pending, 1):
            if b["approval_status"] == "approved":
                when = "APPROVED, awaiting push"
            else:
                when = (f"expires {b['expires_at']:%a %d %b %H:%M} UTC" if b["expires_at"] > now
                        else "EXPIRED - releases at the next daily sweep")
            lines.append(f"*{n}.* `{b['batch_id']}` — {b['vertical']}, {b['contact_count']:,} "
                         f"contacts, {when}\n   review: {settings.public_url}/review/{b['batch_id']}")
        head = (("Open batches:\n" + "\n".join(lines) +
                 "\n\n`/ve confirm <n>` approves · `/ve kill <n> <reason>` · "
                 "`/ve push <n>` dry-runs, `/ve push <n> live` sends.")
                if lines else "No batches awaiting approval or push.")
        # Buttons per batch (Troy, 2026-09-14: "ve status did not give me push
        # buttons"): Approve/Kill while pending, Push once approved.
        blocks: list[dict] = []
        for n, b in enumerate(pending, 1):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lines[n - 1]}})
            blocks.append(push_button(b["batch_id"]) if b["approval_status"] == "approved"
                          else slack.gate_buttons(b["batch_id"]))
        try:
            with db.cursor() as cur:
                cur.execute("SELECT vertical, status FROM vertical_backlog "
                            " WHERE status IN ('running','paused_llm') ORDER BY projected DESC")
                live = cur.fetchall()
            if live:
                head += "\n\nIn flight:\n" + "\n".join(
                    f"• {r['vertical']} — {r['status']}" for r in live)
        except Exception:
            pass
        text = head + f"\n\nLive contacts: {settings.public_url}/live"
        if not blocks:
            return text
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"`/ve push <n>` for a dry run first. Live contacts: "
                       f"{settings.public_url}/live"}]})
        return {"text": text, "blocks": blocks[:48]}

    if action == "show":
        if len(parts) < 2:
            return "Which batch? `/ve show <number>` or `/ve show all`"
        token = parts[1].strip().strip("`").lower()
        if token in {"all", "pending", "*"}:
            pending = _open_batches()
            if not pending:
                return "No batches pending approval."
            for b in pending:
                _show(b["batch_id"])
            return (f"Re-posted {len(pending)} pending batch"
                    f"{'es' if len(pending) != 1 else ''} with their buttons.")
        batch, problem = _resolve(token)
        return problem or _show(batch["batch_id"])

    # Troy, 2026-09-14: "can I not just approve with confirm <number>?" Yes: the
    # number from `/ve status`, the batch id, or an unambiguous start of the vertical
    # name all name a batch. `confirm` and `ok` mean approve.
    if action in {"confirm", "ok", "yes"}:
        action = "approve"
    if action not in {"approve", "kill", "push"}:
        return (f"Unknown action {action!r}.\n\n" + HELP + "\n_(was: confirm/approve, kill, push, show, "
                "booked, or status.")
    if len(parts) < 2:
        return f"Which batch? `/ve {action} <number from /ve status>`"

    batch, problem = _resolve(parts[1].strip().strip("`"))
    if problem:
        return problem
    batch_id = batch["batch_id"]

    if action == "push":
        return _push(batch, user, live=(len(parts) > 2 and parts[2].lower() == "live"))
    if batch["approval_status"] != "pending":
        return (f"Batch `{batch_id}` is already {batch['approval_status']}"
                f"{' by ' + batch['approved_by'] if batch['approved_by'] else ''}.")

    reason = " ".join(parts[2:]).strip() or None

    if action == "kill":
        if not reason:
            return (f"Why kill `{batch_id}`? Add a reason so the system learns from it: "
                    f"`/ve kill {batch_id} <reason>` (e.g. `wrong buyer`, `copy too "
                    f"generic`, `companies off-vertical`).")
        moved = db.settle_batch(batch_id, "killed", decided_by=user, reason=reason)
        return (f"Killed `{batch_id}` -- {reason}. {moved['contacts']} contacts and "
                f"{moved['companies']} companies suppressed for "
                f"{settings.killed_dedup_days} days.")

    # Approve marks the gate passed AND launches, unless told to hold. Troy,
    # 2026-09-30: the separate push step was the clunky part; the confirm dialog on
    # the button (or the explicit word from the keyboard) is the deliberate step.
    # The two-hourly job was already pushing approved batches on its own, so this
    # only removes the wait, not a safeguard.
    hold = bool(reason) and reason.split()[0].lower() == "hold"
    if hold:
        reason = " ".join(reason.split()[1:]) or None
    db.settle_batch(batch_id, "approved", decided_by=user, reason=reason)
    note = f" Note: {reason}." if reason else ""
    if hold:
        text = (f"Approved `{batch_id}` ({batch['contact_count']:,} contacts), held.{note} "
                f"Launch with the button or `/ve push {batch_id} live`.")
        return {"text": text,
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                           push_button(batch_id)]}
    batch = db.get_batch(batch_id) or batch
    launched = _push(batch, user, live=True)
    head = f"Approved `{batch_id}` ({batch['contact_count']:,} contacts).{note} "
    if isinstance(launched, dict):
        launched = {**launched, "text": head + launched.get("text", "")}
        return launched
    return head + str(launched)


def auto_approve_if_topup(batch_id: str, vertical: str, run_id: str | None) -> bool:
    """Approve a batch without a human when it tops up a vertical that already sends.

    Troy, 2026-09-30. A first launch still waits for /ve approve: a human reads real
    copy for a new audience. A batch for a vertical that is already pushed -- its
    own campaign or a pool -- carries the copy strategy that human already approved,
    for more people, and waiting again only delayed sends. Returns True if approved.
    """
    campaign = db.vertical_is_launched(vertical)
    if not campaign:
        return False
    # A DROPPED vertical's campaign is archived; auto-approving here would just have
    # s7_push refuse it a moment later. Leave it pending for a human to kill.
    outcome = db.vertical_outcome(vertical)
    if outcome and outcome.get("verdict") == "drop":
        return False
    with db.cursor() as cur:
        cur.execute("SELECT approval_status, contact_count FROM batches WHERE batch_id=%s",
                    (batch_id,))
        row = cur.fetchone()
    if not row or row["approval_status"] != "pending":
        return False
    db.settle_batch(batch_id, "approved", decided_by="system:topup",
                    reason=f"top-up of already-launched campaign {campaign}")
    db.log(run_id, "stage6", f"{vertical}: batch {batch_id} auto-approved -- "
           f"{int(row['contact_count'] or 0):,} contacts topping up campaign {campaign}; "
           f"pushes on the next tick")
    # Channel-level on purpose (no thread): Troy, 2026-09-30, "at least notify me".
    slack.post(f":white_check_mark: *{vertical}* top-up auto-approved -- "
               f"{int(row['contact_count'] or 0):,} more contacts into campaign "
               f"{campaign}, same approved copy. Pushes on the next tick.")
    return True


def push_held_batches(run_id: str | None = None) -> list[dict]:
    """Every approved-but-unpushed batch gets one more try, oldest first.

    Called by the two-hourly job. A batch held for capacity launches when campaigns
    finish and free their accounts; one held by a refused load launches once the cause
    is fixed. Both are idempotent, so trying costs nothing.
    """
    from . import s7_push
    out = []
    with db.cursor() as cur:
        cur.execute("SELECT batch_id, vertical FROM batches WHERE approval_status='approved' "
                    " ORDER BY decided_at NULLS LAST, created_at")
        rows = cur.fetchall()
    for b in rows:
        try:
            with _live_push_serial:
                r = s7_push.push(b["batch_id"], live=True)
        except Exception as exc:
            r = {"batch_id": b["batch_id"], "error": str(exc)[:200]}
        db.log(run_id, "stage7", f"retry push {b['batch_id']}: "
               f"{'held' if r.get('held') else 'error' if r.get('error') else 'pushed'}")
        out.append(r)
    return out
