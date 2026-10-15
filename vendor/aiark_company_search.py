#!/usr/bin/env python3
"""
AI.ARK Company Search — Hybrid JSON stdin/stdout + CLI interface.

JSON mode (stdin piped):
  echo '{"industries":["software"],"locations":["United States"],"limit":25}' | python aiark_company_search.py

CLI mode (interactive):
  python aiark_company_search.py search --industries software saas --locations "United States" -n 25
  python aiark_company_search.py search --domains salesforce.com --limit 50

JSON input schema:
  {
    "domains":         ["salesforce.com"],
    "lookalike_urls":  ["https://linkedin.com/company/salesforce"],
    "industries":      ["software", "saas"],
    "locations":       ["United States"],
    "employee_min":    50,
    "employee_max":    500,
    "employee_range":  "50,500",
    "funding_types":   ["SERIES_A", "SERIES_B"],
    "technologies":    ["Salesforce", "HubSpot"],
    "limit":           50,
    "api_key":         "..."
  }

JSON output:
  { "companies": [{ "domain": "...", "name": "...", ... }] }

Progress (stderr JSON lines):
  {"progress": 1, "total": 2, "message": "Searching companies..."}
"""

import argparse
import json
import os
import sys
from typing import Optional

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


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_post(url: str, payload: dict, headers: dict, timeout: float = 60.0) -> dict:
    """POST request using httpx (preferred) or requests fallback. Retries on HTTP 429."""
    import time
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


# ── Progress reporting ───────────────────────────────────────────────────────

def progress(current: int, total: int, message: str):
    print(json.dumps({"progress": current, "total": total, "message": message}),
          file=sys.stderr, flush=True)


def _log(message: str):
    print(json.dumps({"log": message}), file=sys.stderr, flush=True)


# ── Core API call ────────────────────────────────────────────────────────────

def _build_company_account_filters(
    *,
    domain_subset: Optional[list] = None,
    exclude_domains: Optional[list] = None,
    industries: Optional[list] = None,
    locations: Optional[list] = None,
    employee_min: Optional[int] = None,
    employee_max: Optional[int] = None,
    funding_types: Optional[list] = None,
    technologies: Optional[list] = None,
    keywords: Optional[list] = None,
) -> dict:
    """Build the `account` filter block. Domain filter is per-call so the
    multi-domain loop can pass one domain at a time. `exclude_domains` is global —
    it maps to the AllAny `exclude` arm so AI.ARK drops those companies server-side
    (used by 'generate new leads' to skip companies already in the campaign)."""
    account_filters: dict = {}

    if domain_subset or exclude_domains:
        # AllAny filter shape: { any: { include: [...], exclude: [...] } }
        domain_filter: dict = {}
        if domain_subset:
            domain_filter["include"] = list(domain_subset)
        if exclude_domains:
            domain_filter["exclude"] = list(exclude_domains)
        account_filters["domain"] = {"any": domain_filter}
    if industries:
        account_filters["industries"] = {
            "any": {"include": {"mode": "WORD", "content": industries}}
        }
    if locations:
        account_filters["location"] = {"any": {"include": locations}}
    if employee_min is not None or employee_max is not None:
        start = employee_min if employee_min is not None else 0
        end = employee_max if employee_max is not None else 1_000_000
        account_filters["employeeSize"] = {
            "type": "RANGE",
            "range": [{"start": start, "end": end}],
        }
    if technologies:
        account_filters["technologies"] = {
            "any": {"include": {"mode": "WORD", "content": technologies}}
        }
    if keywords:
        account_filters["keyword"] = {
            "any": {"include": {"mode": "WORD", "content": keywords}}
        }
    if funding_types:
        account_filters["funding"] = {"type": funding_types}

    return account_filters


def _paginated_company_query(
    *,
    headers: dict,
    base_payload: dict,
    limit: int,
    label: str = "",
) -> list:
    """Run a paginated POST /companies query and return normalized companies.

    `base_payload` should already include `account` and optionally
    `lookalikeDomains`; this helper only manages page / size / pagination.
    """
    page_size = min(max(1, limit), 100)
    max_pages = max(1, (limit + page_size - 1) // page_size)
    out: list = []

    for page_num in range(max_pages):
        payload = {**base_payload, "page": page_num, "size": page_size}
        _log(f"Sending payload to AI.ARK ({label or 'companies'}): {json.dumps(payload)}")
        data = _http_post(f"{AIARK_BASE_URL}/companies", payload, headers)

        raw = data.get("content", data.get("results", data.get("companies", [])))
        if not raw:
            break

        for item in raw:
            if isinstance(item, dict):
                out.append(_normalize_company(item))

        total_pages = data.get("totalPages", 1)
        if page_num + 1 >= total_pages or len(out) >= limit:
            break

        progress(len(out), limit, f"Fetched {len(out)} companies (page {page_num + 1})...")

    return out[:limit]


def search_companies(
    api_key: str,
    domains: Optional[list] = None,
    lookalike_urls: Optional[list] = None,
    industries: Optional[list] = None,
    locations: Optional[list] = None,
    employee_min: Optional[int] = None,
    employee_max: Optional[int] = None,
    funding_types: Optional[list] = None,
    technologies: Optional[list] = None,
    keywords: Optional[list] = None,
    exclude_domains: Optional[list] = None,
    limit: int = 50,
) -> list:
    """POST /v1/companies — search for companies by filters.

    Behavior matches services/leads-cli/ai-ark/src/commands/companies-search.ts:
      * When `domains` has >= 2 entries, queries each domain separately and
        merges the results (previously all domains were sent in one `any`
        filter capped at 5, which is why we were seeing shortened results).
      * Single-domain / pure-ICP / lookalike queries still go in one call.

    Auth: X-TOKEN header. Returns normalized list of company dicts, deduped
    by domain.
    """
    headers = {
        "X-TOKEN": api_key,
        "Content-Type": "application/json",
    }

    # Multi-domain → loop per domain (matches TypeScript CLI)
    if domains and len(domains) >= 2:
        merged: list = []
        seen: set = set()
        for i, domain in enumerate(domains, start=1):
            account_filters = _build_company_account_filters(
                domain_subset=[domain],
                exclude_domains=exclude_domains,
                industries=industries,
                locations=locations,
                employee_min=employee_min,
                employee_max=employee_max,
                funding_types=funding_types,
                technologies=technologies,
                keywords=keywords,
            )
            base_payload: dict = {}
            if account_filters:
                base_payload["account"] = account_filters
            # lookalikeDomains is global — only attach on the first domain.
            if lookalike_urls and i == 1:
                base_payload["lookalikeDomains"] = lookalike_urls[:5]

            per_domain = _paginated_company_query(
                headers=headers,
                base_payload=base_payload,
                limit=limit,
                label=f"domain {i}/{len(domains)}: {domain}",
            )
            for c in per_domain:
                d = (c.get("domain") or "").lower().strip().replace("www.", "")
                if d and d not in seen:
                    seen.add(d)
                    merged.append(c)
            progress(len(merged), limit, f"Per-domain: {i}/{len(domains)} ({len(merged)} unique)")
        return merged

    # Single-domain, pure-ICP, or lookalike-only query → one paginated call
    account_filters = _build_company_account_filters(
        domain_subset=domains,
        exclude_domains=exclude_domains,
        industries=industries,
        locations=locations,
        employee_min=employee_min,
        employee_max=employee_max,
        funding_types=funding_types,
        technologies=technologies,
        keywords=keywords,
    )
    base_payload = {}
    if account_filters:
        base_payload["account"] = account_filters
    if lookalike_urls:
        base_payload["lookalikeDomains"] = lookalike_urls[:5]

    return _paginated_company_query(
        headers=headers,
        base_payload=base_payload,
        limit=limit,
        label="single-call",
    )


# ── Normalization ────────────────────────────────────────────────────────────

def _normalize_company(item: dict) -> dict:
    """
    Normalize raw AI.ARK company response to a flat dict.
    Matches DiscoLike output shape closely so results can flow
    into the same discovery_results table and companies upsert.

    AI.ARK documented response structure (nested):
      id, summary: { name, description, founded_year, type, staff: { total } },
      link: { domain, website, linkedin, twitter, crunchbase },
      contact: { email, phone: { raw } },
      financial: { funding: { type, total_amount, last_amount, rounds: [...] } },
      location: { headquarter: { city, state, street, postal_code, country } },
      technologies: [{ name, category }],
      industries: [string],
      keywords: [string],
      last_updated: string

    Also handles flat shape in case API differs from docs.
    """
    # ── Sub-objects (nested shape) ──────────────────────────────────────────
    summary = item.get("summary", {}) or {}
    link_obj = item.get("link", {}) or {}
    contact_obj = item.get("contact", {}) or {}
    financial = item.get("financial", {}) or {}
    loc_obj = item.get("location", {}) or {}

    # ── Basic identity ───────────────────────────────────────────────────────
    # Prefer nested summary, fall back to flat top-level keys
    name = summary.get("name", "") or item.get("name", "")
    domain = link_obj.get("domain", "") or item.get("domain", "")
    website_url = link_obj.get("website", "") or item.get("website", "") or (f"https://{domain}" if domain else "")
    linkedin_url = (
        link_obj.get("linkedin", "")
        or item.get("linkedinUrl", "")
        or item.get("linkedin_url", "")
        or item.get("linkedin", "")
        or ""
    )
    twitter_url = link_obj.get("twitter", "") or item.get("twitter", "") or ""
    crunchbase_url = link_obj.get("crunchbase", "") or ""
    description = summary.get("description", "") or item.get("description", "")
    founded_year = str(summary.get("founded_year", "") or item.get("founded_year", "") or "")
    company_type = str(summary.get("type", "") or item.get("type", "") or "")
    last_updated = item.get("last_updated", "")

    # ── Industry ─────────────────────────────────────────────────────────────
    raw_industries = item.get("industries", []) or []
    if isinstance(raw_industries, list):
        industry = ", ".join(str(i) for i in raw_industries if i)
    else:
        industry = str(raw_industries) if raw_industries else ""

    # ── Employee count ────────────────────────────────────────────────────────
    staff_obj = summary.get("staff", {}) or item.get("staff", {}) or {}
    if isinstance(staff_obj, dict):
        employee_count = str(staff_obj.get("total", "") or "")
    else:
        employee_count = str(item.get("employees", "") or "")

    # ── Location ─────────────────────────────────────────────────────────────
    # Docs use "headquarter" (singular); old observed shape used "headquarters"
    hq = loc_obj.get("headquarter", loc_obj.get("headquarters", {})) or {}
    city = hq.get("city", "") if isinstance(hq, dict) else ""
    hq_state = hq.get("state", "") if isinstance(hq, dict) else ""
    country = hq.get("country", "") if isinstance(hq, dict) else ""
    street = hq.get("street", "") if isinstance(hq, dict) else ""
    postal_code = hq.get("postal_code", "") if isinstance(hq, dict) else ""
    location_parts = [p for p in [city, hq_state, country] if p]
    location = ", ".join(location_parts)

    # ── Contact (company-level email/phone) ───────────────────────────────────
    company_email = contact_obj.get("email", "") or ""
    phone_obj = contact_obj.get("phone", {}) or {}
    company_phone = phone_obj.get("raw", "") if isinstance(phone_obj, dict) else str(phone_obj or "")

    # ── Funding ───────────────────────────────────────────────────────────────
    # Docs nest under financial.funding; old shape had flat funding
    funding_obj = financial.get("funding", {}) or item.get("funding", {}) or {}
    total_funding = ""
    funding_stage = ""
    investors_str = ""
    last_funding_amount = ""
    if isinstance(funding_obj, dict):
        # Docs use total_amount / last_amount; old shape used totalFunding
        total_funding = str(
            funding_obj.get("total_amount", funding_obj.get("totalFunding", "")) or ""
        )
        last_funding_amount = str(funding_obj.get("last_amount", "") or "")
        rounds = funding_obj.get("rounds", []) or []
        if rounds and isinstance(rounds, list):
            last_round = rounds[-1]
            if isinstance(last_round, dict):
                funding_stage = last_round.get("type", "")
        investors = funding_obj.get("investors", []) or []
        investor_names = []
        for inv in investors[:5]:
            if isinstance(inv, dict):
                investor_names.append(inv.get("name", str(inv)))
            else:
                investor_names.append(str(inv))
        investors_str = ", ".join(investor_names)

    # ── Technologies ──────────────────────────────────────────────────────────
    # Docs return [{ name, category }]; old shape was [string]
    raw_tech = item.get("technologies", []) or []
    if isinstance(raw_tech, list):
        tech_names = []
        for t in raw_tech:
            if isinstance(t, dict):
                tech_names.append(t.get("name", str(t)))
            elif t:
                tech_names.append(str(t))
        technologies = ", ".join(tech_names)
    else:
        technologies = str(raw_tech) if raw_tech else ""

    # ── Keywords ─────────────────────────────────────────────────────────────
    raw_keywords = item.get("keywords", []) or []
    keywords = ", ".join(str(k) for k in raw_keywords if k) if isinstance(raw_keywords, list) else ""

    # ── Revenue ───────────────────────────────────────────────────────────────
    revenue = str(item.get("revenue", "") or "")

    return {
        # Core identity
        "id": item.get("id", ""),
        "domain": domain,
        "name": name,
        "description": description,
        "linkedin_url": linkedin_url,
        "twitter_url": twitter_url,
        "crunchbase_url": crunchbase_url,
        "website_url": website_url,
        "founded_year": founded_year,
        "company_type": company_type,
        # Industry / category (aliased for DiscoLike compat)
        "industry": industry,
        "industry_groups": industry,
        "category": industry,
        # Size
        "employees": employee_count,
        "employee_count": employee_count,
        # Location
        "city": city,
        "hq_state": hq_state,
        "country": country,
        "street": street,
        "zip": postal_code,
        "location": location,
        # Contact
        "public_emails": company_email,
        "phones": company_phone,
        # Funding (AI.ARK-specific enrichment)
        "total_funding": total_funding,
        "last_funding_amount": last_funding_amount,
        "funding_stage": funding_stage,
        "investors": investors_str,
        # Technology stack
        "technologies": technologies,
        # Keywords
        "keywords": keywords or technologies,
        # Revenue
        "revenue_estimate": revenue,
        # Meta
        "last_updated": last_updated,
        # DiscoLike compat fields (neutral values)
        "similarity": 0,
        "score": "",
        "social_urls": linkedin_url,
        "start_date": founded_year,
        "public_emails": company_email,
        "redirect_domain": "",
        "update_date": last_updated,
    }


# ── JSON mode ────────────────────────────────────────────────────────────────

def json_mode():
    """Read JSON from stdin, process, write JSON to stdout."""
    raw = sys.stdin.read()
    params = json.loads(raw)

    api_key = params.get("api_key") or os.getenv("AIARK_API_KEY", "")
    if not api_key:
        print(json.dumps({"error": "No api_key provided and AIARK_API_KEY not set"}))
        sys.exit(1)

    # Resolve domains (optional for company search)
    domains = params.get("domains", [])
    if not domains and params.get("domain"):
        domains = [params["domain"]]

    lookalike_urls = params.get("lookalike_urls", params.get("lookalikeDomains", []))
    industries = params.get("industries")
    locations = params.get("locations")
    funding_types = params.get("funding_types")
    technologies = params.get("technologies")
    keywords = params.get("keywords")
    limit = int(params.get("limit", 50))

    # employee_range: support "50,500" string or individual min/max
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

    progress(0, 1, "Searching AI.ARK companies...")

    try:
        companies = search_companies(
            api_key=api_key,
            domains=domains if domains else None,
            lookalike_urls=lookalike_urls if lookalike_urls else None,
            industries=industries,
            locations=locations,
            employee_min=employee_min,
            employee_max=employee_max,
            funding_types=funding_types,
            technologies=technologies,
            keywords=keywords,
            limit=limit,
        )
    except Exception as e:
        error_msg = str(e)
        if _HAS_HTTPX and isinstance(e, httpx.HTTPStatusError):
            error_msg = f"AI.ARK API error: {e.response.status_code} {e.response.text[:200]}"
        print(json.dumps({"error": error_msg}))
        sys.exit(1)

    progress(1, 1, f"Complete: {len(companies)} companies")
    print(json.dumps({"companies": companies}))


# ── CLI mode ─────────────────────────────────────────────────────────────────

def cli_mode():
    """Argparse-based CLI interface."""
    parser = argparse.ArgumentParser(
        description="AI.ARK Company Search",
        epilog="Set AIARK_API_KEY env var or pass --api-key",
    )
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("search", help="Search for companies")
    s.add_argument("--domains", nargs="*", help="Seed domain(s)")
    s.add_argument("--lookalike-urls", nargs="*",
                   help="LinkedIn company URLs for lookalike discovery (up to 5)")
    s.add_argument("--industries", nargs="*", help="Industry filter(s), e.g. software saas")
    s.add_argument("--locations", nargs="*", help="Location filter(s), e.g. 'United States'")
    s.add_argument("--employee-min", type=int, help="Min employee count")
    s.add_argument("--employee-max", type=int, help="Max employee count")
    s.add_argument("--funding-types", nargs="*",
                   help="Funding stage(s): SERIES_A, SERIES_B, SERIES_C, SEED, etc.")
    s.add_argument("--technologies", nargs="*", help="Technology filter(s)")
    s.add_argument("--keywords", nargs="*", help="Keyword/product/service filter(s)")
    s.add_argument("-n", "--limit", type=int, default=50, help="Max results (default: 50)")
    s.add_argument("--api-key", help="AI.ARK API key")
    s.add_argument("-o", "--output", help="Output JSON file")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    api_key = getattr(args, "api_key", None) or os.getenv("AIARK_API_KEY", "")
    if not api_key:
        print("ERROR: Set AIARK_API_KEY env var or pass --api-key", file=sys.stderr)
        sys.exit(1)

    companies = search_companies(
        api_key=api_key,
        domains=args.domains,
        lookalike_urls=args.lookalike_urls,
        industries=args.industries,
        locations=args.locations,
        employee_min=args.employee_min,
        employee_max=args.employee_max,
        funding_types=args.funding_types,
        technologies=args.technologies,
        keywords=args.keywords,
        limit=args.limit,
    )

    print(f"Found {len(companies)} companies", file=sys.stderr)
    output = json.dumps({"companies": companies}, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved to {args.output}", file=sys.stderr)
    else:
        print(output)


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not sys.stdin.isatty():
        json_mode()
    else:
        cli_mode()
