"""Slack -- batch summaries and the approval gate (buttons and slash commands).

Decision #4 chose slash commands for v1. Troy, 2026-09-14: "or a button to confirm on
the slack app" -- so every batch message now carries Approve and Kill buttons as well.
Approve asks for confirmation and settles the batch; Kill opens a modal that requires
a reason, because the reason is what the system learns from. The slash commands stay
as the fallback. Buttons need Interactivity enabled on the Slack app with the request
URL {PUBLIC_URL}/slack/interact; until it is, clicks do nothing and the commands work.

Requests are verified with Slack's signing secret (v0 HMAC). An unverified request
is rejected -- this endpoint can approve outbound email, so an open endpoint would let
anyone who learns the URL push a batch.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from ..config import settings
from .http import VendorError, request

POST_MESSAGE = "https://slack.com/api/chat.postMessage"
VIEWS_OPEN = "https://slack.com/api/views.open"
UPLOAD_URL = "https://slack.com/api/files.getUploadURLExternal"


def _headers() -> dict[str, str]:
    if not settings.slack_bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN is not set")
    return {
        "Authorization": f"Bearer {settings.slack_bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def active_channel() -> str:
    """The channel for the client this call is about, resolved NOW.

    settings is built once, when the process starts. That is right for a run, which is
    one client from start to finish, and wrong for the web service, which is one
    long-lived process serving every client. Reading the active profile per post is what
    keeps a second client's messages out of the first client's channel.
    """
    try:
        from .. import client as _client
        ch = _client.profile().slack_channel
        if ch:
            return ch
    except Exception:
        pass
    return settings.slack_channel


def post(text: str, blocks: list | None = None, channel: str | None = None,
         thread_ts: str | None = None, broadcast: bool = False) -> str | None:
    """Post a message; returns the message ts.

    Troy, 2026-09-13: one message per vertical, everything after it as replies in that
    thread, so the channel stays readable and a vertical's whole story is one click.
    `broadcast` also shows a reply in the channel -- used for the messages a human
    must act on (approval, paused, expiring).
    """
    body = {"channel": channel or active_channel(), "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
        if broadcast:
            body["reply_broadcast"] = True
    if blocks:
        body["blocks"] = blocks
    try:
        payload = request("slack", "POST", POST_MESSAGE, headers=_headers(), json_body=body)
    except VendorError as exc:
        print(f"[warn] slack post failed: {exc}", flush=True)
        return None
    if not (payload or {}).get("ok"):
        print(f"[warn] slack rejected message: {(payload or {}).get('error')}", flush=True)
        return None
    return payload.get("ts")


def _channel_id() -> str | None:
    """Resolve '#name' to a channel id for the files API. None when the bot cannot list."""
    name = (active_channel() or "").lstrip("#")
    if not name:
        return None
    if name.startswith(("C", "G")) and name.isupper():
        return name
    try:
        import httpx
        cursor = None
        for _ in range(10):
            params = {"limit": 200, "types": "public_channel,private_channel"}
            if cursor:
                params["cursor"] = cursor
            r = httpx.get("https://slack.com/api/conversations.list", headers=_headers(),
                          params=params, timeout=20).json()
            for ch in r.get("channels") or []:
                if ch.get("name") == name:
                    return ch.get("id")
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        return None
    return None


def upload_file(content: bytes, filename: str, title: str) -> bool:
    """Slack's three-step external upload. Best effort: False when the scope is missing."""
    try:
        import httpx
        r = httpx.get(UPLOAD_URL, headers=_headers(),
                      params={"filename": filename, "length": len(content)}, timeout=20).json()
        if not r.get("ok"):
            print(f"[warn] slack upload url refused: {r.get('error')}", flush=True)
            return False
        httpx.post(r["upload_url"], content=content, timeout=60)
        body = {"files": [{"id": r["file_id"], "title": title}]}
        cid = _channel_id()
        if cid:
            body["channel_id"] = cid
        done = httpx.post("https://slack.com/api/files.completeUploadExternal",
                          headers=_headers(), json=body, timeout=30).json()
        if not done.get("ok"):
            print(f"[warn] slack upload complete refused: {done.get('error')}", flush=True)
            return False
        return bool(cid)
    except Exception as exc:
        print(f"[warn] slack upload failed: {exc}", flush=True)
        return False


def verify_signature(timestamp: str, signature: str, raw_body: bytes) -> bool:
    """Slack v0 request signing. Rejects replays older than 5 minutes."""
    secret = settings.slack_signing_secret
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _safe(text: str, thread_ts: str | None = None, broadcast: bool = False) -> str | None:
    try:
        return post(text, thread_ts=thread_ts, broadcast=broadcast)
    except Exception as exc:                       # a ping must never stop a run
        print(f"[warn] slack ping failed: {exc}", flush=True)
        return None


def run_started(run_id: str, winners: list, verticals: list) -> str | None:
    """Monday's opening line: the seeds and the three verticals being worked."""
    seeds = ", ".join(str((w.get("name") if isinstance(w, dict) else w))[:40]
                      for w in winners[:3]) or "none"
    lines = [f"*Weekly run started* — `{run_id}`", f"• seeds: {seeds}"]
    for v in verticals:
        lines.append(f"• working *{v['name']}* — projected {int(v.get('projected') or 0):,} contacts")
    return _safe("\n".join(lines))


def vertical_started(vertical: str, kept: int, target: int) -> str | None:
    """The top of a vertical's thread. Everything else about it replies here."""
    return _safe(f"▶ *{vertical}* started — {kept:,} companies kept, target {target:,} contacts")


def paused(vertical: str, reason: str, thread_ts: str | None = None) -> None:
    """Once per outage, when a human is needed. Broadcast: this one must be seen."""
    _safe(f"⏸ *{vertical}* paused — {reason}\nBanked progress is kept; it resumes on its "
          f"own within 2 hours of a top-up.", thread_ts, broadcast=True)


def pushed(vertical: str, batch_id: str, leads: int, campaign_name: str,
           thread_ts: str | None = None, started: bool | None = None,
           topup: bool = False) -> None:
    tail = ""
    if started is True:
        tail = (f" — campaign STARTED, sends begin in the next window "
                f"(Mon-Fri {settings.send_start_time[:5]}-{settings.send_end_time[:5]} ET)")
    elif started is False:
        tail = " — campaign is still in DRAFT and needs starting by hand"
    # Troy, 2026-09-30: "at least notify me for top-ups." A system-approved top-up
    # is broadcast to the channel, not buried in the vertical's thread.
    label = "top-up loaded" if topup else "leads loaded"
    _safe(f"✅ *{vertical}* — {leads:,} {label} into `{campaign_name}` "
          f"(batch `{batch_id}`){tail}", thread_ts,
          broadcast=(started is False) or topup)


def cadence_wrong(vertical: str, campaign_id, waits, want: int, template,
                  thread_ts: str | None = None) -> None:
    _safe(f"⚠️ *{vertical}* — campaign `{campaign_id}` was created with a "
          f"{waits[:2]} day gap between emails, not {want}. EmailBison has no API to "
          f"change it, so fix template `{template}` in the UI and the next push inherits "
          f"the right cadence. The leads are loaded either way.",
          thread_ts, broadcast=True)


def sample_ready(client_name: str, arms: list[dict], audiences: list[str],
                 target: int) -> str | None:
    """Posted when a copy-test SAMPLE finishes: what was written, and the two ways on.

    One block per arm, because an arm is what becomes a campaign. The audiences are
    listed inside it rather than split across messages: both arms carry all three, which
    is what keeps the copy comparison honest.
    """
    lines = [f"*{client_name}* — sample copy written. Read it before the real run.",
             f"_Audiences: {', '.join(audiences)}_"]
    blocks: list[dict] = [{"type": "section",
                           "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    for arm in arms:
        spread = arm.get("by_audience") or {}
        detail = " · ".join(f"{k.split('&')[0].strip()[:18]} {v}" for k, v in spread.items())
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"*{arm['name']}* — {arm.get('sendable', 0)} written of "
                       f"{arm.get('contacts', 0)}\n`{arm.get('batch_id')}`"
                       + (f"\n{detail}" if detail else "")}})
    blocks.append({
        "type": "actions", "block_id": f"scale:{client_name}",
        "elements": [{
            "type": "button", "action_id": "scale_run", "style": "primary",
            "value": client_name,
            "text": {"type": "plain_text", "text": f"Scale to {target:,} contacts"},
            "confirm": {
                "title": {"type": "plain_text", "text": "Write the full run?"},
                "text": {"type": "mrkdwn", "text":
                         f"Writes the full copy test for {client_name}: about "
                         f"{target:,} contacts across both arms. The sample batches "
                         f"above stay as they are."},
                "confirm": {"type": "plain_text", "text": "Scale"},
                "deny": {"type": "plain_text", "text": "Not yet"}}}],
    })
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                   "Push a sample batch to EmailBison with `/ve status` then "
                   "`/ve confirm <n>` and `/ve push <n> live`."}]})
    return post(f"{client_name}: sample copy ready for review", blocks=blocks,
                broadcast=False)


def copy_test_result(display_name: str, client_name: str, res) -> str | None:
    """The arms side by side, and a button to make the winner the client's copy."""
    rows = ["*{}* — copy test `{}`".format(display_name, res.experiment_id or "?")]
    for a in sorted(res.arms, key=lambda x: x.reply_rate, reverse=True):
        rows.append(f"*{a.name}* · {a.delivered:,} delivered · {a.replied} replied "
                    f"({a.reply_rate:.2%}) · {a.interested} interested "
                    f"({a.interested_of_replies:.1%} of replies)")
    if res.decided:
        rows.append(f"\n:trophy: *{res.winner}* wins. {res.reason}")
    else:
        rows.append(f"\n_Not decided._ {res.reason}")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}}]
    if res.decided:
        blocks.append({
            "type": "actions", "block_id": f"promote:{client_name}:{res.winner}",
            "elements": [{
                "type": "button", "action_id": "promote_variant", "style": "primary",
                "value": f"{client_name}|{res.winner}",
                "text": {"type": "plain_text", "text": f"Use {res.winner[:24]} from now on"},
                "confirm": {
                    "title": {"type": "plain_text", "text": "Promote this copy?"},
                    "text": {"type": "mrkdwn", "text":
                             f"Every ordinary run for {display_name} will use "
                             f"`{res.winner}` from here. The other arm's prompts stay "
                             f"in the repo and nothing already sent changes."},
                    "confirm": {"type": "plain_text", "text": "Promote"},
                    "deny": {"type": "plain_text", "text": "Not yet"}}}],
        })
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": "Read fifty replies per arm before trusting a reply-rate win: "
                           "a fear-led angle earns reactions, not always interest."}]})
    return post(f"{display_name}: copy test result", blocks=blocks, broadcast=res.decided)


def push_held(vertical: str, batch_id: str, free_daily: int, free_accounts: int,
              want: int, thread_ts: str | None = None) -> None:
    _safe(f"⏸ *{vertical}* — `{batch_id}` is approved but HELD: only {free_daily:,} sends/day "
          f"are free on {free_accounts} unused accounts, and it needs {want:,}/day. It launches "
          f"on its own when running campaigns finish and free their accounts.",
          thread_ts, broadcast=True)


def push_failed(vertical: str, batch_id: str, campaign_id, reason: str,
                thread_ts: str | None = None) -> None:
    _safe(f"⚠️ *{vertical}* — EmailBison refused the lead load for `{batch_id}` "
          f"(campaign {campaign_id}). Batch stays approved; push again once fixed.\n"
          f"```{reason}```", thread_ts, broadcast=True)


def senders_aligned(vertical: str, campaign_name: str, accounts: int, daily: int,
                    target: int, thread_ts: str | None = None) -> None:
    mark = "✅" if daily >= target else "⚠️"
    tail = "" if daily >= target else f" — short of the {target:,}/day target, needs a human look"
    _safe(f"{mark} *{vertical}* — {accounts} sender accounts on `{campaign_name}`, "
          f"{daily:,} sends/day{tail}", thread_ts, broadcast=daily < target)


def verdict(vertical: str, verdict_: str, reason: str, thread_ts: str | None = None) -> None:
    """Daily review, only when it decided something -- silent while pending."""
    icon = "📈" if verdict_ == "scale" else "🛑"
    _safe(f"{icon} *{vertical}* — {verdict_.upper()}: {reason}", thread_ts, broadcast=True)


def expiring(vertical: str, batch_id: str, expires_at, contacts: int,
             thread_ts: str | None = None) -> None:
    _safe(f"⏰ *{vertical}* — batch `{batch_id}` ({contacts:,} contacts) expires "
          f"{expires_at:%a %H:%M} UTC. `/ve status` then `/ve confirm <number>` or `/ve kill <number> <reason>`, "
          f"or it releases back to the pool.", thread_ts, broadcast=True)


def nothing_to_do(run_id: str, why: str) -> None:
    _safe(f"💤 *Weekly run* `{run_id}` found nothing to work on — {why}")


def contacts_found(vertical: str, companies: int, stats: dict, verified: int,
                   surplus: int = 0, thread_ts: str | None = None) -> None:
    """Tell the channel a vertical has its contacts, before copy starts.

    Troy, 2026-09-13: "I would like a slack ping after contacts are found per vertical
    also." Copy is hours of work after this point and the only other message is the
    approval request, so without this a vertical is silent from the moment it starts
    until its batch lands -- or, when a gateway is out of credit, indefinitely.
    """
    found = stats.get("contacts_found") or 0
    lines = [
        f"*{vertical}* — contacts found",
        f"• {companies:,} companies → {found:,} candidates → *{verified:,} verified contacts*",
    ]
    bits = []
    for label, key in (("already emailed", "contacts_dropped_dedup"),
                       ("no address found", "contacts_no_email"),
                       ("catch-all", "contacts_catch_all"),
                       ("from EmailBison", "found_reused")):
        if stats.get(key):
            bits.append(f"{stats[key]:,} {label}")
    if bits:
        lines.append("• " + " · ".join(bits))
    if stats.get("companies_no_contact"):
        lines.append(f"• {stats['companies_no_contact']:,} companies yielded nobody")
    if surplus:
        lines.append(f"• {surplus:,} companies banked as surplus for a top-up")
    lines.append("• next: copy, then an approval request here")
    _safe("\n".join(lines), thread_ts)


def batch_summary_blocks(batch_id: str, vertical: str, rationale: str, stats: dict,
                         samples: list[dict]) -> list[dict]:
    """Per-batch summary required by spec Stage 6.

    Troy, 2026-09-14: the sample emails inline "look clunky" -- they are now behind a
    dropdown; picking one opens it in a pop-up. And the counts come from the batch's
    LIVE contact_count where the caller supplies it (stats["contacts_sendable"] is
    overridden by _show), because the summary stats were frozen when the batch was
    first opened and a topped-up batch read 1,849 while it held 3,000.
    """
    lines = [
        f"*Vertical:* {vertical}",
        f"_{rationale}_",
        "",
        f"*Contacts in this batch:* {stats.get('contacts_sendable', 0):,} at "
        f"{stats.get('companies_kept', 0):,} companies",
        f"*Sourcing:* {stats.get('companies_found', 0):,} companies found, "
        f"{stats.get('companies_dropped_enforcement', 0):,} dropped by the fit gate, "
        f"{stats.get('companies_dropped_dedup', 0):,} already contacted",
        f"*Contacts:* {stats.get('contacts_found', 0):,} found, "
        f"{stats.get('contacts_needs_review', 0):,} failed copy QA",
    ]
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Batch {batch_id}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    if samples:
        options = []
        for i, sample in enumerate(samples[:3], 1):
            who = f"{sample.get('first_name', '')} {sample.get('last_name', '')}".strip()
            label = f"Sample {i}: {who}, {sample.get('title') or '?'} @ " \
                    f"{sample.get('company_name') or sample.get('company_domain') or '?'}"
            options.append({"text": {"type": "plain_text", "text": label[:75]},
                            "value": f"{batch_id}|{i - 1}"})
        blocks.append({
            "type": "section", "block_id": f"samples:{batch_id}",
            "text": {"type": "mrkdwn", "text": "*Sample emails*"},
            "accessory": {"type": "static_select", "action_id": "view_sample",
                          "placeholder": {"type": "plain_text", "text": "Pick one to read"},
                          "options": options},
        })
    blocks.append(gate_buttons(batch_id))
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (f"Or `/ve confirm {batch_id}` · `/ve kill {batch_id} <reason>` · "
                     f"all copy at {settings.public_url}/review/{batch_id} · "
                     f"auto-expires in {settings.approval_timeout_hours}h with no response"),
        }],
    })
    return blocks


def open_sample_modal(trigger_id: str, batch_id: str, index: int) -> bool:
    """The dropdown's pop-up: one contact's two emails, readable."""
    from .. import db
    rows = db.batch_contacts(batch_id)
    if not rows or not 0 <= index < len(rows):
        return False
    c = rows[index]
    who = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or c.get("email")
    head = (f"*{who}* — {c.get('title') or '?'} @ "
            f"{c.get('company_name') or c.get('company_domain')}\n_{c.get('email')}_")
    view = {
        "type": "modal", "callback_id": "sample_view",
        "title": {"type": "plain_text", "text": f"Sample {index + 1}"[:24]},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": head}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Email 1*"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": (c.get("personalization_1") or "")[:2900] or "_(empty)_"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Email 2*"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": (c.get("personalization_2") or "")[:2900] or "_(empty)_"}},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": f"Every contact's copy: {settings.public_url}/review/{batch_id}"}]},
        ],
    }
    try:
        payload = request("slack", "POST", VIEWS_OPEN, headers=_headers(),
                          json_body={"trigger_id": trigger_id, "view": view})
    except VendorError as exc:
        print(f"[warn] slack views.open failed: {exc}", flush=True)
        return False
    if not (payload or {}).get("ok"):
        print(f"[warn] slack rejected sample modal: {(payload or {}).get('error')}", flush=True)
        return False
    return True


def gate_buttons(batch_id: str) -> dict:
    """Approve (with a confirm step) and Kill (opens the reason modal)."""
    return {
        "type": "actions", "block_id": f"gate:{batch_id}",
        "elements": [
            # ONE STEP. Troy, 2026-09-30: "right now i have to approve and then push."
            # Slack's own confirm dialog is the safety step; confirming approves the
            # batch AND starts the push at once. `/ve approve <id> hold` keeps the
            # old two-step for staging a batch without sending it.
            {"type": "button", "action_id": "approve_batch", "style": "primary",
             "value": batch_id,
             "text": {"type": "plain_text", "text": "Approve & launch"},
             "confirm": {
                 "title": {"type": "plain_text", "text": "Approve and launch?"},
                 "text": {"type": "mrkdwn",
                          "text": (f"`{batch_id}`: approved, then pushed to EmailBison "
                                   f"now -- a campaign is created or topped up, senders "
                                   f"attached, leads loaded. This is the step that leads "
                                   f"to email going out.")},
                 "confirm": {"type": "plain_text", "text": "Approve & launch"},
                 "deny": {"type": "plain_text", "text": "Cancel"}}},
            {"type": "button", "action_id": "kill_batch", "style": "danger",
             "value": batch_id, "text": {"type": "plain_text", "text": "Kill"}},
        ],
    }


def open_kill_modal(trigger_id: str, batch_id: str, response_url: str | None) -> bool:
    """The Kill button's modal: one required field, the reason."""
    import json as _json
    view = {
        "type": "modal", "callback_id": "kill_batch_modal",
        "private_metadata": _json.dumps({"batch_id": batch_id, "response_url": response_url}),
        "title": {"type": "plain_text", "text": "Kill batch"},
        "submit": {"type": "plain_text", "text": "Kill"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": (f"Killing `{batch_id}` suppresses its companies and contacts for "
                      f"{settings.killed_dedup_days} days. Say why, so the pipeline "
                      f"learns from it.")}},
            {"type": "input", "block_id": "reason",
             "label": {"type": "plain_text", "text": "Reason"},
             "element": {"type": "plain_text_input", "action_id": "reason_input",
                         "multiline": True,
                         "placeholder": {"type": "plain_text",
                                         "text": "e.g. companies are off-vertical, copy too generic"}}},
        ],
    }
    try:
        payload = request("slack", "POST", VIEWS_OPEN, headers=_headers(),
                          json_body={"trigger_id": trigger_id, "view": view})
    except VendorError as exc:
        print(f"[warn] slack views.open failed: {exc}", flush=True)
        return False
    if not (payload or {}).get("ok"):
        print(f"[warn] slack rejected modal: {(payload or {}).get('error')}", flush=True)
        return False
    return True


def respond(response_url: str | None, text: str, blocks: list | None = None) -> None:
    """Answer an interaction in the channel, under the message that was clicked."""
    if not response_url:
        return
    body = {"response_type": "in_channel", "replace_original": False, "text": text}
    if blocks:
        body["blocks"] = blocks
    try:
        import httpx
        httpx.post(response_url, json=body, timeout=15)
    except Exception as exc:
        print(f"[warn] slack interaction reply failed: {exc}", flush=True)
