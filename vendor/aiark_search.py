#!/usr/bin/env python3
"""
AI Ark Contact Search — Per-domain search with two-tier fallback + JSON stdin/stdout + CLI interface.

Refactored for Step 3 of GTM pipeline:
  - Per-domain search (one API call per domain, each contact tagged with _source_domain)
  - Three-tier fallback: tight (domain + job_titles_to_include + seniority) → recovery (domain + job_titles_recovery + seniority) → broad (domain + seniority only)
  - Dry-run URL preview mode (builds equivalent URL without calling API)
  - _meta block in JSON output with domain_results, tier_used, credits_used

JSON mode (stdin piped):
  echo '{"domains":["salesforce.com"],"job_titles":["VP Sales"],"api_key":"..."}' | python aiark_search.py

CLI mode (interactive):
  python aiark_search.py search salesforce.com --titles "VP Sales" "CRO"

JSON input schema:
  {
    "domains": ["salesforce.com"],
    "job_titles": ["VP Sales", "CRO"],
    "job_titles_recovery": ["Director", "Vice President", "General Manager"],  # Optional: Tier B recovery titles
    "locations": ["United States"],
    "seniority": ["Director", "VP", "C-Level"],
    "industries": ["Software"],
    "limit": 50,
    "min_results_per_domain": 2,    # min per-domain results before fallback
    "dry_run": false,               # URL preview mode
    "api_key": "..."
  }

JSON output:
  {
    "contacts": [...],
    "_meta": {
      "total_found": 12,
      "domain_results": { "acmecorp.com": 7, "hubspot.com": 5 },
      "tier_used": { "acmecorp.com": "tight", "hubspot.com": "broad" },
      "credits_used": 2,
      "timestamp": "2025-04-23T10:00:00Z"
    }
  }
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional
import threading
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    wait as _cf_wait,
    FIRST_COMPLETED,
)

from cost_tracker import CostTracker, BudgetExceededError
from output_formatter import format_output, print_output
from file_utils import load_domains_from_file, load_list_from_file
from config_storage import ConfigStorage

# Prefer httpx, fall back to requests
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

AIARK_BASE_URL = "https://api.ai-ark.com/api/developer-portal/v1"


# ── Persistent AI.ARK client with per-domain search ───────────────────────────

class AIArkClient:
    """Persistent HTTP client for AI.ARK with per-domain search and two-tier fallback."""

    def __init__(self, api_key: str, dry_run: bool = False):
        self.api_key = api_key
        self.dry_run = dry_run
        if _HAS_HTTPX:
            self._client = httpx.Client(
                headers={"X-TOKEN": api_key, "Content-Type": "application/json"},
                timeout=60.0
            )
        else:
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._client and hasattr(self._client, 'close'):
            self._client.close()

    def _request(self, method: str, path: str, payload: dict = None, params: dict = None) -> dict:
        """Single retry spine for API calls with 401/429/5xx handling."""
        url = f"{AIARK_BASE_URL}{path}"
        headers = {
            "X-TOKEN": self.api_key,
            "Content-Type": "application/json",
        }

        backoff = [1, 2, 4]
        for attempt in range(3):
            try:
                if _HAS_HTTPX:
                    if method.upper() == "POST":
                        resp = self._client.post(url, json=payload, headers=headers)
                    else:
                        resp = self._client.get(url, params=params, headers=headers)

                    if resp.status_code == 401:
                        raise RuntimeError(f"AI.ARK auth failed: {resp.text[:100]}")
                    elif resp.status_code == 429:
                        if attempt < len(backoff):
                            _log(f"Rate limited (429). Retry {attempt + 1}/3 in {backoff[attempt]}s...")
                            time.sleep(backoff[attempt])
                            continue
                        raise RuntimeError("AI.ARK rate limited after max retries")
                    elif resp.status_code >= 500:
                        if attempt < len(backoff):
                            _log(f"Server error {resp.status_code}. Retry {attempt + 1}/3...")
                            time.sleep(backoff[attempt])
                            continue
                        raise RuntimeError(f"AI.ARK server error {resp.status_code}: {resp.text[:100]}")
                    elif resp.status_code >= 400:
                        raise RuntimeError(f"AI.ARK {resp.status_code}: {resp.text[:100]}")

                    return resp.json()
                elif _requests:
                    if method.upper() == "POST":
                        resp = _requests.post(url, json=payload, headers=headers, timeout=60)
                    else:
                        resp = _requests.get(url, params=params, headers=headers, timeout=30)

                    if resp.status_code == 401:
                        raise RuntimeError(f"AI.ARK auth failed: {resp.text[:100]}")
                    elif resp.status_code == 429:
                        if attempt < len(backoff):
                            _log(f"Rate limited (429). Retry {attempt + 1}/3 in {backoff[attempt]}s...")
                            time.sleep(backoff[attempt])
                            continue
                        raise RuntimeError("AI.ARK rate limited after max retries")
                    elif resp.status_code >= 500:
                        if attempt < len(backoff):
                            _log(f"Server error {resp.status_code}. Retry {attempt + 1}/3...")
                            time.sleep(backoff[attempt])
                            continue
                        raise RuntimeError(f"AI.ARK server error {resp.status_code}: {resp.text[:100]}")
                    elif resp.status_code >= 400:
                        raise RuntimeError(f"AI.ARK {resp.status_code}: {resp.text[:100]}")

                    return resp.json()
            except RuntimeError:
                raise
            except Exception as e:
                if attempt < len(backoff):
                    _log(f"Connection error: {e}. Retry {attempt + 1}/3...")
                    time.sleep(backoff[attempt])
                    continue
                raise

        raise RuntimeError("Failed after max retries")

    def search_per_domain(
        self,
        domain: str,
        job_titles: Optional[list] = None,
        locations: Optional[list] = None,
        seniority: Optional[list] = None,
        industries: Optional[list] = None,
        department: Optional[list] = None,
        employee_min: Optional[int] = None,
        employee_max: Optional[int] = None,
        technologies: Optional[list] = None,
        keywords: Optional[list] = None,
        limit: int = 100,
        min_results_per_domain: int = 2,
        **extra_filters,
    ) -> tuple:
        """Search single domain with two-tier fallback: tight → recovery.

        Returns (contacts, tier_used, count, track_ids) tuple.
        Each contact gets _source_domain field for attribution.
        track_ids are used to fetch emails via bulk email-finder.
        Tier progression: job_titles (tight) → job_titles_recovery (recovery) → seniority only (broad)
        """
        # Try tight search first: domain + titles + seniority
        tight_contacts, tight_track_ids = self._search_domain_with_filters(
            domain=domain,
            job_titles=job_titles,
            seniority=seniority,
            locations=locations,
            industries=industries,
            department=department,
            employee_min=employee_min,
            employee_max=employee_max,
            technologies=technologies,
            keywords=keywords,
            limit=limit,
            **extra_filters,
        )

        # If tight search succeeds, use it
        if len(tight_contacts) >= min_results_per_domain:
            for contact in tight_contacts:
                contact["_source_domain"] = domain
            return tight_contacts, "tight", len(tight_contacts), tight_track_ids

        # Fallback to recovery: domain + recovery titles + seniority (if recovery titles provided)
        recovery_titles = extra_filters.pop("job_titles_recovery", None)
        if recovery_titles:
            _log(f"Tight search for {domain} returned {len(tight_contacts)} < {min_results_per_domain}. Falling back to recovery search with broader titles...")
            recovery_contacts, recovery_track_ids = self._search_domain_with_filters(
                domain=domain,
                job_titles=recovery_titles,  # Use recovery titles for broader reach
                seniority=seniority,
                locations=locations,
                industries=industries,
                department=department,
                employee_min=employee_min,
                employee_max=employee_max,
                technologies=technologies,
                keywords=keywords,
                limit=limit,
                **extra_filters,
            )

            if len(recovery_contacts) >= min_results_per_domain:
                for contact in recovery_contacts:
                    contact["_source_domain"] = domain
                return recovery_contacts, "recovery", len(recovery_contacts), recovery_track_ids

        # Final fallback to broad: domain + seniority only (drop job_titles)
        _log(f"Recovery search for {domain} returned insufficient results. Falling back to broad search (seniority only)...")
        broad_contacts, broad_track_ids = self._search_domain_with_filters(
            domain=domain,
            job_titles=None,  # Drop titles for broad search
            seniority=seniority,
            locations=locations,
            industries=industries,
            department=department,
            employee_min=employee_min,
            employee_max=employee_max,
            technologies=technologies,
            keywords=keywords,
            limit=limit,
            **extra_filters,
        )

        for contact in broad_contacts:
            contact["_source_domain"] = domain
        return broad_contacts, "broad", len(broad_contacts), broad_track_ids

    def _search_domains_batch(
        self,
        domains: list,
        job_titles: Optional[list] = None,
        locations: Optional[list] = None,
        seniority: Optional[list] = None,
        industries: Optional[list] = None,
        department: Optional[list] = None,
        employee_min: Optional[int] = None,
        employee_max: Optional[int] = None,
        technologies: Optional[list] = None,
        keywords: Optional[list] = None,
        limit: int = 100,
        **extra_filters,
    ) -> tuple:
        """Execute single API call for multiple domains with all filters.

        Returns (contacts, track_ids) tuple. Contacts have 'company_domain' field for attribution.
        """
        page_size = min(limit, 100)
        max_pages = max(1, (limit + page_size - 1) // page_size)
        all_contacts = []
        track_ids = []

        for page_num in range(max_pages):
            # Build account filter (all domains in single "any" array for OR logic)
            account = {
                "domain": _all_any(domains, None, use_all=False)
            }

            # Add other account filters
            if industries:
                account["industries"] = _search_match(industries, None, "WORD")
            if technologies:
                account["technologies"] = _search_match(technologies, None, "WORD")
            if employee_min is not None or employee_max is not None:
                account["employeeSize"] = {
                    "type": "RANGE",
                    "range": [{"start": employee_min or 0, "end": employee_max or 1_000_000}],
                }

            # Build contact filter
            contact = {}

            # Keywords → contact.keyword only
            if keywords:
                contact["keyword"] = _search_match(keywords, None, "SMART")

            # Job titles + job duration
            # NOTE: use `experience.current.title` to match the ai-ark TypeScript CLI
            # (filters.ts buildContactFilter). Previously this was `experience.latest`,
            # which the AI.ARK API treats as a different filter (most-recent role,
            # including past) and produced different result sets.
            if job_titles or extra_filters.get("job_duration_min_months") or extra_filters.get("job_duration_max_months"):
                current = {}
                if job_titles:
                    current["title"] = _search_match(job_titles, None, "SMART")
                if extra_filters.get("job_duration_min_months") or extra_filters.get("job_duration_max_months"):
                    duration = {}
                    if extra_filters.get("job_duration_min_months"):
                        duration["min"] = _months_to_year_month(extra_filters["job_duration_min_months"])
                    if extra_filters.get("job_duration_max_months"):
                        duration["max"] = _months_to_year_month(extra_filters["job_duration_max_months"])
                    current["duration"] = {"currentJob": duration}
                if current:
                    contact["experience"] = {"current": current}

            # Seniority
            mapped_seniority = _normalize_seniority(seniority) if seniority else None
            if mapped_seniority:
                contact["seniority"] = _all_any(mapped_seniority, None)

            # Location
            if locations:
                contact["location"] = _all_any(locations, None)

            # Department
            if department:
                contact["departmentAndFunction"] = _all_any(department, None)

            # Assemble payload
            payload = {"page": page_num, "size": page_size}
            if account:
                payload["account"] = account
            if contact:
                payload["contact"] = contact

            data = self._request("POST", "/people", payload=payload)
            # Use `or []` so a None value from the API doesn't cause TypeError on iteration
            raw_contacts = data.get("content") or data.get("results") or data.get("people") or []

            # Capture trackId for bulk email finding
            track_id = data.get("trackId")
            if track_id and track_id not in track_ids:
                track_ids.append(track_id)

            if not raw_contacts:
                break

            for item in raw_contacts:
                if isinstance(item, dict):
                    all_contacts.append(_normalize_contact(item))

            total_pages = data.get("totalPages", 1)
            if page_num + 1 >= total_pages:
                break

            if len(all_contacts) >= limit:
                break

        return all_contacts[:limit], track_ids

    def _search_domain_with_filters(
        self,
        domain: str,
        job_titles: Optional[list] = None,
        locations: Optional[list] = None,
        seniority: Optional[list] = None,
        industries: Optional[list] = None,
        department: Optional[list] = None,
        employee_min: Optional[int] = None,
        employee_max: Optional[int] = None,
        technologies: Optional[list] = None,
        keywords: Optional[list] = None,
        limit: int = 100,
        **extra_filters,
    ) -> tuple:
        """Execute single API call for ONE domain with the full filter set.

        Matches the ai-ark TypeScript CLI's per-domain people-search behavior
        (see services/leads-cli/ai-ark/src/commands/people-search.ts line 141-170):
        each domain gets its own /people request with the per-domain `limit`
        as the page size, so a 100-domain run of limit=10 returns up to
        100 * 10 = 1000 contacts. Previously the batch path put all domains
        into one `any` filter and capped at one page total across all of them,
        which is the "shortened results" issue we are fixing.

        Returns (contacts, track_ids).
        """
        page_size = min(max(1, limit), 100)
        max_pages = max(1, (limit + page_size - 1) // page_size)
        all_contacts: list = []
        track_ids: list = []

        for page_num in range(max_pages):
            account = {"domain": _all_any([domain], None, use_all=False)}

            if industries:
                account["industries"] = _search_match(industries, None, "WORD")
            if technologies:
                account["technologies"] = _search_match(technologies, None, "WORD")
            if employee_min is not None or employee_max is not None:
                account["employeeSize"] = {
                    "type": "RANGE",
                    "range": [{"start": employee_min or 0, "end": employee_max or 1_000_000}],
                }

            contact: dict = {}

            if keywords:
                contact["keyword"] = _search_match(keywords, None, "SMART")

            # experience.current.title — matches TypeScript CLI (filters.ts).
            if job_titles or extra_filters.get("job_duration_min_months") or extra_filters.get("job_duration_max_months"):
                current: dict = {}
                if job_titles:
                    current["title"] = _search_match(job_titles, None, "SMART")
                if extra_filters.get("job_duration_min_months") or extra_filters.get("job_duration_max_months"):
                    duration: dict = {}
                    if extra_filters.get("job_duration_min_months"):
                        duration["min"] = _months_to_year_month(extra_filters["job_duration_min_months"])
                    if extra_filters.get("job_duration_max_months"):
                        duration["max"] = _months_to_year_month(extra_filters["job_duration_max_months"])
                    current["duration"] = {"currentJob": duration}
                if current:
                    contact["experience"] = {"current": current}

            mapped_seniority = _normalize_seniority(seniority) if seniority else None
            if mapped_seniority:
                contact["seniority"] = _all_any(mapped_seniority, None)

            if locations:
                contact["location"] = _all_any(locations, None)

            if department:
                contact["departmentAndFunction"] = _all_any(department, None)

            payload: dict = {"page": page_num, "size": page_size}
            if account:
                payload["account"] = account
            if contact:
                payload["contact"] = contact

            data = self._request("POST", "/people", payload=payload)
            raw_contacts = data.get("content") or data.get("results") or data.get("people") or []

            track_id = data.get("trackId")
            if track_id and track_id not in track_ids:
                track_ids.append(track_id)

            if not raw_contacts:
                break

            for item in raw_contacts:
                if isinstance(item, dict):
                    all_contacts.append(_normalize_contact(item))

            total_pages = data.get("totalPages", 1)
            if page_num + 1 >= total_pages:
                break
            if len(all_contacts) >= limit:
                break

        return all_contacts[:limit], track_ids


# ── Seniority enum mapping ───────────────────────────────────────────────────

SENIORITY_ENUM_MAP = {
    "founder": "founder",
    "c-level": "c_suite",
    "c_level": "c_suite",
    "c_suite": "c_suite",
    "vp": "vp",
    "vice president": "vp",
    "director": "director",
    "head": "head",
    "manager": "manager",
    "senior": "senior",
    "mid-level": "mid-level",
    "entry": "entry",
    "intern": "intern",
}


def _normalize_seniority(values: Optional[list]) -> list:
    """Convert seniority strings to API enum values."""
    if not values:
        return []
    normalized = []
    for v in values:
        key = v.lower().strip()
        if key in SENIORITY_ENUM_MAP:
            normalized.append(SENIORITY_ENUM_MAP[key])
    return normalized


# ── SQLite cache for people search ───────────────────────────────────────────

class AIArkCache:
    """SQLite-backed result cache for AI.ARK people search (24-hour TTL by default).

    Cache key is SHA256 hash of request parameters (excluding api_key, no_cache).
    TTL: 24 hours (contacts change faster than company profiles).
    """
    DB_PATH = os.path.expanduser("~/.aiark/cache.db")
    TTL = int(os.getenv("AIARK_CACHE_TTL_HOURS", "24")) * 3600

    def __init__(self):
        try:
            os.makedirs(os.path.dirname(self.DB_PATH), exist_ok=True)
            with sqlite3.connect(self.DB_PATH) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS people_search "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at INTEGER NOT NULL)"
                )
                conn.commit()
        except Exception:
            pass

    def _key(self, params: dict) -> str:
        """Generate cache key from request parameters (excluding api_key, no_cache)."""
        clean = {k: v for k, v in params.items() if k not in ("api_key", "no_cache") and v is not None}
        return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()

    def get(self, params: dict):
        """Get cached people search results if fresh (within TTL)."""
        cutoff = int(time.time()) - self.TTL
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                row = conn.execute(
                    "SELECT value FROM people_search WHERE key = ? AND created_at > ?",
                    (self._key(params), cutoff)
                ).fetchone()
                # Cleanup expired entries
                conn.execute("DELETE FROM people_search WHERE created_at <= ?", (cutoff,))
                conn.commit()
            return json.loads(row[0]) if row else None
        except Exception:
            return None

    def set(self, params: dict, results: list):
        """Cache people search results."""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO people_search (key, value, created_at) VALUES (?, ?, ?)",
                    (self._key(params), json.dumps(results), int(time.time()))
                )
                conn.commit()
        except Exception:
            pass


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_post(url: str, payload: dict, headers: dict, timeout: float = 60.0) -> dict:
    """POST request using httpx (preferred) or requests fallback. Retries on HTTP 429."""
    backoff_seconds = [5, 10, 20, 40, 60]
    last_error = None

    for attempt in range(len(backoff_seconds)):
        try:
            if _HAS_HTTPX:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
                    if resp.status_code == 429:
                        if attempt < len(backoff_seconds):
                            wait = backoff_seconds[attempt]
                            _log(f"Rate limited (429). Retry {attempt + 1}/5 in {wait}s...")
                            time.sleep(wait)
                            continue
                    if resp.status_code >= 400:
                        body = resp.text[:500]
                        raise RuntimeError(f"AI.ARK {resp.status_code}: {body}")
                    return resp.json()
            elif _requests:
                resp = _requests.post(url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 429:
                    if attempt < len(backoff_seconds):
                        wait = backoff_seconds[attempt]
                        _log(f"Rate limited (429). Retry {attempt + 1}/5 in {wait}s...")
                        time.sleep(wait)
                        continue
                if resp.status_code >= 400:
                    body = resp.text[:500]
                    raise RuntimeError(f"AI.ARK {resp.status_code}: {body}")
                return resp.json()
            else:
                raise RuntimeError("Neither httpx nor requests is installed")
        except RuntimeError:
            raise
        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise last_error
    raise RuntimeError("Failed to complete POST request after 5 retries")


def _http_get(url: str, params: dict, headers: dict, timeout: float = 30.0) -> dict:
    """GET request using httpx (preferred) or requests fallback. Retries on HTTP 429."""
    backoff_seconds = [5, 10, 20, 40, 60]
    last_error = None

    for attempt in range(len(backoff_seconds)):
        try:
            if _HAS_HTTPX:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.get(url, params=params, headers=headers)
                    if resp.status_code == 429:
                        if attempt < len(backoff_seconds):
                            wait = backoff_seconds[attempt]
                            _log(f"Rate limited (429). Retry {attempt + 1}/5 in {wait}s...")
                            time.sleep(wait)
                            continue
                    if resp.status_code >= 400:
                        body = resp.text[:500]
                        raise RuntimeError(f"AI.ARK {resp.status_code}: {body}")
                    return resp.json()
            elif _requests:
                resp = _requests.get(url, params=params, headers=headers, timeout=timeout)
                if resp.status_code == 429:
                    if attempt < len(backoff_seconds):
                        wait = backoff_seconds[attempt]
                        _log(f"Rate limited (429). Retry {attempt + 1}/5 in {wait}s...")
                        time.sleep(wait)
                        continue
                if resp.status_code >= 400:
                    body = resp.text[:500]
                    raise RuntimeError(f"AI.ARK {resp.status_code}: {body}")
                return resp.json()
            else:
                raise RuntimeError("Neither httpx nor requests is installed")
        except RuntimeError:
            raise
        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise last_error
    raise RuntimeError("Failed to complete GET request after 5 retries")


# ── Progress reporting ───────────────────────────────────────────────────────

def progress(current: int, total: int, message: str):
    print(json.dumps({"progress": current, "total": total, "message": message}),
          file=sys.stderr, flush=True)


def _log(message: str):
    print(json.dumps({"log": message}), file=sys.stderr, flush=True)


# ── Core API calls ───────────────────────────────────────────────────────────

def _months_to_year_month(months: int) -> dict:
    """Convert months to AI.ARK's {year, month} format for job duration filters."""
    return {"year": months // 12, "month": months % 12}


def _build_dry_run_url(
    domain: str,
    job_titles: Optional[list] = None,
    seniority: Optional[list] = None,
    locations: Optional[list] = None,
) -> str:
    """Build a dry-run URL preview for a single-domain search (for verification before API call)."""
    params = {}
    params["domain"] = domain
    if job_titles:
        params["titles"] = ",".join(job_titles)
    if seniority:
        params["seniority"] = ",".join(seniority)
    if locations:
        params["locations"] = ",".join(locations)

    query_string = urllib.parse.urlencode(params)
    return f"{AIARK_BASE_URL}/people/preview?{query_string}"


def _all_any(include: Optional[list], exclude: Optional[list], use_all: bool = False) -> Optional[dict]:
    """AllAny filter shape: { any|all: { include: [...], exclude: [...] } }"""
    if not include and not exclude:
        return None
    condition = {}
    if include:
        condition["include"] = include
    if exclude:
        condition["exclude"] = exclude
    return {"all" if use_all else "any": condition}


def _search_match(include: Optional[list], exclude: Optional[list], mode: str = "SMART") -> Optional[dict]:
    """SearchMatch filter shape: { any: { include: {mode, content}, exclude: {mode, content} } }"""
    if not include and not exclude:
        return None
    condition = {}
    if include:
        condition["include"] = {"mode": mode, "content": include}
    if exclude:
        condition["exclude"] = {"mode": mode, "content": exclude}
    return {"any": condition}


def search_people(
    domains: list,
    api_key: str,
    job_titles: Optional[list] = None,
    job_titles_recovery: Optional[list] = None,
    locations: Optional[list] = None,
    seniority: Optional[list] = None,
    industries: Optional[list] = None,
    department: Optional[list] = None,
    employee_min: Optional[int] = None,
    employee_max: Optional[int] = None,
    technologies: Optional[list] = None,
    keywords: Optional[list] = None,
    limit: int = 100,
    # Exclusion filters (ported from LeadGrow CLI)
    exclude_domains: Optional[list] = None,
    exclude_titles: Optional[list] = None,
    exclude_seniority: Optional[list] = None,
    exclude_industries: Optional[list] = None,
    exclude_locations: Optional[list] = None,
    exclude_department: Optional[list] = None,
    exclude_badges: Optional[list] = None,
    exclude_company_names: Optional[list] = None,
    # Intent/signal filters
    badges: Optional[list] = None,                  # HIRING, OPEN_TO_WORK, CREATOR, INFLUENCER, PAID_SOCIAL_MEMBERS
    job_duration_min_months: Optional[int] = None,  # min months in current role
    job_duration_max_months: Optional[int] = None,  # max months in current role (fresh hires)
    previous_titles: Optional[list] = None,
    skills: Optional[list] = None,
    # Company filters
    funding_types: Optional[list] = None,           # SEED, SERIES_A, SERIES_B, etc.
    revenue_min: Optional[int] = None,
    revenue_max: Optional[int] = None,
    geo: Optional[dict] = None,                     # {"lat": 40.71, "lng": -74.00, "radius": 50, "unit": "km"}
    match_mode: str = "SMART",
    no_cache: bool = False,
    min_results_per_domain: int = 2,
    dry_run: bool = False,
) -> tuple:
    """
    Search for contacts at one or more domains (per-domain approach with three-tier fallback).

    NEW: Per-domain search with source attribution.
    - Searches one domain at a time via AIArkClient.search_per_domain()
    - Each contact tagged with _source_domain for attribution
    - Three-tier fallback: tight (job_titles + seniority) → recovery (job_titles_recovery + seniority) → broad (seniority only)
    - Cross-domain dedup: LinkedIn URL or (first, last, company_domain)
    - Returns (contacts, track_ids, domain_results, tier_used, credits) tuple

    Args:
        job_titles_recovery: recovery tier job titles for tier B fallback search (if insufficient tier A results)
        min_results_per_domain: min contacts per domain before falling back to recovery/broad search (default 2)
        dry_run: if True, build URLs and write to stderr instead of calling API
    """
    # Empty domains = pure ICP search (no domain filter).
    # This is intentional for TAM Pass B — we search by title/industry/location
    # only, without restricting to a specific company list. The _search_domains_batch
    # call will omit the domain filter when domains=[] because _all_any([], None)
    # returns None and _clean() strips it from the payload.
    # Only bail if dry_run is set (no API to call without a URL anchor).

    # Dry-run mode: build URLs without calling API
    if dry_run:
        _log("DRY_RUN: Building URLs for preview (no API calls will be made)")
        for domain in domains:
            url = _build_dry_run_url(
                domain=domain,
                job_titles=job_titles,
                seniority=seniority,
                locations=locations,
            )
            _log(f"URL for {domain}: {url}")
        return [], [], {}, {}, 0

    # Batch search all domains in single API call (cost optimization)
    all_contacts = []
    domain_results = {}  # { domain: count }
    tier_used = {}  # { domain: "tight" | "recovery" | "broad" }
    seen_contacts = set()  # Cross-domain dedup
    all_track_ids = []  # Collect trackIds for bulk email finding
    is_pure_icp_search = not domains
    domain_count = len(domains)

    def _dedup_and_append(contact: dict) -> None:
        """Cross-domain dedup by LinkedIn URL or (first, last, company_domain)."""
        linkedin_url = (contact.get("linkedin_url") or "").strip()
        first_name = (contact.get("first_name") or "").strip()
        last_name = (contact.get("last_name") or "").strip()
        company_domain = (contact.get("company_domain") or "").lower().strip()
        if linkedin_url:
            dedup_key: Optional[tuple] = ("linkedin", linkedin_url)
        elif first_name and last_name and company_domain:
            dedup_key = ("name_domain", first_name, last_name, company_domain)
        else:
            dedup_key = None
        if dedup_key is None or dedup_key not in seen_contacts:
            if dedup_key is not None:
                seen_contacts.add(dedup_key)
            all_contacts.append(contact)

    with AIArkClient(api_key=api_key) as client:
        # Build extra_filters dict (recovery titles + job duration) shared by both paths
        extra_filters = {}
        if job_titles_recovery:
            extra_filters["job_titles_recovery"] = job_titles_recovery
        if job_duration_min_months is not None:
            extra_filters["job_duration_min_months"] = job_duration_min_months
        if job_duration_max_months is not None:
            extra_filters["job_duration_max_months"] = job_duration_max_months

        # ── Multi-domain path: per-domain loop with tier fallback ───────────
        # Matches services/leads-cli/ai-ark/src/commands/people-search.ts line
        # 141-170 — each domain gets its own /people request so the per-domain
        # `limit` is honored. Previously the batch path put all domains in one
        # `any` filter and capped at one page total across all of them, which
        # is the "shortened results" bug reported by Sean.
        if domain_count >= 2 and not is_pure_icp_search:
            progress(0, domain_count, f"Searching {domain_count} domains per-domain (tier fallback)...")
            for i, domain in enumerate(domains, start=1):
                try:
                    contacts_for_domain, tier, count, track_ids = client.search_per_domain(
                        domain=domain,
                        job_titles=job_titles,
                        locations=locations,
                        seniority=seniority,
                        industries=industries,
                        department=department,
                        employee_min=employee_min,
                        employee_max=employee_max,
                        technologies=technologies,
                        keywords=keywords,
                        limit=limit,  # per-domain limit, not aggregate cap
                        min_results_per_domain=min_results_per_domain,
                        **extra_filters,
                    )
                    all_track_ids.extend(track_ids)
                    for contact in contacts_for_domain:
                        _dedup_and_append(contact)
                    domain_results[domain] = count
                    tier_used[domain] = tier
                except Exception as e:
                    _log(f"Error searching {domain}: {e}")
                    domain_results[domain] = 0
                    tier_used[domain] = "error"
                progress(i, domain_count, f"Per-domain: {i}/{domain_count} domains; {len(all_contacts)} unique so far")

            _log(f"Per-domain search complete: {len(all_contacts)} unique contacts across {domain_count} domains")

        # ── Single-domain or pure-ICP path: one batched request ─────────────
        else:
            if is_pure_icp_search:
                progress(0, limit, f"Searching pure ICP contacts for up to {limit} contacts...")
            else:
                progress(0, 1, f"Searching 1 domain ({domains[0] if domains else 'n/a'})...")

            try:
                batch_contacts, track_ids = client._search_domains_batch(
                    domains=domains,
                    job_titles=job_titles,
                    locations=locations,
                    seniority=seniority,
                    industries=industries,
                    department=department,
                    employee_min=employee_min,
                    employee_max=employee_max,
                    technologies=technologies,
                    keywords=keywords,
                    limit=limit,
                    **extra_filters,
                )

                all_track_ids.extend(track_ids)

                # Attribute contacts by company_domain and deduplicate
                domain_counts: dict = {}
                for contact in batch_contacts:
                    _dedup_and_append(contact)
                    company_domain = (contact.get("company_domain") or "").lower().strip()
                    if not is_pure_icp_search and company_domain in domains:
                        domain_counts[company_domain] = domain_counts.get(company_domain, 0) + 1

                if is_pure_icp_search:
                    domain_results["__pure_icp__"] = len(all_contacts)
                    tier_used["__pure_icp__"] = "batch"
                    _log(f"Pure ICP search complete: {len(all_contacts)} unique contacts")
                    progress(len(all_contacts), limit, f"Complete: {len(all_contacts)} pure ICP contacts")
                else:
                    for domain in domains:
                        domain_results[domain] = domain_counts.get(domain, 0)
                        tier_used[domain] = "batch"
                    _log(f"Single-domain search complete: {len(all_contacts)} unique contacts")
                    progress(1, 1, f"Complete: {len(all_contacts)} unique contacts")

            except Exception as e:
                _log(f"Error in batch search: {e}")
                if is_pure_icp_search:
                    domain_results["__pure_icp__"] = 0
                    tier_used["__pure_icp__"] = "error"
                else:
                    for domain in domains:
                        domain_results[domain] = 0
                        tier_used[domain] = "error"

        # Fetch emails via bulk email-finder for all trackIds (parallel)
        if all_track_ids:
            # API supports one email-finder job per unique trackId; dedupe to avoid duplicate submits.
            unique_track_ids = list(dict.fromkeys(t for t in all_track_ids if t))
            if len(unique_track_ids) != len(all_track_ids):
                _log(
                    f"Deduplicated trackIds for email-finder: {len(all_track_ids)} -> {len(unique_track_ids)}"
                )

            max_workers = int(os.getenv("AIARK_EMAIL_FINDER_WORKERS", "5"))
            progress(
                0,
                len(unique_track_ids),
                f"Fetching emails via AI.ARK email-finder ({len(unique_track_ids)} trackIds, {max_workers} parallel)...",
            )
            email_results_by_id = {}  # { ref_id: { email, first_name, last_name, domain } }

            completed_count = 0

            # Circuit breaker: if AI.ARK's email-finder is down, stop wasting the
            # per-trackId poll deadline on every trackId — pause, probe for recovery,
            # and resume (or skip the rest and let the waterfall source the emails).
            breaker = None
            if os.getenv("AIARK_EMAIL_FINDER_BREAKER", "1").strip().lower() not in ("0", "false", "no"):
                breaker = _EmailFinderBreaker(
                    api_key,
                    threshold=int(os.getenv("AIARK_EMAIL_FINDER_BREAKER_THRESHOLD", "5")),
                    probe_interval_sec=float(os.getenv("AIARK_EMAIL_FINDER_PROBE_INTERVAL_SEC", "120")),
                    max_pause_sec=float(os.getenv("AIARK_EMAIL_FINDER_MAX_PAUSE_SEC", "1800")),
                )

            def _fetch_track(track_id: str) -> list:
                return find_emails_bulk(api_key, track_id, breaker=breaker)

            total_tids = len(unique_track_ids)
            next_idx = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                inflight = {}  # future -> track_id
                while next_idx < total_tids or inflight:
                    # Top up the in-flight set to max_workers, unless the breaker is open.
                    while (len(inflight) < max_workers and next_idx < total_tids
                           and not (breaker and breaker.is_open())):
                        tid = unique_track_ids[next_idx]
                        next_idx += 1
                        inflight[executor.submit(_fetch_track, tid)] = tid

                    if not inflight:
                        # Nothing running and we can't submit -> breaker is open. Pause+probe.
                        if breaker and breaker.wait_until_healthy_or_giveup():
                            continue  # recovered -> resume topping up
                        skipped = total_tids - next_idx
                        if breaker:
                            breaker.skipped = skipped
                        _log(f"Email-finder: skipping {skipped} remaining trackIds "
                             f"(AI.ARK email-finder down) — the waterfall will source these emails.")
                        progress(completed_count, total_tids,
                                 f"Email-finder: {completed_count}/{total_tids} done, {skipped} skipped (AI.ARK down)")
                        break

                    done, _ = _cf_wait(set(inflight), return_when=FIRST_COMPLETED)
                    for future in done:
                        track_id = inflight.pop(future)
                        completed_count += 1
                        try:
                            email_results = future.result()
                            for result in email_results:
                                ref_id = result.get("ref_id", "")
                                if ref_id:
                                    email_results_by_id[ref_id] = result
                            progress(
                                completed_count,
                                total_tids,
                                f"Email-finder: {completed_count}/{total_tids} trackIds complete",
                            )
                        except Exception as e:
                            _log(f"Error fetching emails for trackId {track_id}: {e}")
                            progress(
                                completed_count,
                                total_tids,
                                f"Email-finder: {completed_count}/{total_tids} trackIds complete (with errors)",
                            )

            # Merge emails into contacts by id (ref_id)
            _log(f"Email results collected: {len(email_results_by_id)} person IDs with emails")
            if email_results_by_id:
                sample_ids = list(email_results_by_id.keys())[:2]
                _log(f"Sample email result IDs: {sample_ids}")
                # Log sample email result for debugging
                sample_result = email_results_by_id[sample_ids[0]] if sample_ids else None
                if sample_result:
                    _log(f"Sample email result: ref_id={sample_result.get('ref_id')}, email={sample_result.get('email')}")

            # Log sample contact IDs for matching
            if all_contacts:
                sample_contact_ids = [c.get("id") for c in all_contacts[:2]]
                _log(f"Sample contact IDs: {sample_contact_ids}")

            merged_count = 0
            for contact in all_contacts:
                ref_id = contact.get("id", "")
                if ref_id and ref_id in email_results_by_id:
                    email_result = email_results_by_id[ref_id]
                    contact["email"] = email_result.get("email", "")
                    if not contact.get("first_name"):
                        contact["first_name"] = email_result.get("first_name", "")
                    if not contact.get("last_name"):
                        contact["last_name"] = email_result.get("last_name", "")
                    merged_count += 1

            if merged_count > 0:
                _log(f"Merged emails into {merged_count} contacts")
            else:
                _log(f"WARNING: No emails merged. Checked {len(all_contacts)} contacts against {len(email_results_by_id)} email results.")

    # Estimate credits used (approximate: 1 credit per domain search)
    credits_used = 1 if is_pure_icp_search else domain_count

    return all_contacts, all_track_ids, domain_results, tier_used, credits_used




def _safe_get(d, *keys, default=None):
    """Safely traverse nested dicts/lists by key path. Returns default on any miss."""
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key, default)
        elif isinstance(d, list) and isinstance(key, int):
            d = d[key] if len(d) > key else default
        else:
            return default
        if d is None:
            return default
    return d if d is not None else default


def _normalize_contact(item: dict) -> dict:
    """Normalize a raw AI.ARK contact to standard shape.

    AI.ARK API response structure:
      - profile: { first_name, last_name, full_name, headline, summary }
      - link: { linkedin, twitter, github, facebook }
      - company: { summary: { name, industry, staff: { total }, founded_year },
                   link: { domain, domain_ltd, linkedin, website } }
      - department: { seniority, functions: [...], departments: [...] }
      - location: { short, default, country, city }
      - position_groups: [{ company: { name }, profile_positions: [{ title }] }]
      - member_badges: { open_to_work, hiring, premium }
      - email: { output: [{ domainType, mx: { provider } }] }   (enriched only)
      - company: { summary: { name }, link: { domain } }
      - department: { seniority, functions: [...] }
      - location: { short, default, country, city }
      - position_groups: [{ company: { name }, profile_positions: [{ title }] }]
    """
    # Profile (name, headline)
    profile = item.get("profile", {}) or {}
    first_name = profile.get("first_name", "") or item.get("first_name", "")
    last_name = profile.get("last_name", "") or item.get("last_name", "")
    full_name = profile.get("full_name", "") or item.get("full_name", "")
    headline = profile.get("headline", "")

    # Job title from headline or current position
    job_title = ""
    position_groups = item.get("position_groups", [])
    if position_groups and isinstance(position_groups, list):
        for pg in position_groups:
            positions = pg.get("profile_positions", [])
            if positions:
                # First position in first group is current
                job_title = positions[0].get("title", "")
                break
    if not job_title:
        job_title = headline

    # LinkedIn URL
    link_obj = item.get("link", {}) or {}
    linkedin = link_obj.get("linkedin", "") or ""

    # Company info
    company_obj = item.get("company", {}) or {}
    if isinstance(company_obj, dict):
        company_summary = company_obj.get("summary", {}) or {}
        company_link = company_obj.get("link", {}) or {}
        company_name = company_summary.get("name", "")
        company_domain = company_link.get("domain", company_link.get("domain_ltd", ""))
    else:
        company_name = str(company_obj) if company_obj else ""
        company_domain = ""

    # Department / seniority
    dept_obj = item.get("department", {}) or {}
    if isinstance(dept_obj, dict):
        seniority = dept_obj.get("seniority", "")
        functions = dept_obj.get("functions", []) or []
        department = functions[0] if functions else ""
    else:
        seniority = ""
        department = ""

    # Location
    loc_obj = item.get("location", {}) or {}
    if isinstance(loc_obj, dict):
        location = loc_obj.get("short", loc_obj.get("default", ""))
    else:
        location = str(loc_obj) if loc_obj else ""

    # Industry (top-level or from company summary)
    industry = item.get("industry", "") or (company_obj.get("summary", {}) or {}).get("industry", "") if isinstance(company_obj, dict) else ""

    # Additional fields — profile
    # headline: the one-liner displayed under the name on LinkedIn (distinct from job title)
    headline      = profile.get("headline", "") or ""
    profile_bio   = profile.get("summary", "") or ""   # LinkedIn "About" section text
    picture_url   = (profile.get("picture") or {}).get("source", "")
    badges        = item.get("member_badges", {}) or {}
    twitter_url   = link_obj.get("twitter", "") or ""
    github_url    = link_obj.get("github", "") or ""
    facebook_url  = link_obj.get("facebook", "") or ""
    last_updated  = item.get("last_updated", "")

    # Department — full lists (in addition to the existing first-element shortcut)
    # Use `or []` so API null values don't cause TypeError on iteration
    all_functions   = (dept_obj.get("functions")   or []) if isinstance(dept_obj, dict) else []
    all_departments = (dept_obj.get("departments") or []) if isinstance(dept_obj, dict) else []

    # Extended company data
    co_summary       = (company_obj.get("summary", {}) or {}) if isinstance(company_obj, dict) else {}
    company_linkedin = _safe_get(company_obj, "link", "linkedin", default="") if isinstance(company_obj, dict) else ""
    company_website  = _safe_get(company_obj, "link", "website", default="")  if isinstance(company_obj, dict) else ""
    company_headcount = str(_safe_get(co_summary, "staff", "total", default="") or "")
    company_founded  = str(co_summary.get("founded_year", "") or "")

    # Email enrichment sub-object (present only when AI.ARK returns enriched data)
    email_obj        = item.get("email", "")
    # Flat string → standard email; dict → enriched object
    flat_email       = email_obj if isinstance(email_obj, str) else ""
    email_domain_type = _safe_get(email_obj, "output", 0, "domainType", default="") if isinstance(email_obj, dict) else ""
    email_provider    = _safe_get(email_obj, "output", 0, "mx", "provider", default="") if isinstance(email_obj, dict) else ""
    # Resolve flat email from enriched object if not already a string
    if not flat_email and isinstance(email_obj, dict):
        flat_email = _safe_get(email_obj, "output", 0, "email", default="") or ""

    industry = item.get("industry", "") or (co_summary.get("industry", "") if isinstance(company_obj, dict) else "")

    return {
        # Identity
        "id":           item.get("id", ""),
        "first_name":   first_name,
        "last_name":    last_name,
        "full_name":    full_name or f"{first_name} {last_name}".strip(),
        "job_title":    job_title,
        "headline":     headline,
        "profile_summary": profile_bio,   # kept as profile_summary for CSV compatibility
        "profile_bio":  profile_bio,      # explicit alias for downstream personalisation
        # Contact
        "email":        flat_email,
        "email_domain_type": email_domain_type,
        "email_provider":    email_provider,
        "phone":        item.get("phone", ""),
        "linkedin_url": linkedin,
        "twitter_url":  twitter_url,
        "github_url":   github_url,
        "facebook_url": facebook_url,
        "picture_url":  picture_url,
        # Role
        "seniority":    seniority,
        "department":   department,           # first function (backward-compat shortcut)
        "functions":    ";".join(str(f) for f in all_functions if f),
        "departments":  ";".join(str(d) for d in all_departments if d),
        # Company
        "company_name":     company_name,
        "company_domain":   company_domain,
        "domain":           company_domain,
        "company_linkedin": company_linkedin,
        "company_website":  company_website,
        "company_employees_total": company_headcount,
        "company_founded_year":    company_founded,
        # Location
        "location":     location,
        "industry":     industry,
        # Signals
        "open_to_work": badges.get("open_to_work", False),
        "is_hiring":    badges.get("hiring", False),
        "is_premium":   badges.get("premium", False),
        "last_updated": last_updated,
    }




# ── Email export (single person) ─────────────────────────────────────────────

def export_single_person(
    api_key: str,
    person_id: str = None,
    linkedin_url: str = None,
) -> dict:
    """
    POST /v1/people/export/single — get a real-time BounceBan-verified email
    for one person. Provide either person_id (AI.ARK UUID from search results)
    or linkedin_url. Costs 1 credit; 0 credits if no email found.
    Returns a normalized contact dict with 'email' populated.
    """
    if not person_id and not linkedin_url:
        raise ValueError("Must provide either person_id or linkedin_url")

    headers = {
        "X-TOKEN": api_key,
        "Content-Type": "application/json",
    }

    payload = {}
    if person_id:
        payload["id"] = person_id
    if linkedin_url:
        payload["url"] = linkedin_url

    data = _http_post(f"{AIARK_BASE_URL}/people/export/single", payload, headers)
    return _normalize_contact(data)


# ── Reverse lookup (email → person) ──────────────────────────────────────────

def reverse_lookup(api_key: str, email: str) -> dict:
    """
    POST /people/reverse-lookup — given an email, return full person profile.

    Returns dict with person details: { id, email, first_name, last_name,
    title, company_name, linkedin_url, ... }
    """
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}
    payload = {"kind": "CONTACT", "search": email}

    data = _http_post(f"{AIARK_BASE_URL}/people/reverse-lookup", payload, headers)
    return _normalize_contact(data)


# ── Phone finder (LinkedIn URL → phone) ──────────────────────────────────────

def find_mobile_phone(api_key: str, linkedin_url: str) -> dict:
    """
    POST /people/mobile-phone-finder — given a LinkedIn URL, return phone number.

    Returns dict with phone details: { phone, country_code, verified, ... }
    """
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}
    payload = {"linkedinUrl": linkedin_url}

    data = _http_post(f"{AIARK_BASE_URL}/people/mobile-phone-finder", payload, headers)
    if isinstance(data, dict):
        return data
    return {"phone": None, "error": "No phone found or API error"}


# ── Analysis (LinkedIn URL → personality + selling tips) ────────────────────

def analyze_person(api_key: str, linkedin_url: str) -> dict:
    """
    POST /people/analysis — given a LinkedIn URL, return DISC + OCEAN personality
    profile and AI-generated selling tips.

    Returns dict with analysis: { disc_profile, ocean_traits, selling_tips, ... }
    """
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}
    payload = {"linkedinUrl": linkedin_url}

    data = _http_post(f"{AIARK_BASE_URL}/people/analysis", payload, headers)
    if isinstance(data, dict):
        return data
    return {"analysis": None, "error": "No analysis available or API error"}


# ── Bulk export (async track-based export) ──────────────────────────────────

def export_bulk(api_key: str, track_id: str) -> dict:
    """
    POST /people/export — async bulk export with emailVerified field.
    Higher quality than email-finder (uses internal verification, not BounceBan).

    Provide a trackId from a previous people search. Returns job status + results.
    Results include { email, emailVerified, first_name, last_name, ... } for each person.
    """
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}
    payload = {"trackId": track_id}

    data = _http_post(f"{AIARK_BASE_URL}/people/export", payload, headers)
    if isinstance(data, dict):
        return data
    return {"status": None, "error": "Export job failed or API error"}


# ── Credits check ───────────────────────────────────────────────────────────

def check_credits(api_key: str) -> dict:
    """
    GET /payments/credits — check remaining account credit balance.

    Returns dict with: { total_credits, available_credits, used_credits, ... }
    """
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}

    data = _http_get(f"{AIARK_BASE_URL}/payments/credits", {}, headers)
    if isinstance(data, dict):
        return data
    return {"credits": None, "error": "Could not fetch credits or API error"}


# ── Bulk email finder ────────────────────────────────────────────────────────

def _email_finder_health_probe(api_key: str,
                               domain: Optional[str] = None,
                               poll_sec: Optional[float] = None) -> bool:
    """Canary: is AI.ARK's email-finder actually processing jobs right now?

    Runs the real path (people search -> submit -> brief poll) on a stable domain
    and returns True iff the job reaches a terminal state or finds >0 emails within
    `poll_sec`. Used by the circuit breaker to decide when to resume. A probe costs
    one search query (+ up to ~1 credit if the canary finds an email).
    """
    domain = domain or os.getenv("AIARK_EMAIL_FINDER_PROBE_DOMAIN", "salesforce.com")
    if poll_sec is None:
        poll_sec = float(os.getenv("AIARK_EMAIL_FINDER_PROBE_POLL_SEC", "30"))
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}
    webhook_url = os.getenv("AIARK_EMAIL_FINDER_WEBHOOK", "https://webhook.site/unused")
    try:
        with AIArkClient(api_key=api_key) as client:
            _c, _tier, _count, track_ids = client.search_per_domain(
                domain, job_titles=["VP", "Director", "Manager"], limit=10,
            )
    except Exception as e:  # noqa: BLE001
        _log(f"[CIRCUIT] probe search failed: {e}")
        return False
    if not track_ids:
        return False
    tid = track_ids[0]
    try:
        _http_post(
            f"{AIARK_BASE_URL}/people/email-finder",
            {"trackId": tid, "webhook": webhook_url},
            headers,
        )
    except Exception as e:  # noqa: BLE001
        _log(f"[CIRCUIT] probe submit rejected: {e}")
        return False
    deadline = time.time() + poll_sec
    while time.time() < deadline:
        try:
            s = _http_get(f"{AIARK_BASE_URL}/people/email-finder/{tid}/statistics", {}, headers)
        except Exception:  # noqa: BLE001
            time.sleep(4)
            continue
        state = s.get("state", "PENDING")
        st = s.get("statistics") or {}
        if state in ("DONE", "COMPLETED", "FINISHED") or st.get("found", 0) > 0:
            return True
        time.sleep(4)
    return False


class _EmailFinderBreaker:
    """Circuit breaker for the AI.ARK bulk email-finder.

    AI.ARK's email-finder subsystem intermittently goes down: submitted jobs sit
    in PENDING/0-found until our per-trackId deadline, or the trackId 404-expires.
    When that recurs across many trackIds, the phase wastes minutes per trackId for
    zero yield. This breaker watches per-trackId outcomes; after `threshold`
    consecutive outage signals it OPENS. The orchestrator then stops submitting new
    trackIds (un-submitted ones stay valid ~6h, so nothing is lost) and periodically
    probes AI.ARK with a canary. On recovery it CLOSES and submission resumes; if it
    never recovers within `max_pause_sec`, the orchestrator skips the rest and lets
    the email waterfall source those emails instead.

    Thread-safe: `record`/`is_open` are called from the parallel fetch workers.
    """

    def __init__(self, api_key: str, threshold: int = 5,
                 probe_interval_sec: float = 120.0, max_pause_sec: float = 1800.0,
                 probe_fn=None):
        self._api_key = api_key
        self._lock = threading.Lock()
        self._consecutive = 0
        self._open = False
        self.threshold = max(1, threshold)
        self.probe_interval_sec = probe_interval_sec
        self.max_pause_sec = max_pause_sec
        self._probe_fn = probe_fn or (lambda: _email_finder_health_probe(api_key))
        self.skipped = 0

    def record(self, outage: bool) -> None:
        with self._lock:
            if outage:
                self._consecutive += 1
                if self._consecutive >= self.threshold and not self._open:
                    self._open = True
                    _log(f"[CIRCUIT] AI.ARK email-finder looks DOWN "
                         f"({self._consecutive} consecutive outage signals) — opening breaker.")
            elif self._consecutive or self._open:
                self._consecutive = 0
                self._open = False

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def wait_until_healthy_or_giveup(self) -> bool:
        """Block (probing periodically) until AI.ARK recovers or the pause budget is
        exhausted. Returns True if it recovered (resume), False if we gave up."""
        deadline = time.time() + self.max_pause_sec
        _log(f"[CIRCUIT] Pausing bulk email-finder; probing AI.ARK every "
             f"{self.probe_interval_sec:.0f}s for up to {self.max_pause_sec / 60:.0f} min.")
        while True:
            budget_left = deadline - time.time()
            if budget_left <= 0:
                _log("[CIRCUIT] AI.ARK email-finder did not recover within the pause budget — "
                     "skipping remaining trackIds; the email waterfall will source emails.")
                return False
            time.sleep(min(self.probe_interval_sec, budget_left))
            try:
                healthy = bool(self._probe_fn())
            except Exception as e:  # noqa: BLE001
                healthy = False
                _log(f"[CIRCUIT] health probe errored: {e}")
            if healthy:
                with self._lock:
                    self._open = False
                    self._consecutive = 0
                _log("[CIRCUIT] AI.ARK email-finder RECOVERED — resuming bulk email-finder.")
                return True
            _log(f"[CIRCUIT] AI.ARK email-finder still down; "
                 f"{(deadline - time.time()) / 60:.0f} min of probe budget left.")


def find_emails_bulk(api_key: str, track_id: str, breaker=None) -> list:
    """
    Submit a people-search trackId to the bulk email finder.

    Flow:
      POST /v1/people/email-finder  { trackId }  →  starts async job
      GET  /v1/people/email-finder/{trackId}/statistics  →  poll state
      GET  /v1/people/email-finder/{trackId}/inquiries   →  retrieve results

    Each trackId can only be submitted once and expires 6 hours after
    the original people search. Emails are BounceBan-verified by AI.ARK.

    Returns list of dicts: { email, first_name, last_name, domain, ref_id }
    Only contacts where an email was found are included.
    """
    # Breaker open = AI.ARK email-finder is down. Don't submit/poll: the trackId
    # stays valid (~6h from the original search) for a later resume, and the email
    # waterfall will source this contact's email instead.
    if breaker is not None and breaker.is_open():
        return []

    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}

    # AI.ARK currently enforces `webhook` as required on POST /people/email-finder
    # despite the docs listing it as optional. We pass a dummy URL — we don't
    # consume the webhook since we already poll /statistics + /inquiries below.
    # Override via AIARK_EMAIL_FINDER_WEBHOOK env var if you run a real receiver.
    webhook_url = os.getenv("AIARK_EMAIL_FINDER_WEBHOOK", "https://webhook.site/unused")

    # 1. Submit
    try:
        _http_post(
            f"{AIARK_BASE_URL}/people/email-finder",
            {"trackId": track_id, "webhook": webhook_url},
            headers,
        )
    except Exception as e:
        error_msg = str(e)
        if _HAS_HTTPX and isinstance(e, httpx.HTTPStatusError):
            error_msg = f"Email finder submit error: {e.response.status_code} {e.response.text[:200]}"
        if breaker is not None:
            breaker.record(outage=True)  # submit rejected = service down
        raise RuntimeError(error_msg)

    _log(f"Email finder job submitted for trackId {track_id}")

    # Give AI.ARK a brief window to materialize the async bulk job before first poll.
    initial_poll_delay = float(os.getenv("AIARK_EMAIL_FINDER_INITIAL_POLL_DELAY_SEC", "2"))
    if initial_poll_delay > 0:
        time.sleep(initial_poll_delay)

    # 2. Poll statistics until done — bounded by a per-trackId wall-clock deadline
    #    so a perpetually-PENDING trackId can't wedge the whole run. (The
    #    ThreadPoolExecutor that fans these out joins ALL threads on exit, so one
    #    unbounded poll loop hangs the entire email-finder phase.) We deliberately
    #    do NOT bail early on PENDING-with-0-leads: jobs are often PENDING/total=0
    #    while still spinning up, then populate — bailing early drops real emails.
    max_poll_sec = float(os.getenv("AIARK_EMAIL_FINDER_MAX_POLL_SEC", "300"))
    deadline = time.time() + max_poll_sec
    attempt = 0
    outage = False  # True if this trackId is a "service down" signal for the breaker
    while True:
        attempt += 1
        try:
            stats_data = _http_get(
                f"{AIARK_BASE_URL}/people/email-finder/{track_id}/statistics",
                {}, headers
            )
        except Exception as e:
            err = str(e)
            is_bulk_not_found = "404" in err and "bulk not found" in err.lower()
            # Avoid log spam when backend is eventually consistent: log early attempts,
            # then periodic checkpoints for repeated 404 bulk-not-found responses.
            if not is_bulk_not_found or attempt < 3 or attempt % 10 == 0:
                _log(f"Stats poll attempt {attempt} failed: {e}")
            if time.time() >= deadline:
                _log(f"[WARNING] Email-finder trackId {track_id}: poll deadline hit during errors — abandoning")
                outage = True  # never got a clean status within the deadline
                break
            time.sleep(5)
            continue

        state = stats_data.get("state", "PENDING")
        stats = stats_data.get("statistics") or {}
        total = stats.get("total", 0)
        found = stats.get("found", 0)
        progress(found, total or 1, f"Email finder [{state}]: {found}/{total} emails found")

        # AI.ARK returns "DONE" (current) or "COMPLETED" (legacy) when the job is finished.
        if state in ("COMPLETED", "DONE", "FINISHED"):
            break

        if time.time() >= deadline:
            _log(f"[WARNING] Email-finder trackId {track_id}: {max_poll_sec:.0f}s poll deadline reached "
                 f"(state={state}, found={found}/{total}) — proceeding with partial results")
            # Stuck in PENDING with nothing found at the deadline = the outage symptom.
            outage = (found == 0)
            break
        time.sleep(5)

    # 3. Retrieve paginated results.
    # Defensive parsing: AI.ARK has shipped multiple response shapes and may nest
    # items as lists-of-lists. We walk any nested structure and pull dicts only.
    all_results = []
    page = 0
    max_pages = int(os.getenv("AIARK_EMAIL_FINDER_MAX_PAGES", "500"))
    while True:
        if page >= max_pages:
            _log(f"[WARNING] Email-finder trackId {track_id}: hit max page cap ({max_pages}) — stopping pagination")
            break
        data = _http_get(
            f"{AIARK_BASE_URL}/people/email-finder/{track_id}/inquiries",
            {"page": page, "size": 100},
            headers,
        )

        # Normalize to a flat list of item dicts
        if isinstance(data, dict):
            raw_items = data.get("content") or data.get("results") or data.get("items") or []
            has_more = not data.get("last", True)
        elif isinstance(data, list):
            raw_items = data
            has_more = False
        else:
            raw_items = []
            has_more = False

        items = _flatten_to_dicts(raw_items)

        if not items:
            break

        # Debug: log sample item structure on first page
        if page == 0 and items and not _log.__dict__.get("_sample_logged"):
            import json as _json_debug
            _log(f"Email-finder sample item keys: {list(items[0].keys())}")
            _log(f"Email-finder sample item person: {items[0].get('person', {})}")
            _log.__dict__["_sample_logged"] = True

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            # Email finder response has refId as the person reference
            ref_id = item.get("refId", "")

            # Email is in output array, first element has "address" field
            output = item.get("output", [])
            if not isinstance(output, list):
                output = []
            email = ""
            if output and isinstance(output[0], dict):
                email = output[0].get("address", "")

            if not email or not ref_id:
                continue

            person = item.get("person", {})
            if not isinstance(person, dict):
                person = {}

            first_name = person.get("first_name", "")
            last_name = person.get("last_name", "")

            # Extract domain from person's current company experience
            domain = ""
            exp_list = item.get("experience") or []
            if exp_list and isinstance(exp_list, list):
                current_exp = exp_list[0]  # Current position is first
                if isinstance(current_exp, dict):
                    company = current_exp.get("company") or {}
                    if isinstance(company, dict):
                        domain = company.get("domain", "")

            # Always add result; ref_id is the key for mapping back to contacts
            all_results.append({
                "ref_id": ref_id,
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
            })

        if not has_more:
            break
        page += 1

    # Report the outcome to the circuit breaker. Finding any emails always counts as
    # healthy; otherwise honor the poll-loop's outage classification.
    if breaker is not None:
        breaker.record(outage=bool(outage) and not all_results)
    return all_results


def _flatten_to_dicts(node, depth: int = 0) -> list:
    """Walk a nested list/dict structure and return a flat list of dict items.
    Handles cases where the API nests results as lists-of-lists."""
    if depth > 3:
        return []
    out = []
    if isinstance(node, dict):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            out.extend(_flatten_to_dicts(x, depth + 1))
    return out


def _submit_email_finder_job(api_key: str, track_id: str) -> bool:
    """Submit a single email finder job. Returns True if successful."""
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}
    webhook_url = os.getenv("AIARK_EMAIL_FINDER_WEBHOOK", "https://webhook.site/unused")
    try:
        _http_post(
            f"{AIARK_BASE_URL}/people/email-finder",
            {"trackId": track_id, "webhook": webhook_url},
            headers,
        )
        _log(f"Email finder job submitted for trackId {track_id}")
        return True
    except Exception as e:
        error_msg = str(e)
        if _HAS_HTTPX and isinstance(e, httpx.HTTPStatusError):
            error_msg = f"Email finder submit error: {e.response.status_code} {e.response.text[:200]}"
        _log(f"Submission error for trackId {track_id}: {error_msg}")
        return False


def _poll_and_retrieve_emails(api_key: str, track_id: str) -> list:
    """Poll email finder job until completion and retrieve all results."""
    headers = {"X-TOKEN": api_key, "Content-Type": "application/json"}

    # 1. Poll statistics until COMPLETED (max ~10 min)
    for attempt in range(120):
        try:
            stats_data = _http_get(
                f"{AIARK_BASE_URL}/people/email-finder/{track_id}/statistics",
                {}, headers
            )
        except Exception as e:
            _log(f"Stats poll attempt {attempt + 1} failed: {e}")
            time.sleep(2)
            continue

        state = stats_data.get("state", "PENDING")
        stats = stats_data.get("statistics") or {}
        total = stats.get("total", 0)
        found = stats.get("found", 0)
        progress(found, total or 1, f"Email finder [{state}]: {found}/{total} emails found")

        if state in ("COMPLETED", "DONE", "FINISHED", "FAILED"):
            break
        time.sleep(2)

    # 2. Retrieve paginated results
    all_results = []
    page = 0
    while True:
        try:
            data = _http_get(
                f"{AIARK_BASE_URL}/people/email-finder/{track_id}/inquiries",
                {"page": page, "size": 100},
                headers,
            )
        except Exception as e:
            _log(f"Result retrieval failed for trackId {track_id}: {e}")
            break

        # Normalize to a flat list of item dicts
        if isinstance(data, dict):
            raw_items = data.get("content") or data.get("results") or data.get("items") or []
            has_more = not data.get("last", True)
        elif isinstance(data, list):
            raw_items = data
            has_more = False
        else:
            raw_items = []
            has_more = False

        items = _flatten_to_dicts(raw_items)

        if not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue

            email = item.get("email", "")
            if not email:
                continue

            person = item.get("person", {})
            if not isinstance(person, dict):
                person = {}

            first_name = person.get("first_name", "")
            last_name = person.get("last_name", "")
            person_id = person.get("id", "")

            # Extract domain from person's current company experience
            domain = ""
            exp_list = item.get("experience") or []
            if exp_list and isinstance(exp_list, list):
                current_exp = exp_list[0]  # Current position is first
                if isinstance(current_exp, dict):
                    company = current_exp.get("company") or {}
                    if isinstance(company, dict):
                        domain = company.get("domain", "")

            all_results.append({
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
                "ref_id": person_id,
            })

        if not has_more:
            break
        page += 1

    return all_results


def search_and_find_emails(
    domains: list,
    api_key: str,
    job_titles: Optional[list] = None,
    locations: Optional[list] = None,
    seniority: Optional[list] = None,
    industries: Optional[list] = None,
    department: Optional[list] = None,
    employee_min: Optional[int] = None,
    employee_max: Optional[int] = None,
    technologies: Optional[list] = None,
    keywords: Optional[list] = None,
    limit: int = 100,
    no_cache: bool = False,
    dry_run: bool = False,
    **extra_filters,
) -> list:
    """
    Combined search + bulk email finder.

    1. Searches for up to `limit` contacts, collecting one trackId per page.
    2. Submits each trackId to the bulk email finder (one per 100 contacts).
    3. Merges emails back onto contacts using ref_id → contact id matching,
       then falls back to linkedin_url, then (first, last, domain).

    Returns the same normalized contact list as search_people(), with
    'email' populated wherever the email finder found a result.

    limit controls BOTH how many contacts to search and how many emails
    to find — e.g. limit=200 → 2 pages of 100 → 2 email-finder jobs.

    **extra_filters passes through any new filter (exclude_domains, badges,
    job_duration_max_months, etc.) to search_people().
    """
    if domains:
        progress(0, limit, f"Searching {len(domains)} domain(s) for up to {limit} contacts...")
    else:
        progress(0, limit, f"Searching pure ICP contacts for up to {limit} contacts...")

    contacts, track_ids, _, _, _ = search_people(
        domains=domains,
        api_key=api_key,
        job_titles=job_titles,
        locations=locations,
        seniority=seniority,
        industries=industries,
        department=department,
        employee_min=employee_min,
        employee_max=employee_max,
        technologies=technologies,
        keywords=keywords,
        limit=limit,
        no_cache=no_cache,
        dry_run=dry_run,
        **extra_filters,
    )

    if not track_ids:
        _log("No trackIds collected — skipping email finder")
        return contacts

    # Email pre-check: Skip contacts that already have non-empty email field
    contacts_needing_email = [c for c in contacts if not c.get("email")]
    if len(contacts_needing_email) < len(contacts):
        skipped = len(contacts) - len(contacts_needing_email)
        _log(f"Email pre-check: Skipping {skipped} contacts with existing emails")

    if not contacts_needing_email:
        _log("All contacts already have emails — skipping email finder")
        return contacts

    progress(len(contacts_needing_email), limit,
             f"Found {len(contacts_needing_email)} contacts needing emails. Finding emails via {len(track_ids)} trackId(s)...")

    # Build lookups for merging emails back onto contacts (only for contacts needing email)
    id_index: dict = {}        # contact['id'] → list index in original contacts
    linkedin_index: dict = {}  # linkedin_url → list index in original contacts
    name_index: dict = {}      # (first, last, domain) → list index in original contacts

    for i, c in enumerate(contacts):
        # Only index contacts that don't have emails yet
        if c.get("email"):
            continue
        if c.get("id"):
            id_index[c["id"]] = i
        if c.get("linkedin_url"):
            linkedin_index[c["linkedin_url"].rstrip("/")] = i
        key = (
            (c.get("first_name") or "").lower(),
            (c.get("last_name") or "").lower(),
            (c.get("company_domain") or c.get("domain") or "").lower(),
        )
        name_index[key] = i

    emails_found = 0

    # Phase 1: Submit all email finder jobs in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        submit_futures = {
            executor.submit(_submit_email_finder_job, api_key, tid): tid
            for tid in track_ids
        }
        for future in as_completed(submit_futures):
            tid = submit_futures[future]
            try:
                success = future.result()
            except Exception as e:
                _log(f"Submission exception for trackId {tid}: {e}")

    # Phase 2: Poll all email finder jobs concurrently and retrieve results
    all_results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        poll_futures = {
            executor.submit(_poll_and_retrieve_emails, api_key, tid): tid
            for tid in track_ids
        }
        for future in as_completed(poll_futures):
            tid = poll_futures[future]
            try:
                results = future.result()
                all_results.extend(results)
            except Exception as e:
                _log(f"Poll/retrieve failed for trackId {tid}: {e}")

    # Phase 3: Merge all results onto contacts
    for r in all_results:
        # Try ref_id → contact id (most reliable)
        idx = id_index.get(r.get("ref_id"))

        # Fallback: linkedin_url
        if idx is None and r.get("linkedin_url"):
            idx = linkedin_index.get((r["linkedin_url"] or "").rstrip("/"))

        # Last resort: (first, last, domain)
        if idx is None:
            key = (
                (r.get("first_name") or "").lower(),
                (r.get("last_name") or "").lower(),
                (r.get("domain") or "").lower(),
            )
            idx = name_index.get(key)

        if idx is not None and not contacts[idx].get("email"):
            contacts[idx]["email"] = r["email"]
            emails_found += 1

    progress(emails_found, len(contacts),
             f"Complete: {emails_found}/{len(contacts)} emails found")
    return contacts


# ── Phase 1 & 3 Helper Functions ─────────────────────────────────────────────

def generate_review_url(filters: dict, domains: list) -> str:
    """
    Generate AI Ark review URL with all filters except domains.
    Domains cannot be embedded in URLs due to platform limitations.
    """
    params = {}
    if filters.get("job_titles"):
        params["title"] = ",".join(filters["job_titles"])
    if filters.get("locations"):
        params["location"] = ",".join(filters["locations"])
    if filters.get("seniority"):
        params["seniority"] = ",".join(filters["seniority"])
    if filters.get("industries"):
        params["industry"] = ",".join(filters["industries"])
    if filters.get("department"):
        params["department"] = ",".join(filters["department"])
    if filters.get("employee_min"):
        params["employee_min"] = str(filters["employee_min"])
    if filters.get("employee_max"):
        params["employee_max"] = str(filters["employee_max"])

    query_string = urllib.parse.urlencode(params, doseq=True)
    url = f"https://app.ai-ark.com/search/people?{query_string}" if query_string else "https://app.ai-ark.com/search/people"
    return url


def write_domain_paste_file(domains: list, filepath: str = "ai-ark-domains-paste.md") -> None:
    """Write markdown file with domain list for copy-paste into AI Ark platform."""
    if not domains:
        return

    lines = [
        "# AI Ark Domain Paste List",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat()}",
        "\n> Domains do not carry into the review URL (platform limitation).",
        "> Copy the list below and paste it into the **bulk include** box in the AI Ark platform.",
        f"\n## Include ({len(domains)} domains)",
        "\n```",
        ", ".join(sorted(domains)),
        "```",
    ]
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"📋 {len(domains)} domain(s) → {filepath}", file=sys.stderr)


# ── JSON mode ────────────────────────────────────────────────────────────────

def json_mode():
    """Read JSON from stdin, process, write JSON to stdout."""
    raw = sys.stdin.read()
    params = json.loads(raw)

    # Resolve API key
    api_key = params.get("api_key") or os.getenv("AIARK_API_KEY", "")
    if not api_key:
        print(json.dumps({"error": "No api_key provided and AIARK_API_KEY not set"}))
        sys.exit(1)

    # Export single person (action=export_single)
    action = params.get("action", "search")
    if action == "export_single":
        person_id = params.get("id") or params.get("person_id")
        linkedin_url = params.get("url") or params.get("linkedin_url")
        progress(0, 1, "Fetching verified email...")
        try:
            contact = export_single_person(api_key=api_key, person_id=person_id, linkedin_url=linkedin_url)
        except Exception as e:
            error_msg = str(e)
            if _HAS_HTTPX and isinstance(e, httpx.HTTPStatusError):
                error_msg = f"AI Ark API error: {e.response.status_code} {e.response.text[:200]}"
            print(json.dumps({"error": error_msg}))
            sys.exit(1)
        progress(1, 1, "Complete")
        print(json.dumps({"contact": contact}))
        return

    # Export bulk (action=export_bulk)
    if action == "export_bulk":
        track_id = params.get("track_id") or params.get("trackId")
        if not track_id:
            print(json.dumps({"error": "No track_id provided. Use 'track_id' key."}))
            sys.exit(1)
        progress(0, 1, "Submitting bulk export job...")
        try:
            result = export_bulk(api_key=api_key, track_id=track_id)
        except Exception as e:
            error_msg = str(e)
            if _HAS_HTTPX and isinstance(e, httpx.HTTPStatusError):
                error_msg = f"AI Ark API error: {e.response.status_code} {e.response.text[:200]}"
            print(json.dumps({"error": error_msg}))
            sys.exit(1)
        progress(1, 1, "Complete")
        print(json.dumps({"export_result": result}))
        return

    # Check credits (action=credits)
    if action == "credits":
        progress(0, 1, "Checking account credits...")
        try:
            result = check_credits(api_key=api_key)
        except Exception as e:
            error_msg = str(e)
            if _HAS_HTTPX and isinstance(e, httpx.HTTPStatusError):
                error_msg = f"AI Ark API error: {e.response.status_code} {e.response.text[:200]}"
            print(json.dumps({"error": error_msg}))
            sys.exit(1)
        progress(1, 1, "Complete")
        print(json.dumps({"credits": result}))
        return

    # Backward compat: "company_domain" (old BuzzApp) -> "domains" array
    domains = params.get("domains", [])
    if not domains and params.get("company_domain"):
        domains = [params["company_domain"]]
    if not domains and params.get("domain"):
        domains = [params["domain"]]
    if not domains:
        print(json.dumps({"error": "No domain(s) provided. Use 'domains' array or 'company_domain' string."}))
        sys.exit(1)

    job_titles = params.get("job_titles")
    locations = params.get("locations")
    seniority = params.get("seniority", params.get("seniority_levels"))
    industries = params.get("industries")
    department = params.get("department")
    employee_min = params.get("employee_min")
    employee_max = params.get("employee_max")
    if not (employee_min or employee_max) and params.get("employee_range"):
        parts = str(params["employee_range"]).split(",")
        if len(parts) == 2:
            try:
                employee_min = int(parts[0])
                employee_max = int(parts[1])
            except ValueError:
                pass
    technologies = params.get("technologies")
    keywords = params.get("keywords")
    limit = params.get("limit", 50)
    find_emails = params.get("find_emails", False)
    no_cache = params.get("no_cache", False)
    dry_run = params.get("dry_run", False)
    plan = params.get("plan", "starter")
    budget_limit = params.get("budget_limit")

    # New filters ported from LeadGrow CLI
    extra_filters = {
        "exclude_domains": params.get("exclude_domains"),
        "exclude_titles": params.get("exclude_titles"),
        "exclude_seniority": params.get("exclude_seniority"),
        "exclude_industries": params.get("exclude_industries"),
        "exclude_locations": params.get("exclude_locations"),
        "exclude_department": params.get("exclude_department"),
        "exclude_badges": params.get("exclude_badges"),
        "exclude_company_names": params.get("exclude_company_names"),
        "badges": params.get("badges"),
        "job_duration_min_months": params.get("job_duration_min_months"),
        "job_duration_max_months": params.get("job_duration_max_months"),
        "previous_titles": params.get("previous_titles"),
        "skills": params.get("skills"),
        "funding_types": params.get("funding_types"),
        "revenue_min": params.get("revenue_min"),
        "revenue_max": params.get("revenue_max"),
        "geo": params.get("geo"),
        "match_mode": params.get("match_mode", "SMART"),
    }
    # Drop None values to avoid passing them to search_people
    extra_filters = {k: v for k, v in extra_filters.items() if v is not None}

    # Phase 3: Initialize cost tracker
    cost_tracker = CostTracker(plan=plan, budget_limit=budget_limit)

    # Phase 1: Generate review URL and domain paste file
    filters_for_url = {
        "job_titles": job_titles,
        "locations": locations,
        "seniority": seniority,
        "industries": industries,
        "department": department,
        "employee_min": employee_min,
        "employee_max": employee_max,
    }
    review_url = generate_review_url(filters_for_url, domains)
    write_domain_paste_file(domains)
    print(f"🔗 Review in AI Ark: {review_url}", file=sys.stderr)

    # Phase 3: Dry-run cost estimation
    if dry_run:
        # Batch optimization: 1 query for all domains instead of N queries
        estimated_cost = cost_tracker.estimate_aiark_query(
            query_count=1,  # Single batch query
            records_returned=limit * len(domains),  # Total records across all domains
            plan=plan
        )
        budget_remaining = budget_limit - estimated_cost if budget_limit else None
        output = {
            "dry_run": True,
            "estimated_cost": round(estimated_cost, 4),
            "plan": plan,
            "message": f"Dry-run: 1 batch query for {len(domains)} domain(s), estimated ${estimated_cost:.2f} (was ${len(domains) * 0.18:.2f} pre-batch)",
        }
        if budget_remaining is not None:
            output["budget_remaining"] = round(budget_remaining, 4)
        progress(1, 1, "Dry-run complete")
        print(json.dumps(output))
        return

    progress(0, len(domains), f"Searching contacts at {len(domains)} domain(s)...")

    try:
        if find_emails:
            contacts = search_and_find_emails(
                domains=domains,
                api_key=api_key,
                job_titles=job_titles,
                locations=locations,
                seniority=seniority,
                industries=industries,
                department=department,
                employee_min=employee_min,
                employee_max=employee_max,
                technologies=technologies,
                keywords=keywords,
                limit=limit,
                no_cache=no_cache,
                dry_run=dry_run,
                **extra_filters,
            )
            # search_and_find_emails returns contacts only; construct minimal _meta
            domain_results = {}
            tier_used = {}
            credits_used = 0
        else:
            contacts, _, domain_results, tier_used, credits_used = search_people(
                domains=domains,
                api_key=api_key,
                job_titles=job_titles,
                locations=locations,
                seniority=seniority,
                industries=industries,
                department=department,
                employee_min=employee_min,
                employee_max=employee_max,
                technologies=technologies,
                keywords=keywords,
                limit=limit,
                no_cache=no_cache,
                dry_run=dry_run,
                **extra_filters,
            )

        # Phase 3: Track costs (batch model: 1 query for all domains)
        cost_tracker.aiark_query(
            query_count=1,  # Single batch query
            records_returned=len(contacts),
            plan=plan
        )

    except BudgetExceededError as e:
        error_msg = str(e)
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        output = {
            "error": error_msg,
            "budget_exceeded": True,
            "timestamp": timestamp,
        }
        print(json.dumps(output))
        sys.exit(1)
    except Exception as e:
        error_msg = str(e)
        if _HAS_HTTPX and isinstance(e, httpx.HTTPStatusError):
            error_msg = f"AI Ark API error: {e.response.status_code} {e.response.text[:200]}"
        print(json.dumps({"error": error_msg}))
        sys.exit(1)

    # Construct _meta block with cost breakdown
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cost_breakdown = {
        "total_cost": round(cost_tracker.total_cost(), 4),
        "by_service": {k: round(v, 4) for k, v in cost_tracker.by_service.items()},
        "plan": plan,
    }
    if budget_limit:
        budget_info = {
            "limit": budget_limit,
            "used": round(cost_tracker.session_total, 4),
            "remaining": round(budget_limit - cost_tracker.session_total, 4),
            "percent_used": round((cost_tracker.session_total / budget_limit) * 100, 1),
        }
        cost_breakdown["budget"] = budget_info

    meta = {
        "total_found": len(contacts),
        "domain_results": domain_results,
        "tier_used": tier_used,
        "credits_used": credits_used,
        "cost_breakdown": cost_breakdown,
        "timestamp": timestamp,
    }

    output = {"contacts": contacts, "_meta": meta}
    progress(1, 1, f"Complete: {len(contacts)} contacts")
    print(json.dumps(output))


# ── CLI mode ─────────────────────────────────────────────────────────────────

def cli_mode():
    """Argparse-based CLI interface."""
    parser = argparse.ArgumentParser(
        description="AI Ark Contact Search",
        epilog="Set AIARK_API_KEY env var or pass --api-key"
    )
    sub = parser.add_subparsers(dest="command")

    # search subcommand
    s = sub.add_parser("search", help="Search for contacts at domain(s)")
    s.add_argument("domains", nargs="*", help="Company domain(s) (optional if using other filters)")
    s.add_argument("--titles", nargs="*", help="Job title filter(s)")
    s.add_argument("--locations", nargs="*", help="Location filter(s)")
    s.add_argument("--seniority", nargs="*", help="Seniority filter(s): founder, c_suite, vp, director, head, manager, senior, mid-level, entry, intern")
    s.add_argument("--industries", nargs="*", help="Industry filter(s)")
    s.add_argument("--department", nargs="*", help="Department function(s): Sales, Engineering, Marketing")
    s.add_argument("--employee-min", type=int, help="Min employee count")
    s.add_argument("--employee-max", type=int, help="Max employee count")
    s.add_argument("--technologies", nargs="*", help="Technology filter(s)")
    s.add_argument("--keywords", nargs="*", help="Keyword filter(s)")
    s.add_argument("-n", "--limit", type=int, default=50, help="Max results (default: 50)")
    s.add_argument("--find-emails", action="store_true",
                   help="Also run bulk email finder on results (costs credits)")

    # Exclusion filters (ported from LeadGrow CLI)
    s.add_argument("--exclude-domains", nargs="*", help="Exclude these company domains")
    s.add_argument("--exclude-titles", nargs="*", help="Exclude these job titles (e.g. 'Intern', 'Assistant')")
    s.add_argument("--exclude-seniority", nargs="*", help="Exclude these seniority levels")
    s.add_argument("--exclude-industries", nargs="*", help="Exclude these industries")
    s.add_argument("--exclude-locations", nargs="*", help="Exclude these locations")
    s.add_argument("--exclude-department", nargs="*", help="Exclude these departments")
    s.add_argument("--exclude-badges", nargs="*", help="Exclude these badges (e.g. OPEN_TO_WORK)")
    s.add_argument("--exclude-company-names", nargs="*", help="Exclude these company names")

    # Intent / signal filters
    s.add_argument("--badges", nargs="*",
                   help="Profile badges: HIRING, OPEN_TO_WORK, CREATOR, INFLUENCER, PAID_SOCIAL_MEMBERS")
    s.add_argument("--job-duration-min", type=int,
                   help="Min months in current role")
    s.add_argument("--job-duration-max", type=int,
                   help="Max months in current role (use for fresh hires, e.g. 3)")
    s.add_argument("--previous-titles", nargs="*", help="Previous job title filter(s)")
    s.add_argument("--skills", nargs="*", help="Skills filter(s)")

    # Company filters
    s.add_argument("--funding-types", nargs="*",
                   help="Funding types: SEED, SERIES_A, SERIES_B, SERIES_C, etc.")
    s.add_argument("--revenue-min", type=int, help="Min revenue in dollars")
    s.add_argument("--revenue-max", type=int, help="Max revenue in dollars")
    s.add_argument("--geo", help="Geo radius: 'lat,lng,radius' e.g. '40.71,-74.00,50km'")

    s.add_argument("--match-mode", choices=["SMART", "WORD", "STRICT"], default="SMART",
                   help="Search match mode (default: SMART)")
    s.add_argument("--plan", choices=["starter", "pro", "team", "enterprise"], default="starter",
                   help="AI Ark pricing plan (default: starter)")
    s.add_argument("--budget-limit", type=float, help="Budget limit in dollars for cost tracking")
    s.add_argument("--dry-run", action="store_true", help="Estimate cost without API call")
    s.add_argument("--no-cache", action="store_true", help="Bypass SQLite cache (force fresh API call)")
    s.add_argument("--api-key", help="AI Ark API key")
    s.add_argument("-o", "--output", help="Output JSON file")
    s.add_argument("--format", choices=["json", "csv", "table"], default="json",
                   help="Output format (default: json)")
    s.add_argument("--exclude-domain-file", help="Load domains to exclude from file (CSV or one per line)")

    # export subcommand — get verified email for a single person or bulk export
    e = sub.add_parser("export", help="Get verified email (single person or bulk from trackId)")
    e_group = e.add_mutually_exclusive_group(required=True)
    e_group.add_argument("--id", help="AI.ARK person UUID (from search results)")
    e_group.add_argument("--linkedin", help="LinkedIn profile URL")
    e_group.add_argument("--track-id", help="Track ID from previous people search (bulk export with emailVerified)")
    e.add_argument("--api-key", help="AI Ark API key")
    e.add_argument("-o", "--output", help="Output JSON file")

    # lookup subcommand — reverse lookup email to person profile
    l = sub.add_parser("lookup", help="Reverse lookup: email → person profile + LinkedIn URL")
    l.add_argument("--email", required=True, help="Email address to look up")
    l.add_argument("--api-key", help="AI Ark API key")
    l.add_argument("-o", "--output", help="Output JSON file")

    # phone subcommand — find mobile phone from LinkedIn URL
    p = sub.add_parser("phone", help="Find mobile phone from LinkedIn URL")
    p.add_argument("--linkedin", required=True, help="LinkedIn profile URL")
    p.add_argument("--api-key", help="AI Ark API key")
    p.add_argument("-o", "--output", help="Output JSON file")

    # analyze subcommand — analyze personality + generate selling tips
    a = sub.add_parser("analyze", help="Analyze personality profile + generate selling tips from LinkedIn")
    a.add_argument("--linkedin", required=True, help="LinkedIn profile URL")
    a.add_argument("--api-key", help="AI Ark API key")
    a.add_argument("-o", "--output", help="Output JSON file")

    # credits subcommand — check account credits
    c = sub.add_parser("credits", help="Check remaining account credits")
    c.add_argument("--api-key", help="AI Ark API key")
    c.add_argument("-o", "--output", help="Output JSON file")

    # costs subcommand — show session cost breakdown
    cos = sub.add_parser("costs", help="Show session cost breakdown")
    cos.add_argument("--reset", action="store_true", help="Reset cost tracking")
    cos.add_argument("--format", choices=["json", "table"], default="table",
                     help="Output format (default: table)")
    cos.add_argument("-o", "--output", help="Output file")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    api_key = getattr(args, "api_key", None) or os.getenv("AIARK_API_KEY", "")
    if not api_key:
        print("ERROR: Set AIARK_API_KEY env var or pass --api-key", file=sys.stderr)
        sys.exit(1)

    if args.command == "search":
        # Parse geo flag if provided: "lat,lng,radius" or "lat,lng,radiuskm"
        geo_dict = None
        if getattr(args, "geo", None):
            parts = [p.strip() for p in args.geo.split(",")]
            if len(parts) >= 3:
                radius_str = parts[2]
                unit = "km"
                if radius_str.endswith("km"):
                    radius_str = radius_str[:-2]
                elif radius_str.endswith("mi"):
                    radius_str = radius_str[:-2]
                    unit = "mi"
                try:
                    geo_dict = {
                        "lat": float(parts[0]),
                        "lng": float(parts[1]),
                        "radius": float(radius_str),
                        "unit": unit,
                    }
                except ValueError:
                    print(f"WARNING: Could not parse --geo '{args.geo}'", file=sys.stderr)

        plan = getattr(args, "plan", "starter")
        budget_limit = getattr(args, "budget_limit", None)
        dry_run = getattr(args, "dry_run", False)

        # Phase 1: Generate review URL and domain paste file
        domains = args.domains or []
        if domains:
            filters_for_url = {
                "job_titles": args.titles,
                "locations": args.locations,
                "seniority": args.seniority,
                "industries": args.industries,
                "department": args.department,
                "employee_min": args.employee_min,
                "employee_max": args.employee_max,
            }
            review_url = generate_review_url(filters_for_url, domains)
            write_domain_paste_file(domains)
            print(f"🔗 Review in AI Ark: {review_url}", file=sys.stderr)

        # Phase 3: Initialize cost tracker
        cost_tracker = CostTracker(plan=plan, budget_limit=budget_limit)

        # Phase 3: Dry-run cost estimation
        if dry_run:
            estimated_cost = cost_tracker.estimate_aiark_query(
                query_count=len(domains),
                records_returned=args.limit,
                plan=plan
            )
            budget_remaining = budget_limit - estimated_cost if budget_limit else None
            message = f"Dry-run: estimated cost ${estimated_cost:.2f} for {len(domains)} domain(s) × {args.limit} results/domain"
            print(message, file=sys.stderr)
            if budget_remaining is not None:
                print(f"Budget remaining: ${budget_remaining:.2f}", file=sys.stderr)
            return

        # Load excluded domains from file if provided
        exclude_domains = list(getattr(args, "exclude_domains", None) or [])
        if getattr(args, "exclude_domain_file", None):
            try:
                file_domains = load_domains_from_file(args.exclude_domain_file)
                exclude_domains.extend(file_domains)
                print(f"Loaded {len(file_domains)} excluded domains from {args.exclude_domain_file}", file=sys.stderr)
            except FileNotFoundError:
                print(f"ERROR: Exclude domain file not found: {args.exclude_domain_file}", file=sys.stderr)
                sys.exit(1)

        shared_kwargs = dict(
            domains=domains,
            api_key=api_key,
            job_titles=args.titles,
            locations=args.locations,
            seniority=args.seniority,
            industries=args.industries,
            department=args.department,
            employee_min=args.employee_min,
            employee_max=args.employee_max,
            technologies=args.technologies,
            keywords=args.keywords,
            limit=args.limit,
            # Exclusions
            exclude_domains=exclude_domains if exclude_domains else None,
            exclude_titles=getattr(args, "exclude_titles", None),
            exclude_seniority=getattr(args, "exclude_seniority", None),
            exclude_industries=getattr(args, "exclude_industries", None),
            exclude_locations=getattr(args, "exclude_locations", None),
            exclude_department=getattr(args, "exclude_department", None),
            exclude_badges=getattr(args, "exclude_badges", None),
            exclude_company_names=getattr(args, "exclude_company_names", None),
            # Intent / signal
            badges=getattr(args, "badges", None),
            job_duration_min_months=getattr(args, "job_duration_min", None),
            job_duration_max_months=getattr(args, "job_duration_max", None),
            previous_titles=getattr(args, "previous_titles", None),
            skills=getattr(args, "skills", None),
            # Company
            funding_types=getattr(args, "funding_types", None),
            revenue_min=getattr(args, "revenue_min", None),
            revenue_max=getattr(args, "revenue_max", None),
            geo=geo_dict,
            match_mode=getattr(args, "match_mode", "SMART"),
            no_cache=getattr(args, "no_cache", False),
        )

        try:
            if getattr(args, "find_emails", False):
                contacts = search_and_find_emails(**shared_kwargs)
                domain_results = {}
                tier_used = {}
                credits_used = 0
            else:
                contacts, _, domain_results, tier_used, credits_used = search_people(**shared_kwargs)

            # Phase 3: Track costs (batch model: 1 query for all domains)
            cost_tracker.aiark_query(
                query_count=1,  # Single batch query
                records_returned=len(contacts),
                plan=plan
            )

        except BudgetExceededError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(contacts)} contacts", file=sys.stderr)
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build cost breakdown for metadata
        cost_breakdown = {
            "total_cost": round(cost_tracker.total_cost(), 4),
            "by_service": {k: round(v, 4) for k, v in cost_tracker.by_service.items()},
            "plan": plan,
        }
        if budget_limit:
            budget_info = {
                "limit": budget_limit,
                "used": round(cost_tracker.session_total, 4),
                "remaining": round(budget_limit - cost_tracker.session_total, 4),
                "percent_used": round((cost_tracker.session_total / budget_limit) * 100, 1),
            }
            cost_breakdown["budget"] = budget_info

        meta = {
            "total_found": len(contacts),
            "domain_results": domain_results,
            "tier_used": tier_used,
            "credits_used": credits_used,
            "cost_breakdown": cost_breakdown,
            "timestamp": timestamp,
        }

        # Apply --format parameter
        output_format = getattr(args, "format", "json")
        if output_format == "json":
            output = json.dumps({"contacts": contacts, "_meta": meta}, indent=2)
        else:
            output = format_output(contacts, output_format)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output)

    elif args.command == "export":
        track_id = getattr(args, "track_id", None)
        if track_id:
            result = export_bulk(api_key=api_key, track_id=track_id)
            print(f"Bulk export status: {result.get('status', 'unknown')}", file=sys.stderr)
            output = json.dumps({"export_result": result}, indent=2)
        else:
            contact = export_single_person(
                api_key=api_key,
                person_id=getattr(args, "id", None),
                linkedin_url=getattr(args, "linkedin", None),
            )
            print(f"Email: {contact.get('email') or '(not found)'}", file=sys.stderr)
            output = json.dumps({"contact": contact}, indent=2)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output)

    elif args.command == "lookup":
        result = reverse_lookup(api_key=api_key, email=args.email)
        print(f"Found: {result.get('first_name')} {result.get('last_name')} ({result.get('email')})", file=sys.stderr)
        output = json.dumps({"contact": result}, indent=2)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output)

    elif args.command == "phone":
        result = find_mobile_phone(api_key=api_key, linkedin_url=args.linkedin)
        phone = result.get("phone")
        if phone:
            print(f"Phone: {phone}", file=sys.stderr)
        else:
            print(f"No phone found", file=sys.stderr)
        output = json.dumps({"phone_data": result}, indent=2)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output)

    elif args.command == "analyze":
        result = analyze_person(api_key=api_key, linkedin_url=args.linkedin)
        disc = result.get("disc_profile", {})
        print(f"DISC: {disc.get('primary_type', 'Unknown')}", file=sys.stderr)
        output = json.dumps({"analysis": result}, indent=2)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output)

    elif args.command == "credits":
        result = check_credits(api_key=api_key)
        available = result.get("available_credits", result.get("credits", "unknown"))
        total = result.get("total_credits", "unknown")
        print(f"Available: {available} / Total: {total}", file=sys.stderr)
        output = json.dumps({"credits_data": result}, indent=2)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output)

    elif args.command == "costs":
        storage = ConfigStorage()
        if args.reset:
            count = storage.reset_costs()
            print(f"Reset {count} cost entries.", file=sys.stderr)
            return

        costs = storage.get_session_costs()
        summary = storage.get_costs_summary()
        total = sum(summary.values())

        if not costs:
            print("No costs recorded in this session.", file=sys.stderr)
            return

        if args.format == "json":
            output = json.dumps({"costs": costs, "summary": summary, "total": total}, indent=2)
        else:  # table format
            lines = ["Session Costs", "=" * 60]
            for service, service_total in summary.items():
                lines.append(f"  {service:<40} ${service_total:>8.4f}")
            lines.append("=" * 60)
            lines.append(f"  {'TOTAL':<40} ${total:>8.4f}")
            output = "\n".join(lines)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output)


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Detect mode: if explicit subcommand is provided, use CLI mode; else try JSON mode
    if len(sys.argv) > 1 and sys.argv[1] in ("search", "export", "lookup", "phone", "analyze", "credits", "costs"):
        cli_mode()
    elif not sys.stdin.isatty():
        json_mode()
    else:
        cli_mode()
