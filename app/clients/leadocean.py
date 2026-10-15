"""Lead Ocean -- company search. ProductEVO-only, same reasoning as the contact-side
find_leadocean() in finders.py: unproven at this app's volume, and its account-wide
1 req/s rate limit is a real ceiling other clients haven't opted into. Callers gate
on client.profile().name themselves; this module makes no client decision.

Base https://api.leadocean.io, auth x-api-key. Country filters are ISO-2 (US, not
"United States"). The industry enum defaults to 200-of-534 results and silently
truncates -- use the server's own ?q= substring search per term, never the bare list,
or real values go missing with no error (found 2026-09-25).
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..config import settings
from .http import request

BASE_URL = "https://api.leadocean.io"

_ENUM_CACHE: dict[str, list[str]] = {}
_NEXT_AT = [0.0]
_LOCK = threading.Lock()


def _rate_limit_wait() -> None:
    with _LOCK:
        now = time.monotonic()
        wait_for = _NEXT_AT[0] - now
        _NEXT_AT[0] = max(now, _NEXT_AT[0]) + 1.1
    if wait_for > 0:
        time.sleep(wait_for)


def _headers() -> dict[str, str]:
    if not settings.leadocean_api_key:
        raise RuntimeError("LEADOCEAN_API_KEY is not set")
    return {"x-api-key": settings.leadocean_api_key}


def _get(path: str, params: dict | None = None) -> dict:
    _rate_limit_wait()
    return request("leadocean", "GET", f"{BASE_URL}{path}", headers=_headers(), params=params)


def _enum_search(name: str, q: str) -> list[str]:
    cache_key = f"{name}:{q.lower()}"
    if cache_key in _ENUM_CACHE:
        return _ENUM_CACHE[cache_key]
    body = _get(f"/v1/enums/{name}", params={"q": q, "limit": 500})
    values = (body.get("data") or {}).get("values") or []
    _ENUM_CACHE[cache_key] = values
    return values


def map_industries(wanted: list[str]) -> list[str]:
    out: list[str] = []
    for w in wanted or []:
        w = (w or "").strip()
        if not w:
            continue
        matches = _enum_search("industry", w)
        if not matches:
            for word in w.split():
                matches = _enum_search("industry", word)
                if matches:
                    break
        out.extend(matches)
    seen: set[str] = set()
    return [v for v in out if not (v in seen or seen.add(v))]


def find_companies(industries: list[str] | None = None, employee_min: int | None = None,
                   employee_max: int | None = None, limit: int = 25) -> list[dict]:
    """Up to `limit` companies, paging with the API's cursor (100 per page).

    It used to fetch ONE page, so each vertical took at most ~100 companies while Lead
    Ocean held 3,000-10,000 per vertical in the 10-100 band (measured 2026-09-27).
    """
    mapped = map_industries(industries or [])
    if industries and not mapped:
        return []
    try:
        from .. import client as _client
        countries = list(_client.profile().countries) or ["US"]
    except Exception:
        countries = ["US"]
    if len(countries) > 1:
        # One country per request in this API: split the limit and merge, de-duplicated by domain.
        merged: list[dict] = []
        seen_d: set[str] = set()
        for c in countries:
            for rec in _find_companies_in(c, mapped, employee_min, employee_max, max(1, limit // len(countries))):
                if rec["domain"] not in seen_d:
                    seen_d.add(rec["domain"]); merged.append(rec)
        return merged[:limit]
    return _find_companies_in(countries[0], mapped, employee_min, employee_max, limit)


def _find_companies_in(country: str, mapped: list[str], employee_min: int | None, employee_max: int | None, limit: int) -> list[dict]:
    params: dict[str, Any] = {"hqCountry": country}
    if mapped:
        params["industry"] = mapped
    if employee_min is not None:
        params["minEmployees"] = employee_min
    if employee_max is not None:
        params["maxEmployees"] = employee_max
    rows: list[dict] = []
    cursor = None
    while len(rows) < limit:
        page = {**params, "limit": min(100, max(1, limit - len(rows)))}
        if cursor:
            page["cursor"] = cursor
        body = _get("/v1/companies/search", params=page)
        got = body.get("data") or []
        rows.extend(got)
        cursor = (body.get("meta") or {}).get("nextCursor")
        if not got or not cursor:
            break
    out = []
    for rec in rows:
        d = rec.get("company_data", rec)
        addr = d.get("company_primary_address") or {}
        emp = d.get("company_employees") or {}
        inds = d.get("company_industries") or []
        industry = ", ".join(i.get("name3", "") for i in inds if isinstance(i, dict) and i.get("name3"))
        domain = ((d.get("company_web_address") or {}).get("url") or "")
        domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")
        if not domain:
            continue
        out.append({
            "domain": domain,
            "name": d.get("company_name", ""),
            "industry": industry,
            "employees": emp.get("number_of_employees", ""),
            "description": d.get("company_description", ""),
        })
    return out
