"""Jina Reader: a company's website as clean text (https://r.jina.ai/<url>).

The consultant research prompt was written around Jina text plus the company description from the
sourcing vendors. Serper is NOT used for this client. Best effort: returns "" when the site cannot be
read, and the caller says so plainly instead of reaching for another vendor.
"""

from __future__ import annotations

import os
import time

import httpx

READER = "https://r.jina.ai/"

# 2026-10-28: the Jina key was stopped (HTTP 401 "Invalid API key") and every read returned "" with nothing logged, so quick-run
# enrichment judged companies "website unreadable" (177 Building materials, 134 broad angle). Same order as the ProQ/MN build:
# (1) the BuzzLead gateway's own site crawl (POST /research/site-crawl, not Jina), (2) Jina, without the key once it is rejected.
_CRAWL_TIMEOUT = httpx.Timeout(connect=8.0, read=30.0, write=15.0, pool=10.0)
_key_rejected = False


def _crawl_text(domain: str, max_chars: int) -> str:
    """The gateway's site crawl: a few sales-relevant pages as one markdown digest. '' on any failure."""
    from . import gateway_verify as gv
    from ..config import settings
    if not settings.buzzlead_api_key or os.getenv("SITE_READER_GATEWAY", "true").strip().lower() in {"0", "false", "no", "off"}:
        return ""
    body = {"url": f"https://{domain}", "max_pages": 2 if max_chars <= 3000 else 3, "max_markdown_chars": max_chars, "find_linkedin": False}
    for attempt in range(2):
        try:
            r = httpx.post(gv._base() + "/research/site-crawl", json=body, headers=gv._headers(), timeout=_CRAWL_TIMEOUT)
        except httpx.TimeoutException:
            return ""
        except httpx.HTTPError:
            time.sleep(1.0)
            continue
        if r.status_code == 200:
            try:
                text = ((r.json() or {}).get("site_markdown") or "").strip()
            except ValueError:
                return ""
            return text[:max_chars] if len(text) > 80 else ""
        if r.status_code in (429, 502, 503, 504):
            time.sleep(1.0)
            continue
        break
    return ""


def _jina_headers(extra: dict | None = None) -> dict:
    headers = {"Accept": "text/plain", **(extra or {})}
    key = os.getenv("JINA_API_KEY")
    if key and not _key_rejected:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _key_was_rejected(status: int) -> bool:
    """True (once) when the key is refused: the caller retries keyless, and every later call goes keyless."""
    global _key_rejected
    if status in (401, 403) and os.getenv("JINA_API_KEY") and not _key_rejected:
        _key_rejected = True
        print(f"[warn] jina: JINA_API_KEY is rejected (HTTP {status}); reading without a key from now on", flush=True)
        return True
    return False


def website_text(domain: str, max_chars: int = 6000) -> str:
    text = _crawl_text(domain, max_chars)
    if text:
        return text
    for attempt in range(3):
        try:
            r = httpx.get(f"{READER}https://{domain}", headers=_jina_headers(), timeout=40, follow_redirects=True)
        except httpx.HTTPError:
            time.sleep(1.5)
            continue
        if r.status_code == 200 and len(r.text.strip()) > 80:
            return r.text.strip()[:max_chars]
        if _key_was_rejected(r.status_code):
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        break
    return ""


# --------------------------------------------------------------------------------------------------------------------------
# Several pages per school (Lumion, 2026-10-09). The three angles need facts that live on different pages: the application
# format on the admissions page, office hours on the contact page, start dates on a schedule or calendar page. Reading only the
# home page found a usable signal for 4 of 15 schools (PDF angle), 0 of 15 (After 5PM) and 4 of 15 (Next Start Date).
# --------------------------------------------------------------------------------------------------------------------------

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

_LINK_RX = re.compile(r"(?<!!)\[([^\]]{1,120})\]\((\S+?)\)")
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".zip", ".mp4", ".ico")
_SKIP_PATH = ("/blog", "/news", "/press", "/privacy", "/terms", "/login", "/cart", "/shop", "/careers", "/jobs", "/tag/", "/category/")

# Page groups, in the order they matter. Each group wants at most one page. Words are matched in the link text and the path.
PAGE_GROUPS = {
    "apply": ("apply", "application", "admission", "enroll", "register", "get started"),
    "hours": ("contact", "hours", "visit us", "location", "directions", "campus"),
    "dates": ("schedule", "calendar", "start date", "start dates", "dates", "upcoming", "cohort", "class schedule", "term", "intake"),
}


def extract_links(markdown: str, domain: str, skip_paths: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
    """[(text, absolute url)] for links on the SAME site, with files, anchors, mail and noise paths removed. Order preserved."""
    base = f"https://{domain}/"
    host = domain.lower().removeprefix("www.")
    out, seen = [], set()
    for text, url in _LINK_RX.findall(markdown or ""):
        url = url.strip("<>").split("#")[0]
        if not url or url.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base, url)
        p = urlparse(absolute)
        if not p.netloc or p.netloc.lower().removeprefix("www.") != host:
            continue
        if p.path.lower().endswith(_SKIP_EXT) or any(s in p.path.lower() for s in (_SKIP_PATH if skip_paths is None else skip_paths)):
            continue
        key = absolute.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append((text.strip(), absolute))
    return out


def pdf_links(markdown: str, domain: str) -> list[tuple[str, str]]:
    """Links to PDF files on this site, with their link text. An application PDF is the whole point of the PDF angle."""
    base = f"https://{domain}/"
    host = domain.lower().removeprefix("www.")
    out = []
    for text, url in _LINK_RX.findall(markdown or ""):
        absolute = urljoin(base, url.strip("<>"))
        p = urlparse(absolute)
        if p.path.lower().endswith(".pdf") and (not p.netloc or p.netloc.lower().removeprefix("www.") == host):
            out.append((text.strip(), absolute))
    return out


def pick_pages(links: list[tuple[str, str]], limit: int = 3) -> list[str]:
    """One page per group (apply, hours, dates), best match first, no page twice, at most `limit`."""
    chosen: list[str] = []
    for group, words in PAGE_GROUPS.items():
        best, best_score = None, 0
        for text, url in links:
            if url in chosen or urlparse(url).path in ("", "/"):
                continue
            hay = (text + " " + urlparse(url).path).lower()
            score = sum(2 if w in text.lower() else 1 for w in words if w in hay)
            if score > best_score:
                best, best_score = url, score
        if best:
            chosen.append(best)
        if len(chosen) >= limit:
            break
    return chosen


_SIGNAL_RX = re.compile(
    r"(\bhours?\b|\bopen\b|\bclosed?\b|\b\d{1,2}(:\d\d)?\s?(am|pm|a\.m|p\.m)\b|\bmon(day)?\b|\bfri(day)?\b|\bsat(urday)?\b|"
    r"\bstart(s|ing)?\b|\bbegin(s|ning)?\b|\bnext class|\bcohort|\bsession|\bsemester|\bterm\b|\bintake|"
    r"\bapply\b|\bapplication|\benroll|\bregist|\bpdf\b|\bprint\b|\bdownload|\bschedule|\bcalendar|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b)",
    re.I,
)


def _signal_trim(text: str, head: int, extra: int) -> str:
    """The first `head` characters, then ONLY the later lines that mention hours, dates, applying or PDFs, up to `extra` characters.
    A long home page buries the footer hours past any fixed cut (daacademyofnc.com: hours were after character 6,000)."""
    if len(text) <= head + extra:
        return text
    kept, used = [], 0
    for line in text[head:].split("\n"):
        line = line.strip()
        if line and _SIGNAL_RX.search(line):
            line = line[:300]
            if used + len(line) > extra:
                break
            kept.append(line)
            used += len(line) + 1
    return text[:head] + ("\n[...]\n" + "\n".join(kept) if kept else "")


def _fetch_url(url: str, max_chars: int, links_summary: bool = False) -> str:
    # Appends every link on the page, menus included. Without it a site whose navigation is script-built yields no links at all
    # (tonsore.com, hsti.info and azyogaacademy.com all returned zero candidate pages).
    extra = {"X-With-Links-Summary": "true"} if links_summary else None
    for attempt in range(3):
        try:
            r = httpx.get(f"{READER}{url}", headers=_jina_headers(extra), timeout=40, follow_redirects=True)
        except httpx.HTTPError:
            time.sleep(1.0)
            continue
        if r.status_code == 200 and len(r.text.strip()) > 80:
            return r.text.strip()[:max_chars]
        if _key_was_rejected(r.status_code):
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        break
    return ""


def website_text_with_pages(domain: str, home_chars: int = 4500, page_chars: int = 1800, max_pages: int = 3) -> str:
    """The home page plus up to three more pages that carry the angles' signals, and every PDF link found.
    Pages are marked 'PAGE: <url>' so the research prompt can tell them apart. Long pages keep their head and then only the lines
    that mention hours, dates, applying or PDFs. Returns "" when the home page cannot be read."""
    raw = _fetch_url(f"https://{domain}", 80000, links_summary=True)
    if not raw:
        return ""
    body, _, summary = raw.partition("Links/Buttons")
    both = body + "\n" + summary
    links = extract_links(both, domain)
    pdfs = pdf_links(both, domain)
    urls = pick_pages(links, max_pages)
    pages: list[tuple[str, str]] = []
    if urls:
        with ThreadPoolExecutor(max_workers=len(urls)) as pool:
            for url, text in zip(urls, pool.map(lambda u: _fetch_url(u, 80000), urls)):
                if text:
                    pages.append((url, text))
                    pdfs += pdf_links(text, domain)
    parts = [_signal_trim(body, home_chars, 1800)]
    for url, text in pages:
        parts.append(f"\n\nPAGE: {url}\n{_signal_trim(text, page_chars, 900)}")
    seen = set()
    pdf_lines = []
    for text, url in pdfs:
        if url not in seen:
            seen.add(url)
            pdf_lines.append(f"- {text or '(no link text)'} -> {url}")
    if pdf_lines:
        parts.append("\n\nPDF LINKS FOUND ON THE SITE:\n" + "\n".join(pdf_lines[:12]))
    return "".join(parts)


# Spec mode (TFP-COPY-SPEC section 2): the pages that matter depend on the client, so the words come from client.toml research_page_words.
_SPEC_SKIP = tuple(p for p in _SKIP_PATH if p not in ("/careers", "/jobs"))     # the spec reads career pages too
_SPEC_PRIORITY = (("safety", "ehs", "health", "sustainab", "esg", "csr"), ("location", "facilit", "plant", "branch", "mill", "yard", "terminal"),
                  ("product", "capabilit", "service", "logistic", "shipping", "fleet", "transport"), ("about", "career", "who we are"))


def website_text_spec(domain: str, words: tuple[str, ...], max_pages: int = 5, max_chars: int = 25000) -> str:
    """Home page plus up to `max_pages` internal pages whose path or link text matches `words`, safety first, then locations, then products,
    then the rest. Every page is marked '=== PAGE: <url> ===' and lines that repeat across pages (nav, footer) are dropped after their first
    appearance. Total length is capped at `max_chars`, keeping the home page and the safety, locations and products pages first.
    Returns "" when the home page cannot be read. The BuzzLead gateway crawl is tried first (it picks the sales-relevant pages itself)."""
    crawled = _crawl_text(domain, max_chars)
    if crawled:
        return crawled
    raw = _fetch_url(f"https://{domain}", 80000, links_summary=True)
    if not raw:
        return ""
    body, _, summary = raw.partition("Links/Buttons")
    links = extract_links(body + "\n" + summary, domain, skip_paths=_SPEC_SKIP)
    words = tuple(w.lower() for w in words)

    def score(text: str, url: str) -> tuple[int, int]:
        hay = (text + " " + urlparse(url).path).lower()
        if urlparse(url).path in ("", "/") or not any(w in hay for w in words):
            return (99, 0)
        for rank, group in enumerate(_SPEC_PRIORITY):
            if any(g in hay for g in group):
                return (rank, -sum(1 for w in words if w in hay))
        return (len(_SPEC_PRIORITY), -sum(1 for w in words if w in hay))

    ranked = sorted(((score(t, u), t, u) for t, u in links), key=lambda x: x[0])
    urls, seen = [], set()
    for (rank, _n), _t, url in ranked:
        if rank == 99 or url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= max_pages:
            break
    pages: list[tuple[str, str]] = [(f"https://{domain}", body)]
    if urls:
        with ThreadPoolExecutor(max_workers=len(urls)) as pool:
            for url, text in zip(urls, pool.map(lambda u: _fetch_url(u, 60000), urls)):
                if text:
                    pages.append((url, text))
    seen_lines: set[str] = set()
    out, used = [], 0
    for url, text in pages:
        kept = []
        for line in text.splitlines():
            key = line.strip()
            if key and len(key) < 200:
                if key in seen_lines:
                    continue
                seen_lines.add(key)
            kept.append(line)
        chunk = f"=== PAGE: {url} ===\n" + "\n".join(kept).strip()
        room = max_chars - used
        if room <= 200:
            break
        chunk = chunk[:min(len(chunk), room, 9000 if out else 6000)]
        out.append(chunk)
        used += len(chunk) + 2
    return "\n\n".join(out)
