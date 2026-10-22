"""Web-research people finder: an LLM that browses a school's own site and public profiles to name its CURRENT decision makers.

Emmanuel's prompt (2026-10-09, "a heavy lifter"): read the company website (team, leadership, admissions, contact pages), then public
company profiles (ZoomInfo, Crunchbase, TheOrg), and only then LinkedIn via Google ("site:linkedin.com/in"), keep only CURRENT people
whose title is on the target list, and return a JSON array of name, title and domain. The vendor databases (QuickEnrich, Lead Ocean)
hold nobody for about two thirds of small schools, so this finds people they cannot.

Two tools give the model its eyes: web_search (Serper, Google results) and fetch_page (Jina reader). It returns NAMES ONLY. Turning a
name into a verified email is a separate step (resolve_email below), so every address still goes through the verification waterfall.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TURNS = 12
PRICES = {"claude-haiku-4-5-20251001": (1.00, 5.00), "claude-sonnet-4-6": (3.00, 15.00), "claude-sonnet-5": (2.00, 10.00)}

TARGET_TITLES = [
    "Owner", "Co-Owner", "CDL Training Director", "Operator", "Founder", "School Director", "Campus Director", "Director of Admissions",
    "Admissions Director", "Director of Enrollment", "Enrollment Director", "Director of Training", "Chief Instructor", "Enrollment Manager",
    "Admissions Manager", "Enrollment Coordinator", "Admissions Coordinator", "Program Director", "Finance Director",
    "Director of Financial Aid", "Financial Aid Director", "Business Manager", "Operations Manager", "Director of Student Services",
    "Marketing Director", "President", "CEO",
]

SYSTEM = """You are tasked with identifying any and all CURRENT people at a given company whose job titles match one of the target roles.

TARGET JOB TITLES: __TITLES__.

OBJECTIVE: extract the names, job titles and domain of all CURRENT people whose job titles match the target list, for the company you are given.
ONLY RETURN PEOPLE WHO ARE CURRENTLY EMPLOYED AT THE COMPANY. DO NOT RETURN FORMER EMPLOYEES. DO NOT RETURN PEOPLE WITH TITLES OUTSIDE THE TARGET LIST.
If a title is slightly different but clearly equivalent to a target title, include it only if the match is obvious and high confidence.

TOOLS: web_search(query) returns Google results. fetch_page(url) returns the text of a page. Use them in this order.

1. Examine the company website first. Check the About, Team, Leadership, Staff, Faculty, Administration, Admissions, Financial Aid, Student Services,
   Contact and Campus pages, plus location pages, program pages and staff directories. Use fetch_page on the home page, find those links, and fetch them.
   Check the footer and contact pages for named leadership or department contacts, and news or blog pages for bylines that show current staff.
   Do not include former employees, advisors, board members, consultants or outside partners unless clearly shown as current employees in a target role.
2. If you have fewer than 3 valid people from the website, search public profile pages with web_search using the exact domain: zoominfo.com,
   crunchbase.com and theorg.com. Verify the domain matches exactly before using anyone from those pages.
3. ONLY AFTER exhausting the website and public profile pages, use LinkedIn through Google: web_search with site:linkedin.com/in "<Title>" "<Company Name>"
   for the most likely titles (Owner, Founder, President, CEO, Director of Admissions, Campus Director, School Director, Director of Enrollment).
   Only return LinkedIn people who are CURRENTLY employed at the company. Do not guess. Verify current employment with a second source when you can.
4. Cross-reference sources. Only return results you are highly confident in. Prefer people confirmed by more than one source. Do not make assumptions.
5. Be efficient. Stop searching as soon as you have the people you are confident in. Never spend more than 10 tool calls on one company.

FINAL ANSWER: only a valid JSON array, nothing else, no explanation, no notes, no confidence scores, no markdown:
[{"Full Name": "Jane Smith", "First Name": "Jane", "Last Name": "Smith", "Job Title": "CEO", "Domain": "example.com"}]
If you find nobody you are confident in, return []."""

TOOLS = [
    {"name": "web_search", "description": "Search Google and return the top results (title, link, snippet). Supports operators such as site:linkedin.com/in.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "fetch_page", "description": "Fetch a web page and return its text (markdown). Use it on a school's own pages: home, team, leadership, admissions, contact.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]


def _env(name: str) -> str:
    return os.getenv(name) or ""


def _search(query: str) -> str:
    key = _env("SERPER_API_KEY")
    if not key:
        return "search unavailable"
    try:
        r = httpx.post("https://google.serper.dev/search", json={"q": query, "gl": "us", "num": 8},
                       headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=25)
        rows = (r.json() or {}).get("organic") or []
    except Exception as exc:
        return f"search failed: {str(exc)[:80]}"
    out = [{"title": x.get("title"), "link": x.get("link"), "snippet": (x.get("snippet") or "")[:240]} for x in rows[:8]]
    return json.dumps(out) if out else "no results"


def _fetch(url: str, max_chars: int = 5500) -> str:
    if not re.match(r"^https?://", url or ""):
        url = "https://" + (url or "")
    headers = {"Accept": "text/plain"}
    if _env("JINA_API_KEY"):
        headers["Authorization"] = "Bearer " + _env("JINA_API_KEY")
    try:
        r = httpx.get("https://r.jina.ai/" + url, headers=headers, timeout=40, follow_redirects=True)
    except Exception as exc:
        return f"fetch failed: {str(exc)[:80]}"
    if r.status_code != 200 or len(r.text.strip()) < 60:
        return f"could not read that page (HTTP {r.status_code})"
    return r.text.strip()[:max_chars]


def _call(model: str, messages: list, system: str) -> dict:
    body = {"model": model, "max_tokens": 1800, "temperature": 0, "system": system, "messages": messages, "tools": TOOLS}
    for attempt in range(4):
        try:
            r = httpx.post(ANTHROPIC_URL, json=body, timeout=120,
                           headers={"x-api-key": _env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01", "content-type": "application/json"})
        except httpx.HTTPError:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(2 ** attempt * 2, 20))
            continue
        if r.status_code == 400 and "temperature" in r.text.lower() and "temperature" in body:
            body.pop("temperature")
            continue
        if r.status_code != 200:
            raise RuntimeError(f"anthropic {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError("anthropic unavailable")


def parse_people(text: str) -> list[dict]:
    """The JSON array out of the model's final text. Tolerates a code fence and stray words around it."""
    text = (text or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict):
            continue
        first = (p.get("First Name") or "").strip()
        last = (p.get("Last Name") or "").strip()
        full = (p.get("Full Name") or f"{first} {last}").strip()
        if not first and full:
            parts = full.split()
            first, last = parts[0], " ".join(parts[1:])
        title = (p.get("Job Title") or "").strip()
        if first and title:
            out.append({"first_name": first, "last_name": last, "full_name": full, "title": title, "domain": (p.get("Domain") or "").strip().lower()})
    return out


def title_ok(title: str) -> bool:
    """The title is on Emmanuel's target list, or contains one of them as whole words (Owner and Founder, Co-Owner, CEO and President)."""
    t = (title or "").lower()
    for target in TARGET_TITLES:
        if re.search(r"(?<![a-z])" + re.escape(target.lower()) + r"(?![a-z])", t):
            return True
    return False


def find_people(company_name: str, domain: str, model: str | None = None) -> dict:
    """Run the research loop for one company. Returns {people, tool_calls, input_tokens, output_tokens, usd, turns, error}."""
    model = model or os.getenv("PEOPLE_AGENT_MODEL") or DEFAULT_MODEL
    system = SYSTEM.replace("__TITLES__", ", ".join(TARGET_TITLES))
    messages = [{"role": "user", "content": f"Company Name: {company_name}\nDomain: {domain}"}]
    calls = tin = tout = 0
    final = ""
    err = ""
    try:
        for turn in range(MAX_TURNS):
            resp = _call(model, messages, system)
            u = resp.get("usage") or {}
            tin += int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
            tout += int(u.get("output_tokens") or 0)
            blocks = resp.get("content") or []
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if resp.get("stop_reason") != "tool_use" or not uses:
                final = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                break
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for b in uses:
                calls += 1
                args = b.get("input") or {}
                content = _search(args.get("query", "")) if b.get("name") == "web_search" else _fetch(args.get("url", ""))
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": content})
            if calls >= 14:           # hard stop: no company is worth an endless search
                results.append({"type": "text", "text": "Tool budget used up. Give your final JSON array now."})
            messages.append({"role": "user", "content": results})
        else:
            err = "turn limit"
    except Exception as exc:
        err = str(exc)[:120]
    pin, pout = PRICES.get(model, (3.0, 15.0))
    people = [p for p in parse_people(final) if title_ok(p["title"])]
    for p in people:
        p["domain"] = domain
    return {"people": people, "tool_calls": calls, "input_tokens": tin, "output_tokens": tout,
            "usd": round(tin / 1e6 * pin + tout / 1e6 * pout, 4), "error": err}


# ---------------------------------------------------------------------------------------------------- name to email

def email_candidates(first: str, last: str, domain: str) -> list[str]:
    """Likeliest local parts for a small school, first-name-only first: measured 2026-10-09, the only two pattern hits in 9 people were 'first@'."""
    f, l = re.sub(r"[^a-z]", "", first.lower()), re.sub(r"[^a-z]", "", last.lower())
    if not f or not domain:
        return []
    pats = [f, f"{f}.{l}", f"{f[0]}{l}", f"{f}{l}", f"{l}.{f}", f"{f[0]}.{l}", l] if l else [f]
    seen, out = set(), []
    for p in pats:
        if p and p not in seen:
            seen.add(p)
            out.append(f"{p}@{domain}")
    return out


def _mv(email: str) -> str:
    """MillionVerifier verdict for one address: ok, catch_all, invalid, unknown..."""
    from . import gateway_verify
    if gateway_verify.enabled():
        v = gateway_verify.verify_one(email)
        return "ok" if v == "valid" else v
    key = _env("MILLIONVERIFIER_API_KEY")
    if not key:
        return "unavailable"
    try:
        r = httpx.get("https://api.millionverifier.com/api/v3/", params={"api": key, "email": email, "timeout": 15}, timeout=30)
        return (r.json().get("result") or "unknown").lower()
    except Exception:
        return "unavailable"


def resolve_email(first: str, last: str, domain: str) -> tuple[str | None, str]:
    """(email, how). Hunter's finder first (skipped silently when its credits are gone), then the common patterns checked with
    MillionVerifier. Only 'ok' counts; a catch_all domain is reported as how='catch_all' with no email, never guessed at.
    Deliberately NOT waterfall.verify_batch on guesses: a failed guess there fires the paid finder cascade once per guess."""
    try:
        from . import hunter
        found, status = hunter.find_email(domain, first, last)
        if found and status in (None, hunter.DELIVERABLE):
            return found, f"hunter:{status or 'unverified'}"
    except Exception:
        pass
    catch_all = False
    for cand in email_candidates(first, last, domain)[:5]:
        v = _mv(cand)
        if v == "ok":
            return cand, "pattern:ok"
        if v == "catch_all":
            catch_all = True
            break
    return None, "catch_all" if catch_all else "none"
