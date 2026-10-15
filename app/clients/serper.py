"""Serper -- the two research searches in Stage 5, plus website text.

Spec Stage 5: call 1 is Google News for the company (last 10), call 2 is Google Search
for "{company} competitors" (top 10). Results are flattened to compact text because
they are pasted into the research prompt's user message, and raw JSON wastes tokens
the research model should be spending on analysis.
"""

from __future__ import annotations

import re

import httpx

from ..config import settings
from .http import DEFAULT_TIMEOUT, VendorError, request, VendorOutage

NEWS_URL = "https://google.serper.dev/news"
SEARCH_URL = "https://google.serper.dev/search"


class SerperUnavailable(VendorOutage):
    """Serper cannot serve ANY request: out of credits or key rejected.

    Distinct from an empty result on purpose. Serper feeds the news and competitor
    blocks of the research prompt, so when it is dry EVERY contact's research call
    fails -- and a caller that treats that like one bad contact files thousands of
    them as needs_review, each with its verification already paid for. On 2026-09-14
    that was 3,058 contacts in about an hour. This must propagate so the run pauses
    and resumes when the account is topped up.
    """


def _outage(exc: Exception) -> bool:
    body = str(exc).lower()
    return any(t in body for t in ("not enough credits", "insufficient", "quota",
                                   "unauthorized", "forbidden", "invalid api key"))


def _headers() -> dict[str, str]:
    if not settings.serper_api_key:
        raise RuntimeError("SERPER_API_KEY is not set")
    return {"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"}


def news(company: str, limit: int = 10, tbs: str | None = None) -> str:
    try:
        payload = request("serper", "POST", NEWS_URL, headers=_headers(),
                          json_body=({"q": company, "num": limit, "tbs": tbs} if tbs else {"q": company, "num": limit}))
    except Exception as exc:
        if _outage(exc):
            raise SerperUnavailable(str(exc)[:200]) from exc
        raise
    items = (payload or {}).get("news") or []
    if not items:
        return "No Google News results found."
    lines = []
    for item in items[:limit]:
        lines.append(
            f"- {item.get('title','')} | {item.get('source','')} | {item.get('date','')}\n"
            f"  {item.get('link','')}\n"
            f"  {(item.get('snippet') or '').strip()}"
        )
    return "\n".join(lines)


def competitors(company: str, limit: int = 10) -> str:
    try:
        payload = request("serper", "POST", SEARCH_URL, headers=_headers(),
                          json_body={"q": f"{company} competitors", "num": limit})
    except Exception as exc:
        if _outage(exc):
            raise SerperUnavailable(str(exc)[:200]) from exc
        raise
    items = (payload or {}).get("organic") or []
    if not items:
        return "No competitor search results found."
    lines = []
    for item in items[:limit]:
        lines.append(
            f"- {item.get('title','')} | {item.get('link','')}\n"
            f"  {(item.get('snippet') or '').strip()}"
        )
    return "\n".join(lines)


TAG_RX = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
ANY_TAG_RX = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    text = TAG_RX.sub(" ", html)
    text = ANY_TAG_RX.sub(" ", text)
    text = re.sub(r"&(nbsp|amp|quot|#39|lt|gt);", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_SITE_TIMEOUT = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _scrape_via_serper(url: str) -> str:
    """Serper's page scraper, for the sites that refuse a direct request."""
    try:
        resp = httpx.post("https://scrape.serper.dev", headers=_headers(),
                          json={"url": url}, timeout=25)
        if resp.status_code != 200:
            return ""
        return re.sub(r"\s+", " ", str((resp.json() or {}).get("text") or "")).strip()
    except Exception:
        return ""


def web(q: str, limit: int = 5) -> list[dict]:
    """Plain Google search, organic results only: [{title, link, snippet}]. Empty list on any failure except an outage."""
    try:
        payload = request("serper", "POST", SEARCH_URL, headers=_headers(), json_body={"q": q, "num": limit})
    except Exception as exc:
        if _outage(exc):
            raise SerperUnavailable(str(exc)[:200]) from exc
        return []
    return [{"title": (i.get("title") or "").strip(), "link": i.get("link") or "", "snippet": (i.get("snippet") or "").strip()}
            for i in (payload or {}).get("organic", [])[:limit] if i.get("link")]


def search_by_name(company: str, extra_terms: str = "", limit: int = 6) -> str:
    """A general web search for the company BY NAME, for when its own site is dead.

    A domain that fails to load does not mean the business does not exist -- the site
    may have expired while the LinkedIn page, a directory listing or a press mention did
    not. This is the fallback ahead of the sourcing description in research(), tried
    only when website_text() comes back empty.
    """
    q = f"{company} {extra_terms}".strip()
    try:
        payload = request("serper", "POST", SEARCH_URL, headers=_headers(),
                          json_body={"q": q, "num": limit})
    except Exception as exc:
        if _outage(exc):
            raise SerperUnavailable(str(exc)[:200]) from exc
        return ""
    lines = []
    kg = (payload or {}).get("knowledgeGraph") or {}
    if kg.get("description"):
        lines.append(f"{kg.get('title', company)}: {kg['description']}")
    for item in (payload or {}).get("organic", [])[:limit]:
        snippet = (item.get("snippet") or "").strip()
        if snippet:
            lines.append(f"- {item.get('title', '')}: {snippet}")
    return "\n".join(lines)


def website_text(domain: str, max_chars: int = 6000) -> str:
    """Fetch homepage + /about + /press|/news and strip to text (spec Stage 5 step 3).

    Best-effort: a site that blocks us or has no /press simply contributes less text.
    A hard failure here must not kill the contact, but a total blank does -- the
    validator's research check catches that downstream.

    Measured 2026-09-27: 121 of 240 company sites came back empty. The request named
    itself a bot and only ever tried the bare https host, so anything behind a bot filter
    or living on www. read as "could not be fetched" -- and the research brief, and the
    email built on it, were written from nothing. It now asks like a browser, tries the
    www. and http forms, and hands a refusal to Serper's scraper before giving up.
    """
    chunks: list[str] = []
    paths = ["", "/about", "/about-us", "/press", "/news", "/newsroom"]
    seen_paths = 0
    base = None
    # A company page answers in a second or two. The shared 90s read timeout made every
    # dead domain cost minutes across the hosts tried below.
    with httpx.Client(timeout=_SITE_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": _BROWSER_UA,
                               "Accept": "text/html,application/xhtml+xml",
                               "Accept-Language": "en-US,en;q=0.9"}) as c:
        for path in paths:
            if seen_paths >= 3 or sum(len(x) for x in chunks) >= max_chars:
                break
            hosts = [base] if base else [f"https://{domain}", f"https://www.{domain}",
                                         f"http://{domain}"]
            for host in hosts:
                try:
                    resp = c.get(f"{host}{path}")
                except Exception:
                    continue
                if resp.status_code == 200 and resp.text:
                    text = _strip_html(resp.text)
                    if len(text) > 120:
                        chunks.append(f"[{path or '/'}] {text}")
                        seen_paths += 1
                        base = host
                        break
            if not path and not chunks:
                break          # the homepage itself is refused; the subpages will be too
    if not chunks:
        text = _scrape_via_serper(f"https://{domain}")
        if len(text) > 120:
            chunks.append(f"[/] {text}")
    return (" \n".join(chunks))[:max_chars] or "Website could not be fetched."
