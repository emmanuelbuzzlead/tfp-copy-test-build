"""Contact finding: QuickEnrich -> AI.ARK -> Apollo, in that order.

Troy's chain, 2026-09-09. Finding is ours; VERIFYING is the vendored email_waterfall
(MillionVerifier -> TryKit -> BounceBan). Keeping the two halves separate matters: the
waterfall also ships its own finders (TryKit, LeadMagic, IcyPeas, Hunter), and if we let
it source contacts we would silently be running a different chain than the agreed one.
So we hand it an email and ask it to verify, not to find.

The cascade is per company and stops as soon as it has enough candidates, because each
step costs credits and the first step usually succeeds:

  1. QuickEnrich   -- dataset-search by company + title. Cheapest, unlimited plan.
  2. AI.ARK        -- people-search in the filter shape the BuzzLead API docs describe
                      (job_titles / seniority), with its own tight -> recovery -> broad
                      tiering. Used where QuickEnrich came back thin.
  3. Apollo        -- last resort. NOTE: as of 2026-09-09 the Apollo account reports
                      "insufficient credits" on every endpoint, so this step self-skips
                      and logs rather than failing the company.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import client, db
from .clients import quickenrich
from .clients.quickenrich import Contact
from .config import settings

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
# aiark_search.py imports its helpers flatly ("from cost_tracker import ..."), so the
# vendor directory itself has to be importable, not just the package above it.
sys.path.insert(0, str(_ROOT / "vendor"))

# Seniority values AI.ARK understands, mapped from the IDPR title list's intent.
AIARK_SENIORITY = ["cxo", "vp", "director", "head", "owner", "partner"]


def _dedup_key(c: Contact) -> str:
    return (c.email or c.linkedin_url or c.full_name or "").strip().lower()


def _merge(into: dict[str, Contact], found: list[Contact]) -> None:
    for c in found:
        key = _dedup_key(c)
        if key and key not in into:
            into[key] = c


# ---------------------------------------------------------------------------
# step 1 -- QuickEnrich
# ---------------------------------------------------------------------------

def find_quickenrich(domain: str, titles: list[str], limit: int) -> list[Contact]:
    try:
        found = quickenrich.find_people(domain, titles, limit=limit)
    except Exception:
        # find_people raises only when EVERY title errored, i.e. the integration is
        # broken rather than the company being empty. Let the cascade continue to
        # AI.ARK rather than losing the company.
        return []
    # Troy, 2026-09-15: "quick enrich should be US contacts." The row carries the
    # company's country code (no person-level location exists in this source).
    return [c for c in found if quickenrich.is_us(c)]


# ---------------------------------------------------------------------------
# step 2 -- AI.ARK people search
# ---------------------------------------------------------------------------

def find_aiark(domain: str, titles: list[str], limit: int,
               recovery_titles: list[str] | None = None) -> list[Contact]:
    """AI.ARK people-search, in the docs' filter shape.

    search_people() already implements the tight -> recovery -> broad tiering the
    BuzzLead docs describe, so the title list is passed as job_titles with a recovery
    tier rather than being looped one title at a time.
    """
    if not settings.aiark_api_key:
        return []

    # Do NOT let AI.ARK run its bulk email-finder. It is an async job polled for up to
    # 300 seconds PER COMPANY (AIARK_EMAIL_FINDER_MAX_POLL_SEC), and measured on this
    # workspace it was the single slowest thing in the pipeline: ~1-2 minutes a company,
    # about 12 hours for the ~750 companies a vertical now yields, frequently ending in
    # "poll deadline reached" having found nothing.
    #
    # It is also redundant. Finding and verifying the address is the waterfall's job
    # (MillionVerifier -> TryKit -> BounceBan), and it does it from a name and a domain
    # in seconds. AI.ARK's own client says as much where it handles the finder being
    # down: "the waterfall will source these emails." So we take the PEOPLE from AI.ARK
    # and let the waterfall do the email work.
    os.environ.setdefault("AIARK_EMAIL_FINDER_MAX_POLL_SEC", "1")
    os.environ.setdefault("AIARK_EMAIL_FINDER_INITIAL_POLL_DELAY_SEC", "0")

    from vendor.aiark_search import search_people

    global _aiark_dead
    if _aiark_dead:
        _last_aiark_skipped[0] = True
        return []
    _last_aiark_skipped[0] = False
    try:
        result = search_people(
            domains=[domain],
            api_key=settings.aiark_api_key,
            job_titles=titles,
            job_titles_recovery=recovery_titles or None,
            seniority=AIARK_SENIORITY,
            locations=["United States"],
            limit=limit,
            min_results_per_domain=2,
        )
    except Exception as exc:
        # An account with no credit (402) or no entitlement (401) says so on every
        # call and every call is a paid round trip. Until 2026-09-30 this returned []
        # and the company read as "nobody there" -- indistinguishable from a real
        # empty result, so nothing ever came back to it once credit was restored.
        msg = str(exc).lower()
        outage = "402" in msg or "401" in msg or "credit" in msg or "unauthorized" in msg
        if outage:
            _aiark_dead = True
            _last_aiark_skipped[0] = True
        db.record_vendor_usage("aiark", "people_search", 1,
                               status="402" if outage else "error")
        return []

    contacts = result[0] if isinstance(result, tuple) else result
    db.record_vendor_usage("aiark", "people_search", 1, status="ok")
    db.record_vendor_usage("aiark", "people_returned", len(contacts or []), status="ok")
    # NOTE 2026-09-10: /v1/people/search returns 401 on this plan while /v1/companies
    # returns 200 with the same key -- a second entitlement boundary, like the keyword
    # filter. So this step contributes nothing today and the cascade rests on
    # QuickEnrich alone (Apollo is out of credit). It is left wired because the fix is
    # an account change, not a code change.
    out: list[Contact] = []
    for row in contacts or []:
        if not isinstance(row, dict):
            continue
        email = (row.get("email") or "").strip().lower() or None
        first = row.get("first_name") or ""
        last = row.get("last_name") or ""
        if not (first or last):
            full = (row.get("full_name") or row.get("name") or "").strip()
            first, last = (full.split(" ")[0], " ".join(full.split(" ")[1:])) if full else ("", "")
        out.append(Contact(
            first_name=str(first).strip(),
            last_name=str(last).strip(),
            email=email,
            title=row.get("title") or row.get("job_title"),
            linkedin_url=(row.get("linkedin_url") or row.get("personal_linkedin_url")
                          or row.get("Person LinkedIn") or row.get("LinkedIn")),
            company=row.get("company_name") or row.get("Company Name") or row.get("Org"),
            company_domain=domain,
        ))
    return out


# ---------------------------------------------------------------------------
# step 3 -- Apollo
# ---------------------------------------------------------------------------

class ApolloUnavailable(RuntimeError):
    pass


def find_apollo(domain: str, titles: list[str], limit: int) -> list[Contact]:
    """Apollo people search.

    Fixed 2026-09-25: the old one-call `mixed_people/search` (email inline in the
    response) is permanently dead -- HTTP 422 "deprecated for API callers" on every
    request regardless of account credit, confirmed with a funded key. The
    "insufficient credits" 422 from 2026-09-09 was a real, separate outage that has
    since cleared; the circuit breaker in find_contacts() had already learned to
    treat "deprecated" the same as "no credits" (skip and move on), which is why
    this was silently contributing zero contacts rather than erroring loudly.

    The real flow now is two calls:
      1. POST /v1/mixed_people/api_search -- same filters, but the reply has no
         email field, names partially obfuscated.
      2. POST /v1/people/match with {"id": <id from step 1>, "reveal_personal_emails":
         true} -- returns the real email. Costs one credit per call, hit or miss.
    """
    import httpx

    if not settings.apollo_api_key:
        raise ApolloUnavailable("APOLLO_API_KEY not set")
    headers = {"Content-Type": "application/json", "Cache-Control": "no-cache",
              "X-Api-Key": settings.apollo_api_key}
    try:
        r = httpx.post(
            "https://api.apollo.io/v1/mixed_people/api_search",
            headers=headers,
            json={"q_organization_domains": domain, "person_titles": titles[:25],
                  "page": 1, "per_page": min(limit, 25)},
            timeout=90,
        )
    except Exception as exc:
        raise ApolloUnavailable(str(exc)[:120]) from exc

    if r.status_code == 422 and "insufficient credits" in r.text.lower():
        raise ApolloUnavailable("apollo account has no credits")
    if r.status_code == 422 and "deprecated" in r.text.lower():
        raise ApolloUnavailable("apollo api_search endpoint itself deprecated -- check docs.apollo.io")
    if r.status_code >= 400:
        raise ApolloUnavailable(f"HTTP {r.status_code}: {r.text[:100]}")

    people = (r.json() or {}).get("people") or []
    out: list[Contact] = []
    for p in people[:limit]:
        pid = p.get("id")
        if not pid:
            continue
        try:
            mr = httpx.post("https://api.apollo.io/v1/people/match", headers=headers,
                            json={"id": pid, "reveal_personal_emails": True}, timeout=30)
        except Exception:
            continue
        # The search step is free and keeps working on an account with no credits; the
        # reveal is what costs one. Swallowing this made Apollo look healthy while it
        # returned nobody for 360 companies straight (2026-09-27).
        if mr.status_code == 422 and "insufficient credits" in mr.text.lower():
            raise ApolloUnavailable("apollo account has no credits (email reveal)")
        if mr.status_code >= 400:
            continue
        person = (mr.json() or {}).get("person") or {}
        email = (person.get("email") or "").strip().lower()
        if not email or person.get("email_status") not in ("verified", "guessed"):
            continue
        out.append(Contact(
            first_name=person.get("first_name") or p.get("first_name") or "",
            last_name=person.get("last_name") or "",
            email=email,
            title=person.get("title") or p.get("title"),
            linkedin_url=person.get("linkedin_url"),
            company=(p.get("organization") or {}).get("name"),
            company_domain=domain,
        ))
    return out


# ---------------------------------------------------------------------------
# Lead Ocean -- ProductEVO only (see find_contacts()). Onboarded 2026-09-25;
# provides one thing nothing else in this cascade can: a direct "people at this
# exact known domain" lookup (companies/{domain}/people), no discovery job needed.
# HARD RATE LIMIT: 1 request/second, account-wide -- the module-level limiter below
# MUST be respected by every caller, not just this one, or their API 429s outright.
# ---------------------------------------------------------------------------

import threading
import time as _time

_LEADOCEAN_NEXT_AT = [0.0]
_LEADOCEAN_LOCK = threading.Lock()
_LEADOCEAN_INTERVAL_S = 1.1
_leadocean_table_ready = False


def _leadocean_rate_limit_wait() -> None:
    """One request per interval across EVERY process sharing this database.

    The limit is account-wide, and an in-process lock only paces one process: three
    verticals sourcing at once each believed they had the whole second, went 3x too fast
    together and were answered 429 (2026-09-27). The row below is the shared clock; the
    UPDATE takes its lock, so callers queue instead of colliding. Without a database the
    in-process clock still paces a single run.
    """
    global _leadocean_table_ready
    wait_for = None
    try:
        with db.cursor() as cur:
            if not _leadocean_table_ready:
                cur.execute("CREATE TABLE IF NOT EXISTS rate_limits "
                            "(name TEXT PRIMARY KEY, next_at TIMESTAMPTZ NOT NULL)")
                cur.execute("INSERT INTO rate_limits (name, next_at) VALUES ('leadocean', now()) "
                            "ON CONFLICT (name) DO NOTHING")
                _leadocean_table_ready = True
            cur.execute(
                "UPDATE rate_limits SET next_at = greatest(next_at, now()) + make_interval(secs => %s) "
                " WHERE name = 'leadocean' "
                "RETURNING extract(epoch FROM (next_at - make_interval(secs => %s) - now())) AS wait",
                (_LEADOCEAN_INTERVAL_S, _LEADOCEAN_INTERVAL_S))
            row = cur.fetchone()
            wait_for = float(row["wait"]) if row else 0.0
    except Exception:
        wait_for = None
    if wait_for is None:
        with _LEADOCEAN_LOCK:
            now = _time.monotonic()
            wait_for = _LEADOCEAN_NEXT_AT[0] - now
            _LEADOCEAN_NEXT_AT[0] = max(now, _LEADOCEAN_NEXT_AT[0]) + _LEADOCEAN_INTERVAL_S
    if wait_for > 0:
        _time.sleep(min(wait_for, 120))


def _leadocean_get(url: str, params: dict, key: str):
    """GET with the shared pacing, and a 429 waited out rather than treated as a failure.

    Another project on the same account (IDPR and ProductEVO keep separate databases, so
    they cannot share the clock above) can still push us over the limit. Their answer
    says how long to wait, so wait and ask again.
    """
    import httpx
    last = None
    for attempt in range(5):
        _leadocean_rate_limit_wait()
        try:
            r = httpx.get(url, params=params, headers={"x-api-key": key}, timeout=60)
        except Exception as exc:
            last = exc
            _time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 429:
            return r
        try:
            pause = float(r.headers.get("Retry-After") or 2)
        except ValueError:
            pause = 2.0
        _time.sleep(min(max(pause, 1.0), 30) + attempt)
    if last is not None:
        raise LeadOceanUnavailable(str(last)[:120])
    raise LeadOceanUnavailable("rate limited on five consecutive attempts")


_LO_BATCH = 10            # companies per search request
_lo_cache: dict[str, list[dict]] = {}
_qe_cache: dict[str, list] = {}
_cache_lock = threading.Lock()
_GOOD_EMAIL_STATUS = {"verified", "catch_all", "valid", "deliverable"}


def uses_leadocean() -> bool:
    """Per client, from clients/<name>/client.toml (`use_leadocean = true`)."""
    try:
        return bool(getattr(client.profile(), "use_leadocean", False))
    except Exception:
        return False


LEADOCEAN_MAX_TITLES = 50


def leadocean_prefetch(domains: list[str], titles: list[str],
                       run_id: str | None = None) -> int | None:
    """People at MANY companies in one request each, title-matched by Lead Ocean itself.

    One request per company was the design, and the limit is per request: ten companies
    took ten seconds of quota before a single email was looked up. Their search takes a
    list of company domains and a list of titles, so ten companies cost one request
    (measured 2026-09-27: 10 companies, 41 matched people, 0.3s). Results are cached per
    domain for find_leadocean(); a domain with nobody is cached as empty so it is not
    asked about again.

    Returns None (not 0) when skipped for lack of a key -- distinct from a genuine zero
    matches, so the caller does not log "N people fetched in M requests" for a call that
    never actually reached Lead Ocean. 2026-09-29: exactly this collapse hid a missing
    LEADOCEAN_API_KEY on IDPR's Railway project for two days -- the log read as Lead
    Ocean coming back empty on nearly every company, when no request had gone out at all.
    """
    key = os.getenv("LEADOCEAN_API_KEY", "")
    todo = [d for d in dict.fromkeys(domains) if d and "@" not in d and "." in d
            and d.lower() != "null" and d not in _lo_cache]
    if not key:
        return None
    if not todo:
        return 0
    fetched = 0
    for i in range(0, len(todo), _LO_BATCH):
        chunk = todo[i:i + _LO_BATCH]
        rows: list[dict] = []
        cursor = None
        try:
            for _page in range(3):
                # Lead Ocean validates `title` at 50 entries max (HTTP 400 "Array must
                # contain at most 50 element(s)", seen on every batch 2026-10-01 once the
                # key was finally in place). The ladder is priority-ordered, so the
                # first 50 are the ones that matter.
                params = {"domain": ",".join(chunk),
                          "title": ",".join(titles[:LEADOCEAN_MAX_TITLES]), "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                r = _leadocean_get("https://api.leadocean.io/v1/people/search", params, key)
                db.record_vendor_usage("leadocean", "people_search", 1,
                                       status="ok" if r.status_code < 400 else str(r.status_code),
                                       run_id=run_id)
                if r.status_code >= 400:
                    raise LeadOceanUnavailable(f"HTTP {r.status_code}: {r.text[:150]}")
                body = r.json() or {}
                db.record_vendor_usage("leadocean", "people_returned",
                                       len(body.get("data") or []), status="ok", run_id=run_id)
                rows.extend(body.get("data") or [])
                cursor = (body.get("meta") or {}).get("nextCursor")
                if not cursor:
                    break
        except LeadOceanUnavailable as exc:
            if run_id:
                db.log(run_id, "finders", f"leadocean batch of {len(chunk)} skipped: {exc}",
                       level="warn")
            continue          # these domains fall back to the one-company lookup
        grouped: dict[str, list[dict]] = {d: [] for d in chunk}
        for row in rows:
            for exp in ((row.get("contact_data") or {}).get("contact_current_experiences") or []):
                d = db.normalize_domain(exp.get("company_domain") or "")
                if d in grouped:
                    grouped[d].append(row)
                    break
        with _cache_lock:
            _lo_cache.update(grouped)
        fetched += len(rows)
    return fetched


def prefetch_slice(domains: list[str], titles: list[str], want: int,
                   run_id: str | None = None) -> None:
    """QuickEnrich first for the whole slice, then Lead Ocean in batches for the companies
    QuickEnrich left short. Lead Ocean bills per person returned, so it is only asked
    about companies that still need people."""
    if not uses_leadocean():
        return
    from concurrent.futures import ThreadPoolExecutor

    def _qe(d: str):
        try:
            return d, find_quickenrich(d, titles, want)
        except Exception:
            return d, None
    with ThreadPoolExecutor(max_workers=settings.finder_workers) as pool:
        for d, found in pool.map(_qe, [d for d in domains if d not in _qe_cache]):
            if found is not None:
                with _cache_lock:
                    _qe_cache[d] = found
    short = [d for d in domains if len(_qe_cache.get(d) or []) < want]
    n = leadocean_prefetch(short, titles, run_id)
    if run_id:
        if n is None:
            db.log(run_id, "finders", f"leadocean: skipped for {len(short)} companies short "
                   f"after QuickEnrich -- LEADOCEAN_API_KEY not set", level="warn")
        else:
            db.log(run_id, "finders", f"leadocean: {len(short)} of {len(domains)} companies "
                   f"were short after QuickEnrich; {n} people fetched in "
                   f"{-(-len(short) // _LO_BATCH)} requests")


class LeadOceanUnavailable(RuntimeError):
    pass


def find_leadocean(domain: str, titles: list[str], limit: int) -> list[Contact]:
    """Domain-scoped people lookup. Their own email door (/v2/people/email/work)
    only returns an address THEY checked as verified/catch_all -- data:null for
    everyone else, which is common at small companies. Rather than discard a real
    person with no confirmed address, generate the standard name+domain
    permutations and let this app's own email_waterfall verify them, same as any
    other candidate -- see the pipeline this was proven in
    (productevo_pipeline/contacts.py) for the exact pattern.
    """
    import httpx
    import re

    key = os.getenv("LEADOCEAN_API_KEY", "")
    if not key:
        raise LeadOceanUnavailable("LEADOCEAN_API_KEY not set")
    if not domain or "@" in domain or "." not in domain or domain.lower() == "null":
        return []          # junk in the website field; not a lookup worth a request
    with _cache_lock:
        cached = _lo_cache.get(domain)
    titles_lower = [t.lower() for t in titles]
    if cached is not None:
        rows = cached                       # already title-matched by their search
        titles_lower = []
    else:
        r = _leadocean_get(f"https://api.leadocean.io/v1/companies/{domain}/people",
                           {"limit": 10}, key)
        if r.status_code == 404:
            return []
        if r.status_code >= 400:
            raise LeadOceanUnavailable(f"HTTP {r.status_code}: {r.text[:150]}")
        rows = (r.json() or {}).get("data") or []

    def _has_good_email(row: dict) -> bool:
        c = row.get("contact_data") or {}
        if "has_email" not in c:
            return True                     # the one-company door does not publish flags
        return bool(c.get("has_email")) and str(c.get("email_status") or "").lower() in _GOOD_EMAIL_STATUS

    # People whose address Lead Ocean holds go first: theirs is confirmed, a guess is not.
    rows = sorted(rows, key=lambda row: 0 if _has_good_email(row) else 1)
    out: list[Contact] = []
    for row in rows:
        if len(out) >= limit:
            break
        prof = row.get("profile_data") or {}
        contact = row.get("contact_data") or {}
        exp = (contact.get("contact_current_experiences") or [{}])[0]
        title = (exp.get("job_title") or "")
        if titles_lower and not any(t in title.lower() for t in titles_lower):
            continue
        first, last = prof.get("profile_first_name") or "", prof.get("profile_last_name") or ""
        person_id = ((row.get("meta") or {}).get("source_ids") or {}).get("person_id")
        email = None
        # Only ask for an address they say they hold. Asking for every person cost a
        # request each, up to eleven per company, and came back empty for most.
        if person_id and _has_good_email(row):
            try:
                er = _leadocean_get("https://api.leadocean.io/v2/people/email/work",
                                    {"person_id": person_id}, key)
                if er.status_code < 400:
                    data = (er.json() or {}).get("data")
                    if data:
                        email = data.get("email") or data.get("value")
            except Exception:
                pass
        if not email and first:
            f = re.sub(r"[^a-z]", "", first.lower())
            l = re.sub(r"[^a-z]", "", last.lower())
            if f:
                # Best single guess, not all 6 patterns -- the verification
                # waterfall downstream is what actually confirms it; sending it
                # several guesses per person here would just spend more of its
                # credits per candidate for no extra information.
                email = f"{f}.{l}@{domain}" if l else f"{f}@{domain}"
        if not email:
            continue
        out.append(Contact(first_name=first, last_name=last, email=email, title=title,
                           linkedin_url=prof.get("profile_url"), company=None,
                           company_domain=domain))
    return out


# ---------------------------------------------------------------------------
# the cascade
# ---------------------------------------------------------------------------

_apollo_dead = False   # set the first time Apollo reports no credits / no key
_aiark_dead = False    # set the first time AI Ark's people search reports no credit
_last_aiark_skipped = [False]   # per-call: did find_aiark skip for outage this time
_leadocean_dead = False   # set the first time LEADOCEAN_API_KEY is found missing


def _from_prefetched(rows: list | None, domain: str) -> list[Contact]:
    """Contacts the BuzzLead pipeline already sourced for this company."""
    out: list[Contact] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        first = (row.get("first_name") or "").strip()
        last = (row.get("last_name") or "").strip()
        email = (row.get("email") or "").strip().lower() or None
        if not (first or last or email):
            continue
        out.append(Contact(
            first_name=first, last_name=last, email=email,
            title=row.get("title"),
            linkedin_url=row.get("linkedin_url"),
            company=row.get("company_name"),
            company_domain=domain,
        ))
    return out


def find_contacts(domain: str, titles: list[str], want: int, run_id: str | None = None,
                  recovery_titles: list[str] | None = None,
                  prefetched: list | None = None) -> tuple[list[Contact], dict]:
    """Run the cascade until `want` candidates are collected. Returns (contacts, stats).

    `want` is deliberately larger than the number we will keep: the title ladder needs
    choices to rank, and the verification step drops some of whatever it is given.
    Prefetched rows (BuzzLead pipeline artifacts) are used first so we do not spend
    a second finder chain on people we already paid to source.
    """
    global _apollo_dead
    found: dict[str, Contact] = {}
    stats = {"qe": 0, "aiark": 0, "apollo": 0, "apollo_skipped": 0, "buzzlead": 0,
             "leadocean": 0, "leadocean_skipped": 0}

    _merge(found, _from_prefetched(prefetched, domain))
    stats["buzzlead"] = len(found)
    if len(found) >= want:
        return list(found.values())[:want], stats

    with _cache_lock:
        qe_rows = _qe_cache.pop(domain, None)
    _merge(found, qe_rows if qe_rows is not None else find_quickenrich(domain, titles, want))
    stats["qe"] = len(found)
    if len(found) >= want:
        return list(found.values())[:want], stats

    # ProductEVO's cascade is QuickEnrich -> Lead Ocean -> Apollo -> AI Ark (Troy, 2026-09-25,
    # restated 2026-09-27): AI Ark last, to protect a scarce/paid account regardless of
    # its own keep rate. Troy, 2026-09-27: "yes" to the same policy for IDPR -- same
    # AI-Ark-last order, minus Lead Ocean, which IDPR has not opted into. Every OTHER
    # client (not IDPR, not ProductEVO) keeps the original QuickEnrich -> AI Ark -> Apollo.
    # "productevo" here means "this client has Lead Ocean switched on" (client.toml,
    # use_leadocean). It began as a ProductEVO-only step, hence the name.
    productevo = uses_leadocean()
    idpr = client.profile().name == "idpr"

    def _aiark() -> None:
        before = len(found)
        _merge(found, find_aiark(domain, titles, want - len(found), recovery_titles))
        stats["aiark"] = len(found) - before
        if _last_aiark_skipped[0]:
            stats["aiark_skipped"] = 1
            stats.setdefault("missed_vendors", []).append("aiark")

    def _leadocean() -> None:
        global _leadocean_dead
        if _leadocean_dead:
            stats["leadocean_skipped"] = 1
            stats.setdefault("missed_vendors", []).append("leadocean")
            return
        before = len(found)
        try:
            _merge(found, find_leadocean(domain, titles, want - len(found)))
            stats["leadocean"] = len(found) - before
        except LeadOceanUnavailable as exc:
            stats["leadocean_skipped"] = 1
            stats.setdefault("missed_vendors", []).append("leadocean")
            if "not set" in str(exc).lower():
                _leadocean_dead = True
            if run_id:
                db.log(run_id, "finders", f"leadocean skipped for {domain}: {exc}", level="warn")

    if productevo:
        _leadocean()
    elif not idpr:
        _aiark()
    if len(found) >= want:
        return list(found.values())[:want], stats

    before = len(found)
    # Circuit breaker. An account with no credits says so on every call, and every
    # call is a round trip: measured 2026-09-13, ~2 seconds per company, times 3,197
    # companies, for nothing. After the first such answer Apollo is skipped for the
    # rest of the process, and the run log gets one line instead of thousands.
    #
    # Troy, 2026-09-24, asking how to speed up verification: Apollo's endpoint changed
    # -- "This endpoint is deprecated for API callers. Please use the new
    # mixed_people/api_search" -- and that message matches neither "credit" nor
    # "not set", so the breaker never tripped on it. Measured on one live run: 323
    # guaranteed-to-fail calls and counting, one per company that reached this step,
    # for zero contacts. A deprecated endpoint is exactly as permanent as no credits --
    # it will not fix itself mid-run -- so it trips the same breaker.
    if _apollo_dead:
        stats["apollo_skipped"] = 1
        stats.setdefault("missed_vendors", []).append("apollo")
    else:
        try:
            _merge(found, find_apollo(domain, titles, want - len(found)))
            stats["apollo"] = len(found) - before
        except ApolloUnavailable as exc:
            stats["apollo_skipped"] = 1
            stats.setdefault("missed_vendors", []).append("apollo")
            msg = str(exc).lower()
            if "credit" in msg or "not set" in msg or "deprecated" in msg:
                _apollo_dead = True
                if run_id:
                    db.log(run_id, "finders", f"apollo unavailable ({exc}); skipping it "
                           f"for the rest of this run", level="warn")
            elif run_id:
                db.log(run_id, "finders", f"apollo skipped for {domain}: {exc}", level="warn")

    if len(found) >= want:
        return list(found.values())[:want], stats

    # Lead Ocean is ProductEVO ONLY (1 req/s account-wide; no other client has opted in),
    # so for ProductEVO the last step is AI Ark. IDPR has no Lead Ocean step but gets
    # the same AI-Ark-last treatment; every other client already tried AI Ark earlier
    # (above) and has no further step here.
    if productevo or idpr:
        _aiark()

    return list(found.values())[:want], stats
