"""Stage 3 -- Company finder: AI Ark search, then an LLM enforcement pass.

AI Ark's industry and keyword filters are OR-logic, so a search for
["legal", "healthcare"] returns companies matching EITHER. The enforcement pass
re-applies the criteria with AND logic and is what actually holds the ICP: in vertical,
US, funded (or over the revenue floor), not on the exclusion list, not MLM/direct sales.
It replaces Claygent, since Clay is out of the loop.

Order matters for cost (spec section 7): dedup runs BEFORE the enforcement pass and
before any export, so we never pay to reason about a company we already emailed.
"""

from __future__ import annotations

import re
import time
import threading
import os
from concurrent.futures import ThreadPoolExecutor

import json
import sys
from pathlib import Path

from .. import client, db, taxonomy
from ..clients import leadocean, llm, quickenrich
from ..finders import uses_leadocean as finders_uses_leadocean
from ..clients.http import VendorOutage
from ..config import settings
from .s2_expansion import Vertical

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from vendor.aiark_company_search import search_companies  # noqa: E402

ENFORCE_MODEL = "anthropic/claude-haiku-4.5"
ENFORCE_TEMPERATURE = 0.0
ENFORCE_BATCH = 20
ENFORCE_WORKERS = 4
WIDEN_PROBE = 400      # companies judged before a widened sibling is assessed
AUDIENCE_PROBE = 200   # companies judged before the audience's own label is
WIDEN_MIN_KEEP = 0.15  # below this keep rate the sibling is abandoned
# Enforcement keep rate used only to convert a kept-company need into a fresh-fetch
# ceiling. Conservative on purpose: too low over-fetches a little, too high starves.
ASSUMED_KEEP_FOR_CAP = 0.4
# The same floor, applied to the audience's OWN label. It only guarded widened siblings,
# so an audience whose original label was wrong ground on to the fresh cap. Measured
# 2026-09-20: "Personal Care Device & Grooming Tool Brands" anchored on "Personal Care
# Product Manufacturing" -- a label full of cosmetics and supplement makers -- and kept
# 1 of 20. Reaching its target that way means scanning sixteen thousand companies and
# paying the gate to reject every one. Half the sibling probe, because the question here
# is cruder: is this label about this audience at all.   # concurrent gate calls; the LLM client's semaphore is the hard cap

def _enforce_system(fit_rule: str) -> str:
    # Plain replace, not .format() -- the template below contains a literal JSON
    # example with unescaped braces later on.
    return ENFORCE_SYSTEM_TEMPLATE.replace(
        "{fit_rule}", fit_rule or "a plausible buyer of this client's offer")


ENFORCE_SYSTEM_TEMPLATE = """You are a list-quality gate for a B2B outreach pipeline.

For each company, decide KEEP or DROP by applying ALL criteria with AND logic. The
upstream search used OR logic, so many companies will match only one criterion and
must be dropped.

A SUBSTANTIAL PRACTICE COUNTS. The vertical name describes the buyer, not a purity
test. A company with a real, named practice area, product line or client base in the
vertical QUALIFIES -- exclusive specialisation is NOT required and must never be the
reason for a DROP. A full-service law firm with a medical-malpractice practice is in;
a diversified manufacturer with a real diagnostics line is in. What is out is a company
merely ADJACENT to the vertical: a vendor or supplier to it, a distributor or reseller
rather than an operator, or one whose only connection is a passing mention.

KEEP only if ALL are true:
- Operates in the stated vertical, as an operator rather than a supplier to it
- Headquartered in the United States
- Meets the stated funding or revenue floor IF ONE IS GIVEN (when it says NONE, funding plays no part)
- Employee count within the stated range
- Not on the exclusion list, and not an MLM or direct-sales structure

MISSING DATA IS NOT A FAILURE. Drop a company on EVIDENCE that it fails a criterion,
never on the absence of evidence that it passes. The records you are shown are sparse
and get sparser for smaller companies, and the search that produced them has ALREADY
filtered on United States location and on the employee range -- so a blank location,
a blank employee count or a blank funding field tells you nothing and must be treated
as satisfied. "No funding information provided" and "cannot confirm" are not reasons
to drop. Neither is a short or empty description, on its own, evidence that a company
is outside the vertical: it arrived from an industry and NAICS search that matched it.

A record with "keywords_matched": false did not contain the vertical's keyword list.
That is NOT a reason to drop it: the record arrived through a matching industry label
and keyword lists are narrow. Judge it on its description, name and industry exactly
as you would any other record.

A DROP needs EVIDENCE IN THE RECORD. If your reason would say "likely", "probably",
"possibly" or "appears to be", you are guessing -- that is a KEEP. And manufacturing or
sourcing abroad is NOT foreign ownership: drop as foreign only when the record shows the
company itself is non-US or foreign-owned.

IF YOU CANNOT TELL because the record is too thin to judge either way, the verdict is
KEEP, never DROP. A reason containing "no description", "cannot confirm", "insufficient
evidence", "insufficient information" or similar is ALWAYS wrong on its own -- it means
you found no evidence of failure, which is a KEEP by the rule above, not a DROP. Only
write DROP when the record itself states or clearly implies the disqualifying fact.

Judge the vertical fit on what the record actually says. DROP when the record shows
the company is a supplier, distributor, reseller, contract or private-label
manufacturer, a services vendor to the vertical, an MLM, or plainly in a different
business. A wrong KEEP costs enrichment credits and sends a stranger an email about
the wrong industry -- but a wrong DROP throws away a company that fits, and dropping
everything unfamiliar is how a 1,187-company vertical became 24.

THREE VERDICTS, not two. Troy, 2026-09-13: "fits IDPR regardless should be included
in that vertical for more coverage." (That request was IDPR-specific; every client
gets the SAME three-tier structure, but the second tier is defined by THIS client's
own fit rule below, not IDPR's -- see client.toml's `fit_rule` field, which every
client declares for exactly this purpose.)
  KEEP -- an operator in the stated vertical. First choice.
  SECOND-TIER -- NOT an operator in this vertical, but {fit_rule}. Not a supplier,
          reseller, agency, MLM, nonprofit, government contractor or staffing firm.
          These are the second tier: used to fill the vertical toward its contact
          target when KEEPs alone fall short. Return this tier's decision as the
          literal string "IDPR" (the field name kept for compatibility across every
          client's stored verdicts -- it is a tier label, not a claim this company
          fits IDPR specifically).
  DROP -- not a match for this client's fit rule at all, or fails a hard criterion.

Return STRICT JSON only:
{"results": [{"domain": "...", "decision": "KEEP" or "IDPR" or "DROP", "reason": "under 8 words"}]}
Return one entry for every company given, in the same order."""


# AI Ark industries are a LinkedIn-style taxonomy, not free text, and the filter ORs
# every term you give it. Measured 2026-09-10 (US, 11-500 employees):
#   Fintech 3,133 (all real)   Financial Services 43,927 (incl. government offices)
#   Insurtech 580              Insurance 17,858
#   Law Practice 17,469 (firms) Legal Services 3,215 (Attorney General offices, "Retired")
#   SaaS 5,411                 Software Development 36,386
#   Health Tech 0 (not a term) Hospitals and Health Care 64,546
# Also gated on this plan, verified 2026-09-10: `lookalikeDomains` returns 200 but is
# SILENTLY IGNORED -- three different seed sets returned the identical 1,063,382-company
# result. Do not build on it; it will look like it works.
# One broad sibling in the list swallows the tight term and the enforcement pass then
# rejects 99% of what comes back. So every proposed term is probed for its match count
# first: 0 means it is not a taxonomy term, above the ceiling means it is too broad to
# be a targeting choice at all. Both are dropped before any company is fetched.
# Raised from 6,000 on 2026-09-11. The ceiling dates from when every term went into ONE
# AI Ark request and a broad sibling drowned the tight ones; terms are now searched
# separately and unioned, so a 6-9k label cannot drown anything -- it can only add.
# Measured that day, labels sitting in 6-9k with real unworked supply: environmental
# services 7,969, food and beverage manufacturing 7,326, retail apparel 6,367, and
# medical equipment manufacturing itself at 6,079 -- which only survived because it
# was the sole term and got rescued. 'Banking' (9,797) stays out: it is lenders and
# regional banks, a sector rather than a vertical.
TERM_CEILING = 9000
TERM_FLOOR = 25
# Widening is a WATERFALL, one sibling at a time. Troy, 2026-09-11: "always go back to
# the pool to pull fresh leads before going to sister verticals ... and it should
# waterfall down to sister verticals too." So: the vertical's own labels are paged to
# exhaustion first (the fetch is seeded with everything already taken, so "exhausted"
# means exhausted); then ONE sibling label is added and paged to exhaustion; then the
# next. Adding two at once let the second dilute the first before it was used up.
# Troy, 2026-09-13: "lock in how many siblings a vertical can pull so it doesn't take
# forever." Three hard bounds, any one of which ends widening:
MAX_WIDENINGS = 2            # at most two sister labels per vertical
SIBLING_FRESH_CAP = 1500     # at most this many fresh companies judged per sister label
STAGE3_TIME_BUDGET_S = 45 * 60   # and the whole sourcing stage stops at 45 minutes
# A term rejected for breadth is only rescued if it is CLOSE to the ceiling. "Medical
# Equipment Manufacturing" at 6,079 names exactly the vertical and misses by 1.3%;
# "Real Estate" at 47,865 is eight times over and is a different business from the
# proptech platforms its vertical asked for. Rescuing the latter on 2026-09-11 fetched
# 4,554 realtors, brokerages and property managers to find 73 platforms -- enforcement
# rejected 95%, exactly as the ceiling comment above predicts.
RESCUE_MAX = int(TERM_CEILING * 1.25)   # 11,250: still refuses Real Estate (47,865), Wellness (20,408)
PAGE_SIZE = 100

# AI Ark rate-limits under the load of several verticals paging it together. One gate
# for the whole process, and a retry ladder for the 429s that still get through.
_AIARK_GATE = threading.BoundedSemaphore(int(os.getenv("AIARK_MAX_CONCURRENCY", "2")))
AIARK_RETRIES = 4
AIARK_BACKOFF = (5, 10, 20, 40)   # seconds
MAX_PAGES = 80        # scan up to 8,000 companies looking for enough fresh ones


def _term_count(term: str, vertical: Vertical) -> int | None:
    """totalElements for a single industry term under the vertical's size/location."""
    import httpx

    body = {"account": {
        "industries": {"any": {"include": {"mode": "WORD", "content": [term]}}},
        "location": {"any": {"include": ["United States"]}},
        "employeeSize": {"type": "RANGE",
                         "range": [{"start": vertical.employee_min, "end": vertical.employee_max}]},
    }, "page": 0, "size": 1}
    try:
        r = httpx.post("https://api.ai-ark.com/api/developer-portal/v1/companies",
                       headers={"X-TOKEN": settings.aiark_api_key,
                                "Content-Type": "application/json"},
                       json=body, timeout=60)
        if r.status_code != 200:
            return None
        return int((r.json() or {}).get("totalElements") or 0)
    except Exception:
        return None


def calibrate_industries(vertical: Vertical, run_id: str | None = None) -> list[str]:
    """Keep only the terms that are real taxonomy entries of usable breadth.

    With one safety net: if EVERY term is rejected for breadth, the narrowest of them is
    kept anyway, because _fetch_fresh searches each term as its OWN query and unions the
    results. An extra term can therefore only ADD supply -- it never narrows the others.

    The ceiling is a cliff edge and the medical device run of 2026-09-11 fell off it.
    "Medical Equipment Manufacturing" counts 6,079 against a 6,000 ceiling, so the
    vertical's only label was discarded by a margin of 1.3% and sourcing ran on its two
    NAICS codes alone: 339112 (4,098) + 334510 (1,064) = the 5,162 the run reported. The
    dropped label would have been a third query worth 6,079 on its own.

    Note the asymmetry with stage 2b: probe() sends industries and NAICS in ONE filter,
    and AI Ark ANDs them, so the same vertical measures 2,449 there. That makes the
    freshness gate pessimistic, but it does not change what sourcing can reach.

    Terms under the floor are NOT rescued: those are invented phrases that match nothing
    ("Health Tech"), and keeping one would filter the search down to zero results.
    """
    kept, dropped = [], []
    too_broad: list[tuple[int, str]] = []
    for term in vertical.industries:
        n = _term_count(term, vertical)
        if n is None:
            kept.append(term)          # probe failed; do not lose the term on a blip
            continue
        if n < TERM_FLOOR:
            dropped.append(f"{term} ({n}: not a taxonomy term)")
        elif n > TERM_CEILING:
            dropped.append(f"{term} ({n:,}: too broad)")
            too_broad.append((n, term))
        else:
            kept.append(term)
    rescued = None
    if not kept and too_broad:
        n, term = min(too_broad)
        if n <= RESCUE_MAX:
            rescued = term
            kept.append(term)
    if dropped:
        db.log(run_id, "stage3", f"{vertical.name}: calibrated industries -> {kept}",
               dropped=dropped, rescued=rescued)
    return kept


def _account_filter(vertical: Vertical, industries: list[str] | None,
                    naics: list[str] | None) -> dict:
    """One AI Ark account filter. Either an industry label set OR a NAICS code set.

    NAICS is a second, independent taxonomy and it is far more precise than the
    LinkedIn-style labels: 541110 "Offices of Lawyers" is 21,229 actual law firms,
    where the label "Legal Services" (3,215) is padded with Attorney General offices
    and records literally named "Retired". Codes must be SIX digits -- the records
    carry ['513210','541511','522320'] and a 4-digit prefix like "5411" matches
    nothing, returning a confident zero.
    """
    account: dict = {
        "location": {"any": {"include": ["United States"]}},
        "employeeSize": {"type": "RANGE",
                         "range": [{"start": vertical.employee_min,
                                    "end": vertical.employee_max}]},
    }
    if naics:
        account["naics"] = {"any": {"include": naics}}
    if industries:
        account["industries"] = {"any": {"include": {"mode": "WORD", "content": industries}}}
    return account


_KW_STOPWORDS = frozenset({
    "and", "the", "for", "with", "firm", "firms", "company", "companies", "group",
    "services", "service", "solutions", "solution", "based", "provider", "providers",
})


def _tokens(text: str) -> set[str]:
    """Word set of a string, punctuation folded to spaces so hyphens split cleanly."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return {t for t in cleaned.split() if len(t) > 2}


def _keyword_hit(company: dict, keywords: list[str]) -> bool:
    """Local keyword match -- the server-side keyword filter this plan cannot use.

    account.keyword 401s on this plan, but every company record carries its own
    `keywords` list (Signifyd: 'fraud prevention', 'guaranteed payments', ...), plus a
    name, description and industry string. Matching here reproduces the filter after
    the fetch instead of before it: it costs pages rather than credits, and unlike the
    server filter it is inspectable.

    Matching is deliberately BROAD. Troy, 2026-09-11: "we need the keywords to be as
    broad as possible, still relating to niche but broad enough for us to keep enough
    data." Exact-phrase matching was the opposite of that -- the generated keywords are
    multi-word phrases like "asset management boutique", "FDA clearance" and
    "point-of-care", and a company describing itself as "boutique asset manager" or
    "cleared by the FDA" failed a substring test while being exactly what was asked for.

    So a keyword hits when either:
      - the whole phrase appears as a substring (the old rule, still the strongest), or
      - at least half its meaningful tokens appear as WORDS in the company's text.

    Half, not all, because word order and inflection vary and the vertical is already
    constrained by industry and NAICS before this runs -- this filter refines a
    pre-narrowed pool rather than selecting from the open market.
    """
    if not keywords:
        return True
    # keywords arrives as a list from the raw API but as a comma-joined STRING once
    # _normalize_company has flattened it. Iterating the string yields its characters,
    # so the haystack became "c h e ..." and the keyword half of this filter silently
    # matched nothing -- every hit was coming from the name and description alone.
    kw = company.get("keywords") or []
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",")]
    hay = " ".join(str(x).lower() for x in [
        company.get("name"), company.get("description"), company.get("industry"),
        " ".join(kw),
    ] if x)
    hay_tokens = _tokens(hay)

    for raw in keywords:
        k = (raw or "").strip().lower()
        if len(k) <= 2:
            continue
        if k in hay:
            return True
        wanted = {t for t in _tokens(k) if t not in _KW_STOPWORDS}
        if not wanted:
            continue
        # A two-token keyword must match BOTH tokens. Half-matching them is what turns
        # "medical malpractice" into "medical": measured on the 72-company medical-device
        # sample, half-matching pulled 57 of them into the health-law vertical, because
        # the qualifier that carries the whole meaning was the half being dropped.
        # Three tokens and up can afford to lose one and still name the same niche.
        need = len(wanted) if len(wanted) <= 2 else (len(wanted) + 1) // 2
        if len(wanted & hay_tokens) >= need:
            return True
    return False


def _fetch_fresh(vertical: Vertical, industries: list[str], want: int,
                 run_id: str | None = None,
                 exclude: set[str] | None = None) -> tuple[list[dict], dict]:
    """Page through AI Ark until `want` companies SURVIVE dedup, or the scan ceiling hits.

    Why the cap is counted in survivors, not raw results: AI Ark returns a stable
    ordering, and IDPR's past campaigns were built from the head of that same list.
    Measured 2026-09-10 on consumer fintech -- 177 of the first 200 results were
    already in the 223k-contact history. Fetching "the first 800" therefore re-fetches
    the 800 already emailed and yields nothing. Dedup is applied per page so the paid
    enforcement pass and enrichment only ever see companies that are genuinely new.
    """
    import httpx
    from vendor.aiark_company_search import _normalize_company

    headers = {"X-TOKEN": settings.aiark_api_key, "Content-Type": "application/json"}

    # Each term is searched SEPARATELY and the results merged. AI Ark ORs the terms in
    # one request, so a broad sibling simply outranks a tight one and fills every page
    # with its own results -- the tight term contributes nothing even though it is in
    # the query. Separate searches give each term its own share of the budget.
    queries: list[tuple[str, dict]] = []
    for code in (vertical.naics or []):
        queries.append((f"naics:{code}", _account_filter(vertical, None, [code])))
    for term in industries:
        queries.append((f"industry:{term}", _account_filter(vertical, [term], None)))
    if not queries:
        return [], {"scanned": 0, "blocked_by_dedup": 0, "fresh": 0, "pages": 0,
                    "available": 0, "keyword_filtered": 0, "per_query": {},
                    "vendor_error": False}

    per_query_want = max(want // len(queries) + 1, 10)
    fresh: list[dict] = []
    # Seeded with everything the CALLER has already taken. Without this a second fetch
    # restarts at page 0, fills its per-query quota with the same companies it handed
    # over last time, and the caller discards them all as already seen -- reading as
    # "source exhausted" with most of the universe never opened. Medical device,
    # 2026-09-11: 3,764 of 11,241 scanned, then exhausted at exactly the first slice's
    # 1,475 fresh, leaving 7,477 companies in its own labels unread.
    seen: set[str] = set(exclude or ())
    scanned = blocked_total = kw_filtered = kw_passthrough = 0
    total_available = 0
    per_query: dict[str, int] = {}
    queries_failed = 0

    # A heartbeat. Paging, dedup and the fit gate used to write nothing at all until
    # the whole loop finished, so a run that was working and a run that was dead looked
    # identical from Slack for forty minutes at a time. Once a minute is enough to tell
    # them apart and far too little to be noise.
    beat = [time.time()]

    def _beat(msg: str) -> None:
        if time.time() - beat[0] >= 60:
            beat[0] = time.time()
            db.log(run_id, "stage3", f"{vertical.name}: {msg}")

    for label, account in queries:
        got = 0
        for page in range(MAX_PAGES):
            if got >= per_query_want or len(fresh) >= want:
                break
            # A 429 or a 5xx is NOT the end of the query. With three verticals paging AI
            # Ark at once (2026-09-13) a 429 on page 3 made Boutique Wealth read as
            # "source exhausted" at 1,110 fresh with 12,000 companies unopened -- the
            # paging bug's cousin, silent and in the unsafe direction. Retry with
            # backoff behind a gate shared by every vertical; give up only after that.
            r = None
            for attempt in range(AIARK_RETRIES + 1):
                try:
                    with _AIARK_GATE:
                        r = httpx.post("https://api.ai-ark.com/api/developer-portal/v1/companies",
                                       headers=headers,
                                       json={"account": account, "page": page, "size": PAGE_SIZE},
                                       timeout=90)
                except Exception as exc:
                    r = None
                    err = str(exc)
                else:
                    if r.status_code == 200:
                        break
                    err = f"HTTP {r.status_code}"
                    if r.status_code < 500 and r.status_code != 429:
                        break                       # a real 4xx: not retryable
                if attempt < AIARK_RETRIES:
                    time.sleep(AIARK_BACKOFF[attempt])
            if r is None or r.status_code != 200:
                db.log(run_id, "stage3",
                       f"{vertical.name}/{label} page {page}: {err} after "
                       f"{AIARK_RETRIES + 1} attempts; giving up on this query",
                       level="warn")
                db.record_vendor_usage("aiark", "company_page", 1,
                                       status=str(r.status_code) if r is not None else "error",
                                       run_id=run_id, vertical=vertical.name)
                if page == 0:
                    queries_failed += 1
                if r is not None and r.status_code in (401, 402):
                    # Out of credit / unauthorised answers every query the same way;
                    # each further query is a paid, refused round trip. Stop here --
                    # queries_failed == len(queries) below reads as vendor_error.
                    queries_failed = len(queries)
                break
            d = r.json() or {}
            db.record_vendor_usage("aiark", "company_page", 1, status="ok",
                                   run_id=run_id, vertical=vertical.name)
            db.record_vendor_usage("aiark", "companies_returned",
                                   len(d.get("content") or []), status="ok",
                                   run_id=run_id, vertical=vertical.name)
            if page == 0:
                total_available += int(d.get("totalElements") or 0)
            _beat(f"scanning {label} page {page}: {scanned:,} seen, "
                  f"{len(fresh):,} fresh of {want:,} wanted")
            raw_rows = d.get("content") or []
            rows = [_normalize_company(x) for x in raw_rows]
            if not rows:
                break
            # Every record names the taxonomy entries it belongs to. Harvesting them is
            # how the supply map (app/taxonomy.py) learns terms nobody thought to propose.
            try:
                taxonomy.record_seen(
                    (t for x in raw_rows for t in (x.get("industries") or [])),
                    (c for x in raw_rows for c in (x.get("naics") or [])))
            except Exception:
                pass
            scanned += len(rows)
            for c in rows:
                c["domain"] = db.normalize_domain(c.get("domain") or "")
            rows = [c for c in rows if c["domain"] and c["domain"] not in seen]

            # Keywords are a pre-filter to save gate calls, not the ICP judge. A company
            # that arrived through a MATCHING INDUSTRY LABEL has already been vouched for
            # by the taxonomy, so a keyword miss sends it to the gate flagged rather than
            # binning it. Troy, 2026-09-13: "for companies that fail the keyword filter, i
            # want them to go through a cheap prompt on if they are a fit". NAICS-sourced
            # companies stay keyword-strict: codes like 541511 pull in noise. Friday's
            # first medical-device slice binned 1,054 label-matched companies this way.
            # Troy, 2026-09-13: NAICS misses go to the gate too. The codes on a vertical
            # are the 1-3 OPERATOR codes stage 2 chose (339112, 334510), not the noisy
            # codes harvested off records, so a keyword miss under them deserves the same
            # second look as one under the label. Every verdict is banked with
            # _keyword_hit and _source_query, so the keep rate per source is measured,
            # and this can be tightened per source if the numbers say so.
            matched = []
            for c in rows:
                hit = _keyword_hit(c, vertical.keywords)
                c["_keyword_hit"] = hit
                matched.append(c)
                if not hit:
                    kw_passthrough += 1

            blocked = db.blocked_domains(c["domain"] for c in matched)
            blocked_total += len(blocked)
            for c in matched:
                seen.add(c["domain"])
                if c["domain"] not in blocked:
                    c["_source_query"] = label
                    fresh.append(c)
                    got += 1
            for c in rows:
                seen.add(c["domain"])
            if d.get("last"):
                break
        per_query[label] = got
        if len(fresh) >= want or queries_failed == len(queries):
            break

    # Every query failed (HTTP error, not a normal empty page) and nothing was ever
    # scanned: AI Ark itself is unavailable (dead key, out of credit, an outage) --
    # not a genuine "this market has nothing left". Confirmed 2026-09-27/28: Product
    # Design and Medical Device were both marked permanently exhausted off a run of
    # queries that were all 402 "insufficient credit", not an actual empty search.
    vendor_error = queries_failed > 0 and queries_failed == len(queries) and scanned == 0
    stats = {"scanned": scanned, "blocked_by_dedup": blocked_total,
             "fresh": len(fresh), "available": total_available,
             "keyword_filtered": kw_filtered, "keyword_passthrough": kw_passthrough,
             "per_query": per_query, "vendor_error": vendor_error}
    return fresh[:want], stats


def _search(vertical: Vertical, exclude: list[str], run_id: str | None = None) -> list[dict]:
    """AI Ark company search, with the keyword filter treated as optional.

    Adding `keywords` to an otherwise-identical request returns 401 on this account --
    verified 2026-09-08: industries-only succeeds, industries+keywords fails, and the
    same industries-only call succeeds again immediately after. So the 401 is an
    entitlement signal for the keyword filter, not a bad key, despite reading as auth.

    Losing keywords costs little: AI Ark's industry and keyword filters are both
    OR-logic, so keywords only widen the net. What actually holds the ICP is the
    enforcement pass below, and the keywords still reach it as context. So try the
    richer query, and fall back rather than failing the whole vertical.
    """
    if not settings.aiark_api_key:
        raise RuntimeError("AIARK_API_KEY is not set")
    industries = calibrate_industries(vertical, run_id)
    if not industries:
        db.log(run_id, "stage3",
               f"{vertical.name}: no usable industry terms survived calibration "
               f"(had {vertical.industries}); skipping search", level="warn")
        return []
    funding = [vertical.funding_floor] if vertical.funding_floor else None
    common = dict(
        api_key=settings.aiark_api_key,
        industries=industries,
        locations=["United States"],
        employee_min=vertical.employee_min,
        employee_max=vertical.employee_max,
        funding_types=funding,
        exclude_domains=exclude or None,
        limit=settings.company_cap_per_vertical,
    )
    if vertical.keywords:
        try:
            return search_companies(keywords=vertical.keywords, **common)
        except Exception as exc:
            db.log(run_id, "stage3",
                   f"{vertical.name}: keyword filter rejected ({str(exc)[:70]}), "
                   "retrying on industries alone", level="warn")
    return search_companies(**common)


def _no_funding(name: str) -> str:
    """Strip a leading funding qualifier from a vertical name for prompt use.

    Funding stopped being a criterion on 2026-09-13, but verticals minted before
    that are still NAMED "Funded X", and the name is fed to the gate two lines
    above "FUNDING FLOOR: NONE -- never drop a company for its funding status".
    Troy, 2026-09-14: "we aren't searching for funded right?"
    """
    out = re.sub(r"^\s*(funded|vc[- ]backed|venture[- ]backed|series [a-d]\+?)\s+",
                 "", name, flags=re.I).strip()
    return out or name


# IDPR's exclusion list used to be written into this prompt and sent for every client,
# so ProductEVO's companies were judged against "law firms are IN scope -- never drop a
# company for being a law firm", which is Interdependence's rule and nobody else's.
# Troy, 2026-09-21: "it should only apply to IDPR." It lives in each client's
# exclusions.md, which is where they already write this down; IDPR's file carries the
# same list word for word, law-firm note included, so its judging is unchanged.
LEGACY_EXCLUSIONS = ("MSPs, VARs, IT consulting/services, government contractors, "
                     "nonprofits, staffing firms, non-US, pre-seed, MLM/direct-sales. "
                     "Law firms are IN scope -- never drop a company for being a law firm.")


def _client_exclusions() -> str:
    from .. import client
    try:
        text = (client.profile().exclusions or "").strip()
    except Exception:
        text = ""
    return text[:1500] or LEGACY_EXCLUSIONS


def _enforce_chunk(vertical: Vertical, chunk: list[dict],
                   force_model: bool = False) -> dict[str, tuple[str, str]]:
    # A client whose research prompt IS the fit gate (research_fit_gate) gets no second, model-based company
    # check here: every company the providers return inside the band goes forward, and the research prompt
    # decides (stage 3b, runner._work_vertical, before any contact is found).
    # force_model: the supply probe still wants the model's keep rate, or every vertical would read 100%.
    if client.profile().research_fit_gate and not force_model:
        return {c.get("domain"): ("KEEP", "fit is decided by the research prompt") for c in chunk}
    listing = [
        {
            "domain": c.get("domain"),
            "name": c.get("name"),
            # 250, not 400. Descriptions are ~60% of the input tokens on a 20-company
            # call and the gate's decision turns on the first sentence or two: what the
            # company does and whether it operates or supplies. Measured 2026-09-13,
            # enforcement cost $2.60-3.23 per vertical, not the $0.20 first estimated.
            "description": (c.get("description") or "")[:250],
            "employees": c.get("employee_count") or c.get("staff_total"),
            "location": c.get("location") or c.get("country"),
            "funding": c.get("funding_type") or c.get("last_funding_type"),
            "industry": c.get("industry"),
            "keywords_matched": bool(c.get("_keyword_hit", True)),
        }
        for c in chunk
    ]
    user = (
        f"VERTICAL: {_no_funding(vertical.name)}\n"
        f"RATIONALE: {vertical.rationale}\n"
        f"EMPLOYEE RANGE: {vertical.employee_min}-{vertical.employee_max}\n"
        f"FUNDING FLOOR: {vertical.funding_floor or 'NONE -- funding is NOT a criterion; never drop a company for its funding status or for having none'}\n"
        f"REVENUE FLOOR (USD): {vertical.revenue_floor_usd or 'N/A'}\n"
        f"DISQUALIFIERS: {json.dumps(vertical.disqualifiers)}\n"
        f"EXCLUSION LIST (this client's own):\n{_client_exclusions()}\n\n"
        f"COMPANIES:\n{json.dumps(listing, indent=1, default=str)}"
    )
    # Pre-copy chain: Anthropic direct first (Troy, 2026-09-13), then OpenRouter,
    # then Concentrate.
    system = _enforce_system(client.profile().fit_rule)
    payload = llm.complete_json(ENFORCE_MODEL, system, user,
                                ENFORCE_TEMPERATURE, max_tokens=2000,
                                provider="precopy")
    results = payload.get("results") if isinstance(payload, dict) else payload
    out: dict[str, tuple[str, str]] = {}
    chunk_names = {db.normalize_domain(c.get("domain") or ""): (c.get("name") or "")
                   for c in chunk}
    for row in results or []:
        if not isinstance(row, dict):
            continue
        domain = db.normalize_domain(row.get("domain") or "")
        if not domain:
            continue
        decision = str(row.get("decision", "DROP")).upper()
        reason = row.get("reason") or ""
        # Safety net, not a style preference: the prompt explicitly forbids dropping
        # for missing data, but the model does it anyway often enough to matter --
        # measured 2026-09-25, "no description"/"cannot confirm"/"insufficient
        # evidence" showed up in real DROP reasons in a random sample. Catch those
        # deterministically rather than trust wording alone to hold every time.
        if decision == "DROP" and _MISSING_DATA_DROP_RE.search(reason):
            decision, reason = "KEEP", f"overridden (missing-data drop): {reason}"
        # Backstop for the regulated-firearms exclusion (ProductEVO, 2026-09-26). A thin
        # record gives the model nothing to exclude on, and the missing-data rule above
        # correctly defaults those to KEEP -- so "Autrey's Armory" and "Joe's Gun Works",
        # both with empty descriptions, got through. The NAME is the one field that is
        # never empty, so check it. Name and domain only: description words ("rifle
        # scope covers", "firearms optics") describe allowed accessories.
        if decision in ("KEEP", "IDPR") and client.profile().name == "productevo":
            if (_FIREARMS_NAME_RE.search(chunk_names.get(domain, ""))
                    or _FIREARMS_DOMAIN_RE.search(domain)):
                decision, reason = "DROP", "regulated firearms business (name check)"
        out[domain] = (decision, reason)
    return out


_FIREARMS_NAME_RE = re.compile(
    r"\b(armou?ry|firearms?|ammo|ammunition|gun\s?works|gunsmith\w*|gun\s?shop|"
    r"suppressors?|silencers?|arms|rifles?|munitions?|ordnance|kalashnikov)\b", re.IGNORECASE)
# Domains have no word boundaries ("autreysarmory.com"), so substrings -- kept to
# tokens that do not occur inside ordinary words.
_FIREARMS_DOMAIN_RE = re.compile(r"armory|firearm|ammunition|gunworks|gunsmith|munition|ordnance|rifles", re.IGNORECASE)


_MISSING_DATA_DROP_RE = re.compile(
    r"no description|cannot confirm|insufficient (evidence|information|data)|"
    r"no (info|information) (provided|available)|unable to (confirm|determine|verify)",
    re.IGNORECASE,
)


def _from_buzzlead(vertical: Vertical, want: int, run_id: str,
                   exclude: set[str]) -> tuple[list[dict], list[dict], dict]:
    """Primary company+contact source. Empty lists when the key is missing or the job fails."""
    from ..clients import buzzlead
    from .. import keywords
    from .s4_people import MARKETING_FIRST, BARE_TITLES

    if not buzzlead.configured():
        return [], [], {"skipped": "no BUZZLEAD_API_KEY"}
    kws = keywords.expand(vertical)
    if kws:
        vertical.keywords = kws
    titles = MARKETING_FIRST + BARE_TITLES
    try:
        seeds = [c["domain"] for c in db.staged(vertical.name, judged=True) if c.get("domain")][:3]
    except Exception:
        seeds = []
    def _step(step: str, secs: int) -> None:
        db.log(run_id, "stage3",
               f"{vertical.name}: BuzzLead is at '{step}' ({secs // 60}m in)")

    companies, contacts, stats = buzzlead.source_vertical(
        vertical, titles, kws,
        max_records=max(want, 50),
        contacts_per_domain=settings.contacts_per_company,
        seed_domains=seeds,
        on_step=_step,
    )
    fresh = []
    seen = set(exclude)
    for c in companies:
        domain = db.normalize_domain(c.get("domain") or "")
        if not domain or domain in seen:
            continue
        c["domain"] = domain
        c["_source_query"] = "buzzlead"
        c["_keyword_hit"] = _keyword_hit(c, vertical.keywords)
        fresh.append(c)
        seen.add(domain)
    blocked = db.blocked_domains(c["domain"] for c in fresh)
    kept = [c for c in fresh if c["domain"] not in blocked]
    stats = {**stats, "blocked_by_dedup": len(blocked), "fresh": len(kept)}
    return kept, contacts, stats


def prefetch(vertical: Vertical, run_id: str) -> int:
    """Fetch, keyword-filter, dedup and STAGE a vertical without judging it.

    Used when the LLM is out: the parts of sourcing that need no LLM still run, so the
    work is banked and resume() only has the gate left to do.
    """
    industries = calibrate_industries(vertical, run_id)
    if not industries and not vertical.naics:
        return 0
    already = {c["domain"] for c in db.staged(vertical.name)}
    fresh, _ = _fetch_fresh(vertical, industries, settings.company_cap_per_vertical,
                            run_id, exclude=already)
    n = db.stage_companies(vertical.name, run_id, fresh, source="aiark")
    db.log(run_id, "stage3", f"{vertical.name}: prefetched and staged {n} companies "
           f"for judging once the LLM is back")
    return n


def find(vertical: Vertical, run_id: str, target_contacts: int | None = None,
         widen: bool = True) -> tuple[list[dict], dict]:
    """Source companies until the vertical can supply target_contacts, or supply runs out.

    Sized against CONTACTS, not a company count. The cap counts FRESH companies,
    enforcement then keeps roughly a third, and contact density varies ten-fold by
    vertical -- measured 2026-09-10, medical device returned 2.4 candidates per company
    and law firms 0.74 -- so one fixed company number overshoots badly in one vertical
    and starves the other.

    So it fetches a batch, enforces it, measures the real keep rate, and decides from
    that whether another batch is needed. Bounded by max_fresh_per_vertical, and it
    stops early when the source is exhausted rather than spinning on empty pages.
    """
    industries = calibrate_industries(vertical, run_id)
    if not industries and not vertical.naics:
        from ..clients import buzzlead as _bl
        if not _bl.configured():
            db.log(run_id, "stage3", f"{vertical.name}: no usable industry terms or NAICS "
                   f"codes after calibration (had {vertical.industries})", level="warn")
            return [], {"companies_found": 0, "companies_dropped_dedup": 0,
                        "companies_dropped_enforcement": 0, "companies_kept": 0}
        db.log(run_id, "stage3", f"{vertical.name}: no AI Ark terms; sourcing via BuzzLead")

    # A scale-up asks for what it still needs; a test run asks for the standard target.
    # And a scale-up does NOT widen into sister labels (Troy, 2026-09-13): sister
    # companies pulled into a winner's campaign are recency-blocked for 90 days, so the
    # sister cannot then be tested on its own -- and their replies land on the winner,
    # diluting the very signal that made it a winner. Own labels, own IDPR tier and
    # banked surplus only; if that cannot reach the target, the gap report says so and
    # Monday's proposal tests the sisters as verticals in their own right.
    # SIZE ON WHAT THIS VERTICAL ACTUALLY YIELDS, not the 1.2-per-company assumption.
    # Troy, 2026-09-30: Monday's launches landed at 1,217 / 438 / 422 of 3,000 and the
    # top-ups added hundreds. The arithmetic was the reason: 2,584 companies were
    # pulled "for 3,000 contacts" on an assumed 1.2 contacts each, and the measured
    # figure this week -- QuickEnrich alone, Apollo and AI Ark out of credit -- was
    # 0.16 to 0.2. At that rate a 3,000-contact vertical needs 15,000+ fit companies;
    # the pull was sized for a sixth of that, so it could never reach the target,
    # and neither could any top-up drawing from the surplus it banked. Own measured
    # density first, then the client-wide median, then the assumption -- floored so a
    # single bad night cannot demand an absurd fetch.
    target = target_contacts or settings.target_contacts
    density_src = "assumed"
    density = max(settings.assumed_contacts_per_company, 0.1)
    try:
        own = db.vertical_density(vertical.name)
        if own:
            density, density_src = max(own, settings.min_sizing_density), "measured (own)"
        else:
            med = db.yield_medians().get("density")
            if med:
                density, density_src = max(med, settings.min_sizing_density), "measured (median)"
    except Exception as exc:
        db.log(run_id, "stage3", f"{vertical.name}: density lookup failed ({exc}); "
               f"sizing on the assumption", level="warn")
    need_kept = int(target / density)
    db.log(run_id, "stage3", f"{vertical.name}: sizing on {density:.2f} contacts/company "
           f"({density_src}) -> {need_kept:,} kept companies for {target:,} contacts")
    kept: list[dict] = []
    drops: list[dict] = []

    # Resume before fetching. A halted run leaves companies staged: some already judged
    # KEEP (carry them), some never judged (judge them now, no fetch needed).
    kept = db.staged(vertical.name, judged=True)
    pending = db.staged(vertical.name, judged=False)
    if kept or pending:
        db.log(run_id, "stage3", f"{vertical.name}: resuming with {len(kept)} kept and "
               f"{len(pending)} unjudged companies staged by an earlier run")
        for company in kept:
            company["vertical"] = vertical.name
            company.setdefault("_tier", "vertical")
        chunks = [pending[i:i + ENFORCE_BATCH] for i in range(0, len(pending), ENFORCE_BATCH)]
        with ThreadPoolExecutor(max_workers=ENFORCE_WORKERS) as pool:
            judged = [(c, pool.submit(_enforce_chunk, vertical, c)) for c in chunks]
        for chunk, fut in judged:
            verdicts = fut.result()                          # VendorOutage propagates
            db.record_verdicts(vertical.name, verdicts)
            for company in chunk:
                decision, reason = verdicts.get(company["domain"],
                                                ("DROP", "no verdict returned"))
                if decision in ("KEEP", "IDPR"):
                    company["vertical"] = vertical.name
                    company["_tier"] = "vertical" if decision == "KEEP" else "idpr"
                    kept.append(company)
                else:
                    drops.append({"domain": company["domain"], "reason": reason})
    scanned = blocked = kw_filtered = kw_passthrough = 0
    available = 0
    fetched_total = 0
    widened = 0
    round_kept = round_judged = 0   # verdicts since the last widening
    failed_chunks = 0
    seen_domains: set[str] = {c["domain"] for c in kept} | {d["domain"] for d in drops}
    def _buzzlead_step() -> None:
        nonlocal fetched_total, blocked
        # BuzzLead first. AI Ark's list-head is what IDPR already emailed; DiscoLike
        # lookalikes and keyword TAM behind the BuzzLead API are how we get off it.
        if len(kept) < need_kept:
            try:
                bl_fresh, bl_contacts, bl_stats = _from_buzzlead(
                    vertical, need_kept - len(kept), run_id, seen_domains)
            except Exception as exc:
                bl_fresh, bl_contacts, bl_stats = [], [], {}
                db.log(run_id, "stage3", f"{vertical.name}: BuzzLead sourcing failed "
                       f"({exc}); falling back to AI Ark", level="warn")
            if not bl_fresh:
                # A zero from the PRIMARY source used to be silent: the log line sat inside
                # the `if bl_fresh` below, so a run that fell straight through to AI Ark
                # looked identical to one BuzzLead had filled. ProductEVO's 2026-09-20 run
                # did exactly that, and the fallback it dropped to is both the list IDPR has
                # already emailed and the account that ran out of credit the next morning.
                db.log(run_id, "stage3",
                       f"{vertical.name}: BuzzLead returned NOTHING "
                       f"({bl_stats.get('skipped') or bl_stats or 'no reason given'}); "
                       f"falling back to AI Ark, whose list-head is already-emailed",
                       level="warn")
            if bl_fresh:
                db.stage_companies(vertical.name, run_id, bl_fresh, source="buzzlead")
                fetched_total += len(bl_fresh)
                blocked += int(bl_stats.get("blocked_by_dedup") or 0)
                db.log(run_id, "stage3",
                       f"{vertical.name}: BuzzLead returned {len(bl_fresh)} fresh companies "
                       f"({bl_stats.get('contacts', 0)} contacts prefetched)", **{
                           k: v for k, v in bl_stats.items() if k not in {"tam", "lookalike"}
                       })
                chunks = [bl_fresh[i:i + ENFORCE_BATCH]
                          for i in range(0, len(bl_fresh), ENFORCE_BATCH)]
                with ThreadPoolExecutor(max_workers=ENFORCE_WORKERS) as pool:
                    futures = [(chunk, pool.submit(_enforce_chunk, vertical, chunk))
                               for chunk in chunks]
                for chunk, fut in futures:
                    try:
                        verdicts = fut.result()
                        db.record_verdicts(vertical.name, verdicts)
                    except Exception as exc:
                        db.log(run_id, "stage3",
                               f"BuzzLead enforcement chunk left for resume: {exc}",
                               level="warn")
                        continue
                    for company in chunk:
                        decision, reason = verdicts.get(company["domain"],
                                                        ("DROP", "no verdict returned"))
                        if decision in ("KEEP", "IDPR"):
                            company["vertical"] = vertical.name
                            company["_tier"] = "vertical" if decision == "KEEP" else "idpr"
                            kept.append(company)
                            seen_domains.add(company["domain"])
                        else:
                            drops.append({"domain": company["domain"], "reason": reason})
                # Prefetched people ride along so Stage 4 does not re-find these companies.
                by_domain = {}
                for p in bl_contacts or []:
                    d = db.normalize_domain(p.get("company_domain") or "")
                    if d:
                        by_domain.setdefault(d, []).append(p)
                for company in kept:
                    extra = by_domain.get(company["domain"])
                    if extra:
                        company["_contacts"] = extra
            elif bl_stats.get("skipped"):
                db.log(run_id, "stage3", f"{vertical.name}: BuzzLead skipped "
                       f"({bl_stats['skipped']}); using AI Ark")

    def _qe_lo_step() -> None:
        nonlocal fetched_total, blocked
        # QuickEnrich + Lead Ocean company-finder. Onboarded 2026-09-25 so this stage draws
        # on more than BuzzLead + AI Ark -- both proved live in the ProductEVO sourcing
        # test the same day. QuickEnrich is available to every client (same unlimited-plan
        # account contacts already use); Lead Ocean stays ProductEVO-only, same reasoning
        # as finders.py's find_leadocean() -- unproven at this app's volume, and its 1 req/s
        # account-wide rate limit is a real ceiling other clients haven't opted into.
        def _qe_companies():
            return quickenrich.find_companies(industries=vertical.industries,
                                              employee_min=vertical.employee_min,
                                              employee_max=vertical.employee_max,
                                              limit=max(need_kept - len(kept), 0) * 3)

        def _lo_companies():
            if not finders_uses_leadocean():
                return []
            return leadocean.find_companies(industries=vertical.industries,
                                            employee_min=vertical.employee_min,
                                            employee_max=vertical.employee_max,
                                            limit=max(need_kept - len(kept), 0) * 3)

        for source_name, fetch_fn in (("quickenrich", _qe_companies), ("leadocean", _lo_companies)):
            if len(kept) >= need_kept:
                break
            try:
                raw = fetch_fn()
            except Exception as exc:
                db.log(run_id, "stage3", f"{vertical.name}: {source_name} sourcing failed "
                       f"({exc})", level="warn")
                continue
            fresh = []
            for c in raw or []:
                domain = db.normalize_domain(c.get("domain") or "")
                if not domain or domain in seen_domains:
                    continue
                c["domain"] = domain
                c["_source_query"] = source_name
                c["_keyword_hit"] = _keyword_hit(c, vertical.keywords)
                fresh.append(c)
                seen_domains.add(domain)
            if not fresh:
                db.log(run_id, "stage3", f"{vertical.name}: {source_name} returned nothing new")
                continue
            blocked_here = db.blocked_domains(c["domain"] for c in fresh)
            fresh = [c for c in fresh if c["domain"] not in blocked_here]
            if not fresh:
                continue
            db.stage_companies(vertical.name, run_id, fresh, source=source_name)
            fetched_total += len(fresh)
            blocked += len(blocked_here)
            db.log(run_id, "stage3", f"{vertical.name}: {source_name} returned "
                   f"{len(fresh)} fresh companies")
            chunks = [fresh[i:i + ENFORCE_BATCH] for i in range(0, len(fresh), ENFORCE_BATCH)]
            with ThreadPoolExecutor(max_workers=ENFORCE_WORKERS) as pool:
                futures = [(chunk, pool.submit(_enforce_chunk, vertical, chunk)) for chunk in chunks]
            for chunk, fut in futures:
                try:
                    verdicts = fut.result()
                    db.record_verdicts(vertical.name, verdicts)
                except Exception as exc:
                    db.log(run_id, "stage3",
                           f"{source_name} enforcement chunk left for resume: {exc}", level="warn")
                    continue
                for company in chunk:
                    decision, reason = verdicts.get(company["domain"],
                                                    ("DROP", "no verdict returned"))
                    if decision in ("KEEP", "IDPR"):
                        company["vertical"] = vertical.name
                        company["_tier"] = "vertical" if decision == "KEEP" else "idpr"
                        kept.append(company)
                    else:
                        drops.append({"domain": company["domain"], "reason": reason})

    # ProductEVO order (Troy, 2026-09-27): QuickEnrich -> Lead Ocean -> BuzzLead -> AI Ark.
    # Measured keep rates: Lead Ocean 14.2%, QuickEnrich 9.7%, AI Ark 7.5%, BuzzLead 6.4%.
    # Every other client keeps BuzzLead first.
    if finders_uses_leadocean():
        _qe_lo_step()
        _buzzlead_step()
    else:
        _buzzlead_step()
        _qe_lo_step()

    stage_started = time.time()
    round_fresh = 0                  # fresh companies fetched since the last widening
    # Why the loop below stopped, so a caller can tell "genuinely no more real supply"
    # from "chose not to keep paying for a bad match" or "ran out of THIS pass's clock".
    # Troy, 2026-09-25: Venture Capital and Product Design were both marked exhausted
    # forever after their OWN labels scored a poor keep rate on a small sample --
    # 2% and 9%, on 1,500 of 16,606 and 17,693 available -- and stopped without ever
    # widening (widen=False on a scale-up, correctly, so a sister vertical never
    # contaminates the winner's own signal). That is a label-precision problem, not
    # proof the other 80%+ of the market does not exist, and treating it as exhaustion
    # permanently locked out a vertical that had barely been looked at.
    stop_reason = "target_reached"   # the default, happy-path exit of the while loop
    # The fresh-company ceiling scales with the sizing above. 9,000 was enough at 1.2
    # contacts per company; at a measured 0.2 it would stop a vertical at a third of
    # the companies it needs, silently, and read as "source exhausted". The ceiling is
    # now the larger of the fixed cap and what the sizing calls for (with headroom
    # for the enforcement keep rate), under an absolute cap so a run cannot run away.
    fresh_cap = max(settings.max_fresh_per_vertical,
                    min(int(need_kept / max(ASSUMED_KEEP_FOR_CAP, 0.1) * 1.25),
                        settings.max_fresh_absolute))
    if fresh_cap != settings.max_fresh_per_vertical:
        db.log(run_id, "stage3", f"{vertical.name}: fresh-company ceiling raised to "
               f"{fresh_cap:,} to fit the sizing (fixed cap {settings.max_fresh_per_vertical:,})")
    while len(kept) < need_kept and fetched_total < fresh_cap:
        if time.time() - stage_started > STAGE3_TIME_BUDGET_S:
            stop_reason = "stage3_time_budget"
            db.log(run_id, "stage3", f"{vertical.name}: sourcing time budget "
                   f"({STAGE3_TIME_BUDGET_S // 60} min) reached with {len(kept)} kept; "
                   f"moving on", level="warn")
            break
        want = min(settings.company_cap_per_vertical,
                   fresh_cap - fetched_total)
        if widened:
            want = min(want, max(SIBLING_FRESH_CAP - round_fresh, 0))
            if want <= 0:
                bad = industries[-1]
                industries = industries[:-1]
                db.log(run_id, "stage3", f"{vertical.name}: sister {bad!r} reached its "
                       f"{SIBLING_FRESH_CAP}-company cap with {len(kept)}/{need_kept} kept")
                round_fresh = 0
                round_kept = round_judged = 0
                if widened >= MAX_WIDENINGS:
                    db.log(run_id, "stage3", f"{vertical.name}: sister limit "
                           f"({MAX_WIDENINGS}) reached; stopping at {len(kept)} kept",
                           level="warn")
                    break
                want = min(settings.company_cap_per_vertical,
                           fresh_cap - fetched_total)
                # ...but never fetch far more than this run actually needs. The cap is
                # sized for a full 3,000-contact vertical; a sample needs 66 companies
                # and was still fetching and PAYING THE GATE TO JUDGE 1,500 of them
                # (Troy, 2026-09-22: "sample copy should not take that long"). Three
                # times the shortfall covers a poor keep rate and stops there.
                still_need = max(need_kept - len(kept), 0)
                if still_need:
                    want = min(want, max(still_need * 3, 100))
        still_need = max(need_kept - len(kept), 0)
        if still_need:
            want = min(want, max(still_need * 3, 100))
        fresh, fstats = _fetch_fresh(vertical, industries, want, run_id,
                                     exclude=seen_domains)
        fresh = [c for c in fresh if c["domain"] not in seen_domains]
        seen_domains.update(c["domain"] for c in fresh)
        # Bank the fetch before the gate sees it: fetching is free, the gate is not
        # always available.
        db.stage_companies(vertical.name, run_id, fresh, source="aiark")
        scanned += fstats["scanned"]
        blocked += fstats["blocked_by_dedup"]
        kw_filtered += fstats["keyword_filtered"]
        kw_passthrough += fstats.get("keyword_passthrough", 0)
        available = max(available, fstats["available"] or 0)
        fetched_total += len(fresh)
        round_fresh += len(fresh)
        if not fresh and fstats.get("vendor_error"):
            # Every query this round failed outright (402/401/5xx) -- AI Ark itself is
            # down or out of credit, not a market that's genuinely been searched and
            # found empty. Widening would just retry the same dead vendor. Stop short
            # of "source_exhausted" so scale_vertical leaves this vertical partial
            # instead of permanently marking it exhausted.
            db.log(run_id, "stage3", f"{vertical.name}: stopping at {fetched_total} "
                   f"fresh -- AI Ark is unavailable (every query this round errored), "
                   f"not genuinely out of companies", level="warn")
            stop_reason = "vendor_unavailable"
            break
        if not fresh:
            # Out of companies but short of target: WIDEN rather than quit. Ask the
            # supply map for sibling labels that belong to this vertical and keep going.
            # Starts tight and widens only as far as needed -- the opposite of Proptech
            # (started at 47,865 and drowned) and Beauty (started at 834 and starved).
            if widen and widened < MAX_WIDENINGS and len(kept) < need_kept:
                extra = taxonomy.siblings_for(vertical.name, vertical.rationale,
                                              industries + list(vertical.naics or []),
                                              want=1)
                extra = [t for t in extra if t not in industries]
                if extra:
                    widened += 1
                    round_fresh = 0
                    round_kept = round_judged = 0
                    industries = industries + extra
                    db.log(run_id, "stage3", f"{vertical.name}: exhausted at "
                           f"{fetched_total} fresh with {len(kept)}/{need_kept} kept; "
                           f"widening into {extra}")
                    continue
                db.log(run_id, "stage3", f"{vertical.name}: exhausted at {fetched_total} "
                       f"fresh with {len(kept)}/{need_kept} kept; supply map offered no "
                       f"sibling label to widen into", level="warn")
            db.log(run_id, "stage3", f"{vertical.name}: source exhausted after "
                   f"{fetched_total} fresh companies", level="warn")
            stop_reason = "source_exhausted"
            break

        # A sibling label that is not keeping is not worth paging to the cap. Biotech,
        # 2026-09-13: its own labels kept 43%; the "therapeutics" sibling it widened
        # into kept ~1% across 4,800 companies before the fresh cap stopped it. After a
        # fair look (WIDEN_PROBE judged), a round keeping under WIDEN_MIN_KEEP is dropped
        # from the search and the next sibling is asked for instead.
        if (not widened and round_judged >= AUDIENCE_PROBE
                and round_kept / round_judged < WIDEN_MIN_KEEP):
            db.log(run_id, "stage3",
                   f"{vertical.name}: its own labels {industries + list(vertical.naics or [])} "
                   f"kept only {round_kept}/{round_judged} "
                   f"({round_kept / round_judged:.0%}); this audience is not what the "
                   f"label holds, so stopping rather than paying to reject the rest",
                   level="warn")
            stop_reason = "label_keep_rate_too_low"
            break
        if widened and round_judged >= WIDEN_PROBE and round_kept / round_judged < WIDEN_MIN_KEEP:
            bad = industries[-1]
            industries = industries[:-1]
            db.log(run_id, "stage3", f"{vertical.name}: sibling {bad!r} kept "
                   f"{round_kept}/{round_judged} ({round_kept / round_judged:.0%}); abandoning it",
                   level="warn")
            round_kept = round_judged = 0
            fresh = []          # force the widening branch below to ask for the next one
        db.log(run_id, "stage3",
               f"{vertical.name}: scanned {fstats['scanned']} of {fstats['available']}, "
               f"{fstats['keyword_filtered']} keyword-filtered, "
               f"{fstats['blocked_by_dedup']} already emailed, "
               f"{len(fresh)} fresh to enforce (have {len(kept)}/{need_kept} kept)")

        # Judge chunks four at a time. Chunks are independent, and the LLM client's own
        # semaphore already caps in-flight gateway calls, so this turns a 190-call
        # sequential walk (~16 minutes for 3,797 companies) into ~4. Troy, 2026-09-13:
        # "why does judgement take 1.5 hours?"
        chunks = [fresh[i:i + ENFORCE_BATCH] for i in range(0, len(fresh), ENFORCE_BATCH)]
        db.log(run_id, "stage3", f"{vertical.name}: fit-checking {len(fresh):,} companies "
               f"in {len(chunks)} chunks")
        with ThreadPoolExecutor(max_workers=ENFORCE_WORKERS) as pool:
            futures = [(chunk, pool.submit(_enforce_chunk, vertical, chunk)) for chunk in chunks]
        for chunk, fut in futures:
            try:
                verdicts = fut.result()
                db.record_verdicts(vertical.name, verdicts)
                round_judged += len(chunk)
                round_kept += sum(1 for d, _ in verdicts.values() if d.upper() in ("KEEP", "IDPR"))
            except VendorOutage:
                # Out of credit or bad key. Dropping the chunk and carrying on would
                # turn every remaining company into a DROP and record the vertical as
                # exhausted -- on 2026-09-11 Concentrate ran dry mid-run and that is
                # exactly what started to happen. Surface it; the runner re-queues.
                raise
            except Exception as exc:
                # NOT a drop. A chunk the gate never judged is unjudged, not rejected --
                # its staged rows keep verdict NULL and resume() judges them later. On
                # 2026-09-13, 141 chunks failed this way and counting them as drops would
                # have retired 2,820 companies that were never looked at.
                failed_chunks += 1
                db.log(run_id, "stage3",
                       f"enforcement chunk not judged, left for resume ({len(chunk)}): {exc}",
                       level="warn")
                continue
            for company in chunk:
                decision, reason = verdicts.get(company["domain"],
                                                ("DROP", "no verdict returned"))
                if decision in ("KEEP", "IDPR"):
                    company["vertical"] = vertical.name
                    company["_tier"] = "vertical" if decision == "KEEP" else "idpr"
                    kept.append(company)
                else:
                    drops.append({"domain": company["domain"], "reason": reason})

        # Nothing survived a whole batch: another pass would spend the same for the same
        # nothing, so stop rather than grinding through the ceiling.
        if not kept:
            db.log(run_id, "stage3", f"{vertical.name}: no company survived enforcement "
                   f"in {fetched_total} fresh; stopping", level="warn")
            stop_reason = "label_keep_rate_too_low"
            break
    else:
        # The while condition itself went false, not a break -- distinguish hitting the
        # hard per-vertical ceiling (a real, if generous, cap) from genuinely reaching
        # the target.
        if len(kept) < need_kept:
            stop_reason = "max_fresh_cap"

    # Vertical-fit companies lead; IDPR-fit follow. Downstream stages take companies in
    # list order and cap on contacts, so the second tier is only reached when the first
    # falls short of the target -- which is exactly its purpose.
    if failed_chunks:
        db.log(run_id, "stage3", f"{vertical.name}: {failed_chunks} chunks "
               f"({failed_chunks * ENFORCE_BATCH} companies) left unjudged for resume",
               level="warn")
    # Never-emailed companies go to the finders first. Troy, 2026-09-14: "only when
    # we've exhausted never emailed can we add [companies] not within 90 days." Tier is
    # still the outer key -- a vertical-fit company we last emailed 100 days ago belongs
    # in this campaign's story more than an IDPR-fit company we have never touched, and
    # the IDPR tier is only reached when vertical-fit runs short anyway. So the order is
    # vertical-never, vertical-touched, idpr-never, idpr-touched. Because stage 4 walks
    # this list in order and stops at the contact cap, the touched companies are simply
    # never reached when the fresh ones can fill the batch.
    try:
        touched = db.ever_contacted_domains(c["domain"] for c in kept)
    except Exception:
        touched = set()
    for c in kept:
        c["_touched"] = c["domain"] in touched
    # ProQ / MasterNow rank previously-emailed firms by age (120+, then 90+, then 60+)
    # instead of the plain touched flag. age_rank is None for every other client, which
    # keeps their order exactly as it was.
    from .. import consultant_gate

    def _history_key(c: dict) -> int:
        rank = consultant_gate.age_rank(c["domain"])
        return rank if rank is not None else (1 if c.get("_touched") else 0)

    kept.sort(key=lambda c: (0 if c.get("_tier", "vertical") == "vertical" else 1,
                             _history_key(c)))
    db.log(run_id, "stage3", f"{vertical.name}: {len(kept) - len(touched & {c['domain'] for c in kept})} "
           f"never-emailed companies first, {len(touched & {c['domain'] for c in kept})} previously emailed behind them")
    # Hand the finders only what the target needs. Everything above it stays banked as
    # surplus for a top-up. Troy, 2026-09-13: "we pull till we get the number of
    # companies that could give us 3k contacts and store the rest." Judging can overshoot
    # -- a resumed vertical judges its whole staged backlog in one pass -- and without
    # this medical device would have sent ~3,700 companies through the finders when
    # ~1,300 already yield 3,000 contacts at the measured 2.77 candidates per company.
    if len(kept) > need_kept:
        db.log(run_id, "stage3", f"{vertical.name}: {len(kept)} kept, sending the top "
               f"{need_kept} to the finders and banking {len(kept) - need_kept} as surplus")
        kept = kept[:need_kept]
    stats = {
        "companies_kept_idpr_tier": sum(1 for c in kept if c.get("_tier") == "idpr"),
        "companies_found": scanned,
        "companies_dropped_dedup": blocked,
        "companies_dropped_enforcement": len(drops),
        "companies_kept": len(kept),
        "keyword_filtered": kw_filtered,
        "keyword_passthrough": kw_passthrough,
        "available_in_aiark": available,
        "needed_for_target": need_kept,
        "drop_reasons_sample": drops[:10],
        "stop_reason": stop_reason,
    }
    db.log(run_id, "stage3",
           f"{vertical.name}: kept {len(kept)} of {need_kept} needed for "
           f"{settings.target_contacts} contacts",
           **{k: v for k, v in stats.items() if not k.endswith("_sample")})
    return kept, stats
