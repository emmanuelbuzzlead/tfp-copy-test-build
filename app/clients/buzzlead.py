"""BuzzLead Research & Pipeline API -- company + contact sourcing.

Calling AI Ark's company search alone walks the head of one ranked industry list.
IDPR has already emailed most of that head, so the freshness probe reports TAM as
saturated when DiscoLike lookalikes, keyword TAM and SignalBase still have supply.

This client launches phase 1 of the BuzzLead pipeline (discover → contact-source →
email waterfall) and STOPS at the lead-review gate. We never approve copy and we
never push to EmailBison from here -- this expander writes its own copy and has
its own Slack gate.

A missing key is not an error: Stage 3 falls back to AI Ark. A failed or thin
BuzzLead run also falls back, so a bad job cannot starve a vertical.
"""

from __future__ import annotations

import csv
import os
import io
import re
import time
from typing import Any
from urllib.parse import urlparse

from ..config import settings
from .http import VendorError, request

DEFAULT_BASE = "https://research-615339619342.us-east4.run.app"
POLL_SECONDS = 30
# COMPANIES ONLY. Their pipeline's value to us is discovery: its companies pass our fit
# gate at 52% against 13% for the paged industry search. Everything after `discover` is
# either redundant or a liability -- enrichment adds no field our gate reads (the raw
# and enriched files are byte-identical on every column we use), their contacts are
# beaten by our own broad query, and EVERY crash we have seen happened after discovery.
# Measured 2026-09-22 on a 400-company job: company file readable at 2.2 min, the job
# still running at 30. Set BUZZLEAD_COMPANIES_ONLY=false to wait for the full job.
COMPANIES_POLL_S = int(os.getenv("BUZZLEAD_COMPANIES_POLL_S", "20"))
COMPANIES_MAX_WAIT_S = int(os.getenv("BUZZLEAD_COMPANIES_MAX_WAIT_S", str(12 * 60)))


def companies_only() -> bool:
    raw = os.getenv("BUZZLEAD_COMPANIES_ONLY")
    return True if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def poll_for_companies(job_id: str, campaign: str, on_step=None
                       ) -> tuple[list[dict], dict]:
    """Wait only until the company file exists, then stop caring about the job."""
    deadline = time.time() + COMPANIES_MAX_WAIT_S
    seen = None
    started = time.time()
    while time.time() < deadline:
        st = _get(f"/pipeline/{job_id}/status") or {}
        step = st.get("step") if isinstance(st, dict) else None
        if on_step and step and step != seen:
            seen = step
            try:
                on_step(step, int(time.time() - started))
            except Exception:
                pass
        try:
            cos, _ = fetch_artifacts(job_id, campaign)
        except Exception:
            cos = []
        if cos:
            return cos, {"job_id": job_id, "status": "companies_only",
                         "waited_s": int(time.time() - started),
                         "companies": len(cos)}
        if isinstance(st, dict) and is_failed(st) and not at_lead_gate(st):
            return [], {"job_id": job_id, "status": _status_name(st),
                        "error": st.get("error") or st.get("reason")}
        time.sleep(COMPANIES_POLL_S)
    return [], {"job_id": job_id, "status": "timeout",
                "error": f"no company file after {COMPANIES_MAX_WAIT_S // 60} min"}
# Matches Stage 3's sourcing budget. A hung job must not block the weekly run.
# 40 minutes was not enough and the shortfall was silent. Measured 2026-09-21: a job
# for 833 companies was still in `company_enrich` after FIFTY minutes, and that step
# scales with the company count. So a full-size vertical timed out, the job was
# abandoned mid-enrichment, and the run fell back to AI Ark -- which reads exactly like
# "BuzzLead returned nothing" even though the job was working and would have finished.
# A 50-company job reaches the gate in about half an hour, an 833-company one does not.
# 45 minutes, not 150. Troy, 2026-09-21: "why is it making things so slow? I relaunched
# an hour ago and I have no result." He was right and the 150 was my error: waiting
# longer only pays if the wait is the only way to keep the work, and it is not --
# salvage takes whatever the job has written by then. So: stop waiting early, keep the
# companies, let the fallback fill the rest.
POLL_TIMEOUT_S = int(os.getenv("BUZZLEAD_POLL_TIMEOUT_S", str(45 * 60)))
LEAD_GATE = {"awaiting_approval", "awaiting_leads", "lead_gate", "awaiting_lead_approval"}
COPY_GATE = {"awaiting_copy_approval", "awaiting_copy"}
FAILED = {"failed", "error", "aborted", "cancelled", "canceled", "timeout"}


def enabled() -> bool:
    """BUZZLEAD_ENABLED=false skips it entirely and sources from the company search.

    A kill switch, not a setting. Troy, 2026-09-21: "please run everything without the
    BuzzLead thing, now." Their pipeline was crashing and slow that afternoon, and a
    primary source having a bad day should never be the reason a run cannot happen --
    the fallback exists precisely for this.
    """
    raw = os.getenv("BUZZLEAD_ENABLED")
    return True if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def configured() -> bool:
    return bool(settings.buzzlead_api_key) and enabled()


def _base() -> str:
    return (settings.buzzlead_base or DEFAULT_BASE).rstrip("/")


def _headers() -> dict[str, str]:
    if not settings.buzzlead_api_key:
        raise RuntimeError("BUZZLEAD_API_KEY is not set")
    return {
        "Authorization": f"Bearer {settings.buzzlead_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get(path: str, params: dict | None = None) -> Any:
    return request("buzzlead", "GET", f"{_base()}{path}",
                   headers=_headers(), params=params)


def _post(path: str, body: dict) -> Any:
    return request("buzzlead", "POST", f"{_base()}{path}",
                   headers=_headers(), json_body=body)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s or "vertical")[:48]


def _domain(value: object) -> str:
    raw = (str(value or "")).strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0].strip()


def tam_strategy(vertical, titles: list[str], keywords: list[str]) -> dict:
    return {
        "id": f"ve-tam-{_slug(vertical.name)}",
        "name": vertical.name,
        "type": "tam",
        "job_titles_to_include": titles,
        "industries": list(vertical.industries or []),
        "keywords": list(keywords or []),
        "search_parameters": {
            "employee_range": f"{vertical.employee_min},{vertical.employee_max}",
            "geography": "United States",
        },
        "target_persona": {"description": vertical.rationale or ""},
    }


def lookalike_strategy(vertical, titles: list[str], seed_domains: list[str]) -> dict:
    return {
        "id": f"ve-lla-{_slug(vertical.name)}",
        "name": f"{vertical.name} lookalike",
        "type": "lookalike",
        "job_titles_to_include": titles,
        "seed_domains": [d for d in seed_domains if d][:3],
        "search_parameters": {
            "employee_range": f"{vertical.employee_min},{vertical.employee_max}",
            "geography": "United States",
        },
        "target_persona": {"description": vertical.rationale or ""},
    }


def start_discovery(strategy: dict, campaign: str, *, max_records: int,
                    contacts_per_domain: int) -> str:
    """Launch phase 1. Returns job_id. Does not send email."""
    body = {
        "campaign": campaign,
        "strategy": strategy,
        "options": {
            "max_records": max_records,
            "contacts_per_domain": contacts_per_domain,
            "tam_limit": max_records,
            "find_emails": True,
            "review_copy": True,
            "skip_notion": True,
            "dedup": True,
            "generate_new_leads": True,
            "employee_range": strategy.get("search_parameters", {}).get("employee_range"),
            "geography": "United States",
            "seed_domains": ",".join(strategy.get("seed_domains") or []) or None,
        },
        "repair": True,
    }
    payload = _post("/pipeline/full", body)
    if not isinstance(payload, dict):
        raise VendorError("buzzlead", None, f"unexpected launch payload: {payload!r}"[:300])
    job_id = (payload.get("job_id") or payload.get("id")
              or (payload.get("data") or {}).get("job_id")
              or (payload.get("data") or {}).get("id"))
    if not job_id:
        raise VendorError("buzzlead", None, f"launch returned no job_id: {payload}"[:400])
    return str(job_id)


def _status_name(payload: dict) -> str:
    for key in ("status", "state", "gate", "step", "phase"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        if isinstance(value, dict):
            inner = value.get("name") or value.get("status") or value.get("state")
            if inner:
                return str(inner).strip().lower()
    return ""


def at_lead_gate(payload: dict) -> bool:
    status = _status_name(payload)
    blob = " ".join(str(v) for v in (
        status, payload.get("gate"), payload.get("waiting_on"),
        payload.get("current_step"), payload.get("step"),
    ) if v).lower()
    if any(g in blob for g in COPY_GATE):
        # Copy gate means leads are already approved -- artifacts are still usable.
        return True
    return status in LEAD_GATE or any(g in blob for g in LEAD_GATE)


def is_failed(payload: dict) -> bool:
    return _status_name(payload) in FAILED or bool(payload.get("error"))


def poll_until_gate(job_id: str, timeout_s: int = POLL_TIMEOUT_S,
                    interval_s: int = POLL_SECONDS,
                    on_step=None) -> dict:
    """Wait for the lead gate, reporting each step change through `on_step`.

    Their pipeline runs discover -> company_enrich -> contacts -> scoring -> outreach,
    and until now a caller saw none of it: forty minutes of total silence, then either
    companies or nothing. `on_step` is how the run log learns where inside BuzzLead it
    actually is, which is the difference between "slow" and "stuck".
    """
    deadline = time.time() + timeout_s
    last: dict = {}
    seen = None
    started = time.time()
    while time.time() < deadline:
        last = _get(f"/pipeline/{job_id}/status") or {}
        if not isinstance(last, dict):
            last = {"raw": last}
        step = last.get("step")
        if on_step and step and step != seen:
            seen = step
            try:
                on_step(step, int(time.time() - started))
            except Exception:
                pass                       # reporting must never break the wait
        if at_lead_gate(last) or is_failed(last):
            return last
        time.sleep(interval_s)
    # TIMING OUT IS NOT LOSING. Raising here meant the caller dropped the job and
    # everything it had already written, so a 40-minute cap threw away a job that was
    # 90% done. The caller salvages the artifacts instead, which is what makes a SHORT
    # cap affordable: we stop waiting without paying for the wait.
    last = dict(last)
    last["status"] = "timeout"
    last["error"] = (f"still at {_status_name(last) or 'unknown'} after "
                     f"{timeout_s // 60} min; not waiting longer")
    return last


def _artifact_rows(content: str) -> list[dict]:
    if not content or not content.strip():
        return []
    text = content.lstrip("\ufeff")
    # Artifact fetch wraps files as {path, content, ...}; tolerate a bare CSV too.
    if text[:1] in "{[":
        try:
            import json
            payload = json.loads(text)
            if isinstance(payload, dict) and isinstance(payload.get("content"), str):
                text = payload["content"]
            elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return payload
        except ValueError:
            pass
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader if any((v or "").strip() for v in row.values())]


def _pick(row: dict, *names: str) -> str:
    lowered = {(k or "").strip().lower(): v for k, v in row.items()}
    for name in names:
        for key, value in lowered.items():
            if name == key or name in key:
                return (str(value) if value is not None else "").strip()
    return ""


def parse_companies(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for row in rows:
        domain = _domain(_pick(row, "company_domain", "domain", "website",
                               "company_url", "url"))
        if not domain or domain in seen:
            continue
        seen.add(domain)
        employees = _pick(row, "employee_count", "employees", "staff_total", "size")
        try:
            employee_count = int(float(employees)) if employees else None
        except ValueError:
            employee_count = None
        out.append({
            "domain": domain,
            "name": _pick(row, "company_name", "name", "organization", "org") or domain,
            "description": _pick(row, "description", "about", "summary"),
            "industry": _pick(row, "industry", "industries"),
            "employee_count": employee_count,
            "location": _pick(row, "country", "location", "hq"),
            "keywords": _pick(row, "keywords"),
            "_source": "buzzlead",
        })
    return out


def parse_contacts(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for row in rows:
        email = _pick(row, "email", "work_email", "best_email").lower()
        domain = _domain(_pick(row, "company_domain", "domain", "website", "email_domain"))
        if not domain and "@" in email:
            domain = email.rsplit("@", 1)[1]
        if not domain:
            continue
        key = email or (_pick(row, "linkedin_url", "linkedin") + "|" + domain)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "email": email or None,
            "first_name": _pick(row, "first_name", "firstname", "first"),
            "last_name": _pick(row, "last_name", "lastname", "last"),
            "title": _pick(row, "title", "job_title", "jobtitle"),
            "linkedin_url": _pick(row, "linkedin_url", "linkedin", "person_linkedin") or None,
            "company_domain": domain,
            "company_name": _pick(row, "company_name", "company", "organization"),
            "_source": "buzzlead",
        })
    return out


def _list_artifacts(limit: int = 500) -> list[dict]:
    """Every artifact the API will list, newest first.

    The search endpoint IGNORES `q` and `campaign` -- passing them returns something
    that is not even a list, so the old call produced nothing at all. `run_slug` is the
    only parameter it honours, and an empty one lists everything. Measured 2026-09-21.
    """
    found = _get("/artifacts/search", {"run_slug": "", "limit": limit}) or {}
    items = found if isinstance(found, list) else (
        found.get("results") or found.get("artifacts") or found.get("data") or [])
    return [i for i in items if isinstance(i, dict)]


def fetch_artifacts(job_id: str, campaign: str) -> tuple[list[dict], list[dict]]:
    """Pull companies + contacts from the run's stored artifacts.

    MATCHED ON THE CAMPAIGN SLUG, NOT THE JOB ID. Artifacts are stored under
    `<campaign>/<date>-<strategy>-<random>/<file>` and the job id appears nowhere in
    that path, so the old `job_id not in path` filter discarded every artifact of every
    run. BuzzLead has been doing the work and this function has been throwing all of it
    away -- which is why the primary company source contributed nothing, and why the AI
    Ark fallback did all the sourcing until it ran out of credit (found 2026-09-21,
    with 2,595 companies sitting in one unread file from that morning's IDPR run).
    """
    prefix = campaign.lower().rstrip("/") + "/"
    items = [i for i in _list_artifacts()
             if str(i.get("path") or i.get("file") or "").lower().startswith(prefix)]
    if not items:
        items = [i for i in _list_artifacts()
                 if job_id in str(i.get("path") or i.get("file") or "")]
    companies_rows, contact_rows = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("file") or ""
        name = (item.get("file") or path).lower()
        if not path:
            continue
        try:
            payload = _get("/artifacts/fetch", {"path": path})
        except VendorError:
            continue
        content = payload.get("content") if isinstance(payload, dict) else payload
        if not isinstance(content, str):
            continue
        rows = _artifact_rows(content)
        if "compan" in name or "enriched" in name:
            companies_rows.extend(rows)
        elif any(tok in name for tok in ("contact", "validated", "lead", "people")):
            contact_rows.extend(rows)
    if not companies_rows and not contact_rows:
        # Search was too narrow (run_slug not in the path). Retry on campaign only.
        found = _get("/artifacts/search", {"q": campaign, "limit": 50}) or {}
        items = found.get("results") or found.get("artifacts") or found.get("data") or []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or ""
            name = (item.get("file") or path).lower()
            if job_id and job_id not in path and job_id not in name:
                continue
            try:
                payload = _get("/artifacts/fetch", {"path": path})
            except VendorError:
                continue
            content = payload.get("content") if isinstance(payload, dict) else payload
            if not isinstance(content, str):
                continue
            rows = _artifact_rows(content)
            if "compan" in name:
                companies_rows.extend(rows)
            elif any(tok in name for tok in ("contact", "validated", "lead", "people")):
                contact_rows.extend(rows)
    return parse_companies(companies_rows), parse_contacts(contact_rows)


INTERRUPTED = ("interrupted", "service restart", "crash", "resume to continue")


def was_interrupted(status: dict) -> bool:
    """Their pipeline restarted under the job. Not our failure and not a dead end."""
    text = f"{status.get('error') or ''} {status.get('reason') or ''}".lower()
    return any(w in text for w in INTERRUPTED)


def resume_job(job_id: str) -> bool:
    """Ask BuzzLead to carry on from its last checkpoint.

    Their own error says to: "Pipeline was interrupted (service restart or crash).
    Resume to continue from the last checkpoint." Nothing called it, so every
    interrupted job was a total loss and the run fell back to AI Ark -- which is the
    already-emailed list, and today is also the account with no credit. All three of
    IDPR's Monday verticals died this way and sourced zero companies.

    A 409 means the job never reached a checkpoint, so there is nothing to resume and
    the caller should start a fresh one instead.
    """
    try:
        _post(f"/pipeline/{job_id}/resume", {})
        return True
    except VendorError as exc:
        if exc.status == 409:
            return False
        raise


def _run_one(strategy: dict, campaign: str, *, max_records: int,
             contacts_per_domain: int, on_step=None) -> tuple[list[dict], list[dict], dict]:
    job_id = start_discovery(strategy, campaign, max_records=max_records,
                             contacts_per_domain=contacts_per_domain)
    status = poll_until_gate(job_id, on_step=on_step)

    # An interrupted job is worth one recovery: resume from its checkpoint, or start
    # over once if it never reached one. Anything else and we hand the vertical to a
    # fallback that is both staler and, today, out of credit.
    tries = 0
    while is_failed(status) and not at_lead_gate(status) and was_interrupted(status) \
            and _status_name(status) != "timeout" and tries < 2:
        tries += 1
        if resume_job(job_id):
            status = poll_until_gate(job_id, on_step=on_step)
        else:
            job_id = start_discovery(strategy, campaign, max_records=max_records,
                                     contacts_per_domain=contacts_per_domain)
            status = poll_until_gate(job_id, on_step=on_step)

    if is_failed(status) and not at_lead_gate(status):
        # A FAILED JOB IS NOT AN EMPTY ONE. Their pipeline writes companies.csv as it
        # goes, so a job that crashes in a later step has already banked everything it
        # found. This returned [] without even looking, and on 2026-09-21 that discarded
        # 1,174 companies for Sporting & Outdoor and 1,570 for Hardware & Building
        # Materials -- both sitting in artifacts while the run reported "BuzzLead
        # returned nothing" and fell back to an account with no credit.
        salvaged, salvaged_people = [], []
        try:
            salvaged, salvaged_people = fetch_artifacts(job_id, campaign)
        except Exception:
            pass
        return salvaged, salvaged_people, {
            "job_id": job_id, "status": _status_name(status),
            "attempts": tries + 1, "salvaged": len(salvaged),
            "error": status.get("error") or status.get("reason")}
    companies, contacts = fetch_artifacts(job_id, campaign)
    return companies, contacts, {
        "job_id": job_id, "status": _status_name(status),
        "companies": len(companies), "contacts": len(contacts),
        "type": strategy.get("type"),
    }


def source_vertical(vertical, titles: list[str], keywords: list[str], *,
                    max_records: int, contacts_per_domain: int,
                    seed_domains: list[str] | None = None,
                    on_step=None) -> tuple[list[dict], list[dict], dict]:
    """TAM first; lookalike if we have seeds and TAM came back thin.

    Never calls copy/approve. Returns (companies, contacts, stats).
    """
    if not configured():
        return [], [], {"skipped": "no BUZZLEAD_API_KEY"}

    campaign = f"idpr-ve-{_slug(vertical.name)}"
    if companies_only():
        job_id = start_discovery(tam_strategy(vertical, titles, keywords), campaign,
                                 max_records=max_records,
                                 contacts_per_domain=contacts_per_domain)
        cos, stats = poll_for_companies(job_id, campaign, on_step=on_step)
        return cos, [], {"tam": stats, "lookalike": None,
                         "companies": len(cos), "contacts": 0,
                         "mode": "companies_only"}

    companies, contacts, tam_stats = _run_one(
        tam_strategy(vertical, titles, keywords), campaign,
        max_records=max_records, contacts_per_domain=contacts_per_domain,
        on_step=on_step)

    seeds = [d for d in (seed_domains or []) if d][:3]
    lookalike_stats = None
    if seeds and len(companies) < max(max_records // 2, 50):
        lla_campaign = campaign + "-lla"
        more_c, more_p, lookalike_stats = _run_one(
            lookalike_strategy(vertical, titles, seeds), lla_campaign,
            max_records=max_records, contacts_per_domain=contacts_per_domain)
        have = {c["domain"] for c in companies}
        companies.extend(c for c in more_c if c["domain"] not in have)
        have_p = {(p.get("email") or p.get("linkedin_url") or "") for p in contacts}
        contacts.extend(p for p in more_p
                        if (p.get("email") or p.get("linkedin_url") or "") not in have_p)

    # Attach prefetched people to their company so Stage 4 does not re-find them.
    by_domain: dict[str, list] = {}
    for p in contacts:
        by_domain.setdefault(p["company_domain"], []).append(p)
    for c in companies:
        attached = by_domain.get(c["domain"]) or []
        if attached:
            c["_contacts"] = attached

    return companies, contacts, {
        "tam": tam_stats, "lookalike": lookalike_stats,
        "companies": len(companies), "contacts": len(contacts),
    }
