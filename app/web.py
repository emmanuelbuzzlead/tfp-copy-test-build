"""Railway web service: the CSV upload door and the Slack slash-command endpoint.

  POST /upload/performance   weekly EmailBison dashboard export -> campaign_stats
  POST /upload/history       one-time backfill of prior sends -> dedup history
  POST /slack/command        /ve confirm|approve|kill|status
  POST /slack/interact       Approve / Kill buttons and the kill-reason modal
  GET  /health               liveness + config presence (no secrets)

Both upload routes require the UPLOAD_TOKEN bearer; the Slack route requires a valid
Slack v0 signature. Neither is optional -- these endpoints decide who gets emailed.
"""

from __future__ import annotations

import httpx

import threading

import csv
import importlib
import io
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import db
from .clients import slack
from .config import settings
from .stages import s1_performance, s6_approval

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Ensure the schema exists on boot.

    Railway's Postgres is only reachable on postgres.railway.internal, which resolves
    inside Railway's network and nowhere else -- so `railway run ... initdb` from a
    laptop cannot create the tables. Every statement in SCHEMA is CREATE ... IF NOT
    EXISTS, so running it on each boot is idempotent and makes a deploy self-sufficient.
    A failure here is logged, not raised: /health should come up and report the problem
    rather than crash-looping with no way to see why.
    """
    try:
        db.init_schema()
        print("[startup] schema ok", flush=True)
    except Exception as exc:
        print(f"[startup] SCHEMA INIT FAILED: {exc}", flush=True)
    # A redeploy kills whatever the cron container was doing, and the next tick is up
    # to two hours away. Kick a resume here, in the background; resume() itself refuses
    # to run beside a live run, so this can never double up on the cron.
    def _resume_on_boot() -> None:
        try:
            from . import runner, wave1_fill
            try:
                if wave1_fill.release_dead_lock():
                    print("[startup] released the fill lock left by the previous container", flush=True)
            except Exception as exc:
                print(f"[startup] lock release skipped: {exc}", flush=True)
            out = runner.resume()
            print(f"[startup] resume: {out.get('status')}", flush=True)
        except Exception as exc:
            print(f"[startup] resume skipped: {exc}", flush=True)
    # IDPR gets its two-hourly tick from a separate Railway cron container; this build has
    # none, so the web service ticks itself. Same resume(): it probes the vendors with a
    # real-sized call, finishes paused verticals when they answer, and backs off when a
    # run is live. A vendor outage (Concentrate 402, 2026-10-08) therefore heals unaided.
    def _resume_every_two_hours() -> None:
        import os
        import time
        every = max(int(os.environ.get("RESUME_INTERVAL_MINUTES", "120") or 120), 15) * 60
        while True:
            time.sleep(every)
            try:
                from . import runner
                out = runner.resume()
                print(f"[tick] resume: {out.get('status')}", flush=True)
            except Exception as exc:
                print(f"[tick] resume failed: {exc}", flush=True)
    if settings.pipeline_enabled and settings.database_url:
        threading.Thread(target=_resume_on_boot, daemon=True).start()
        threading.Thread(target=_resume_every_two_hours, daemon=True).start()
        import os as _os
        if _os.environ.get("NIGHTLY_ENABLED", "").strip().lower() in ("1", "true", "yes"):
            from . import nightly
            nightly.start()          # daily copy-test check: scale the leader, decide at day 10 (after 700 sends each), pause losers
        if _os.environ.get("TESTRUN_ENABLED", "").strip().lower() in ("1", "true", "yes"):
            # The first test run (source, find contacts, write copy, load drafts) runs inside this service and resumes after a redeploy.
            from . import testrun
            try:
                with db.cursor() as _cur:      # single instance: a booting process means the previous container's job died with it
                    _cur.execute("DELETE FROM job_locks WHERE name = %s", (testrun.LOCK,))
            except Exception as exc:
                print(f"[startup] testrun lock release skipped: {exc}", flush=True)
            testrun.ensure_running()

            def _watchdog() -> None:
                import time as _t
                while True:
                    _t.sleep(300)
                    try:
                        testrun.ensure_running()
                    except Exception as exc:
                        print(f"[watchdog] scale-up check failed: {exc}", flush=True)
            threading.Thread(target=_watchdog, daemon=True).start()
    yield


app = FastAPI(title="IDPR Vertical Expander", lifespan=lifespan)


def _require_upload_token(authorization: str | None) -> None:
    if not settings.upload_token:
        raise HTTPException(500, "UPLOAD_TOKEN is not configured on the service")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not secrets.compare_digest(supplied, settings.upload_token):
        raise HTTPException(401, "bad or missing upload token")


# The tables /health insists on. It was a hand-kept list with a hardcoded "14", and
# three tables added over the past week -- client_state, copy_tests and client_arms --
# were never added to it, so a database missing all three still reported "14/14
# present". A count derived from the list cannot drift from it that way.
# Modules that tracked code only imports INSIDE functions, so a missing file passes
# every import at boot and fails hours later at the first call. Keep this in step
# with `grep -rn "from \.\.\? import" app/`.
_LAZY_IMPORTS = [
    "app.testgrid", "app.company_fit", "app.cost_tests", "app.scaling",
    "app.wave1_fill", "app.clients.leadocean", "app.modes", "app.report",
    "app.taxonomy", "app.stages.s7_push", "app.stages.s8_review", "app.runner", "app.nightly", "app.angle_derivation", "app.scale_review", "app.materials_fill", "app.sourcing_health",
]

_HEALTH_TABLES = [
    "runs", "companies", "contacts", "batches", "logs",
    "campaign_stats", "uploads", "do_not_contact", "ve_campaigns",
    "replied_companies", "verticals_run", "send_exposures",
    "outcome_events", "decision_snapshots",
    "client_state", "copy_tests", "client_arms",
]


@app.get("/runway", response_class=HTMLResponse)
def runway_page() -> HTMLResponse:
    """The one-screen runway dashboard in plain words (app/runway_page.py). Read-only, refreshes itself."""
    try:
        from . import runway_page as page
        return HTMLResponse(page.page_html())
    except Exception as exc:
        return HTMLResponse(f"<pre>runway unavailable: {exc}</pre>", status_code=500)


@app.get("/scorecard", response_class=PlainTextResponse)
def sourcing_scorecard() -> PlainTextResponse:
    """Is sourcing working well? Yield, fit rate, speed, held reasons, bounce rate and provider health (app/sourcing_health.py). Read-only."""
    try:
        from . import sourcing_health
        return PlainTextResponse(sourcing_health.report())
    except Exception as exc:
        return PlainTextResponse(f"scorecard unavailable: {exc}", status_code=500)


@app.get("/runway.txt", response_class=PlainTextResponse)
def runway_text() -> PlainTextResponse:
    """The same numbers as plain text, for pasting into Slack or a note."""
    from . import runway
    try:
        return PlainTextResponse(runway.report().replace("*", "") + "\n\n" + runway.by_vertical())
    except Exception as exc:
        return PlainTextResponse(f"runway unavailable: {exc}", status_code=500)


@app.get("/health")
def health() -> JSONResponse:
    body = settings.describe()
    # WHICH BUILD IS SERVING THIS. During a rolling deploy the OLD container keeps
    # answering /health perfectly well, so "postgres ok" says nothing about whether the
    # code just shipped is live. On 2026-09-21 that had me restart a ProductEVO run
    # twice against the very build I had just fixed, and read the same traceback twice.
    # The newest source mtime changes with every image, so it identifies the build
    # without needing a version anyone has to remember to bump.
    try:
        import os
        from datetime import datetime, timezone
        from pathlib import Path as _P
        _app_dir = _P(__file__).resolve().parent
        newest = max(os.path.getmtime(f) for f in _app_dir.rglob("*.py"))
        body["build"] = datetime.fromtimestamp(
            newest, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception as exc:
        body["build"] = f"unknown ({str(exc)[:60]})"
    # EVERY MODULE THE CODE LAZILY IMPORTS, tried at health time. 2026-09-29: a deploy
    # built from a clean checkout was missing app/testgrid.py -- an uncommitted file
    # production had been running for days. Nothing noticed until a push needed a new
    # campaign three hours later and died on the import; the daily review died on the
    # same line at 23:02. /health said "ok" the whole time, because the missing module
    # is only imported inside functions. So it is imported here, on purpose, and the
    # deploy step is meant to read this field and refuse to go live on anything but ok.
    missing = []
    for mod in _LAZY_IMPORTS:
        try:
            importlib.import_module(mod)
        except Exception as exc:
            missing.append(f"{mod}: {str(exc)[:80]}")
    body["imports"] = "ok" if not missing else "MISSING: " + "; ".join(missing)
    try:
        db.ping()
        with db.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM information_schema.tables "
                " WHERE table_schema='public' AND table_name = ANY(%s)",
                (_HEALTH_TABLES,),
            )
            found = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM contacts WHERE status='pushed'")
            history = cur.fetchone()["n"]
        body["postgres"] = "ok"
        body["tables"] = f"{found}/{len(_HEALTH_TABLES)} present"
        body["dedup_history_rows"] = history
    except Exception as exc:
        body["postgres"] = f"FAILED: {exc}"
    return JSONResponse(body)


@app.post("/upload/performance")
async def upload_performance(
    file: UploadFile = File(...),
    as_of: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """EmailBison dashboard export -- an OVERRIDE on the API figures, not the only source.

    Stage 1 reads campaign aggregates from the API every run, so this upload is optional:
    use it to correct a number by hand or to confirm figures that look like they lag.
    Uploaded values win for the campaigns they mention and mark them stats_verified.

    The response echoes the header mapping it inferred, so a schema change in the export
    shows up as a visibly wrong mapping instead of quietly wrong winners.
    """
    _require_upload_token(authorization)
    raw = await file.read()
    try:
        rows, report = s1_performance.parse_performance_csv(raw, file.filename or "")
    except s1_performance.IngestError as exc:
        raise HTTPException(422, str(exc)) from exc

    upload_id = f"perf-{uuid.uuid4().hex[:8]}"
    stamp = date.fromisoformat(as_of) if as_of else date.today()
    stored = s1_performance.persist(rows, upload_id, stamp)

    themes, stats = s1_performance.select_seed_themes(rows)
    winners = themes  # stats already carries as_seed() winners
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO uploads (upload_id, kind, filename, row_count, detail) "
            "VALUES (%s,'performance',%s,%s,%s)",
            (upload_id, file.filename, stored, json.dumps({**report, **stats}, default=str)),
        )
    db.log(None, "upload", f"ingested {stored} campaign rows from {file.filename}",
           upload_id=upload_id, mapping=report["mapping"])

    return JSONResponse({
        "upload_id": upload_id,
        "as_of": stamp.isoformat(),
        "rows_stored": stored,
        "header_mapping": report["mapping"],
        "unmapped_fields": report["unmapped_fields"],
        "would_win_now": stats["winners"],
        "note": "these figures now override the API for these campaigns",
        "rejected_sample": stats["rejected_sample"],
    })


@app.post("/upload/history")
async def upload_history(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """One-time (or repeatable) backfill so dedup covers pre-pipeline sends.

    Accepts any CSV carrying an email and/or a domain column. Rows land as
    status='pushed', source='backfill'. That is HISTORY, not a permanent block:
    blocked_contacts ignores backfill timestamps and trusts recent_contacts, so a
    person last emailed more than recontact_days ago is eligible again.
    """
    _require_upload_token(authorization)
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    headers = reader.fieldnames or []

    def find(*names: str) -> str | None:
        lowered = {(h or "").strip().lower(): h for h in headers}
        for want in names:
            for key, original in lowered.items():
                if want in key:
                    return original
        return None

    email_col = find("email")
    domain_col = find("domain", "website", "company_url")
    company_col = find("company_name", "company", "organization")
    if not email_col and not domain_col:
        raise HTTPException(422, f"need an email or domain column; saw {headers}")

    emails: list[tuple] = []
    domains: list[tuple] = []
    for row in reader:
        email = (row.get(email_col) or "").strip().lower() if email_col else ""
        domain = db.normalize_domain(row.get(domain_col) or "") if domain_col else ""
        if not domain and "@" in email:
            domain = email.split("@", 1)[1]
        if email and "@" in email:
            emails.append((email, domain or None))
        if domain:
            domains.append((domain, (row.get(company_col) or "").strip() or None))

    upload_id = f"hist-{uuid.uuid4().hex[:8]}"
    with db.cursor() as cur:
        if domains:
            cur.executemany(
                """INSERT INTO companies (domain, name, status, source)
                   VALUES (%s,%s,'pushed','backfill')
                   ON CONFLICT (domain) DO NOTHING""",
                domains,
            )
        if emails:
            cur.executemany(
                """INSERT INTO contacts (email, company_domain, status, source)
                   VALUES (%s,%s,'pushed','backfill')
                   ON CONFLICT (email) DO NOTHING""",
                emails,
            )
        cur.execute(
            "INSERT INTO uploads (upload_id, kind, filename, row_count, detail) "
            "VALUES (%s,'history',%s,%s,%s)",
            (upload_id, file.filename, len(emails),
             json.dumps({"emails": len(emails), "domains": len(domains),
                         "headers": headers}, default=str)),
        )

    db.log(None, "backfill", f"history backfill from {file.filename}",
           emails=len(emails), domains=len(domains))
    return JSONResponse({
        "upload_id": upload_id,
        "emails_ingested": len(emails),
        "domains_ingested": len(domains),
        "columns_used": {"email": email_col, "domain": domain_col, "company": company_col},
    })


@app.post("/slack/command")
async def slack_command(request: Request) -> PlainTextResponse:
    raw = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not slack.verify_signature(timestamp, signature, raw):
        # This endpoint can approve outbound mail, so an unverified call is refused.
        raise HTTPException(401, "bad slack signature")

    form = dict(pair.split("=", 1) for pair in raw.decode().split("&") if "=" in pair)
    from urllib.parse import unquote_plus

    text = unquote_plus(form.get("text", ""))
    user = unquote_plus(form.get("user_name", "") or form.get("user_id", "unknown"))
    response_url = unquote_plus(form.get("response_url", ""))

    # Slack discards any reply that takes longer than 3 seconds, and a status that
    # walks Postgres from Railway measured 1.7s on a quiet afternoon (2026-09-13). So:
    # acknowledge at once, do the work in the background, and deliver the answer to
    # the response_url Slack provides -- which also lets the answer be longer than an
    # inline reply and, for status, visible to the channel rather than only the caller.
    def _deliver() -> None:
        try:
            reply = s6_approval.handle_command(text, user)
        except Exception as exc:
            reply = f"Command failed: {exc}"
        if not response_url:
            return
        msg = _as_message(reply)
        try:
            httpx.post(response_url, json={"response_type": "in_channel", **msg}, timeout=15)
        except Exception as exc:
            print(f"[warn] slash reply delivery failed: {exc}", flush=True)

    if response_url:
        threading.Thread(target=_deliver, daemon=True).start()
        return PlainTextResponse("Working on it…")
    # No response_url (e.g. a direct test call): answer inline as before.
    try:
        reply = s6_approval.handle_command(text, user)
    except Exception as exc:
        reply = f"Command failed: {exc}"
    return PlainTextResponse(_as_message(reply)["text"])


def _as_message(reply) -> dict:
    """handle_command returns plain text or {"text", "blocks"}; Slack wants a dict."""
    if isinstance(reply, dict):
        return {"text": reply.get("text", ""), **({"blocks": reply["blocks"]} if reply.get("blocks") else {})}
    return {"text": str(reply)}


def interaction_to_command(payload: dict) -> tuple[str, str | None, str | None]:
    """Translate a Slack interaction payload into (kind, command, user).

    kind is "command" (run it through the slash handler), "kill_modal" (open the
    reason modal; command carries the batch id) or "ignore". Pure, so it is testable
    without Slack: the button and the slash command must mean exactly the same thing.
    """
    who = payload.get("user") or {}
    user = who.get("username") or who.get("name") or who.get("id") or "unknown"
    kind = payload.get("type")
    if kind == "block_actions":
        action = (payload.get("actions") or [{}])[0]
        if action.get("action_id") == "view_sample":
            value = ((action.get("selected_option") or {}).get("value") or "")
            return ("sample_modal", value, user) if "|" in value else ("ignore", None, user)
        batch_id = (action.get("value") or "").strip()
        if not batch_id:
            return "ignore", None, user
        if action.get("action_id") == "approve_batch":
            return "command", f"approve {batch_id}", user
        if action.get("action_id") == "push_batch":
            return "command", f"push {batch_id} live", user
        if action.get("action_id") == "promote_variant":
            return "command", f"promote {batch_id.replace('|', ' ')}", user
        if action.get("action_id") == "scale_run":
            # `full` is what turns the sample into the real run.
            return "command", f"run {batch_id} full", user
        if action.get("action_id") == "kill_batch":
            return "kill_modal", batch_id, user
        if action.get("action_id") == "scale_lookalike_10k":
            return "scale_lookalike", batch_id, user
        if (action.get("action_id") or "").startswith("scale_review_"):
            return "scale_review", batch_id, user
        if (action.get("action_id") or "").startswith("add_leads_"):
            return "add_leads", batch_id, user
        if (action.get("action_id") or "").startswith("launch_lookalike"):
            return "launch_lookalike", batch_id, user
        return "ignore", None, user
    if kind == "view_submission":
        view = payload.get("view") or {}
        if view.get("callback_id") != "kill_batch_modal":
            return "ignore", None, user
        try:
            meta = json.loads(view.get("private_metadata") or "{}")
        except ValueError:
            meta = {}
        reason = (((view.get("state") or {}).get("values") or {}).get("reason") or {}) \
            .get("reason_input", {}).get("value") or ""
        batch_id = meta.get("batch_id") or ""
        if not batch_id or not reason.strip():
            return "ignore", None, user
        return "command", f"kill {batch_id} {reason.strip()}", user
    return "ignore", None, user


@app.post("/slack/interact")
async def slack_interact(request: Request) -> JSONResponse:
    """Buttons and the kill modal. Same gate as the slash command, same verification."""
    raw = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not slack.verify_signature(timestamp, signature, raw):
        raise HTTPException(401, "bad slack signature")
    from urllib.parse import parse_qs
    form = parse_qs(raw.decode())
    try:
        payload = json.loads((form.get("payload") or ["{}"])[0])
    except ValueError:
        return JSONResponse({})
    kind, command, user = interaction_to_command(payload)

    if kind == "sample_modal":
        batch_id, _, idx = command.partition("|")
        threading.Thread(target=slack.open_sample_modal,
                         args=(payload.get("trigger_id"), batch_id, int(idx or 0)),
                         daemon=True).start()
        return JSONResponse({})

    if kind == "kill_modal":
        # trigger_id is valid for three seconds; open the modal at once.
        response_url = payload.get("response_url")
        threading.Thread(target=slack.open_kill_modal,
                         args=(payload.get("trigger_id"), command, response_url),
                         daemon=True).start()
        return JSONResponse({})

    if kind == "add_leads":
        response_url = payload.get("response_url")
        cid, _, n = command.partition("|")

        def _add() -> None:
            from . import materials_fill
            try:
                r = materials_fill.add_leads(int(cid), int(n), user)
                text = (f":heavy_plus_sign: Adding {r['added']:,} leads to campaign {r['campaign']}: it holds {r['was']:,}, the goal is now {r['target']:,}. {r['note']} _(by {user})_"
                        if r.get("ok") else f"Campaign {cid}: {r['reason']}  _(by {user})_")
            except Exception as exc:
                text = f"Could not add leads to campaign {cid}: {exc}  _(by {user})_"
            if response_url:
                slack.respond(response_url, text)
            else:
                slack.post(text)

        threading.Thread(target=_add, daemon=True).start()
        return JSONResponse({})

    if kind == "scale_review":
        response_url = payload.get("response_url")
        key, _, new_target = command.rpartition("|")

        def _scale() -> None:
            from . import scale_review
            try:
                r = scale_review.approve(key, int(new_target), user)
                text = (f":rocket: Approved: *{r['label']}* now scaling to {r['target']:,} leads. _(by {user})_" if r.get("ok")
                        else f"{key}: {r.get('reason', 'nothing to do')}  _(by {user})_")
            except Exception as exc:
                text = f"Could not scale {key}: {exc}  _(by {user})_"
            if response_url:
                slack.respond(response_url, text)
            else:
                slack.post(text)

        threading.Thread(target=_scale, daemon=True).start()
        return JSONResponse({})

    if kind == "scale_lookalike":
        response_url = payload.get("response_url")
        angle = command

        def _approve() -> None:
            from . import testgrid
            try:
                result = testgrid.approve_lookalike_scale_to_10k(angle, user)
                if result.get("target_set"):
                    text = f":rocket: Approved -- *{angle}* now scaling to 10,000 leads. _(by {user})_"
                else:
                    text = f"{angle}: {result.get('reason', 'nothing to do')}  _(by {user})_"
            except Exception as exc:
                text = f"Could not approve {angle}: {exc}  _(by {user})_"
            if response_url:
                slack.respond(response_url, text)
            else:
                slack.post(text)

        threading.Thread(target=_approve, daemon=True).start()
        return JSONResponse({})

    if kind == "launch_lookalike":
        response_url = payload.get("response_url")
        angle_id, _, parent_angle = command.partition("|")

        def _launch() -> None:
            from . import angle_derivation
            try:
                result = angle_derivation.launch_new_test_angle(angle_id, parent_angle)
                if result.get("status") == "launched":
                    text = (f":rocket: Launched *{result['angle']}* (campaign {result['campaign_id']}) "
                           f"off {parent_angle}. _(by {user})_")
                else:
                    text = f"Could not launch {angle_id}: {result.get('reason', result)}  _(by {user})_"
            except Exception as exc:
                text = f"Could not launch {angle_id}: {exc}  _(by {user})_"
            if response_url:
                slack.respond(response_url, text)
            else:
                slack.post(text)

        threading.Thread(target=_launch, daemon=True).start()
        return JSONResponse({})

    if kind == "command":
        response_url = payload.get("response_url")
        if payload.get("type") == "view_submission":
            try:
                response_url = json.loads((payload.get("view") or {})
                                          .get("private_metadata") or "{}").get("response_url")
            except ValueError:
                response_url = None

        def _act() -> None:
            try:
                reply = s6_approval.handle_command(command, user)
            except Exception as exc:
                reply = f"Command failed: {exc}"
            msg = _as_message(reply)
            text = f"{msg['text']}  _(by {user})_"
            if response_url:
                slack.respond(response_url, text, msg.get("blocks"))
            else:
                slack.post(text, blocks=msg.get("blocks"))

        if command.startswith("push ") and command.endswith(" live") and response_url:
            slack.respond(response_url, f"Pushing `{command.split()[1]}` live, started by "
                                        f"{user}. Progress lands in the vertical's thread.")
        threading.Thread(target=_act, daemon=True).start()
        if payload.get("type") == "view_submission":
            return JSONResponse({"response_action": "clear"})
        return JSONResponse({})
    return JSONResponse({})


@app.get("/report/friday.pdf")
def friday_report_pdf():
    """The Friday report, rendered now from live data. Linked from the Slack post."""
    from fastapi.responses import Response
    from . import report
    data = report.gather()
    return Response(report.render_pdf(data), media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="idpr-friday-{data["as_of"]:%Y-%m-%d}.pdf"'})


# ---------------------------------------------------------------------------
# batch review pages
# ---------------------------------------------------------------------------
# The Slack channel clears its history on a retention policy, so a batch posted there
# can vanish before anyone reads it -- and the 72h approval timer keeps running. The
# batch itself lives in Postgres, so these pages serve it directly and do not expire.

def _esc(v) -> str:
    return (str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_EXTRA_CSS = ("<style>.pill.live{background:#27ae60;color:#fff}"
              ".pill.warn{background:#c0392b;color:#fff}"
              ".pill.muted{background:#ddd;color:#444}"
              ".card.warn{border-left:4px solid #c0392b}"
              ".card.live{border-left:4px solid #27ae60}"
              ".card.muted{border-left:4px solid #bbb}"
              ".stat.warn b{color:#c0392b}</style>")
_PAGE_CSS = """
<style>
 body{font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1d2129}
 .wrap{max-width:920px;margin:0 auto;padding:28px 20px 60px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 10px}
 .muted{color:#65676b;font-size:13px}
 .card{background:#fff;border:1px solid #dfe2e6;border-radius:10px;padding:16px 18px;margin:12px 0}
 .stat{display:inline-block;min-width:150px;margin:3px 18px 3px 0;font-size:13px}
 .stat b{display:block;font-size:19px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #eceef0;vertical-align:top}
 th{background:#f0f2f5;font-weight:600}
 pre{white-space:pre-wrap;word-wrap:break-word;background:#f6f7f9;border:1px solid #e4e6eb;
     border-radius:7px;padding:10px 12px;margin:6px 0;font:13px/1.5 ui-monospace,Menlo,monospace}
 a{color:#1b74e4} .pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;
   background:#e7f3ff;color:#1b74e4} .warn{background:#fff3cd;color:#8a6d3b}
</style>"""


@app.get("/review", response_class=HTMLResponse)
def review_index() -> HTMLResponse:
    """Every batch awaiting a decision, newest first."""
    try:
        rows = db.pending_batches()
    except Exception as exc:
        return HTMLResponse(f"<pre>database unavailable: {_esc(exc)}</pre>", status_code=503)
    items = "".join(
        f'<div class="card"><a href="/review/{_esc(b["batch_id"])}"><b>{_esc(b["vertical"])}</b></a>'
        f'<div class="muted">{_esc(b["batch_id"])} &middot; {b["contact_count"]} contacts '
        f'&middot; {b["company_count"]} companies &middot; expires '
        f'{b["expires_at"]:%Y-%m-%d %H:%M} UTC</div></div>'
        for b in rows
    ) or '<div class="card muted">No batches awaiting approval.</div>'
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Batches awaiting approval</title>"
                        f"{_PAGE_CSS}<div class=wrap><h1>Batches awaiting approval</h1>"
                        f"<p class=muted>Approve in Slack with <code>/ve approve &lt;batch_id&gt;</code>. "
                        f"<a href='/history'>Past approvals &rarr;</a>"
                        f"</p>{items}</div>")


@app.get("/history", response_class=HTMLResponse)
def review_history() -> HTMLResponse:
    """Every batch ever, decided or not, with what happened to it.

    Slack clears the channel, so "what did I approve last month" is unanswerable there.
    These rows are the durable record: batch, vertical, size, decision, who decided and
    when, alongside the vertical's eventual scale/drop verdict where one exists.
    """
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT b.batch_id, b.vertical, b.contact_count, b.company_count, "
                "       b.approval_status, b.approved_by, b.created_at, b.decided_at, "
                "       o.verdict, o.reason "
                "  FROM batches b "
                "  LEFT JOIN vertical_outcomes o ON o.vertical = b.vertical "
                " ORDER BY b.created_at DESC LIMIT 300")
            rows = cur.fetchall()
    except Exception as exc:
        return HTMLResponse(f"<pre>database unavailable: {_esc(exc)}</pre>", status_code=503)

    def pill(status):
        colour = {"approved": "#e3f6e8;color:#1a7f37", "killed": "#ffe9e9;color:#b42318",
                  "auto_killed": "#ffe9e9;color:#b42318",
                  "pending": "#e7f3ff;color:#1b74e4"}.get(status, "#eceef0;color:#65676b")
        return f'<span class=pill style="background:{colour}">{_esc(status)}</span>'

    body = "".join(
        f"<tr><td><a href='/review/{_esc(r['batch_id'])}'>{_esc(r['vertical'])}</a>"
        f"<div class=muted>{_esc(r['batch_id'])}</div></td>"
        f"<td>{r['contact_count']}<div class=muted>{r['company_count']} cos</div></td>"
        f"<td>{pill(r['approval_status'])}"
        f"<div class=muted>{_esc(r['approved_by'] or '')}</div></td>"
        f"<td class=muted>{r['created_at']:%Y-%m-%d %H:%M}"
        f"{'<br>decided ' + format(r['decided_at'], '%Y-%m-%d %H:%M') if r['decided_at'] else ''}</td>"
        f"<td class=muted>{_esc(r['verdict'] or '')}"
        f"{'<br>' + _esc(str(r['reason'])[:60]) if r['reason'] else ''}</td></tr>"
        for r in rows
    ) or "<tr><td colspan=5 class=muted>No batches yet.</td></tr>"

    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>Approval history</title>{_PAGE_CSS}"
        f"<div class=wrap><h1>Approval history</h1>"
        f"<p class=muted>Every batch and what happened to it. "
        f"<a href='/review'>Awaiting approval &rarr;</a></p>"
        f"<div class=card><table>"
        f"<tr><th>Vertical</th><th>Size</th><th>Decision</th><th>When</th>"
        f"<th>Scale/drop verdict</th></tr>{body}</table></div></div>")


_LIVE_STATUSES = ("verified", "draft", "reserved", "approved", "pushed")


def _client_bar(active: str | None, suffix: str = "") -> str:
    """Whose pipeline you are looking at, and a link to each of the others.

    Troy, 2026-09-20: "I would like it to have a separate live webpage, not using the
    same one for IDPR." Every page used to read whichever database the web process was
    started against, so two clients shared one page and it silently showed IDPR's
    numbers under ProductEVO's run.
    """
    from . import client as _client
    try:
        names = _client.available()
    except Exception:
        return ""
    if len(names) < 2:
        return ""
    bits = []
    for n in names:
        try:
            label = _client.load(n).display_name
        except Exception:
            label = n
        if n == active:
            bits.append(f"<b>{_esc(label)}</b>")
        else:
            bits.append(f"<a href='/c/{_esc(n)}/live{suffix}'>{_esc(label)}</a>")
    return f"<p class=muted>Showing: {' &middot; '.join(bits)}</p>"


def _resolve_client(name: str | None):
    """The profile to read, and the database context to read it in."""
    import contextlib as _ctx

    from . import client as _client
    if not name:
        try:
            return _client.profile(), _ctx.nullcontext()
        except Exception:
            return None, _ctx.nullcontext()
    if name not in _client.available():
        return None, None
    return _client.load(name), db.for_client(name)


def _run_banner(client_name: str | None) -> str:
    """What this client's newest run is doing, right now.

    The "In flight" list reads vertical_backlog, which only the weekly EXPAND path
    writes to. A copy test takes its audiences straight from the client's profile and
    never touches the backlog, so ProductEVO's page showed nothing running while a run
    was very much running (Troy, 2026-09-21). The runs table is the one place every
    path writes to, so that is what this reads.
    """
    import contextlib as _ctx
    from datetime import datetime, timezone
    # THIS CLIENT's database. db.cursor() alone reads whichever one the web process
    # booted against, so both pages showed the same run id (caught while testing).
    ctx = db.for_client(client_name) if client_name else _ctx.nullcontext()
    try:
        with ctx:
            with db.cursor() as cur:
                cur.execute("SELECT run_id, started_at, finished_at, status "
                            "  FROM runs ORDER BY started_at DESC LIMIT 1")
                run = cur.fetchone()
                if not run:
                    return ""
                # This query used to sit INSIDE the `if not run:` block, after its
                # `return ""` -- unreachable. So `last` was always None: the banner said
                # "no steps logged yet" forever, and `quiet` measured from the run's
                # START rather than its last step, so every run older than 45 minutes
                # read "It may have died" no matter how alive it was.
                #
                # 2026-09-23: run-20260923T120423 was logging every few seconds and the
                # page announced it had "logged nothing for 85 min". That is the whole
                # reason this page could not be trusted to show a run working.
                cur.execute("SELECT stage, message, ts FROM logs "
                            " WHERE run_id=%s ORDER BY id DESC LIMIT 1", (run["run_id"],))
                last = cur.fetchone()
                # WHICH audiences it is on. A vertical gets no card until it banks a
                # company, so a run's first half hour showed a live banner above a page
                # with nothing on it -- Troy, 2026-09-21: "on the web the new runs are
                # not showing". A copy test keeps its audiences on the test row; the
                # expand path marks them running in the backlog.
                working = []
                try:
                    cur.execute("SELECT audiences FROM copy_tests WHERE run_id=%s",
                                (run["run_id"],))
                    row = cur.fetchone()
                    if row and row["audiences"]:
                        a = row["audiences"]
                        working = a if isinstance(a, list) else json.loads(a)
                except Exception:
                    working = []
                if not working:
                    try:
                        cur.execute("SELECT vertical FROM vertical_backlog "
                                    " WHERE status IN ('running','paused_llm')"
                                    " ORDER BY vertical")
                        working = [r["vertical"] for r in cur.fetchall()]
                    except Exception:
                        working = []
    except Exception:
        return ""

    now = datetime.now(timezone.utc)
    when = (last["ts"] if last else run["started_at"])
    quiet = int((now - when).total_seconds() // 60)
    tail = (f'{_esc(last["stage"])}: {_esc(str(last["message"])[:120])}'
            if last else "no steps logged yet")

    if run["finished_at"]:
        note = (f'Last run <b>{_esc(run["status"])}</b>, '
                f'{run["finished_at"]:%a %d %b %H:%M} UTC. Nothing running now.')
        tone = "muted"
    elif quiet > 45:
        note = (f'Run <code>{_esc(run["run_id"])}</code> has logged nothing for '
                f'<b>{quiet} min</b>. It may have died; a redeploy of the web service '
                f'kills a run in progress.')
        tone = "warn"
    else:
        note = (f'Run <code>{_esc(run["run_id"])}</code> is working. '
                f'Last step {quiet} min ago.')
        tone = "live"
    audiences = ""
    if working:
        audiences = ("<div class=muted>Working: "
                     + " &middot; ".join(_esc(w) for w in working[:6]) + "</div>")
    # THE WEEK'S QUOTA, where a human looks first. Troy, 2026-09-30: 15,000 leads a
    # week is what the pipeline exists to produce; a short Monday should be visible
    # on Wednesday, not the next Monday.
    quota = ""
    try:
        with ctx:
            w = db.weekly_progress()
        pct = w["leads"] / w["quota"] if w["quota"] else 0
        behind = w["leads"] < w["pace_quota"] * 0.8
        quota = (f'<div class=muted><span class="stat {"warn" if behind else ""}">'
                 f'<b>{w["leads"]:,}</b> of {w["quota"]:,} leads this week ({pct:.0%})'
                 f'</span> <span class=stat>on pace for <b>{w["on_pace_for"]:,}</b>'
                 f'</span></div>')
    except Exception:
        pass
    spend = ""
    try:
        from . import report as _report
        with ctx:
            lines_ = _report.vendor_spend(7)
        if lines_:
            spend = "<div class=muted>" + " &middot; ".join(_esc(x) for x in lines_) + "</div>"
    except Exception:
        pass
    return (f'<div class="card {tone}"><b>Right now:</b> {note}'
            f'{audiences}{quota}{spend}<div class=muted>{tail}</div></div>')


@app.get("/c/{client_name}/live", response_class=HTMLResponse)
def live_index_for(client_name: str) -> HTMLResponse:
    """One client's live page. The same view, against that client's own database."""
    return live_index(client_name)


@app.get("/live", response_class=HTMLResponse)
def live_index(client_name: str | None = None) -> HTMLResponse:
    """What the pipeline has banked, refreshing every 30 seconds.

    Troy, 2026-09-13: "can it have a live feed of verified contacts so i can check?"
    Rows appear here the moment a verification slice clears -- before copy, before
    any batch exists -- straight from the contacts table, so this is the same truth
    a resume would pick up from.
    """
    profile, ctx = _resolve_client(client_name)
    if ctx is None:
        return HTMLResponse("<pre>no such client</pre>", status_code=404)
    who = profile.display_name if profile else "this pipeline"
    base = f"/c/{client_name}" if client_name else ""
    try:
      with ctx:
        with db.cursor() as cur:
            cur.execute(
                # NOT filtered on source. A contact this pipeline found and verified
                # keeps source='backfill' when the email already existed in the
                # 223,863-row history (ON CONFLICT does not rewrite source), so that
                # filter hid 2,249 genuinely re-verified contacts and the page read
                # 4,218 where the pipeline held 5,119. The 221,332 untouched backfill
                # rows carry no vertical, so IS NOT NULL is the filter that matters.
                "SELECT vertical, status, count(*) AS n, max(updated_at) AS last "
                "  FROM contacts WHERE vertical IS NOT NULL "
                "   AND status = ANY(%s) AND updated_at > now() - interval '14 days' "
                " GROUP BY 1,2 ORDER BY 1,2", (list(_LIVE_STATUSES),))
            rows = cur.fetchall()
            cur.execute(
                "SELECT vertical, status FROM vertical_backlog "
                " WHERE status IN ('running','paused_llm') ORDER BY projected DESC")
            active = cur.fetchall()
            # Every status, not just the in-flight ones. Troy, 2026-09-22: "the website
            # does not carry the running, paused, finished tag". A card showing counts
            # with no state cannot be read: 240 contacts means one thing while a
            # vertical is still working and another once it has finished.
            cur.execute("SELECT vertical, status FROM vertical_backlog")
            all_state = {r["vertical"]: r["status"] for r in cur.fetchall()}
            # A COPY TEST never touches vertical_backlog -- it takes its audiences
            # straight from the client profile -- so every ProductEVO card had no tag at
            # all. Derive the state from the run that last worked the vertical: still
            # open means working, closed means finished.
            try:
                cur.execute(
                    "SELECT c.vertical, r.finished_at "
                    "  FROM (SELECT vertical, max(run_id) AS run_id FROM contacts "
                    "         WHERE vertical IS NOT NULL GROUP BY 1) c "
                    "  JOIN runs r ON r.run_id = c.run_id")
                for r in cur.fetchall():
                    all_state.setdefault(
                        r["vertical"], "done" if r["finished_at"] else "running")
            except Exception:
                pass
            cur.execute(
                # staged_at, NOT found_at. found_at means "the finders have processed
                # it", which only ever happens to a company that PASSED the fit check --
                # so filtering on it silently dropped every rejected company and the page
                # reported found and passed as the same number (Troy spotted it
                # immediately, 2026-09-21). staged_at is when the company was banked.
                "SELECT vertical, count(*) AS staged, "
                "       count(verdict) AS judged, "
                "       count(*) FILTER (WHERE verdict IN ('KEEP','IDPR')) AS kept, "
                "       max(staged_at) AS last "
                "  FROM staged_companies "
                " WHERE staged_at > now() - interval '14 days' "
                " GROUP BY 1")
            cos = {r["vertical"]: dict(r) for r in cur.fetchall()}
            # Troy, 2026-10-08: "the live page should be synced daily with bison, the
            # numbers are not tallying." pushed_lead_count (the "pushed" stat below)
            # only ever grows; it never reflects what EmailBison later removes on its
            # own (Venture Capital alone: 1,810 stopped leads our own bookkeeping never
            # saw). bison_total_leads is synced once a day by runner.sync_bison_leads
            # and shown alongside, not instead of, the push count.
            cur.execute(
                "SELECT vertical, bison_total_leads, bison_synced_at FROM ve_campaigns "
                " WHERE bison_total_leads IS NOT NULL AND vertical <> %s", (db.POOL_VERTICAL,))
            bison = {r["vertical"]: dict(r) for r in cur.fetchall()}
        # Cumulative, all-time -- so a vertical shows "explored 20% of its real market"
        # rather than a false "exhausted" reading from a small early sample. Troy,
        # 2026-09-25.
        try:
            market = db.market_exploration()
        except Exception:
            market = {}
    except Exception as exc:
        return HTMLResponse(f"<pre>database unavailable: {_esc(exc)}</pre>", status_code=503)

    per: dict[str, dict] = {}
    for r in rows:
        d = per.setdefault(r["vertical"], {"last": None})
        d[r["status"]] = r["n"]
        if r["last"] and (d["last"] is None or r["last"] > d["last"]):
            d["last"] = r["last"]
    state = {a["vertical"]: a["status"] for a in active}
    # A vertical that is still sourcing has companies and no contacts yet, so it needs a
    # card of its own or the page stays blank through the longest part of a run.
    for v in cos:
        per.setdefault(v, {"last": None})
    # NEWEST FIRST. It was ordered by how much each vertical held, so the one being
    # worked right now sat wherever its count happened to put it -- usually last,
    # because it has banked the least. Most recent activity is what anyone opening this
    # page is looking for. Companies count as activity too, or a vertical that is still
    # sourcing has no timestamp at all and sinks to the bottom.
    def _recency(k):
        stamps = [per[k].get("last"), (cos.get(k) or {}).get("last")]
        stamps = [t for t in stamps if t]
        return max(stamps) if stamps else datetime.min.replace(tzinfo=timezone.utc)

    TAGS = {"running": ("working", "live"), "paused_llm": ("paused", "warn"),
            "done": ("finished", "muted"), "queued": ("queued", "muted")}
    cards = []
    now = datetime.now(timezone.utc)
    for v in sorted(per, key=_recency, reverse=True):
        d = per[v]
        banked = sum(d.get(k, 0) for k in ("verified", "draft", "reserved", "approved"))
        st = state.get(v) or all_state.get(v, "")
        # vertical_backlog.status is a static flag: _work_verticals() sets it back to
        # 'running' the moment it reclaims a paused vertical, but a top-up or copy pass
        # that touches the SAME vertical through a different path never writes it, so
        # the column can sit frozen on 'paused_llm' from an old incident while real
        # copy is actively being drafted. Troy, 2026-10-05: the serper outage that
        # paused Commercial Real Estate that morning was long refilled and the vertical
        # had drafted 1,426 fresh contacts in the last hour, yet the page still called
        # it paused. Recent contact activity overrides a stale paused_llm the same way
        # the ProductEVO fallback above already overrides a missing one.
        if st == "paused_llm" and d.get("last") and (now - d["last"]).total_seconds() < 7200:
            st = "running"
        label, tone_cls = TAGS.get(st, (st, "muted"))
        pill = (f'<span class="pill {tone_cls}">{_esc(label)}</span>' if label else "")
        last = f'{d["last"]:%H:%M:%S} UTC' if d["last"] else "-"
        c = cos.get(v) or {}
        co_line = ""
        if c:
            waiting = int(c["staged"] or 0) - int(c["judged"] or 0)
            staged = int(c["staged"] or 0)
            kept_n = int(c["kept"] or 0)
            rate = f" ({kept_n / staged:.0%})" if staged else ""
            co_line = (f'<div class=muted><span class=stat><b>{staged:,}</b>companies '
                       f'sourced</span>'
                       f'<span class=stat><b>{kept_n:,}</b>passed the fit check{rate}'
                       f'</span>'
                       f'<span class=stat><b>{waiting:,}</b>still being judged</span>'
                       f'</div>')
        mk = market.get(v)
        if mk and mk.get("market_available"):
            scanned = int(mk["market_scanned_total"] or 0)
            avail = int(mk["market_available"])
            pct = min(scanned / avail, 1.0) if avail else 0
            # A stopped-early "poor match" reading should never look like exhaustion --
            # the whole point of surfacing this at all. Under 30% explored is a plain
            # signal, not a warning color; there is real market left either way.
            tone = "warn" if pct >= 0.9 and pct < 1.0 else ""
            co_line += (f'<div class=muted><span class="stat {tone}">'
                        f'<b>{pct:.0%}</b>of its real market explored '
                        f'({scanned:,} of {avail:,})</span></div>')
        bs = bison.get(v)
        bison_line = ""
        if bs and bs.get("bison_total_leads") is not None:
            pushed_total = int(d.get("pushed", 0))
            live_total = int(bs["bison_total_leads"])
            # Only worth a flag when it actually disagrees -- a brand-new campaign with
            # nothing pushed yet matching 0 in Bison is not a discrepancy.
            tone = "warn" if pushed_total and abs(live_total - pushed_total) > 0 else ""
            synced = bs.get("bison_synced_at")
            synced_s = f'{synced:%d %b %H:%M} UTC' if synced else "never"
            bison_line = (f'<div class=muted><span class="stat {tone}">'
                         f'<b>{live_total:,}</b>actually in Bison right now</span>'
                         f'<span class=stat>synced {synced_s}</span></div>')
        cards.append(
            f'<div class=card><a href="{base}/live/{_esc(v)}"><b>{_esc(v)}</b></a> {pill}'
            + co_line +
            f'<div><span class=stat><b>{banked:,}</b>banked</span>'
            f'<span class=stat><b>{d.get("verified", 0):,}</b>verified, awaiting copy</span>'
            f'<span class=stat><b>{d.get("draft", 0):,}</b>copy written</span>'
            f'<span class=stat><b>{d.get("reserved", 0) + d.get("approved", 0):,}</b>in a batch</span>'
            f'<span class=stat><b>{d.get("pushed", 0):,}</b>pushed</span></div>'
            + bison_line +
            f'<div class=muted>last change {last}</div></div>')
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=30>"
        f"<title>{_esc(who)} - live contacts</title>{_PAGE_CSS}{_EXTRA_CSS}"
        f"<div class=wrap>"
        f"<h1>{_esc(who)} &mdash; live contacts</h1>" + _client_bar(client_name) +
        f"<p class=muted>Banked as each verification slice clears. Refreshes every 30s. "
        f"<a href='/review'>Awaiting approval &rarr;</a></p>"
        + _run_banner(client_name)
        + ("".join(cards) or '<div class="card muted">Nothing banked in the last 14 days.</div>')
        + "</div>")


@app.get("/c/{client_name}/scaling", response_class=HTMLResponse)
def scaling_index_for(client_name: str) -> HTMLResponse:
    """One client's scaling page, against that client's own database."""
    return scaling_index(client_name)


@app.get("/scaling", response_class=HTMLResponse)
def scaling_index(client_name: str | None = None) -> HTMLResponse:
    """Where every vertical stands on the way to its lead target.

    Troy, 2026-09-23: "I would like a tab I can click on that takes me to anything that
    is scaling ... it should have a scaling or scaled tag and a tag if its reached its
    limit so i know."

    The live page answers "what is being built right now". This answers the different
    question the daily review actually decides: which audiences have earned more, how
    far each has got, and what is stopping the rest. A vertical can be short of target
    for four unrelated reasons -- still being judged, waiting behind the daily cap, out
    of supply for good, or out of time on one run -- and they need different actions.
    """
    profile, ctx = _resolve_client(client_name)
    if ctx is None:
        return HTMLResponse("<pre>no such client</pre>", status_code=404)
    who = profile.display_name if profile else "this pipeline"
    base = f"/c/{client_name}" if client_name else ""
    target = settings.scale_target_leads
    try:
        with ctx:
            rows = db.scaling_overview()
            banked = {r["vertical"]: r["n"] for r in db.banked_awaiting_copy(1)}
    except Exception as exc:
        return HTMLResponse(f"<pre>database unavailable: {_esc(exc)}</pre>", status_code=503)

    def state_of(r: dict) -> tuple[str, str, str]:
        """(tag, tone, what it means) -- the four reasons differ and need different acts."""
        if r.get("scale_exhausted_at"):
            return ("AT ITS LIMIT", "warn",
                    f"supply ran out at {int(r.get('scale_exhausted_at_leads') or 0):,} "
                    f"leads. Widen employee size, geography or titles to reopen it.")
        if int(r["leads"] or 0) >= target:
            return ("SCALED", "live", f"reached the {target:,}-lead target.")
        if r["verdict"] == "scale":
            return ("SCALING", "live",
                    f"a winner at {float(r['rate'] or 0):.1%}; tops up toward "
                    f"{target:,} on the days it is one of the top "
                    f"{settings.scale_max_per_run}.")
        if r["verdict"] == "drop":
            return ("DROPPED", "muted", "judged below the bar; not scaled again.")
        if r["pooled"]:
            return ("POOLED", "muted",
                    "shares a campaign with other small verticals, and is scored on its "
                    "own replies rather than the campaign's blended number.")
        return ("BEING JUDGED", "muted",
                _esc(str(r.get("reason") or "not yet judged")))

    cards = []
    for r in rows:
        tag, tone, meaning = state_of(r)
        leads = int(r["leads"] or 0)
        pct = min(100, int(leads * 100 / target)) if target else 0
        wait = banked.get(r["vertical"], 0)
        wait_line = (f'<div class=muted>{wait:,} contacts already sourced and verified, '
                     f'waiting only for copy</div>' if wait else "")
        cards.append(
            f'<div class="card {tone}"><b>{_esc(r["vertical"])}</b> '
            f'<span class="pill {tone}">{_esc(tag)}</span>'
            f'<div><span class=stat><b>{leads:,}</b>leads of {target:,}</span>'
            f'<span class=stat><b>{pct}%</b>of target</span>'
            f'<span class=stat><b>{int(r["sends"] or 0):,}</b>sent</span>'
            f'<span class=stat><b>{int(r["interested"] or 0):,}</b>interested</span>'
            f'<span class=stat><b>{float(r["rate"] or 0):.1%}</b>of replies</span></div>'
            f'{wait_line}'
            f'<div class=muted>{meaning}</div></div>')

    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{_esc(who)} scaling</title>"
        f"<meta http-equiv=refresh content=60>{_PAGE_CSS}{_EXTRA_CSS}<div class=wrap>"
        f"<h1>{_esc(who)} &mdash; scaling</h1>" + _client_bar(client_name) +
        f"<p class=muted>How far each audience has got toward {target:,} leads. "
        f"<a href='{base}/live'>Live contacts &rarr;</a> &middot; "
        f"<a href='/review'>Awaiting approval &rarr;</a></p>"
        + ("".join(cards) or '<div class="card muted">No campaigns yet.</div>')
        + "</div>")


@app.get("/learning", response_class=HTMLResponse)
def learning_index() -> HTMLResponse:
    """Current experiment arms and observed audience/persona outcomes."""
    try:
        from . import experiments
        # Every experiment the exposures actually carry. Asking for one hardcoded id
        # made a copy test invisible on the page built to show it (found 2026-09-19).
        seen = db.experiments_seen() or [experiments.COPY_EXPERIMENT]
        variants = []
        for exp in seen:
            for row in db.experiment_performance(exp):
                variants.append({**row, "experiment_id": exp})
        segments = db.learning_segments()
        allocation = experiments.weights(refresh=True)
    except Exception as exc:
        return HTMLResponse(f"<pre>database unavailable: {_esc(exc)}</pre>", status_code=503)

    variant_rows = "".join(
        f"<tr><td>{_esc(r.get('experiment_id',''))}</td>"
        f"<td>{_esc(r['variant_id'])}</td><td>{int(r['delivered'] or 0):,}</td>"
        f"<td>{int(r['replied'] or 0):,}</td><td>{int(r['interested'] or 0):,}</td>"
        f"<td>{int(r['booked'] or 0):,}</td>"
        f"<td>{allocation.get(r['variant_id'], 0):.0%}</td></tr>" for r in variants
    ) or "<tr><td colspan=6 class=muted>No experiment exposures yet.</td></tr>"
    segment_rows = "".join(
        f"<tr><td>{_esc(r['vertical'])}</td><td>{_esc(r['title_segment'])}</td>"
        f"<td>{int(r['delivered'] or 0):,}</td><td>{int(r['replied'] or 0):,}</td>"
        f"<td>{int(r['interested'] or 0):,}</td><td>{int(r['booked'] or 0):,}</td></tr>"
        for r in segments
    ) or "<tr><td colspan=6 class=muted>No segment has 50 delivered contacts yet.</td></tr>"
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=60>"
        f"<title>Learning loop</title>{_PAGE_CSS}<div class=wrap><h1>Learning loop</h1>"
        f"<p class=muted>Interested replies and booked meetings are the outcomes. "
        f"Allocation keeps a 20% exploration floor after both copy arms mature.</p>"
        f"<div class=card><h2>Copy experiment</h2><table><tr><th>Variant</th>"
        f"<th>Matured</th><th>Replies</th><th>Interested</th><th>Booked</th>"
        f"<th>Next allocation</th></tr>{variant_rows}</table></div>"
        f"<div class=card><h2>Observed segments</h2><table><tr><th>Vertical</th>"
        f"<th>Persona</th><th>Delivered</th><th>Replies</th><th>Interested</th>"
        f"<th>Booked</th></tr>{segment_rows}</table></div></div>")


@app.get("/c/{client_name}/live/{vertical}", response_class=HTMLResponse)
def live_vertical_for(client_name: str, vertical: str, limit: int = 300) -> HTMLResponse:
    return live_vertical(vertical, limit, client_name)


@app.get("/c/{client_name}/live/{vertical}/log", response_class=HTMLResponse)
def live_vertical_log_for(client_name: str, vertical: str, limit: int = 150) -> HTMLResponse:
    return live_vertical_log(vertical, limit, client_name)


@app.get("/live/{vertical}/log", response_class=HTMLResponse)
def live_vertical_log(vertical: str, limit: int = 150, run_id: str | None = None,
                      client_name: str | None = None) -> HTMLResponse:
    """The raw run log lines that mention this vertical, newest first.

    Troy, 2026-09-29, asking whether Lead Ocean contributed to a manual --topup run:
    every per-vendor breakdown (found_leadocean, found_quickenrich, ...) is written
    here by db.log() the moment it happens, but a --topup is launched as a
    subprocess whose stdout goes to a log FILE inside the container -- invisible to
    `railway logs` and, until now, only reachable through the Slack-only, signature-
    verified /ve progress command. This is the same data, read straight from the
    logs table instead, so it does not need Slack, SSH, or a direct database
    connection to check.

    Message-text matching alone misses stage4's own per-vendor tally: `db.log(run_id,
    "stage4", f"built {n} verified contacts", **stats)` never puts the vertical's
    name IN the message text, only in `detail` (the same dict this route now
    searches too) -- so a plain ILIKE on message alone silently dropped exactly the
    line this was built to find. ?run_id=... narrows to one run once you have it.
    """
    profile, ctx = _resolve_client(client_name)
    if ctx is None:
        return HTMLResponse("<pre>no such client</pre>", status_code=404)
    base = f"/c/{client_name}" if client_name else ""
    try:
        with ctx:
            with db.cursor() as cur:
                if run_id:
                    cur.execute(
                        "SELECT run_id, stage, level, message, detail, ts FROM logs "
                        " WHERE run_id = %s ORDER BY ts DESC LIMIT %s",
                        (run_id, limit))
                else:
                    cur.execute(
                        "SELECT run_id, stage, level, message, detail, ts FROM logs "
                        " WHERE message ILIKE %s OR detail::text ILIKE %s "
                        " ORDER BY ts DESC LIMIT %s",
                        (f"%{vertical}%", f"%{vertical}%", limit))
                rows = cur.fetchall()
    except Exception as exc:
        return HTMLResponse(f"<pre>log read failed: {_esc(str(exc))}</pre>", status_code=500)
    lines = []
    for r in rows:
        detail = f" <code>{_esc(str(r['detail']))}</code>" if r.get("detail") else ""
        lines.append(f"<div><code>{r['ts']:%Y-%m-%d %H:%M:%S}</code> "
                     f"<a href='?run_id={r['run_id']}'>{_esc(r['run_id'] or '')}</a> "
                     f"<b>{_esc(r['stage'] or '')}</b> [{_esc(r['level'])}] "
                     f"{_esc(r['message'])}{detail}</div>")
    body = "\n".join(lines) if lines else "<p class=muted>no log lines mention this vertical</p>"
    return HTMLResponse(
        f"<html><head><title>{_esc(vertical)} -- log</title>"
        f"<style>body{{font-family:monospace;font-size:13px;padding:16px}}"
        f"code{{color:#888}}</style></head><body>"
        f"<p><a href='{base}/live/{vertical}'>&larr; contacts</a></p>"
        f"<h3>{_esc(vertical)} -- last {len(rows)} matching log lines</h3>"
        f"{body}</body></html>")


@app.get("/live/{vertical}", response_class=HTMLResponse)
def live_vertical(vertical: str, limit: int = 300,
                  client_name: str | None = None) -> HTMLResponse:
    """The contacts themselves for one vertical, newest first."""
    profile, ctx = _resolve_client(client_name)
    if ctx is None:
        return HTMLResponse("<pre>no such client</pre>", status_code=404)
    who = profile.display_name if profile else ""
    base = f"/c/{client_name}" if client_name else ""
    try:
      with ctx:
        with db.cursor() as cur:
            cur.execute(
                "SELECT email, first_name, last_name, title, company_name, company_domain, "
                "       status, updated_at FROM contacts "
                " WHERE vertical=%s AND status = ANY(%s) "
                " ORDER BY updated_at DESC LIMIT %s",
                (vertical, list(_LIVE_STATUSES), max(1, min(limit, 3000))))
            rows = cur.fetchall()
            cur.execute("SELECT count(*) AS n FROM contacts WHERE vertical=%s "
                        "  AND status = ANY(%s)", (vertical, list(_LIVE_STATUSES)))
            total = cur.fetchone()["n"]
    except Exception as exc:
        return HTMLResponse(f"<pre>database unavailable: {_esc(exc)}</pre>", status_code=503)
    body = "".join(
        f"<tr><td>{_esc(r['first_name'])} {_esc(r['last_name'])}<div class=muted>{_esc(r['title'])}</div></td>"
        f"<td>{_esc(r['company_name'] or r['company_domain'])}<div class=muted>{_esc(r['company_domain'])}</div></td>"
        f"<td class=muted>{_esc(r['email'])}</td>"
        f"<td><span class=pill>{_esc(r['status'])}</span></td>"
        f"<td class=muted>{r['updated_at']:%H:%M:%S}</td></tr>"
        for r in rows) or "<tr><td colspan=5 class=muted>Nothing yet.</td></tr>"
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=30>"
        f"<title>{_esc(vertical)} - live</title>{_PAGE_CSS}<div class=wrap>"
        f"<h1>{_esc(vertical)}</h1>"
        f"<p class=muted>{_esc(who)}{' &middot; ' if who else ''}{total:,} contacts banked "
        f"&middot; showing newest {len(rows)} &middot; refreshes every 30s &middot; "
        f"<a href='{base}/live'>&larr; all verticals</a> &middot; "
        f"<a href='{base}/live/{_esc(vertical)}?limit=3000'>show all</a></p>"
        f"<div class=card><table><tr><th>Person</th><th>Company</th><th>Email</th><th>Status</th>"
        f"<th>Banked</th></tr>{body}</table></div></div>")


@app.get("/review/{batch_id}", response_class=HTMLResponse)
def review_batch(batch_id: str) -> HTMLResponse:
    """One batch in full: every contact and both emails, not a three-sample summary."""
    try:
        batch = db.get_batch(batch_id)
        contacts = db.batch_contacts(batch_id) if batch else []
    except Exception as exc:
        return HTMLResponse(f"<pre>database unavailable: {_esc(exc)}</pre>", status_code=503)
    if not batch:
        return HTMLResponse("<pre>no such batch</pre>", status_code=404)

    s = batch.get("summary") or {}
    vert = s.get("vertical") or {}
    stats = "".join(
        f'<span class=stat><b>{s.get(k, 0)}</b>{label}</span>'
        for k, label in (("companies_kept", "companies kept"),
                         ("companies_dropped_dedup", "already emailed"),
                         ("companies_dropped_enforcement", "failed quality gate"),
                         ("contacts_found", "contacts found"),
                         ("contacts_sendable", "with approved copy"),
                         ("contacts_needs_review", "copy failed QA"))
    )
    rows = "".join(
        f"<tr><td><b>{_esc((c.get('first_name') or '') + ' ' + (c.get('last_name') or ''))}</b>"
        f"<div class=muted>{_esc(c.get('title'))}</div></td>"
        f"<td>{_esc(c.get('company_name') or c.get('company_domain'))}"
        f"<div class=muted>{_esc(c.get('email'))}</div></td>"
        f"<td><pre>{_esc(c.get('personalization_1'))}</pre>"
        f"<pre>{_esc(c.get('personalization_2'))}</pre></td></tr>"
        for c in contacts
    )
    warn = ("" if batch["approval_status"] == "pending" else
            f'<p><span class="pill warn">{_esc(batch["approval_status"])}</span></p>')
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{_esc(batch['vertical'])}</title>{_PAGE_CSS}"
        f"<div class=wrap><h1>{_esc(batch['vertical'])}</h1>"
        f"<p class=muted>{_esc(batch_id)} &middot; expires {batch['expires_at']:%Y-%m-%d %H:%M} UTC"
        f"</p>{warn}"
        f"<div class=card><i>{_esc(vert.get('rationale'))}</i></div>"
        f"<div class=card>{stats}</div>"
        f"<h2>All {len(contacts)} contacts</h2>"
        f"<div class=card><table><tr><th>Person</th><th>Company</th><th>Email 1 / Email 2</th></tr>"
        f"{rows}</table></div>"
        f"<p class=muted>Approve: <code>/ve approve {_esc(batch_id)}</code> &nbsp; "
        f"Kill: <code>/ve kill {_esc(batch_id)} &lt;reason&gt;</code></p></div>")
