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


def website_text(domain: str, max_chars: int = 6000) -> str:
    key = os.getenv("JINA_API_KEY")
    headers = {"Accept": "text/plain"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(2):
        try:
            r = httpx.get(f"{READER}https://{domain}", headers=headers, timeout=40, follow_redirects=True)
        except httpx.HTTPError:
            time.sleep(1.5)
            continue
        if r.status_code == 200 and len(r.text.strip()) > 80:
            return r.text.strip()[:max_chars]
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


def extract_links(markdown: str, domain: str) -> list[tuple[str, str]]:
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
        if p.path.lower().endswith(_SKIP_EXT) or any(s in p.path.lower() for s in _SKIP_PATH):
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
    key = os.getenv("JINA_API_KEY")
    headers = {"Accept": "text/plain"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if links_summary:
        # Appends every link on the page, menus included. Without it a site whose navigation is script-built yields no links at all
        # (tonsore.com, hsti.info and azyogaacademy.com all returned zero candidate pages).
        headers["X-With-Links-Summary"] = "true"
    for attempt in range(2):
        try:
            r = httpx.get(f"{READER}{url}", headers=headers, timeout=40, follow_redirects=True)
        except httpx.HTTPError:
            time.sleep(1.0)
            continue
        if r.status_code == 200 and len(r.text.strip()) > 80:
            return r.text.strip()[:max_chars]
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
