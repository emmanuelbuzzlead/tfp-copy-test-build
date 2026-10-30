"""Quick Enrich -- Python port of buzzlead-signal-engine/src/lib/quickenrich.ts.

Endpoints and auth verified in that TypeScript client (docs checked 2026-08-20):
  base https://app.quickenrich.io
  auth Authorization: Bearer <key>  AND  x-api-key: <key>   (the TS client sends both)
  GET /api/employees/dataset-search   company_url + title [+ has_email, page]
  GET /api/employees/search           linkedin_url | company_url+first_name+last_name

Behaviours carried over from the TS client because they were learned the hard way:
  - An unparseable email_verification_date must not discard the contact. In the TS
    version a RangeError out of the date parse threw away otherwise-good records;
    the date is metadata, the email is the point.
  - Response field names vary by endpoint, so map defensively rather than indexing.
"""

from __future__ import annotations

from typing import Any

from concurrent.futures import ThreadPoolExecutor

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import settings
from .http import request

EMAIL_RX = re.compile(r".+@.+\..+")
NA_RX = re.compile(r"^n/?a$", re.IGNORECASE)


TITLE_WAVE = 8   # titles queried concurrently per company; 3 parallel verticals share the plan


@dataclass
class Contact:
    first_name: str
    last_name: str
    email: str | None
    title: str | None
    linkedin_url: str | None
    company: str | None
    company_domain: str | None
    phone: str | None = None
    email_verified_at: datetime | None = None
    # Quick Enrich's address fields are the COMPANY's headquarters (verified 2026-09-15:
    # all 20 Stryker rows carry Portage MI; all 20 Mimecast rows carry London). There is
    # no person-level location in this source.
    country_code: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def _headers() -> dict[str, str]:
    if not settings.quickenrich_api_key:
        raise RuntimeError("QUICKENRICH_API_KEY is not set")
    return {
        "Content-Type": "application/json",
        "x-api-key": settings.quickenrich_api_key,
        "Authorization": f"Bearer {settings.quickenrich_api_key}",
    }


def _is_real_email(value: object) -> bool:
    return isinstance(value, str) and bool(EMAIL_RX.match(value)) and not NA_RX.match(value)


def _parse_date(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    for parse in (
        lambda t: datetime.fromisoformat(t),
        lambda t: datetime.strptime(t, "%Y-%m-%d"),
        lambda t: datetime.strptime(t, "%m/%d/%Y"),
    ):
        try:
            dt = parse(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None  # metadata only -- never a reason to drop the contact


def _map(row: dict, fallback_domain: str | None = None) -> Contact | None:
    if not row:
        return None
    email = row.get("email") or row.get("work_email") or row.get("best_email")
    if not email:
        # Some endpoints return emails as a list of strings, others as [{"email": ...}].
        emails = row.get("emails")
        if isinstance(emails, list) and emails:
            head = emails[0]
            email = head.get("email") if isinstance(head, dict) else head
    full = (row.get("full_name") or row.get("name") or "").strip()
    first = row.get("first_name") or (full.split(" ")[0] if full else "")
    last = row.get("last_name") or (" ".join(full.split(" ")[1:]) if full else "")
    phone_raw = row.get("employee_phone") or row.get("phone") or row.get("mobile")
    return Contact(
        first_name=str(first or "").strip(),
        last_name=str(last or "").strip(),
        email=str(email).strip().lower() if _is_real_email(email) else None,
        title=row.get("title") or row.get("job_title"),
        linkedin_url=row.get("employee_linkedin") or row.get("linkedin_url") or row.get("linkedin"),
        company=row.get("company_name") or row.get("company"),
        company_domain=(row.get("email_domain") or row.get("company_url") or fallback_domain),
        phone=str(phone_raw) if phone_raw and not NA_RX.match(str(phone_raw)) else None,
        email_verified_at=_parse_date(
            row.get("email_verification_date")
            or row.get("email_verified_at")
            or row.get("verified_at")
        ),
        country_code=(str(row.get("country_code") or "").strip().upper() or None),
    )


US_CODES = {"US", "USA"}


def is_us(contact: "Contact") -> bool:
    """Troy, 2026-09-15: "quick enrich should be US contacts." The API ignores every
    country parameter (verified: identical totals with country/location set), so the
    filter is local, on the company country code the row carries. A blank code is kept:
    the company itself was already US-filtered at sourcing."""
    if not contact.country_code or contact.country_code in US_CODES:
        return True
    try:
        from .. import client as _client
        return contact.country_code in {"CAN" if c == "CA" else c for c in _client.profile().countries} | set(_client.profile().countries)
    except Exception:
        return False


def _rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "employees", "people", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        if payload.get("email") or payload.get("first_name"):
            return [payload]
    return []


def _find_broad(company_domain: str, limit: int) -> list:
    """Everyone at one company, in ONE call, no title filter and no email filter.

    The endpoint takes a single title per call, so this client walked 36 titles per
    company. It also accepts NO title, and then returns the whole company at once --
    which nobody had tried. Measured on 20 companies, 2026-09-22:

        narrow walk, 12 titles, has_email=1 ->  0.70 people/company in 424s
        one broad call, no filters          -> 10.35 people/company in  63s

    Fifteen times the people in a seventh of the time, same vendor, same account.
    has_email is dropped too: the verification waterfall resolves an address from a
    name and a domain, so filtering on a pre-existing email at query time throws away
    people we can reach perfectly well. BuzzLead's advantage in contacts was only ever
    this -- it queries broadly and we did not.

    Every downstream rule is unchanged: US only, the client's title ranking, banned
    personas, the mail-gateway block and the recency window all still apply.
    """
    rows: list = []
    for page in range(1, 4):
        try:
            payload = request(
                "quickenrich", "GET",
                f"{settings.quickenrich_base}/api/employees/dataset-search",
                headers=_headers(),
                params={"company_url": company_domain, "has_email": 0, "page": page},
            )
        except Exception:
            break
        page_rows = _rows(payload)
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(rows) >= limit * 3:
            break                      # plenty to rank from; stop paying for pages
    out: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        contact = _map(row, fallback_domain=company_domain)
        if not contact or not contact.full_name:
            continue
        key = (contact.email or contact.linkedin_url or contact.full_name).lower()
        out.setdefault(key, contact)
    return list(out.values())


def find_people(company_domain: str, titles: list[str], limit: int = 10) -> list[Contact]:
    """Find people at one company matching the IDPR title list.

    Quick Enrich's dataset-search takes a single title per call, so this walks the
    title list in priority order and stops as soon as it has enough candidates --
    the list is 30+ titles long and querying all of them for every company would be
    the single most expensive thing this pipeline does.
    """
    # ONE broad call first. The title walk below is kept as the fallback for a company
    # the broad query cannot see, and for the case where the endpoint changes shape.
    try:
        broad = _find_broad(company_domain, limit)
    except Exception:
        broad = []
    if len(broad) >= 2:
        return broad[:limit]

    found: dict[str, Contact] = {}
    errors: list[str] = []

    def _query(title: str) -> tuple[str, Any]:
        """Every page for this title, not just page 1.

        The endpoint accepts `page`. Sending none silently returns the first page,
        which is how a company with 15 marketing leaders yielded whoever happened
        to sit on page 1 of the first titles that hit.
        """
        rows: list = []
        errors: list[str] = []
        for page in range(1, 4):  # three pages is plenty for one title at one company
            try:
                payload = request(
                    "quickenrich",
                    "GET",
                    f"{settings.quickenrich_base}/api/employees/dataset-search",
                    headers=_headers(),
                    # has_email must be 1/0. Verified live 2026-09-07: the string "true"
                    # (which is what httpx sends for a Python bool) is rejected with
                    # 422 "The has email field must be true or false." Sending 1 works.
                    params={"company_url": company_domain, "title": title,
                            "has_email": 1, "page": page},
                )
            except Exception as exc:
                errors.append(str(exc))
                break
            page_rows = _rows(payload)
            if not page_rows:
                break
            rows.extend(page_rows)
        if not rows and errors:
            return title, RuntimeError(errors[0])
        return title, rows

    # One title per call is the API's shape; one call AT A TIME was ours, and it was
    # the single slowest thing in the pipeline. Measured 2026-09-13: 36 titles in
    # sequence took 66-91 seconds per company, ~70% of a 41-second-per-worker finder
    # stage that would have needed 4.5 hours for 3,197 companies. Titles are still
    # walked in priority order and the walk still stops once `limit` is met -- but in
    # waves of TITLE_WAVE run concurrently, so a wave costs one round trip, not eight.
    for start in range(0, len(titles), TITLE_WAVE):
        if len(found) >= limit:
            break
        wave = titles[start:start + TITLE_WAVE]
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            results = list(pool.map(_query, wave))
        for title, payload in results:
            if isinstance(payload, Exception):
                errors.append(f"{title}: {payload}")
                continue
            rows = payload if isinstance(payload, list) else _rows(payload)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                contact = _map(row, fallback_domain=company_domain)
                if not contact or not contact.full_name:
                    continue
                key = (contact.email or contact.linkedin_url or contact.full_name).lower()
                found.setdefault(key, contact)

    if not found and errors:
        # Every title errored, so this is a broken integration, not an empty company.
        raise RuntimeError(
            f"quickenrich returned no contacts for {company_domain} and every call failed: "
            + "; ".join(errors[:3])
        )
    return list(found.values())[:limit]


# ── Company Finder (TAM discovery) ──────────────────────────────────────────
# Onboarded 2026-09-25 to widen stage3 beyond BuzzLead + AI Ark -- QuickEnrich's
# company-finder endpoint was only ever used for its contact side in this app.
# Filters demand EXACT validated values from /lookups/*, so free-text industries
# (LinkedIn-style names, the same ones Vertical.industries already carries) are
# resolved against the real lookup list first; an unvalidated value 422s the whole
# request. Proven live against this exact endpoint 2026-09-25.

_LOOKUP_CACHE: dict[str, list[str]] = {}


def _lookup(name: str) -> list[str]:
    if name not in _LOOKUP_CACHE:
        payload = request("quickenrich", "GET",
                          f"{settings.quickenrich_base}/api/lookups/{name}",
                          headers=_headers())
        _LOOKUP_CACHE[name] = (payload.get("data") or []) if isinstance(payload, dict) else []
    return _LOOKUP_CACHE[name]


def _norm(s: str) -> str:
    s = (s or "").strip().lower().replace("&", "and")
    return " ".join("".join(ch for ch in s if ch.isalnum() or ch.isspace()).split())


def map_industries(wanted: list[str]) -> list[str]:
    valid = _lookup("industries")
    by_norm = {_norm(v): v for v in valid}
    out: list[str] = []
    for w in wanted or []:
        wn = _norm(w)
        if not wn:
            continue
        if wn in by_norm:
            out.append(by_norm[wn])
            continue
        subs = [v for n, v in by_norm.items() if wn in n or n in wn]
        out.extend(subs)
    seen: set[str] = set()
    return [v for v in out if not (v in seen or seen.add(v))]


def map_employee_range(emp_min: int | None, emp_max: int | None, strict_min: bool = False) -> list[str]:
    """The headcount bands that overlap the range. `strict_min` keeps only bands that start at or above emp_min, so a 50 floor never takes
    the "20 - 99" band's 20-person companies."""
    if emp_min is None and emp_max is None:
        return []
    lo, hi = emp_min or 0, emp_max or 10**9
    out = []
    for label in _lookup("employee-ranges"):
        txt = str(label).replace(",", "").strip()
        try:
            if "-" in txt:
                a, b = (p.strip() for p in txt.split("-", 1))
                r_lo, r_hi = int(a), int(b)
            elif txt.endswith("+"):
                r_lo, r_hi = int(txt[:-1].strip()), 10**9
            else:
                r_lo = r_hi = int(txt)
        except ValueError:
            continue
        if r_lo <= hi and r_hi >= lo and not (strict_min and r_lo < lo):
            out.append(label)
    return out


def find_companies(industries: list[str] | None = None, employee_min: int | None = None,
                   employee_max: int | None = None, limit: int = 100,
                   exclude: set[str] | None = None, strict_min: bool = False) -> list[dict]:
    """POST /api/companies/company-finder -> normalized company dicts (domain, name,
    industry, employees, description), in the client's countries. Returns [] rather than
    raising on no usable filters -- an unfiltered query would page the whole dataset.

    Pages with the cursor (meta.next_cursor): offset pages stop at the first 10,000 results
    (422 "Offset pagination is limited to the first 10,000 results"), so a vertical with more
    than 10,000 matches found "nothing new" forever once we held the first 10,000 (TFP
    Construction, 2026-10-28: 28,545 matches, 9,836 stored, top-up found 0). `exclude`
    (domains already stored) is skipped and does not count toward `limit`, so `limit` means
    NEW companies."""
    exclude = exclude or set()
    mapped_ind = map_industries(industries or [])
    ranges = map_employee_range(employee_min, employee_max, strict_min)
    if strict_min and not ranges:
        return []                    # no band fits inside the range: searching without a size filter would return every size
    try:
        from .. import client as _client
        countries = list(_client.profile().countries) or ["US"]
    except Exception:
        countries = ["US"]
    payload: dict[str, Any] = {"country_code": {"include": countries, "exclude": []}}
    if mapped_ind:
        payload["industry"] = {"include": mapped_ind, "exclude": []}
    if ranges:
        payload["number_of_employees"] = {"include": ranges, "exclude": []}
    if "industry" not in payload:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    cursor: str | None = None
    per_page = 100 if exclude else min(100, max(10, limit))
    while len(out) < limit:
        nav = {"cursor": cursor} if cursor else {"page": 1}
        body = request("quickenrich", "POST",
                       f"{settings.quickenrich_base}/api/companies/company-finder",
                       headers=_headers(),
                       json_body={**payload, **nav, "per_page": per_page})
        rows = (body.get("data") or []) if isinstance(body, dict) else []
        if not rows:
            break
        for rec in rows:
            domain = (rec.get("url") or rec.get("final_email_domain") or "").lower().replace("www.", "")
            if not domain or domain in seen:
                continue
            if domain in exclude:
                seen.add(domain)
                continue
            seen.add(domain)
            out.append({
                "domain": domain,
                "name": rec.get("company_name") or "",
                "industry": rec.get("industry") or "",
                "employees": rec.get("employee_count") or "",
                "description": rec.get("bio_li_snippet") or rec.get("home_page_text_snippet") or "",
            })
            if len(out) >= limit:
                break
        meta = (body.get("meta") or {}) if isinstance(body, dict) else {}
        cursor = meta.get("next_cursor")
        if not cursor or meta.get("has_more") is False:
            break
    return out
