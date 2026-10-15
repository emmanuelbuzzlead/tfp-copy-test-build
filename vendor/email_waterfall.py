#!/usr/bin/env python3
"""
Email Waterfall — Conditional cascade verification with JSON stdin/stdout + CLI.

Phase 1: Email Finders (TryKit, LeadMagic)
  → Generate candidate emails from actual finder APIs (NO pattern guessing)

Phase 2: Million Verifier v3 Quality Triage
  → Route based on quality field: good (valid) → risky (secondary) → bad (invalid)

Phase 3: Secondary Validators (TryKit validator + BounceBan fallback)
  → For "risky" quality scores, validate with secondary sources

Phase 4: Email Guard Enrichment (non-gating)
  → On final valid emails only, enrich with email host provider info

JSON mode (stdin piped):
  echo '{"contacts":[{"first_name":"John","last_name":"Doe","domain":"acme.com","email":"john@acme.com"}]}' | python email_waterfall.py

CLI mode:
  python email_waterfall.py run contacts.csv --output results.csv
  python email_waterfall.py verify john@acme.com

JSON output:
  {
    "results": [
      {
        "first_name": "John", "last_name": "Doe", "domain": "acme.com",
        "input_email": "john@acme.com",
        "valid_email": "john@acme.com", "email_source": "input",
        "email_host": "google.com", "status": "valid", "steps_run": 3
      }
    ],
    "summary": {
      "total": 1, "valid": 1, "catch_all": 0, "unknown": 0
    }
  }
"""

import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional, List

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    httpx = None
    _HAS_HTTPX = False

try:
    import requests as _requests
except ImportError:
    _requests = None


# ── Inline config ────────────────────────────────────────────────────────────

RATE_LIMITS = {
    "millionverifier": 0.5,
    "trykit":          0.5,
    "bounceban":       0.5,
    "leadmagic":       0.5,
    "emailguard":      0.5,
    "icypeas":         0.1,  # 10 calls/sec limit, so 0.1 per call
    "hunter":          0.5,  # plenty of headroom on standard plans
}


def _log(message: str, level: str = "info"):
    """Log to stderr as JSON line."""
    print(json.dumps({"level": level, "message": message}), file=sys.stderr, flush=True)


def progress(current: int, total: int, message: str):
    print(json.dumps({"progress": current, "total": total, "message": message}),
          file=sys.stderr, flush=True)


def _get_key(api_keys: dict, name: str, env_var: Optional[str] = None) -> str:
    """Get API key from dict or environment.

    Short-circuits with an empty string for providers marked dead by the
    pre-flight probe. Callers already handle empty-key as "skip this
    step", so dead providers cleanly drop out without hitting the slow
    DNS-timeout / 401-retry path that was killing per-contact budgets.
    """
    if _is_provider_dead(name):
        return ""
    key = api_keys.get(name, "")
    if not key and env_var:
        key = os.getenv(env_var, "")
    if not key:
        key = os.getenv(f"{name.upper()}_API_KEY", "")
    return key


def _get_icypeas_auth(api_keys: dict) -> str:
    """Return the IcyPeas Authorization header value: '<api_key>:<api_secret>'.

    IcyPeas requires both credentials combined in the Authorization header.
    Falls back to just the API key if no secret is configured (for backwards
    compat), but will 401 without both.
    """
    key = _get_key(api_keys, "icypeas")
    if not key:
        return ""
    # .env uses ICYPEAS_SECRET_KEY; ICYPEAS_API_SECRET kept as a fallback alias.
    secret = (
        api_keys.get("icypeas_secret", "")
        or os.getenv("ICYPEAS_SECRET_KEY", "")
        or os.getenv("ICYPEAS_API_SECRET", "")
    )
    if secret:
        return f"{key}:{secret}"
    return key


# ── Dead-provider tracker (populated by pre-flight) ──────────────────────────
#
# When pre-flight's probe fails for an optional provider (TryKit host gone,
# IcyPeas account dead, etc.) we add it here. Subsequent _get_key() calls
# for that provider return empty, so every step function short-circuits
# instead of hitting a 5-10s DNS / TCP / auth timeout. This was the silent
# 60-second per-contact budget killer surfaced by run 3 (54 / 60 rejected
# contacts had steps_run=0 — they timed out on dead TryKit before ever
# reaching the format guesser).

_DEAD_PROVIDERS: set = set()
_DEAD_PROVIDERS_LOCK = threading.Lock()


def _reset_dead_providers() -> None:
    with _DEAD_PROVIDERS_LOCK:
        _DEAD_PROVIDERS.clear()


def _mark_provider_dead(name: str) -> None:
    if not name:
        return
    with _DEAD_PROVIDERS_LOCK:
        _DEAD_PROVIDERS.add(name.lower())


def _is_provider_dead(name: str) -> bool:
    if not name:
        return False
    with _DEAD_PROVIDERS_LOCK:
        return name.lower() in _DEAD_PROVIDERS


# ── Per-API call/outcome tally (for the run analysis) ────────────────────────
# Counts exactly what each waterfall API returned across the batch — e.g.
# MillionVerifier verdict split (good/ok/risky/bad/error) and each secondary
# validator's outcomes — so the analysis md can report exact per-API numbers
# (the per-source breakdown only shows the *winning* finder per validated email).
import collections as _collections

_API_STATS: "_collections.Counter" = _collections.Counter()
_API_STATS_LOCK = threading.Lock()


def _reset_api_stats() -> None:
    with _API_STATS_LOCK:
        _API_STATS.clear()


def _api_stat(key: str) -> None:
    if not key:
        return
    with _API_STATS_LOCK:
        _API_STATS[key] += 1


def _api_stats_snapshot() -> dict:
    with _API_STATS_LOCK:
        return dict(_API_STATS)


# Hard-required providers. Without these the waterfall cannot triage email
# quality and would otherwise silently mark every contact as 'unknown',
# producing a zero-valid run that looks successful in unattended pipelines.
REQUIRED_WATERFALL_KEYS = ("millionverifier",)


def _check_required_waterfall_keys(api_keys: dict) -> None:
    """Raise RuntimeError listing any missing required keys.

    Centralizes the fail-fast check so unattended runs surface missing
    credentials at run start instead of completing 'successfully' with
    every contact marked unknown.
    """
    missing = []
    for name in REQUIRED_WATERFALL_KEYS:
        if not _get_key(api_keys, name):
            missing.append(name)
    if missing:
        env_hints = ", ".join(f"{n.upper()}_API_KEY" for n in missing)
        raise RuntimeError(
            f"email_waterfall: missing required API key(s): {missing}. "
            f"Set the corresponding env var(s): {env_hints}. "
            f"Without these the waterfall cannot triage candidates and would "
            f"silently mark every contact as 'unknown'."
        )


# Providers we know how to probe + the endpoints used. Probes are chosen to be
# either credit-free (auth-check / balance endpoints) or single-credit cheap.
_PREFLIGHT_TIMEOUT = 5.0


def _probe_provider(name: str, api_key: str) -> tuple:
    """Return (ok: bool, message: str) after a tiny live request.

    The message is what we display in the pre-flight log. For dead keys we
    surface the upstream error verbatim (truncated) so the operator can
    identify whether it's an auth, billing, or rate-limit problem.
    """
    if not api_key:
        return (False, "no API key configured")

    try:
        if name == "millionverifier":
            # Free credits endpoint — does not consume credits.
            data = _http_get(
                "https://api.millionverifier.com/api/v3/credits/",
                {"api": api_key},
                timeout=_PREFLIGHT_TIMEOUT,
            )
            credits = data.get("credits", data.get("credit"))
            if credits is not None:
                return (True, f"credits remaining: {credits}")
            return (True, "auth ok")

        if name == "leadmagic":
            # Free credits endpoint.
            data = _http_get(
                "https://api.leadmagic.io/v1/credits",
                None,
                {"X-API-Key": api_key},
                timeout=_PREFLIGHT_TIMEOUT,
            )
            return (True, f"credits remaining: {data.get('credits', '?')}")

        if name == "trykit":
            # Verify a throwaway email (uses ~1 credit on success, 0 on auth failure).
            # realtime=True returns synchronously instead of via webhook. Host is
            # api.trykitt.ai (api.trykitapp.com is a dead domain — DNS getaddrinfo fails).
            data = _http_post(
                "https://api.trykitt.ai/job/verify_email",
                {"email": "preflight@example.com", "realtime": True},
                {"x-api-key": api_key, "Content-Type": "application/json"},
                timeout=_PREFLIGHT_TIMEOUT,
            )
            return (True, "auth ok (~1 credit consumed)")

        if name == "icypeas":
            # IcyPeas requires Authorization: <api_key>:<api_secret>
            # api_key here is already the combined string from _get_icypeas_auth
            data = _http_post(
                "https://app.icypeas.com/api/email-search",
                {"firstname": "Preflight", "lastname": "Probe", "domainOrCompany": "example.com"},
                {"Authorization": api_key, "Content-Type": "application/json"},
                timeout=_PREFLIGHT_TIMEOUT,
            )
            if data.get("success") is False:
                err = data.get("error") or data.get("message") or "unknown"
                return (False, f"icypeas refused: {err}")
            return (True, "auth ok (~1 credit consumed)")

        if name == "hunter":
            # Free /account endpoint — reports remaining searches + verifications.
            data = _http_get(
                "https://api.hunter.io/v2/account",
                {"api_key": api_key},
                timeout=_PREFLIGHT_TIMEOUT,
            )
            d = data.get("data") or {}
            reqs = d.get("requests") or {}
            searches = reqs.get("searches") or {}
            verifs = reqs.get("verifications") or {}
            s_avail = searches.get("available")
            s_used = searches.get("used") or 0
            v_avail = verifs.get("available")
            v_used = verifs.get("used") or 0
            plan = d.get("plan_name", "?")
            if s_avail is not None or v_avail is not None:
                parts = []
                if s_avail is not None:
                    parts.append(f"searches: {s_avail - s_used} left")
                if v_avail is not None:
                    parts.append(f"verifications: {v_avail - v_used} left")
                return (True, f"plan={plan} | {' | '.join(parts)}")
            return (True, f"auth ok (plan={plan})")

        if name == "bounceban":
            data = _http_get(
                "https://api.bounceban.com/v1/verify/single",
                {"email": "preflight@example.com"},
                {"Authorization": api_key},
                timeout=_PREFLIGHT_TIMEOUT,
            )
            return (True, "auth ok (~1 credit consumed)")

        if name == "emailguard":
            data = _http_get(
                "https://api.emailguard.io/v1/verify",
                {"email": "preflight@example.com"},
                {"Authorization": f"Bearer {api_key}"},
                timeout=_PREFLIGHT_TIMEOUT,
            )
            return (True, "auth ok (~1 credit consumed)")

        return (False, "unknown provider (no probe defined)")

    except Exception as e:
        # _http_* helpers raise httpx HTTPStatusError on 4xx/5xx. Extract
        # the status + a short body snippet for actionable feedback.
        msg = str(e)
        # Try to pull a snippet of the response body if it was an HTTP error
        if "Client error '4" in msg or "Server error '5" in msg:
            return (False, msg.split("\n")[0][:160])
        return (False, f"{type(e).__name__}: {msg[:160]}")


# Optional providers we still probe so dead keys surface loudly. Required
# providers (millionverifier) are already enforced in _check_required_waterfall_keys.
_PREFLIGHT_OPTIONAL_PROVIDERS = ("trykit", "leadmagic", "icypeas", "hunter", "bounceban", "emailguard")


# OpenRouter model IDs used in primary / fallback positions across the
# pipeline. Each is probed individually because OpenRouter supports per-key
# model restrictions (a key may work for some models and 403 for others).
#
# Sources (kept in sync manually when models change):
#   contact_qa.py        PRIMARY_MODEL / FALLBACK_MODEL
#   openrouter_client.py MODELS dict (extract / reason / generate)
#   campaign_research.py model_chain in score_contacts*
#   client_enrichment.py DEFAULT_MODEL
_OPENROUTER_MODELS_TO_PROBE = (
    ("contact_qa.primary",        "google/gemini-2.5-flash-lite"),
    ("contact_qa.fallback",       "anthropic/claude-haiku-4.5"),
    ("router.extract",            "google/gemini-flash-1.5"),
    ("router.reason",             "anthropic/claude-sonnet-4"),
    ("scoring.chain[0]",          "openai/gpt-5.4-nano"),
    ("scoring.chain[2]",          "anthropic/claude-sonnet-4-6"),
)


def _probe_openrouter_model(model: str, api_key: str) -> tuple:
    """Probe a single OpenRouter model with a 1-token prompt.

    Returns (ok, message). The cost is microscopic (~$0.0001 per probe)
    but lets us detect: (a) dead OpenRouter key, (b) key-level model
    restrictions where some models 403 while others succeed.
    """
    if not api_key:
        return (False, "no OPENROUTER_API_KEY configured")

    try:
        data = _http_post(
            "https://openrouter.ai/api/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
                "temperature": 0,
            },
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=_PREFLIGHT_TIMEOUT,
        )
        if isinstance(data, dict) and (data.get("choices") or data.get("id")):
            return (True, "auth ok")
        return (False, f"unexpected response shape: {str(data)[:120]}")
    except Exception as e:
        msg = str(e)
        if "401" in msg:
            return (False, f"401 — key auth failed (rotate / check OpenRouter dashboard)")
        if "402" in msg:
            return (False, f"402 — insufficient credits on OpenRouter account")
        if "403" in msg:
            return (False, f"403 — model restricted for this key (check key model permissions)")
        if "404" in msg:
            return (False, f"404 — model ID not found on OpenRouter (renamed/retired?)")
        if "429" in msg:
            return (False, f"429 — rate limited (transient, usually self-heals)")
        return (False, f"{type(e).__name__}: {msg[:160]}")


def _preflight_provider_health(api_keys: dict) -> dict:
    """Probe every configured waterfall provider and surface ✗ keys loudly.

    Runs once at the start of run_waterfall_batch. Required providers
    failing the probe raise RuntimeError (this previously was a silent
    "every contact marked unknown" failure mode). Optional providers
    failing just log a warning so the operator knows a paid integration
    is contributing zero — the rest of the cascade falls through cleanly.

    Skip with WATERFALL_PREFLIGHT=false (default: enabled).
    """
    if os.getenv("WATERFALL_PREFLIGHT", "true").strip().lower() in ("false", "0", "no"):
        _log("Pre-flight provider probes skipped (WATERFALL_PREFLIGHT=false)")
        return {}

    _log("Pre-flight: probing waterfall providers (auth + credits)...")
    results: dict = {}

    # Required first — fail fast if these are dead.
    for name in REQUIRED_WATERFALL_KEYS:
        key = _get_key(api_keys, name)
        ok, msg = _probe_provider(name, key)
        marker = "OK" if ok else "FAIL"
        _log(f"  [{marker}] {name:<16} (required)  {msg}", "info" if ok else "error")
        results[name] = (ok, msg)
        if not ok:
            raise RuntimeError(
                f"email_waterfall: required provider {name!r} failed pre-flight: {msg}. "
                f"Fix the key or unblock the account at the provider's dashboard "
                f"before re-running. Without {name} the waterfall cannot verify "
                f"candidates and would mark every contact 'unknown'."
            )

    # Optional providers — warn but continue. Dead ones get marked so
    # downstream step functions short-circuit instead of waiting on DNS
    # timeouts (saves 5-10s per contact per dead provider).
    for name in _PREFLIGHT_OPTIONAL_PROVIDERS:
        # IcyPeas uses a combined key:secret auth string
        key = _get_icypeas_auth(api_keys) if name == "icypeas" else _get_key(api_keys, name)
        if not key:
            _log(f"  [--] {name:<16} (optional)  not configured")
            results[name] = (False, "no key")
            continue
        ok, msg = _probe_provider(name, key)
        marker = "OK" if ok else "WARN"
        level = "info" if ok else "warning"
        if not ok:
            _mark_provider_dead(name)
            msg = f"DEAD — calls to {name} are now SKIPPED for this batch. {msg}"
        _log(f"  [{marker}] {name:<16} (optional)  {msg}", level)
        results[name] = (ok, msg)

    # OpenRouter models — probe each primary/fallback model used downstream
    # (contact QA, scoring, email generation, client enrichment). A working
    # key for one model doesn't guarantee another model is accessible —
    # OpenRouter supports per-key model restrictions.
    or_key = os.getenv("OPENROUTER_API_KEY", "")
    if not or_key:
        _log("  [--] openrouter       (LLM)       not configured — Contact QA + scoring + email gen will fail", "warning")
        results["openrouter"] = (False, "no key")
    else:
        _log("  -- OpenRouter LLM models (primary + fallback chains) --")
        any_or_fail = False
        for role, model in _OPENROUTER_MODELS_TO_PROBE:
            ok, msg = _probe_openrouter_model(model, or_key)
            marker = "OK" if ok else "WARN"
            level = "info" if ok else "warning"
            tag = f"openrouter:{role}"
            if not ok:
                any_or_fail = True
                msg = f"{model} — {msg}"
            else:
                msg = f"{model} — {msg}"
            _log(f"  [{marker}] {tag:<28} {msg}", level)
            results[tag] = (ok, msg)
        if any_or_fail:
            _log(
                "  ! One or more OpenRouter models failed probe — Contact QA / scoring / "
                "email gen will partially or fully fall through to error placeholders.",
                "warning",
            )

    return results


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_get(url: str, params: dict = None, headers: dict = None, timeout: float = 15.0) -> dict:
    if _HAS_HTTPX:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, params=params or {}, headers=headers or {})
            resp.raise_for_status()
            return resp.json()
    elif _requests:
        resp = _requests.get(url, params=params or {}, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Neither httpx nor requests installed")


def _http_post(url: str, payload: dict = None, headers: dict = None, timeout: float = 15.0) -> dict:
    if _HAS_HTTPX:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload or {}, headers=headers or {})
            resp.raise_for_status()
            return resp.json()
    elif _requests:
        resp = _requests.post(url, json=payload or {}, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Neither httpx nor requests installed")


# ── WaterfallResult dataclass ────────────────────────────────────────────────

@dataclass
class WaterfallResult:
    """Tracks the full state of a contact through the waterfall."""
    first_name: str = ""
    last_name: str = ""
    domain: str = ""
    company_name: str = ""
    input_email: str = ""

    # Pass-through fields from upstream (sourcing/QA/ICP scoring) preserved for
    # downstream message enrichment. Without these, column_pipeline runs without
    # job titles and falls back to generic copy.

    # Contact identity
    title: str = ""
    full_name: str = ""
    linkedin_url: str = ""
    twitter_url: str = ""
    phone: str = ""
    picture_url: str = ""
    aiark_id: str = ""

    # Role context
    seniority: str = ""
    department: str = ""

    # Contact location (person's location, not company)
    location: str = ""            # e.g. "Hillsborough, New Jersey"
    contact_city: str = ""
    contact_state: str = ""
    contact_country: str = ""

    # Company enrichment (from DiscoLike or AI.ARK company data)
    company_description: str = ""
    company_city: str = ""
    company_state: str = ""
    company_country: str = ""
    company_employees: str = ""
    company_industry: str = ""
    company_similarity: str = ""  # DiscoLike similarity score
    company_linkedin_url: str = ""

    # Pipeline metadata
    campaign_slug: str = ""
    tier: str = ""
    icp_tier: str = ""
    icp_score: str = ""
    profile_summary: str = ""     # LinkedIn bio — valuable for personalisation
    is_hiring: str = ""
    open_to_work: str = ""

    # Outputs
    valid_email: str = ""
    email_source: str = ""
    email_host: str = ""
    status: str = "pending"      # pending, valid, invalid, catch_all, unknown
    steps_run: int = 0
    steps_log: List[str] = field(default_factory=list)

    # Intermediate: candidate emails found by finders
    candidate_emails: List[dict] = field(default_factory=list)

    # Error tracking
    errors: List[str] = field(default_factory=list)

    # Extra fields from the source CSV that don't map to a named attribute.
    # Populated in run_waterfall so nothing from the AI-ARK export is dropped.
    pass_through: dict = field(default_factory=dict)

    def _lock(self):
        lk = getattr(self, "_lk", None)
        if lk is None:
            lk = threading.Lock()
            object.__setattr__(self, "_lk", lk)
        return lk

    def add_candidate(self, email: str, source: str, confidence: float = 0.5):
        """Add a candidate email from a finder step (thread-safe)."""
        if not email:
            return
        with self._lock():
            if email not in [c["email"] for c in self.candidate_emails]:
                self.candidate_emails.append({
                    "email": email,
                    "source": source,
                    "confidence": confidence,
                })

    def log_step(self, step_name: str, result: str):
        with self._lock():
            self.steps_run += 1
            self.steps_log.append(f"{step_name}: {result}")

    def to_output(self) -> dict:
        """Return the final output dict — all fields needed for outreach personalisation."""
        return {
            # Contact identity
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name or f"{self.first_name} {self.last_name}".strip(),
            "title": self.title,
            "seniority": self.seniority,
            "department": self.department,
            "linkedin_url": self.linkedin_url,
            "twitter_url": self.twitter_url,
            "phone": self.phone,
            "picture_url": self.picture_url,
            "aiark_id": self.aiark_id,
            "profile_summary": self.profile_summary,

            # Contact location
            "location": self.location,
            "contact_city": self.contact_city,
            "contact_state": self.contact_state,
            "contact_country": self.contact_country,

            # Contact signals
            "is_hiring": self.is_hiring,
            "open_to_work": self.open_to_work,

            # Company
            "domain": self.domain,
            "company_name": self.company_name,
            "company_linkedin_url": self.company_linkedin_url,
            "company_description": self.company_description,
            "company_city": self.company_city,
            "company_state": self.company_state,
            "company_country": self.company_country,
            "company_employees": self.company_employees,
            "company_industry": self.company_industry,
            "company_similarity": self.company_similarity,

            # Email validation outputs
            "input_email": self.input_email,
            "valid_email": self.valid_email,
            "email_source": self.email_source,
            "email_host": self.email_host,
            "status": self.status,
            "steps_run": self.steps_run,
            "candidates_found": len(self.candidate_emails),

            # Pipeline metadata
            "campaign_slug": self.campaign_slug,
            "tier": self.tier,
            "icp_tier": self.icp_tier,
            "icp_score": self.icp_score,

            # Extra fields from source CSV (pass-through — named fields above win on conflict)
            **self.pass_through,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1: EMAIL FINDERS (Steps 1-5)
# TryKit, LeadMagic only — NO pattern guessing
# ══════════════════════════════════════════════════════════════════════════════

def step_01_existing_email(result: WaterfallResult, api_keys: dict):
    """Step 1: Check if contact already has an email."""
    if result.input_email:
        result.add_candidate(result.input_email, "input", confidence=0.95)
        result.log_step("existing_email", f"has input email: {result.input_email}")
    else:
        result.log_step("existing_email", "no input email")


def step_02_trykit_find(result: WaterfallResult, api_keys: dict):
    """Step 2: TryKit email finder."""
    key = _get_key(api_keys, "trykit")
    if not key:
        result.log_step("trykit_find", "skipped (no key)")
        return

    try:
        full_name = f"{result.first_name} {result.last_name}".strip()
        payload = {
            "fullName": full_name,
            "domain": result.domain,
            "realtime": True,
        }
        if result.linkedin_url:
            payload["linkedinStandardProfileURL"] = result.linkedin_url
        data = _http_post(
            "https://api.trykitt.ai/job/find_email",
            payload,
            {"x-api-key": key, "Content-Type": "application/json"},
        )
        email = data.get("email", "")
        if email and email != "no-results-found" and "@" in email:
            result.add_candidate(email, "trykit", confidence=0.80)
        result.log_step("trykit_find", f"found: {email or 'none'}")
        time.sleep(RATE_LIMITS.get("trykit", 0.5))
    except Exception as e:
        result.log_step("trykit_find", f"error: {e}")
        result.errors.append(f"trykit_find: {e}")


def step_03_leadmagic_find(result: WaterfallResult, api_keys: dict):
    """Step 3: LeadMagic email finder."""
    key = _get_key(api_keys, "leadmagic")
    if not key:
        result.log_step("leadmagic_find", "skipped (no key)")
        return

    try:
        data = _http_post(
            "https://api.leadmagic.io/v1/people/email-finder",
            {"first_name": result.first_name, "last_name": result.last_name, "domain": result.domain},
            {"X-API-Key": key, "Content-Type": "application/json"},
        )
        email = data.get("email", "")
        if email and "@" in email:
            result.add_candidate(email, "leadmagic", confidence=0.78)
        result.log_step("leadmagic_find", f"found: {email or 'none'}")
        time.sleep(RATE_LIMITS.get("leadmagic", 0.5))
    except Exception as e:
        result.log_step("leadmagic_find", f"error: {e}")
        result.errors.append(f"leadmagic_find: {e}")


def _icypeas_poll_search(
    search_id: str,
    api_key: str,
    max_attempts: int = 6,
    initial_delay: float = 0.8,
) -> Optional[dict]:
    """Poll IcyPeas for an async search result.

    IcyPeas submissions return an `_id`; the actual results live behind a
    poll. POST `{id: <search_id>}` (note: NOT `_id` — the API rejects that
    field name with a ModeOrIdShouldBeUsedError) to
    /api/bulk-single-searchs/read. Response shape:
        { success: true,
          items: [
            { _id, status: "DONE" | "NOT_FOUND" | "NONE" | ...,
              results: { firstname, lastname, emails: [{email, ...}], ... } } ] }

    Returns the first item dict once status reaches a terminal value, else
    None on timeout. Light exponential backoff (~0.8 → 5s), ~10s wall max.
    """
    delay = initial_delay
    for _ in range(max_attempts):
        time.sleep(delay)
        try:
            data = _http_post(
                "https://app.icypeas.com/api/bulk-single-searchs/read",
                {"id": search_id},
                {"Authorization": api_key, "Content-Type": "application/json"},
            )
        except Exception:
            delay = min(delay * 1.5, 5.0)
            continue

        if not data.get("success"):
            return None

        items = data.get("items") or []
        if not items:
            delay = min(delay * 1.5, 5.0)
            continue

        item = items[0]
        status = (item.get("status") or "").upper()
        # Terminal states observed in IcyPeas responses
        if status in ("DONE", "COMPLETED", "FOUND", "NOT_FOUND", "FAILED", "ERROR"):
            return item
        # "NONE" (initial) and "IN_PROGRESS" — keep polling
        delay = min(delay * 1.5, 5.0)

    return None


def _icypeas_extract_email(item: dict) -> Optional[str]:
    """Pull the first email out of an IcyPeas finder result.

    Real response shape (verified live 2026-06-03):
        { status: "DONE" | "NOT_FOUND",
          results: { emails: [{email: "tim.cook@apple.com", ...}], ... } }
    NOT_FOUND items return None (their emails array is empty).
    """
    if not isinstance(item, dict):
        return None
    if (item.get("status") or "").upper() == "NOT_FOUND":
        return None
    results = item.get("results")
    if not isinstance(results, dict):
        return None
    emails = results.get("emails")
    if not isinstance(emails, list) or not emails:
        return None
    for entry in emails:
        if isinstance(entry, dict):
            v = entry.get("email")
            if isinstance(v, str) and "@" in v:
                return v
        elif isinstance(entry, str) and "@" in entry:
            return entry
    return None


def _icypeas_extract_certainty(item: dict) -> Optional[str]:
    """Map an IcyPeas verification item into our 'valid'/'catch_all'/'invalid' vocabulary.

    IcyPeas verification response uses the same envelope as finder:
        { status: "DONE" | "NOT_FOUND" | "FAILED",
          results: { emails: [{email, certainty, ...}], ... } }

    Heuristic:
      - status NOT_FOUND or empty emails → "invalid"
      - emails[].certainty / state mapped via _normalize
      - status DONE with email present but no certainty signal → "valid"
        (IcyPeas often returns the email without a separate verdict field
        when verification succeeds)
      - anything else / unrecognized → None (cascade falls through)
    """
    if not isinstance(item, dict):
        return None
    item_status = (item.get("status") or "").upper()

    def _normalize(raw: str) -> Optional[str]:
        s = (raw or "").lower()
        if s in ("ok", "valid", "verified", "deliverable", "true"):
            return "valid"
        if s in ("catch_all", "catch-all", "accept_all", "acceptall"):
            return "catch_all"
        if s in ("invalid", "undeliverable", "bounce", "bad", "false", "not_found"):
            return "invalid"
        return None

    # Terminal: NOT_FOUND / FAILED / ERROR → invalid
    if item_status in ("NOT_FOUND", "FAILED", "ERROR"):
        return "invalid"

    results = item.get("results")
    if isinstance(results, dict):
        emails = results.get("emails") or []
        if not emails:
            return "invalid"
        # Walk emails looking for an explicit verdict signal
        for entry in emails:
            if not isinstance(entry, dict):
                continue
            for key in ("certainty", "state", "status", "verdict", "result"):
                v = entry.get(key)
                if isinstance(v, str):
                    verdict = _normalize(v)
                    if verdict:
                        return verdict
            # Boolean flags some providers use
            if entry.get("isCatchAll") is True or entry.get("catch_all") is True:
                return "catch_all"
            if entry.get("verified") is True:
                return "valid"
        # DONE state with email but no explicit verdict — treat as valid
        # (IcyPeas's email-verification endpoint is implicit-pass-on-DONE)
        if item_status in ("DONE", "COMPLETED", "FOUND"):
            return "valid"
    return None


def step_04_icypeas_find(result: WaterfallResult, api_keys: dict):
    """Step 4: IcyPeas email finder (async — submit then poll for result)."""
    # Check feature flag: only run if explicitly enabled via env or if no strong candidates yet
    use_icypeas = os.getenv("WATERFALL_ICYPEAS_FINDER", "auto").strip().lower()
    if use_icypeas == "false":
        result.log_step("icypeas_find", "skipped (disabled)")
        return

    # Auto mode: only run if we have < 2 candidates (to control spend)
    if use_icypeas == "auto" and len(result.candidate_emails) >= 2:
        result.log_step("icypeas_find", f"skipped (auto mode, {len(result.candidate_emails)} candidates exist)")
        return

    key = _get_icypeas_auth(api_keys)
    if not key:
        result.log_step("icypeas_find", "skipped (no key)")
        return

    try:
        payload = {
            "firstname": result.first_name or "",
            "lastname": result.last_name or "",
            "domainOrCompany": result.domain,
        }
        data = _http_post(
            "https://app.icypeas.com/api/email-search",
            payload,
            {"Authorization": key, "Content-Type": "application/json"},
        )

        if not data.get("success"):
            result.log_step("icypeas_find", f"API error: {data.get('error', 'unknown')}")
            return

        search_id = data.get("item", {}).get("_id", "")
        if not search_id:
            result.log_step("icypeas_find", "no search_id returned")
            return

        # Poll the async endpoint until the search completes (was a no-op before
        # — IcyPeas was contributing zero candidates because we never read
        # results back).
        poll_item = _icypeas_poll_search(search_id, key)
        if not poll_item:
            result.log_step("icypeas_find", f"timeout polling search_id={search_id}")
            return

        email = _icypeas_extract_email(poll_item)
        if email:
            result.add_candidate(email, "icypeas", confidence=0.75)
            result.log_step("icypeas_find", f"found: {email}")
            _log(f"[icypeas:HIT] {result.first_name} {result.last_name} @ {result.domain} -> {email}")
        else:
            status = (poll_item.get("status") or "").upper()
            result.log_step("icypeas_find", f"no email in result (status={status})")

        time.sleep(RATE_LIMITS.get("icypeas", 0.1))
    except Exception as e:
        result.log_step("icypeas_find", f"error: {e}")
        result.errors.append(f"icypeas_find: {e}")


def step_04b_hunter_find(result: WaterfallResult, api_keys: dict):
    """Hunter.io email finder — synchronous, returns best-guess email + score.

    Hunter's email-finder endpoint takes domain + first_name + last_name and
    returns a single best-match email with a 0-100 confidence score. Their
    plans cover 500-50000 searches/month so this is generally the safest
    paid-coverage option.

    Skipped silently if the key is missing or marked dead by pre-flight.
    """
    key = _get_key(api_keys, "hunter")
    if not key:
        result.log_step("hunter_find", "skipped (no key / dead)")
        return
    try:
        domain = _clean_domain(result.domain) if result.domain else ""
        if not domain or not result.first_name or not result.last_name:
            result.log_step("hunter_find", "skipped (need first, last, domain)")
            return
        data = _http_get(
            "https://api.hunter.io/v2/email-finder",
            {
                "domain": domain,
                "first_name": result.first_name,
                "last_name": result.last_name,
                "api_key": key,
            },
            timeout=15.0,
        )
        d = data.get("data") or {}
        email = d.get("email")
        score = d.get("score") or 0
        if email and "@" in email:
            # Hunter score is 0-100. Translate to our 0-1 confidence band,
            # floored at 0.5 so we still trust low-score finds (verification
            # is the final gate anyway).
            confidence = max(0.5, min(0.95, score / 100.0))
            result.add_candidate(email, "hunter", confidence=confidence)
            result.log_step("hunter_find", f"found: {email} (score={score})")
            _log(f"[hunter:HIT] {result.first_name} {result.last_name} @ {domain} -> {email} (score={score})")
        else:
            result.log_step("hunter_find", "no email returned")
        time.sleep(RATE_LIMITS.get("hunter", 0.5))
    except Exception as e:
        result.log_step("hunter_find", f"error: {e}")
        result.errors.append(f"hunter_find: {e}")


def step_05_dedup_candidates(result: WaterfallResult, api_keys: dict):
    """Step 5: Deduplicate and rank candidates by confidence."""
    seen = {}
    for c in result.candidate_emails:
        email = c["email"].lower().strip()
        if email not in seen or c["confidence"] > seen[email]["confidence"]:
            seen[email] = c
    result.candidate_emails = sorted(seen.values(), key=lambda x: -x["confidence"])
    result.log_step("dedup", f"{len(result.candidate_emails)} unique candidates")


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2: MILLION VERIFIER v3 QUALITY TRIAGE (Steps 6-8)
# ══════════════════════════════════════════════════════════════════════════════

def _is_transient_http_error(exc: Exception) -> bool:
    """True for errors worth retrying — timeouts/connection drops and HTTP 429/5xx.
    Deliberately False for 4xx (esp. 401/403/400): retrying a bad key/request just
    re-burns the per-contact budget (the reason the old retry path was removed)."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if code is not None:
        return code == 429 or 500 <= int(code) < 600
    name = type(exc).__name__.lower()
    return any(t in name for t in ("timeout", "connect", "transport", "readerror", "writeerror", "remoteprotocol"))


def _verify_millionverifier_quality(email: str, api_key: str, max_retries: int = 2) -> Optional[dict]:
    """Get MV v3 response with quality field. Returns dict or None on error.

    Retries only TRANSIENT failures (timeouts / 429 / 5xx) with a short backoff —
    a transient MV blip otherwise drops the contact straight to 'unknown'. Auth/4xx
    errors are not retried (bounded per-contact cost)."""
    for attempt in range(max_retries + 1):
        try:
            data = _http_get(
                "https://api.millionverifier.com/api/v3/",
                {"api": api_key, "email": email},
            )
            quality = (data.get("quality") or "").lower()  # good, ok, risky, bad
            _api_stat(f"millionverifier:{quality or 'empty'}")
            return {
                "quality": quality,
                "result": (data.get("result") or "").lower(),    # ok, invalid, catch_all
                "raw": data
            }
        except Exception as exc:  # noqa: BLE001
            if attempt < max_retries and _is_transient_http_error(exc):
                time.sleep(0.6 * (attempt + 1))
                continue
            _api_stat("millionverifier:error")
            return None
    _api_stat("millionverifier:error")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3: SECONDARY VALIDATORS (Steps 9-10)
# TryKit Validator + BounceBan Fallback
# ══════════════════════════════════════════════════════════════════════════════

def _verify_trykit_validator(email: str, api_key: str) -> Optional[str]:
    """TryKit email validator (not finder). Returns 'valid', 'catch_all', 'invalid', or None.

    POST api.trykitt.ai/job/verify_email with realtime=True (synchronous). Host/auth
    differ from the finder's — the old api.trykitapp.com host is dead (DNS fails), which
    is why this validator silently never ran and risky emails went unconfirmed.
    """
    try:
        data = _http_post(
            "https://api.trykitt.ai/job/verify_email",
            {"email": email, "realtime": True},
            {"x-api-key": api_key, "Content-Type": "application/json"},
        )
        validity = (data.get("validity") or "").lower()
        if validity in ("valid", "valid-risky", "deliverable"):
            return "valid"
        if validity in ("catch-all", "catch_all", "accept-all", "accept_all", "acceptall"):
            return "catch_all"
        if validity == "invalid":
            return "invalid"
        # "unknown" / "risky" / other → None so the next validator (BounceBan/…) can try
        return None
    except Exception:
        return None


def _secondary_verify(email: str, api_keys: dict) -> Optional[str]:
    """Secondary validator chain: TryKit → BounceBan (strictly only if TryKit=invalid).

    Rules:
      - TryKit 'valid'/'valid-risky' → return 'valid' immediately, BounceBan skipped
      - TryKit 'catch_all'           → return 'catch_all', BounceBan skipped
      - TryKit 'invalid'             → escalate to BounceBan as final arbiter
      - TryKit None/inconclusive     → return None (caller trusts MV's verdict alone)
      - BounceBan only ever runs when TryKit explicitly returned 'invalid'

    IcyPeas, LeadMagic, and Hunter are finder-only — they are NOT used as validators.
    Returns 'valid' / 'catch_all' / 'invalid' / None.
    """
    trykit_key = _get_key(api_keys, "trykit")
    trykit_verdict = None
    if trykit_key:
        try:
            trykit_verdict = _verify_trykit_validator(email, trykit_key)
        except Exception:
            _api_stat("secondary:trykit:error")
        else:
            _api_stat(f"secondary:trykit:{trykit_verdict or 'inconclusive'}")

    if trykit_verdict == "valid":
        return "valid"
    if trykit_verdict == "catch_all":
        return "catch_all"

    if trykit_verdict == "invalid":
        # TryKit confirmed bad — give BounceBan one final say
        bounceban_key = _get_key(api_keys, "bounceban")
        if bounceban_key:
            try:
                bb_verdict = _verify_bounceban_validator(email, bounceban_key)
            except Exception:
                _api_stat("secondary:bounceban:error")
                bb_verdict = None
            else:
                _api_stat(f"secondary:bounceban:{bb_verdict or 'inconclusive'}")
            if bb_verdict is not None:
                return bb_verdict
        return "invalid"  # TryKit said invalid, BounceBan unavailable — trust TryKit

    # TryKit was inconclusive (None) or unavailable — return None so the cascade
    # falls through to the modular fallback (trust MV's verdict alone).
    return None


def _verify_hunter_validator(email: str, api_key: str) -> Optional[str]:
    """Hunter.io email verifier. Returns 'valid', 'catch_all', 'invalid', or None.

    Hunter response fields used:
      data.status: valid | invalid | accept_all | webmail | disposable | unknown
      data.result: deliverable | undeliverable | risky
    """
    try:
        data = _http_get(
            "https://api.hunter.io/v2/email-verifier",
            {"email": email, "api_key": api_key},
            timeout=10.0,
        )
        d = data.get("data") or {}
        status = (d.get("status") or "").lower()
        result = (d.get("result") or "").lower()
        if status == "valid" or result == "deliverable":
            return "valid"
        if status == "accept_all":
            return "catch_all"
        if status in ("invalid", "disposable") or result == "undeliverable":
            return "invalid"
        # webmail / unknown / risky → None so the next validator can try
        return None
    except Exception:
        return None


def _verify_leadmagic_validator(email: str, api_key: str) -> Optional[str]:
    """LeadMagic email validator (cheap — ~0.25 credit). Returns 'valid',
    'catch_all', 'invalid', or None.

    POST https://api.leadmagic.io/email-validate (no /v1/ prefix; that's the finder),
    X-API-Key header. Response `email_status`: valid | valid_catch_all | catch_all |
    unknown | invalid (→ None for unknown/other so the cascade falls through). Ordered
    before BounceBan so a cheap valid/deliverable confirmation skips the pricier call.
    """
    try:
        data = _http_post(
            "https://api.leadmagic.io/email-validate",
            {"email": email},
            {"X-API-Key": api_key, "Content-Type": "application/json"},
        )
        status = (data.get("email_status") or "").strip().lower()
        if status in ("valid", "valid_catch_all", "deliverable"):
            return "valid"
        if status in ("catch_all", "accept_all"):
            return "catch_all"
        if status == "invalid":
            return "invalid"
        return None  # unknown / disposable / no_mx → inconclusive, try the next verifier
    except Exception:
        return None


def _verify_bounceban_validator(email: str, api_key: str) -> Optional[str]:
    """BounceBan email validator. Returns 'valid', 'catch_all', 'invalid', or None.

    Auth: Authorization header (raw api_key — not Bearer, not query param).
    """
    try:
        data = _http_get(
            "https://api.bounceban.com/v1/verify/single",
            {"email": email},
            {"Authorization": api_key},
        )
        result = (data.get("result") or "").lower()
        if result == "deliverable":
            return "valid"
        elif result == "accept_all":
            return "catch_all"
        else:  # undeliverable, unknown
            return "invalid"
    except Exception:
        return None


def _verify_icypeas_validator(email: str, api_key: str) -> Optional[str]:
    """IcyPeas email verifier. Returns 'valid', 'catch_all', 'invalid', or None.

    IcyPeas verification is async — POST returns an `_id`, results live behind
    a poll. This function does the submit + poll round-trip and parses the
    verdict. Returns None on timeout or unparseable response so the cascade
    falls through to the next validator.
    """
    try:
        data = _http_post(
            "https://app.icypeas.com/api/email-verification",
            {"email": email},
            {"Authorization": api_key, "Content-Type": "application/json"},
        )
        if not data.get("success"):
            return None
        search_id = data.get("item", {}).get("_id", "")
        if not search_id:
            return None

        poll_item = _icypeas_poll_search(search_id, api_key)
        if not poll_item:
            return None
        return _icypeas_extract_certainty(poll_item)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4: EMAIL GUARD ENRICHMENT (Step 11 — non-gating)
# ══════════════════════════════════════════════════════════════════════════════

def _enrich_emailguard(email: str, api_key: str) -> Optional[str]:
    """Email Guard enrichment (non-gating). Returns email host provider or None."""
    try:
        auth = api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}"
        data = _http_get(
            "https://api.emailguard.io/v1/verify",
            {"email": email},
            {"Authorization": auth},
        )
        return data.get("email_host", "")
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CONDITIONAL CASCADE ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1B: FORMAT-GUESSER FALLBACK (Step 7)
# Generates likely email patterns when finders return nothing and verifies
# them through the same MV cascade. Last-resort coverage for the gap between
# AI.ARK-provided emails and what TryKit/LeadMagic can find.
# ══════════════════════════════════════════════════════════════════════════════

# Most common B2B local-part patterns, in their canonical lowercase form.
# `f` = first name, `l` = last name (both lowercased, alphanumerics + dot/underscore preserved).
EMAIL_FORMAT_PATTERNS: list[tuple[str, callable]] = [
    ("first.last", lambda f, l: f"{f}.{l}"),       # john.smith — mid/large default
    ("flast",      lambda f, l: f"{f[:1]}{l}"),    # jsmith     — enterprise default
    ("first",      lambda f, l: f),                # john       — small/startup default
    ("firstlast",  lambda f, l: f"{f}{l}"),        # johnsmith
    ("first_last", lambda f, l: f"{f}_{l}"),       # john_smith
    ("firstl",     lambda f, l: f"{f}{l[:1]}"),    # johns
    ("f.last",     lambda f, l: f"{f[:1]}.{l}"),   # j.smith
    ("last.first", lambda f, l: f"{l}.{f}"),       # smith.john (rare but seen)
    ("last",       lambda f, l: l),                # smith      (rare)
]
_PATTERN_BY_NAME = dict(EMAIL_FORMAT_PATTERNS)

# Thread-safe domain → format cache. Populated when any contact gets a valid
# email (so later contacts at the same domain in the same batch try that
# pattern first). Cleared at the start of run_waterfall_batch.
_DOMAIN_FORMAT_CACHE: dict = {}
_DOMAIN_FORMAT_CACHE_LOCK = threading.Lock()


def _reset_domain_format_cache() -> None:
    with _DOMAIN_FORMAT_CACHE_LOCK:
        _DOMAIN_FORMAT_CACHE.clear()


def _remember_domain_format(domain: str, fmt: str) -> None:
    if not domain or not fmt:
        return
    with _DOMAIN_FORMAT_CACHE_LOCK:
        _DOMAIN_FORMAT_CACHE[domain.lower().strip()] = fmt


def _get_remembered_domain_format(domain: str) -> Optional[str]:
    if not domain:
        return None
    with _DOMAIN_FORMAT_CACHE_LOCK:
        return _DOMAIN_FORMAT_CACHE.get(domain.lower().strip())


def _clean_name_part(s: str) -> str:
    """Lowercase + strip whitespace + drop apostrophes/spaces/hyphens.

    Keeps names like O'Brien → obrien, Mary-Anne → maryanne so the patterns
    produce sensible locals.
    """
    s = (s or "").lower().strip()
    s = s.replace("'", "").replace("-", "").replace(" ", "")
    return s


def _clean_domain(domain: str) -> str:
    """Strip protocol/path/www so we always email <pattern>@<bare-domain>."""
    d = (domain or "").lower().strip()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def _detect_email_format(email: str, first: str, last: str) -> Optional[str]:
    """Detect which named pattern an existing email uses for a person.

    Returns the pattern name (e.g. "first.last") or None if no pattern fits.
    """
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0].lower().strip()
    f = _clean_name_part(first)
    l = _clean_name_part(last)
    if not f or not l:
        return None
    for name, fn in EMAIL_FORMAT_PATTERNS:
        try:
            if fn(f, l) == local:
                return name
        except (IndexError, ValueError):
            continue
    return None


def _parse_employee_count(raw) -> int:
    """Extract numeric headcount from messy inputs ('200', '50-100', '1,000+', '')."""
    if isinstance(raw, int):
        return raw
    s = str(raw or "").replace(",", "").replace("+", "").strip()
    if not s:
        return 0
    if "-" in s:
        s = s.split("-", 1)[0].strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _format_order_for_size(employees: int) -> list[str]:
    """Order patterns by what's most likely for this company size.

    Small (<50):    first@ wins (low collision, founder-era inboxes)
    Mid (50-500):   first.last is standard
    Large (500+):   flast or first.last (collision-resistant + enterprise norm)
    """
    if employees < 50:
        return ["first", "first.last", "flast", "firstlast", "first_last", "firstl", "f.last"]
    if employees < 500:
        return ["first.last", "flast", "first", "firstlast", "first_last", "firstl", "f.last"]
    return ["flast", "first.last", "first", "firstlast", "first_last", "firstl", "f.last"]


def _build_guess_candidates(
    result: WaterfallResult,
    intelligent_first: Optional[str],
) -> list[tuple[str, str]]:
    """Build the ordered (email, pattern_name) list of guesses to try.

    `intelligent_first` (when not None) is prepended to the size-based order.
    Already-tried candidate emails are skipped so we don't re-verify them.
    """
    f = _clean_name_part(result.first_name)
    l = _clean_name_part(result.last_name)
    domain = _clean_domain(result.domain)
    if not f or not l or not domain:
        return []

    employees = _parse_employee_count(result.company_employees)
    order = _format_order_for_size(employees)
    if intelligent_first and intelligent_first in _PATTERN_BY_NAME:
        order = [intelligent_first] + [n for n in order if n != intelligent_first]

    already_tried = {c["email"].lower() for c in result.candidate_emails}
    out: list[tuple[str, str]] = []
    seen_emails: set = set()
    for name in order:
        fn = _PATTERN_BY_NAME.get(name)
        if not fn:
            continue
        try:
            local = fn(f, l)
        except (IndexError, ValueError):
            continue
        if not local:
            continue
        email = f"{local}@{domain}"
        key = email.lower()
        if key in already_tried or key in seen_emails:
            continue
        seen_emails.add(key)
        out.append((email, name))
    return out


def step_07_format_guesser(result: WaterfallResult, api_keys: dict):
    """Last-resort: guess common B2B email patterns and verify them.

    Runs only when the waterfall has produced no valid/catch_all email AND we
    have first + last + domain. Each guess is verified via the standard MV
    cascade. Loop exits on the first 'valid' or (as last fallback) 'catch_all'.

    Logs at every step:
      - format_guesser_init        (size + intelligent_first hint)
      - format_guesser_skipped     (when prerequisites are missing)
      - guess_<pattern>            (per email verified — quality verdict)
      - format_guesser_done        (final outcome: found / exhausted)
    """
    # Format-guesser is OFF by default: the deep-dive showed it's busy but near-zero
    # net yield (it surfaced ~3/520 and ~1/164 final valids) while MV-checking every
    # pattern it tries — most of the waterfall's MillionVerifier spend. Enable it
    # explicitly with the `--guess-emails` CLI flag (which sets WATERFALL_FORMAT_GUESSER)
    # or by exporting WATERFALL_FORMAT_GUESSER=true.
    if os.getenv("WATERFALL_FORMAT_GUESSER", "false").strip().lower() not in ("true", "1", "yes", "on"):
        result.log_step("format_guesser_skipped", "disabled by default (enable with --guess-emails)")
        return

    if result.status in ("valid", "catch_all"):
        return

    f_raw = (result.first_name or "").strip()
    l_raw = (result.last_name or "").strip()
    domain_raw = (result.domain or "").strip()
    if not f_raw or not l_raw or not domain_raw:
        result.log_step(
            "format_guesser_skipped",
            f"missing name/domain (first={bool(f_raw)} last={bool(l_raw)} domain={bool(domain_raw)})",
        )
        return

    mv_key = _get_key(api_keys, "millionverifier")
    if not mv_key:
        result.log_step("format_guesser_skipped", "no MV key (cannot verify guesses)")
        return

    # Intelligence: prefer a format detected on this contact's own input
    # email; else a format already seen on this domain earlier in the batch;
    # else fall through to the size-based default order.
    intelligent_first: Optional[str] = None
    intelligent_source = "size_default"
    if result.input_email:
        fmt = _detect_email_format(result.input_email, f_raw, l_raw)
        if fmt:
            intelligent_first = fmt
            intelligent_source = f"input_email_format={fmt}"
    if intelligent_first is None:
        cached = _get_remembered_domain_format(domain_raw)
        if cached:
            intelligent_first = cached
            intelligent_source = f"domain_cache_format={cached}"

    employees = _parse_employee_count(result.company_employees)
    candidates = _build_guess_candidates(result, intelligent_first)
    if not candidates:
        result.log_step("format_guesser_skipped", "no candidates to guess (clean name/domain returned empty)")
        return

    result.log_step(
        "format_guesser_init",
        f"contact={f_raw} {l_raw} @ {_clean_domain(domain_raw)} | "
        f"employees={employees or 'unknown'} | hint={intelligent_source} | "
        f"queue=[{', '.join(name for _, name in candidates)}]",
    )
    # Surface to the main run log so the operator can see guesser activity
    # in real time (result.log_step entries are per-contact only and don't
    # bubble up otherwise).
    _log(
        f"[guesser:init] {f_raw} {l_raw} @ {_clean_domain(domain_raw)} "
        f"— emp={employees or '?'} hint={intelligent_source} "
        f"queue=[{', '.join(name for _, name in candidates)}]"
    )

    found_catch_all: Optional[tuple[str, str]] = None
    # Modular MV-only fallback: if MV says ok/risky and the whole secondary
    # chain returns None for every guess, accept the best ok/risky guess
    # as a last resort. Same philosophy as step_06.
    found_mv_only: Optional[tuple[str, str, str]] = None  # (email, pattern, quality)

    for email, pattern in candidates:
        try:
            mv_resp = _verify_millionverifier_quality(email, mv_key)
        except Exception as e:
            result.log_step(f"guess_{pattern}", f"{email}: MV error {e}")
            continue
        if not mv_resp:
            result.log_step(f"guess_{pattern}", f"{email}: MV returned no response")
            continue

        quality = mv_resp.get("quality", "")
        if quality == "good":
            result.add_candidate(email, f"guesser:{pattern}", confidence=0.7)
            result.valid_email = email
            result.email_source = f"guesser:{pattern}"
            result.status = "valid"
            result.log_step(
                f"guess_{pattern}",
                f"{email}: mv=good -> VALID (status=valid, source=guesser:{pattern})",
            )
            result.log_step("format_guesser_done", f"found via {pattern} after {result.steps_run} checks")
            _log(f"[guesser:HIT ] {f_raw} {l_raw} @ {_clean_domain(domain_raw)} -> VALID via {pattern}: {email} (mv=good)")
            time.sleep(RATE_LIMITS.get("millionverifier", 0.5))
            return

        if quality in ("ok", "risky"):
            # Modular secondary cascade — every available verifier in turn.
            # Dead/missing providers skip themselves through _get_key empty.
            secondary_verdict = _secondary_verify(email, api_keys)
            if secondary_verdict == "valid":
                result.add_candidate(email, f"guesser:{pattern}", confidence=0.65)
                result.valid_email = email
                result.email_source = f"guesser:{pattern}"
                result.status = "valid"
                result.log_step(
                    f"guess_{pattern}",
                    f"{email}: mv={quality} + secondary=valid -> VALID",
                )
                result.log_step(
                    "format_guesser_done",
                    f"found via {pattern} after {result.steps_run} checks",
                )
                _log(f"[guesser:HIT ] {f_raw} {l_raw} @ {_clean_domain(domain_raw)} -> VALID via {pattern}: {email} (mv={quality}+secondary=valid)")
                time.sleep(RATE_LIMITS.get("millionverifier", 0.5))
                return
            if secondary_verdict == "catch_all" and found_catch_all is None:
                found_catch_all = (email, pattern)
                result.log_step(
                    f"guess_{pattern}",
                    f"{email}: mv={quality} + secondary=catch_all (held as fallback)",
                )
            elif secondary_verdict == "invalid":
                result.log_step(
                    f"guess_{pattern}",
                    f"{email}: mv={quality} + secondary=invalid (skipped)",
                )
            else:
                # Secondary chain inconclusive (all None). Hold as MV-only fallback.
                if found_mv_only is None:
                    found_mv_only = (email, pattern, quality)
                result.log_step(
                    f"guess_{pattern}",
                    f"{email}: mv={quality}, no secondary signal (held as MV-only)",
                )
        elif quality == "bad":
            result.log_step(f"guess_{pattern}", f"{email}: mv=bad (skipped)")
        else:
            result.log_step(f"guess_{pattern}", f"{email}: mv={quality or 'no-quality'} (skipped)")

        time.sleep(RATE_LIMITS.get("millionverifier", 0.5))

    # Exhausted all patterns. Fallback order: catch_all > mv_only > MISS.
    if found_catch_all:
        email, pattern = found_catch_all
        result.add_candidate(email, f"guesser:{pattern}", confidence=0.5)
        result.valid_email = email
        result.email_source = f"guesser:{pattern}"
        result.status = "catch_all"
        result.log_step(
            "format_guesser_done",
            f"all patterns weak; using catch_all from {pattern}: {email}",
        )
        _log(f"[guesser:CA  ] {f_raw} {l_raw} @ {_clean_domain(domain_raw)} -> catch_all via {pattern}: {email}")
    elif found_mv_only:
        email, pattern, quality = found_mv_only
        result.add_candidate(email, f"guesser:{pattern}_mv_only", confidence=0.5)
        result.valid_email = email
        result.email_source = f"guesser:{pattern}_mv_only"
        result.status = "valid"
        result.log_step(
            "format_guesser_done",
            f"no secondary available; accepting MV-only via {pattern}: {email}",
        )
        _log(
            f"[guesser:MV  ] {f_raw} {l_raw} @ {_clean_domain(domain_raw)} -> "
            f"VALID via {pattern} (mv={quality}, no secondary): {email}"
        )
    else:
        result.log_step(
            "format_guesser_done",
            f"exhausted {len(candidates)} patterns; status remains {result.status}",
        )
        _log(f"[guesser:MISS] {f_raw} {l_raw} @ {_clean_domain(domain_raw)} -> exhausted {len(candidates)} patterns", "warning")


def step_06_verify_conditional_cascade(result: WaterfallResult, api_keys: dict):
    """
    Conditional cascade verification:
    1. Million Verifier v3 quality triage (good → valid, risky → secondary, bad → invalid)
    2. Secondary validators on risky emails (TryKit → BounceBan fallback)
    3. Email Guard enrichment on final valid emails only
    """
    if not result.candidate_emails:
        result.status = "unknown"
        result.log_step("verify_cascade", "no candidates to verify")
        return

    mv_key = _get_key(api_keys, "millionverifier")
    eg_key = _get_key(api_keys, "emailguard")

    best_catch_all: Optional[str] = None
    best_catch_all_source = ""
    # Modular fallback: when MV says ok/risky and the secondary chain is
    # entirely dead (TryKit gone, IcyPeas/BounceBan/Hunter missing or
    # inconclusive), we accept the best ok/risky candidate as a last resort.
    # This honors the "if a stage is dead we carry on with the knowledge
    # we have" philosophy — MV's verdict alone is still meaningful.
    best_mv_only: Optional[str] = None
    best_mv_only_source = ""
    best_mv_only_quality = ""

    for candidate in result.candidate_emails:
        email = candidate["email"]
        source = candidate["source"]

        # Step 6: Million Verifier v3 triage
        # Defense in depth — entry-point checks (run_waterfall / run_waterfall_batch)
        # already reject runs without the MV key, but if a caller bypasses those
        # we still fail loud here rather than silently marking the contact unknown.
        if not mv_key:
            raise RuntimeError(
                "email_waterfall: MillionVerifier key missing inside cascade. "
                "This should have been caught at run_waterfall entry. "
                "Set MILLIONVERIFIER_API_KEY."
            )

        mv_resp = _verify_millionverifier_quality(email, mv_key)
        if not mv_resp:
            # MV errored even after transient retries — don't drop the candidate to
            # 'unknown'; let the secondary validators decide on this email instead.
            result.log_step("verify_mv", f"error checking {email}; trying secondary validators")
            secondary_verdict = _secondary_verify(email, api_keys)
            result.log_step("verify_secondary", f"{email}: {secondary_verdict or 'no-decision'} (mv-error)")
            if secondary_verdict == "valid":
                result.valid_email = email
                result.email_source = source
                result.status = "valid"
                result.log_step("verify_result", f"VALID (MV error + secondary=valid): {email}")
                if eg_key:
                    host = _enrich_emailguard(email, eg_key)
                    if host:
                        result.email_host = host
                        result.log_step("enrich_eg", f"host: {host}")
                    time.sleep(RATE_LIMITS.get("emailguard", 0.5))
                return
            elif secondary_verdict == "catch_all" and best_catch_all is None:
                best_catch_all = email
                best_catch_all_source = source
            time.sleep(RATE_LIMITS.get("millionverifier", 0.5))
            continue

        quality = mv_resp.get("quality", "")
        result.log_step("verify_mv_quality", f"{email}: quality={quality}")

        # ─ Quality routing ─
        if quality == "good":
            # VALID: skip secondary validators
            result.valid_email = email
            result.email_source = source
            result.status = "valid"
            result.log_step("verify_result", f"VALID (MV quality=good): {email}")
            # Enrich with Email Guard
            if eg_key:
                host = _enrich_emailguard(email, eg_key)
                if host:
                    result.email_host = host
                    result.log_step("enrich_eg", f"host: {host}")
                time.sleep(RATE_LIMITS.get("emailguard", 0.5))
            return

        elif quality in ("ok", "risky"):
            # Modular secondary cascade — dead providers are silently skipped
            # via _get_key short-circuit. Returns the first definitive verdict
            # (valid/catch_all/invalid) or None if no working secondary could
            # decide.
            secondary_verdict = _secondary_verify(email, api_keys)
            result.log_step("verify_secondary", f"{email}: {secondary_verdict or 'no-decision'}")

            if secondary_verdict == "valid":
                result.valid_email = email
                result.email_source = source
                result.status = "valid"
                result.log_step("verify_result", f"VALID (MV={quality} + secondary=valid): {email}")
                if eg_key:
                    host = _enrich_emailguard(email, eg_key)
                    if host:
                        result.email_host = host
                        result.log_step("enrich_eg", f"host: {host}")
                    time.sleep(RATE_LIMITS.get("emailguard", 0.5))
                return

            elif secondary_verdict == "catch_all" and best_catch_all is None:
                best_catch_all = email
                best_catch_all_source = source

            elif secondary_verdict == "invalid":
                continue  # try next candidate

            else:
                # No working secondary could decide (None).
                # Policy: a "risky" email is ONLY valid if a secondary validator
                # (TryKit / BounceBan / IcyPeas / Hunter) positively confirms it
                # (valid / deliverable / catch-all). We do NOT accept "risky" on
                # MV's verdict alone — drop it. Only the softer "ok" quality may
                # fall back to MV-only when every secondary is unavailable.
                if quality != "risky" and best_mv_only is None:
                    best_mv_only = email
                    best_mv_only_source = source
                    best_mv_only_quality = quality

        elif quality == "bad":
            # INVALID: skip secondary validators
            result.log_step("verify_result", f"INVALID (MV quality=bad): {email}")
            continue

        time.sleep(RATE_LIMITS.get("millionverifier", 0.5))

    # Fallback chain — preference order: catch_all > mv_only > unknown.
    # catch_all means SOMEONE in the secondary chain confirmed the domain
    # accepts everything; mv_only means we have MV's "ok/risky" but no
    # secondary signal at all. Both beat dropping the contact entirely.
    if best_catch_all:
        result.valid_email = best_catch_all
        result.email_source = best_catch_all_source
        result.status = "catch_all"
        result.log_step("verify_result", f"CATCH_ALL (best): {best_catch_all}")
    elif best_mv_only:
        # Modular fallback: trust MV's verdict when no secondary is available.
        # Tag source with `_mv_only` so analytics can see how many emails came
        # through this lower-confidence path.
        result.valid_email = best_mv_only
        result.email_source = f"{best_mv_only_source}_mv_only"
        result.status = "valid"
        result.log_step(
            "verify_result",
            f"VALID (MV={best_mv_only_quality}, no secondary available — modular fallback): {best_mv_only}",
        )
        _log(
            f"[verify:mv-only] {result.first_name} {result.last_name} @ {result.domain} -> "
            f"{best_mv_only} (mv={best_mv_only_quality}, no secondary; source={best_mv_only_source})"
        )
        if eg_key:
            host = _enrich_emailguard(email, eg_key)
            if host:
                result.email_host = host
    else:
        result.status = "unknown"
        result.log_step("verify_result", "no valid email found")


# ══════════════════════════════════════════════════════════════════════════════
# WATERFALL ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

FINDER_STEPS = [
    step_01_existing_email,
    step_02_trykit_find,
    step_03_leadmagic_find,
    step_04_icypeas_find,
    step_04b_hunter_find,
    step_05_dedup_candidates,
]


def _run_finders_parallel(step_fns: list, result: "WaterfallResult", api_keys: dict, max_workers: int = 8) -> None:
    """Run finder steps concurrently (they don't depend on each other)."""
    if not step_fns:
        return

    def _one(fn):
        try:
            fn(result, api_keys)
        except Exception as e:
            result.errors.append(f"{fn.__name__}: {e}")
            _log(f"Step {fn.__name__} failed: {e}", "warning")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_one, step_fns))


def _run_finders_sequential(result: "WaterfallResult", api_keys: dict) -> None:
    """Sequential finder waterfall: TryKit → LeadMagic → IcyPeas → Hunter.

    Each finder only runs if all previous finders added no new candidate emails.
    This implements the cost-control cascade the user specified:
      - TryKit runs first (requires Full Name + Domain, optionally LinkedIn URL)
      - LeadMagic only runs if TryKit found nothing
      - IcyPeas only runs if TryKit + LeadMagic both found nothing
      - Hunter only runs if all three above found nothing
    The pre_count baseline excludes the input_email already loaded via
    step_01_existing_email, so "new" means a different address from a finder.
    """
    pre_count = len(result.candidate_emails)

    step_02_trykit_find(result, api_keys)
    if len(result.candidate_emails) > pre_count:
        step_05_dedup_candidates(result, api_keys)
        return

    step_03_leadmagic_find(result, api_keys)
    if len(result.candidate_emails) > pre_count:
        step_05_dedup_candidates(result, api_keys)
        return

    step_04_icypeas_find(result, api_keys)
    if len(result.candidate_emails) > pre_count:
        step_05_dedup_candidates(result, api_keys)
        return

    step_04b_hunter_find(result, api_keys)
    step_05_dedup_candidates(result, api_keys)


def _parse_location(location_str: str) -> tuple:
    """Parse 'City, State' or 'City, State, Country' into (city, state, country)."""
    if not location_str:
        return "", "", ""
    parts = [p.strip() for p in location_str.split(",")]
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[2]


def run_waterfall(contact: dict, api_keys: dict) -> WaterfallResult:
    """Run full waterfall for a single contact. Passes all upstream fields through.

    Raises RuntimeError if any required provider key is missing — this is a
    fail-fast guard so unattended runs never silently produce zero-valid output.
    """
    _check_required_waterfall_keys(api_keys)

    location = contact.get("location", "")
    city, state, country = _parse_location(location)

    # Company location may come from DiscoLike enrichment fields
    addr = contact.get("address") or {}
    if isinstance(addr, dict):
        company_city    = addr.get("city", "")
        company_state   = addr.get("state", "")
        company_country = addr.get("country", "")
    else:
        company_city = company_state = company_country = ""

    # AI-ARK field name aliases — "LinkedIn" → "Person LinkedIn" → linkedin_url
    linkedin_url = (
        contact.get("linkedin_url")
        or contact.get("personal_linkedin_url")
        or contact.get("Person LinkedIn")   # post-mapping name from AI-ARK export
        or contact.get("LinkedIn")           # raw AI-ARK export column name
        or ""
    )
    # AI-ARK exports company as "Org"; pipeline maps it to "Company Name"
    company_name = (
        contact.get("company_name")
        or contact.get("Company Name")
        or contact.get("Org")
        or contact.get("company_clean")
        or ""
    )
    company_linkedin_url = (
        contact.get("company_linkedin_url")
        or contact.get("Company LinkedIn")
        or contact.get("company_linkedin")
        or ""
    )

    # Named fields we explicitly map — anything else goes into pass_through
    _KNOWN_FIELDS = {
        "first_name", "last_name", "full_name", "title", "job_title",
        "seniority", "department",
        "linkedin_url", "personal_linkedin_url", "Person LinkedIn", "LinkedIn",
        "twitter_url", "phone", "picture_url",
        "id", "aiark_id", "profile_summary", "is_hiring", "open_to_work",
        "location",
        "domain", "company_domain",
        "company_name", "Company Name", "Org", "company_clean",
        "company_linkedin_url", "Company LinkedIn", "company_linkedin",
        "company_description", "description",
        "address", "company_city", "company_state", "company_country",
        "company_employees", "employees", "company_industry", "industry",
        "similarity", "company_similarity",
        "campaign_slug", "tier", "icp_tier", "icp_score",
        "email", "valid_email",
    }
    pass_through = {k: v for k, v in contact.items() if k not in _KNOWN_FIELDS}

    result = WaterfallResult(
        # Contact identity
        first_name      = contact.get("first_name", ""),
        last_name       = contact.get("last_name", ""),
        full_name       = contact.get("full_name", ""),
        title           = contact.get("title", "") or contact.get("job_title", ""),
        seniority       = contact.get("seniority", ""),
        department      = contact.get("department", ""),
        linkedin_url    = linkedin_url,
        twitter_url     = contact.get("twitter_url", ""),
        phone           = contact.get("phone", ""),
        picture_url     = contact.get("picture_url", ""),
        aiark_id        = contact.get("id", "") or contact.get("aiark_id", ""),
        profile_summary = contact.get("profile_summary", ""),
        is_hiring       = str(contact.get("is_hiring", "")),
        open_to_work    = str(contact.get("open_to_work", "")),

        # Contact location
        location        = location,
        contact_city    = city,
        contact_state   = state,
        contact_country = country,

        # Company
        domain               = contact.get("domain", "") or contact.get("company_domain", ""),
        company_name         = company_name,
        company_linkedin_url = company_linkedin_url,
        company_description  = contact.get("company_description", "") or contact.get("description", ""),
        company_city         = company_city or contact.get("company_city", ""),
        company_state        = company_state or contact.get("company_state", ""),
        company_country      = company_country or contact.get("company_country", ""),
        company_employees    = contact.get("company_employees", "") or contact.get("employees", ""),
        company_industry     = contact.get("company_industry", "") or contact.get("industry", ""),
        company_similarity   = str(contact.get("similarity", "") or contact.get("company_similarity", "")),

        # Pipeline metadata
        campaign_slug = contact.get("campaign_slug", ""),
        tier          = contact.get("tier", ""),
        icp_tier      = contact.get("icp_tier", ""),
        icp_score     = str(contact.get("icp_score", "")),

        # Email input
        input_email = contact.get("email", "") or contact.get("valid_email", ""),

        # Extra AI-ARK fields preserved verbatim
        pass_through = pass_through,
    )

    has_input_email = bool(result.input_email)

    # Phase 1: Existing email or find one
    try:
        step_01_existing_email(result, api_keys)

        if not has_input_email:
            # No email from AI-ARK — run finder waterfall (TryKit → LeadMagic → IcyPeas → Hunter)
            _run_finders_sequential(result, api_keys)
        else:
            step_05_dedup_candidates(result, api_keys)
    except Exception as e:
        result.errors.append(f"finder_phase: {e}")

    # Phase 2-4: Verify candidates
    try:
        step_06_verify_conditional_cascade(result, api_keys)
    except Exception as e:
        result.errors.append(f"verify_cascade: {e}")
        _log(f"Verification failed: {e}", "error")

    # If AI-ARK email failed verification, run finder waterfall as fallback
    if has_input_email and result.status not in ("valid", "catch_all"):
        _log(f"Input email {result.input_email} failed verification, running finder waterfall", "info")
        try:
            _run_finders_sequential(result, api_keys)
            step_06_verify_conditional_cascade(result, api_keys)
        except Exception as e:
            result.errors.append(f"finder_fallback: {e}")

    # Last-resort: generate format-based guesses and verify each. Recovers
    # contacts where every finder returned nothing or every candidate ended
    # 'unknown'. Cheap (only MV credits) and ordered by company-size + any
    # known format we've seen for the same domain earlier in the batch.
    if result.status not in ("valid", "catch_all"):
        try:
            step_07_format_guesser(result, api_keys)
        except Exception as e:
            result.errors.append(f"format_guesser: {e}")
            _log(f"Format guesser failed for {result.first_name} {result.last_name}: {e}", "warning")

    # Remember the verified format on this domain so later contacts in the
    # same batch try it first (cross-contact intelligence — same domain
    # almost always uses the same pattern).
    if result.valid_email and result.first_name and result.last_name and result.domain:
        fmt = _detect_email_format(result.valid_email, result.first_name, result.last_name)
        if fmt:
            _remember_domain_format(result.domain, fmt)

    return result


def run_waterfall_batch(contacts: list, api_keys: dict, max_workers: Optional[int] = None) -> list:
    """Parallel waterfall across contacts.

    Raises RuntimeError immediately if required provider keys are missing,
    rather than spawning N workers that each fail the same way deep inside.
    """
    _check_required_waterfall_keys(api_keys)

    # Reset cross-batch state BEFORE preflight so a previous batch's dead-set
    # doesn't shadow the current run's actual provider health.
    _reset_dead_providers()
    _reset_domain_format_cache()
    _reset_api_stats()

    # Pre-flight: live-probe every configured provider so dead keys surface
    # loudly up-front instead of silently 401'ing all batch long. Required
    # providers (millionverifier) failing raises immediately. Optional
    # providers failing get added to the dead-set so step functions skip
    # them via the _get_key short-circuit.
    _preflight_provider_health(api_keys)

    if max_workers is None:
        max_workers = int(os.getenv("WATERFALL_WORKERS", "20"))
    per_contact_timeout = float(os.getenv("WATERFALL_TIMEOUT", "60"))
    total = len(contacts)

    results: List[Optional[WaterfallResult]] = [None] * total
    done = {"n": 0}
    progress_lock = threading.Lock()

    def _worker(idx: int, contact: dict) -> WaterfallResult:
        return run_waterfall(contact, api_keys)

    def _timed_worker(idx: int, contact: dict) -> WaterfallResult:
        try:
            inner = ThreadPoolExecutor(max_workers=1)
            try:
                fut = inner.submit(_worker, idx, contact)
                return fut.result(timeout=per_contact_timeout)
            except TimeoutError:
                r = WaterfallResult(
                    first_name=contact.get("first_name", ""),
                    last_name=contact.get("last_name", ""),
                    domain=contact.get("domain", ""),
                    company_name=contact.get("company_name", ""),
                    input_email=contact.get("email", ""),
                )
                r.status = "unknown"
                r.errors.append(f"timeout after {per_contact_timeout}s")
                return r
            finally:
                inner.shutdown(wait=False)
        except Exception as e:
            r = WaterfallResult(
                first_name=contact.get("first_name", ""),
                last_name=contact.get("last_name", ""),
                domain=contact.get("domain", ""),
                company_name=contact.get("company_name", ""),
                input_email=contact.get("email", ""),
            )
            r.status = "unknown"
            r.errors.append(f"worker error: {e}")
            return r

    def _run_and_report(idx: int, contact: dict) -> None:
        r = _timed_worker(idx, contact)
        results[idx] = r
        with progress_lock:
            done["n"] += 1
            n = done["n"]
        name = f"{r.first_name} {r.last_name}".strip()
        domain = (contact.get("domain") or "").lower().strip()
        progress(n, total, f"Waterfall {n}/{total}: {name} @ {domain} → {r.status}")
        _log(f"[{n}/{total}] {name} @ {domain} → {r.status}: {r.valid_email or 'none'}")

    _log(f"Waterfall: {total} contacts, {max_workers}-wide parallel, conditional cascade verification")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_and_report, i, c) for i, c in enumerate(contacts)]
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc:
                _log(f"Worker crashed: {exc}", "error")

    final: List[WaterfallResult] = [r for r in results if r is not None]
    valid_count = sum(1 for r in final if r.status == "valid")
    catch_all_count = sum(1 for r in final if r.status == "catch_all")
    unknown_count = sum(1 for r in final if r.status == "unknown")
    progress(total, total,
             f"Complete: {valid_count} valid, {catch_all_count} catch-all, {unknown_count} unknown out of {total}")

    # Per-source breakdown — which finder/guesser actually produced each
    # validated email. Helps tune the cascade: if guesser:* is dominating,
    # the finder providers aren't covering and you're spending MV credits
    # on guesses; if existing_email dominates, AI.ARK is doing the work.
    from collections import Counter
    source_counts: Counter = Counter()
    for r in final:
        if r.status in ("valid", "catch_all"):
            src = (r.email_source or "unknown")
            # Group all guesser:<pattern> rows under their pattern for readability.
            source_counts[src] += 1

    if source_counts:
        _log("Waterfall validated emails by source:")
        for src, c in source_counts.most_common():
            pct = (c * 100 / max(1, valid_count + catch_all_count))
            _log(f"  {src:<28} {c:>4}  ({pct:>5.1f}%)")
        # Roll up guesser:* into a single "guesser (any pattern)" total for the run.json stats
        guesser_total = sum(c for s, c in source_counts.items() if s.startswith("guesser:"))
        if guesser_total:
            _log(f"  guesser (any pattern)        {guesser_total:>4}  ({guesser_total*100/max(1, valid_count+catch_all_count):.1f}%)")

    # Per-API call/outcome tally — exact numbers from each validator API across
    # the batch (MillionVerifier verdict split + each secondary validator's
    # outcomes), for the run analysis.
    api_stats = _api_stats_snapshot()
    if api_stats:
        mv = {k.split(":", 1)[1]: v for k, v in api_stats.items() if k.startswith("millionverifier:")}
        if mv:
            mv_total = sum(mv.values())
            _log(f"Waterfall API tally — MillionVerifier: {mv_total} checks  ("
                 + ", ".join(f"{q}={mv[q]}" for q in sorted(mv)) + ")")
        sec = sorted(k for k in api_stats if k.startswith("secondary:"))
        if sec:
            _log("Waterfall API tally — secondary validators:")
            for k in sec:
                _log(f"  {k.split(':', 1)[1]:<24} {api_stats[k]:>4}")

    return final


# ── JSON mode ────────────────────────────────────────────────────────────────

def json_mode():
    """Read JSON from stdin, run waterfall, write JSON to stdout."""
    raw = sys.stdin.read()
    params = json.loads(raw)

    contacts = params.get("contacts", [])
    if not contacts:
        print(json.dumps({"error": "No contacts provided"}))
        sys.exit(1)

    api_keys = params.get("api_keys", {})
    results = run_waterfall_batch(contacts, api_keys)

    output = {
        "results": [r.to_output() for r in results],
        "summary": {
            "total": len(results),
            "valid": sum(1 for r in results if r.status == "valid"),
            "catch_all": sum(1 for r in results if r.status == "catch_all"),
            "unknown": sum(1 for r in results if r.status == "unknown"),
        }
    }

    print(json.dumps(output))


# ── CLI mode ─────────────────────────────────────────────────────────────────

def cli_mode():
    """Argparse-based CLI interface."""
    parser = argparse.ArgumentParser(
        description="Email Waterfall — Conditional cascade verification",
        epilog="Set API keys via environment variables"
    )
    sub = parser.add_subparsers(dest="command")

    # run subcommand
    run = sub.add_parser("run", help="Run waterfall on a CSV")
    run.add_argument("input_csv", help="Input CSV with first_name, last_name, domain columns")
    run.add_argument("-o", "--output", help="Output CSV path")

    # verify subcommand
    v = sub.add_parser("verify", help="Verify a single email")
    v.add_argument("email", help="Email to verify")

    # find subcommand
    f = sub.add_parser("find", help="Find email for a person")
    f.add_argument("first_name")
    f.add_argument("last_name")
    f.add_argument("domain")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Build api_keys from environment
    api_keys = {}
    for provider in ("millionverifier", "trykit", "leadmagic", "bounceban", "emailguard", "icypeas"):
        key = os.getenv(f"{provider.upper()}_API_KEY", "")
        if key:
            api_keys[provider] = key
    # IcyPeas needs a second credential (key:secret in the Authorization header).
    icypeas_secret = os.getenv("ICYPEAS_SECRET_KEY", "") or os.getenv("ICYPEAS_API_SECRET", "")
    if icypeas_secret:
        api_keys["icypeas_secret"] = icypeas_secret

    if args.command == "run":
        with open(args.input_csv, encoding="utf-8") as f:
            contacts = list(csv.DictReader(f))

        print(f"Running waterfall on {len(contacts)} contacts...", file=sys.stderr)
        results = run_waterfall_batch(contacts, api_keys)

        # Output CSV — only VALID emails
        valid_results = [r for r in results if r.status == "valid"]
        output_path = args.output or args.input_csv.replace(".csv", "-valid.csv")
        if valid_results:
            keys = list(valid_results[0].to_output().keys())
            with open(output_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows([r.to_output() for r in valid_results])
            print(f"Saved {len(valid_results)} valid emails to {output_path}", file=sys.stderr)
        else:
            print(f"No valid emails found", file=sys.stderr)

    elif args.command == "verify":
        contact = {"first_name": "", "last_name": "", "domain": "", "email": args.email}
        result = run_waterfall(contact, api_keys)
        print(json.dumps(result.to_output(), indent=2))

    elif args.command == "find":
        contact = {
            "first_name": args.first_name,
            "last_name": args.last_name,
            "domain": args.domain,
        }
        result = run_waterfall(contact, api_keys)
        print(json.dumps(result.to_output(), indent=2))


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Check if CLI args were provided (run/verify/find commands)
    has_cli_args = len(sys.argv) > 1 and any(cmd in sys.argv for cmd in ["run", "verify", "find"])

    if has_cli_args:
        cli_mode()
    elif not sys.stdin.isatty():
        json_mode()
    else:
        cli_mode()
