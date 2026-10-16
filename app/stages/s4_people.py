"""Stage 4 -- People + deliverable emails.

Up to CONTACTS_PER_COMPANY (default 4) people per company, chosen down the title
priority ladder. Multiple contacts per company is only safe because company-level
stop-on-reply is in place -- see app/stages/stop_on_reply.py -- so the first reply from
anyone at a company pulls their colleagues out of the sequence.

The do-not-contact list is enforced here as a HARD BLOCK, twice: on the company before
any Quick Enrich spend, and again on each resolved email address, because a verified
address can sit on a different domain than the company record (parent company, rebrand)
and a client reached through the back door is still a client.

Title list is IDPR's, verbatim from the spec. Two rules from the spec shape selection:

1. Size rule: under 50 employees prefer CEO/Founder/Owner; 50+ prefer CMO/VP
   Marketing/Head of Marketing, then comms/PR, then generic exec.

2. Every title on IDPR's list is selectable, generic VPs included (Troy, 2026-09-10:
   VPs of Sales, Communications and IT are in the ICP). There is no department
   blacklist. What the ladder does is ORDER them, so a company with a CMO and a VP of
   Sales yields the CMO first and the VP of Sales only if four slots remain. Support
   staff -- assistants, coordinators -- are the one hard exclusion, since an executive
   assistant is never the buyer.
"""

from __future__ import annotations

import time as _time

import re
from concurrent.futures import ThreadPoolExecutor

from .. import db, dnc, finders, mx, reuse
from ..clients import quickenrich, waterfall
from ..config import settings

FALLBACK_TITLES = [
    "Founder", "Co-Founder", "CEO", "President", "Vice President", "Managing Director",
    "Owner", "CMO", "Chief Marketing Officer", "VP of Marketing", "VP Marketing",
    "Vice President of Marketing", "Head of Marketing", "Director of Marketing",
    "Senior Marketing Manager", "Brand Director", "Brand Marketing Manager",
    "Product Marketing Director", "Chief Communications Officer", "VP of Communications",
    "Head of Communications", "Director of Communications",
    "Corporate Communications Manager", "Public Relations Manager",
    "Media Relations Manager", "Strategic Communications Director",
    "Head of Brand Communications", "Brand & Communications Director",
    "Marketing Communications Manager", "VP of Public Relations",
    "Public Relations Director", "Head of Public Relations", "VP",
    "Director of Digital", "VP of Digital", "Head of Digital",
]

def _titles() -> list[str]:
    """The active client's buyer titles, best first."""
    from .. import client
    return list(client.profile().titles) or FALLBACK_TITLES


TITLES = _titles()


def _titles_are_declared() -> bool:
    """True when the client wrote its OWN title list, not the generic fallback.

    Distinct from EXCLUDED_PERSONAS below on purpose -- see _declared_rank().
    """
    from .. import client
    try:
        return bool(client.profile().titles)
    except Exception:
        return False


TITLES_ARE_DECLARED = _titles_are_declared()


# Search order: the specific, high-value titles first so the per-title query budget is
# spent where it pays. The bare titles trail at the end.
COMPANY_SLICE = 150  # companies found+verified+banked together; the loop stops at the cap

MARKETING_FIRST = [t for t in TITLES if t not in {"VP", "President", "Vice President"}]
BARE_TITLES = ["VP", "President", "Vice President"]


def _excluded_personas() -> tuple[str, ...]:
    from .. import client
    try:
        return client.profile().excluded_personas()
    except Exception:
        return ()


EXCLUDED_PERSONAS = _excluded_personas()
# A bare "VP" matches every vice president there is, which is the point of a recovery
# net and also how a client that BANS Sales ends up holding a VP of Sales. The net stays;
# what comes back through it is filtered against the client's own excluded personas.
# The words a function actually appears under. "CMO" contains no "marketing" and
# "Chief Revenue Officer" no "sales", so a ban written in plain English missed the two
# most senior people it was meant to exclude.
# Widened 2026-09-22, when the broad contact query started surfacing titles the narrow
# title-walk never could. It kept a "Senior Accountant" for a client that bans Finance,
# because the list had "accounting" but not "accountant". A wider net finds more edge
# titles, so the ban has to know more of the words a function really goes by.
PERSONA_WORDS = {
    "marketing": ["marketing", "cmo", "brand", "demand gen", "growth", "seo",
                  "content", "social media", "communications", "public relations",
                  "advertising", "campaign"],
    "sales": ["sales", "revenue", "cro", "business development", "bizdev",
              "account executive", "account manager", "sdr", "bdr", "partnerships",
              "channel"],
    "finance": ["finance", "financial", "cfo", "controller", "treasurer",
                "accounting", "accountant", "bookkeeper", "bookkeeping",
                "payroll", "audit", "auditor",
                "tax", "billing", "accounts payable", "accounts receivable"],
    "hr": ["hr", "human resources", "people", "talent", "chro", "recruiting",
           "recruiter", "benefits", "compensation", "culture"],
    "legal": ["legal", "counsel", "compliance officer", "general counsel",
              "paralegal", "attorney", "contracts manager"],
}
_persona_terms: list[str] = []
for _p in EXCLUDED_PERSONAS:
    _persona_terms += PERSONA_WORDS.get(_p, [_p])
PERSONA_RX = (re.compile(r"\b(" + "|".join(re.escape(w) for w in _persona_terms)
                         + r")\b", re.IGNORECASE) if _persona_terms else None)


def persona_banned(title: str | None) -> bool:
    """True when this title is a function the client said never to contact."""
    if not PERSONA_RX or not title:
        return False
    return bool(PERSONA_RX.search(title))

MARKETING_RX = re.compile(r"\b(cmo|marketing|brand|growth|demand gen)\b", re.IGNORECASE)
COMMS_RX = re.compile(r"\b(communications?|public relations|pr|media relations|press)\b",
                      re.IGNORECASE)
# The company's actual head. "president" is NOT here -- "Vice President of Global Sales"
# contains it, and treating that as a company head put it above every marketing title.
# A standalone President is handled separately below.
EXEC_RX = re.compile(r"\b(ceo|chief executive|founder|co-?founder|owner|"
                     r"managing director|managing partner)\b", re.IGNORECASE)
PRESIDENT_RX = re.compile(r"\bpresident\b", re.IGNORECASE)
VICE_RX = re.compile(r"\b(vice[\s-]?president|vp|svp|evp|avp)\b", re.IGNORECASE)
DIGITAL_RX = re.compile(r"\bdigital\b", re.IGNORECASE)

# Support staff only. Troy, 2026-09-10: VPs of Sales, Communications and IT ARE in the
# IDPR ICP, so there is no department blacklist -- the title list is the inclusion list
# and a VP of anything is a legitimate target, just a lower-priority one than a
# marketing or comms leader. This overrides the original spec line "NEVER selects VP of
# Sales/Engineering/Finance-type titles even as fallback".
# An assistant or coordinator is still never the buyer, at any seniority.
ASSISTANT_RX = re.compile(r"\b(assistant|secretary|intern|coordinator)\b", re.IGNORECASE)
BARE_RX = re.compile(r"^\s*(vp|vice president|president)\s*$", re.IGNORECASE)


# Entries too generic to rank on. A client's list may contain a bare "VP" as a recovery
# term, and matching on it would rank "VP of Sales" as though the client had asked for
# it by name -- which is exactly what it did to IDPR on the first attempt at this,
# lifting a title their ladder deliberately puts last from 20 to 68.
_FILLER = {"of", "the", "and", "for", "to", "a", "at", "in"}
_UNRANKABLE = {"vp", "svp", "evp", "avp", "president", "vice president", "director",
               "manager", "head", "lead", "chief", "officer", "owner"}


_HARD_SUPPORT_RX = re.compile(r"\b(assistant|secretary|intern)\b", re.IGNORECASE)


def _include_wins() -> bool:
    from .. import client as _c
    try:
        return bool(_c.profile().include_wins)
    except Exception:
        return False


def _contains_declared_title(title: str) -> bool:
    """True when every word of some declared buyer title appears in this title (word SET, filler words ignored)."""
    if not TITLES_ARE_DECLARED:
        return False
    t_words = set(re.findall(r"[a-z&]+", title.lower())) - _FILLER
    for declared in TITLES:
        d_words = set(re.findall(r"[a-z&]+", declared.lower())) - _FILLER
        if d_words and d_words <= t_words:
            return True
    return False



def _declared_rank(title: str) -> int | None:
    """Where this title sits in the client's own list, or None if it is not on it.

    Only for a client that has declared its OWN title list: writing down 35 deliberate
    titles is already a statement that they have thought about their buyer, and that
    list should outrank a generic ladder built for somebody else. This used to also
    require a SEPARATE excluded-personas list before it would trust the client's own
    titles, which meant IDPR's real list sat unused and CFOs, engineers and lab techs
    fell through to the generic ladder's catch-all score of 15. Troy, 2026-09-25: "I
    gave you job titles and I expect people with at least matching job titles" -- the
    title list is the authorization on its own, no separate exclusion list required.

    Matched on the longest declared title contained in the real one, at word boundaries,
    so "VP of Operations, Americas" matches "VP of Operations" and not "Operations".
    """
    if not TITLES_ARE_DECLARED:
        return None
    # Word SET, not word order. A client writes "Product Director" and the world writes
    # "Director of Product"; both name the same person, and an exact-phrase match scored
    # the commoner spelling of a declared buyer as an unknown (15 against 77).
    t_words = set(re.findall(r"[a-z&]+", title.lower())) - _FILLER
    best = None
    for i, declared in enumerate(TITLES):
        d_words = set(re.findall(r"[a-z&]+", declared.lower())) - _FILLER
        if not d_words:
            continue
        extra = t_words - d_words
        # _UNRANKABLE only blocks using a bare/generic declared entry to pull a MORE
        # SPECIFIC title up to its rank (extra is non-empty: "VP" must not rank "VP of
        # Sales" as though the client named it). A title that matches the bare entry
        # EXACTLY -- "President" the input against "President" the declared title, extra
        # empty -- is that entry, and skipping it here dropped "President" and "Owner"
        # to the flat exact-match fallback (60) instead of their own place in the list,
        # which is why every real company head scored differently EXCEPT those two.
        if declared.lower().strip() in _UNRANKABLE and extra:
            continue
        if d_words <= t_words and (best is None or len(d_words) > best[1]):
            best = (i, len(d_words), len(extra))
    if best is None:
        return None
    # A title that says MORE than the declared one ranks just below it. "VP of
    # Operations" is the declared buyer; "VP of Clinical Operations" is still an
    # operations VP and still selectable, but the client asked for the first by name.
    return min(best[0] + 2 * best[2], 60)


# The ROLE a declared title is about, so a match is on the job and not the wording.
# Troy, 2026-09-22: "make job titles match but not word for word." The strict filter
# demanded the client's exact string, so "Vice President of Operations" failed against
# "VP of Operations" and six companies yielded 24 people and ZERO usable contacts.
# Every synonym here still has to clear the banned-persona check first, so widening the
# match cannot let Sales, Marketing, Finance, HR or Legal back in.
ROLE_WORDS = {
    "operations": ["operations", "operating", "ops", "coo", "plant", "facility"],
    "product":    ["product", "r&d", "research and development", "innovation",
                   "design", "engineering", "npd", "new product"],
    "supply":     ["supply chain", "supply", "sourcing", "procurement", "purchasing",
                   "logistics", "materials", "vendor", "import"],
    "production": ["production", "manufacturing", "factory", "fabrication", "assembly"],
    "head":       ["founder", "co-founder", "owner", "president", "ceo",
                   "chief executive", "managing director", "managing partner",
                   "general manager", "gm", "proprietor", "principal"],
}
SENIOR_WORDS = ("chief", "head", "director", "vp", "vice president", "svp", "evp",
                "manager", "lead", "principal", "senior", "owner", "founder",
                "president", "ceo", "coo", "partner", "gm", "general manager",
                "supervisor", "specialist", "engineer", "coordinator")

# Words specific enough to PROVE a client's own title list wants this role family --
# narrower than ROLE_WORDS above on purpose. ROLE_WORDS is used to test a CANDIDATE's
# title once a family is already open; this is used to decide whether to open it at all.
# "product" alone is not here: it is also half of "Product Marketing Director", a
# marketing title, and IDPR's declared list has no R&D or engineering title anywhere in
# it. Blob-matching the bare word "product" against the whole list opened the "product"
# (R&D/engineering) family for a PR firm and let Software Engineer, Design Engineer and
# Product Manager through as "similar" to a marketing director. Troy, 2026-09-25.
ROLE_TRIGGER_WORDS = {
    "operations": ["operations", "operating", "coo", "plant", "facility"],
    "product":    ["r&d", "research and development", "innovation", "engineering",
                   "npd", "new product"],
    "supply":     ["supply chain", "supply", "sourcing", "procurement", "purchasing",
                   "logistics", "materials", "vendor", "import"],
    "production": ["production", "manufacturing", "factory", "fabrication", "assembly"],
    "head":       ROLE_WORDS["head"],
}


def _declared_roles() -> set[str]:
    """Which role families this client's own title list actually covers."""
    out = set()
    blob = " ".join(TITLES).lower()
    for role, words in ROLE_TRIGGER_WORDS.items():
        if any(w in blob for w in words):
            out.add(role)
    return out


DECLARED_ROLES = _declared_roles()


# "design" is a PRODUCT word and also a CREATIVE one. Measured 2026-09-22 on 1,421 banked
# contacts: role matching let through "Senior Graphic and Visual Designer", "Lead Graphic
# Designer" and "Video Production Manager" -- six in six hundred, all people who never buy
# manufacturing. A founder who happens to carry a creative title still counts, so this
# only strikes when the title has no ownership word.
_CREATIVE_RX = re.compile(
    r"\b(graphic|visual|web|ux|ui|video|photo\w*|content|social media|copywrit\w*|"
    r"art director|creative director)\b", re.IGNORECASE)
_OWNER_RX = re.compile(
    r"\b(founder|co-?founder|owner|president|ceo|chief executive|partner|principal)\b",
    re.IGNORECASE)


def role_match(title: str) -> str | None:
    """The declared role family this title belongs to, if any.

    Requires BOTH a role word and a seniority word, so "Operations" alone does not
    qualify a Shipping and Receiving Clerk while "VP of Operations", "Director of
    Door Operations" and "Plant Manager" all do.
    """
    t = (title or "").lower()
    if not t:
        return None
    if _CREATIVE_RX.search(t) and not _OWNER_RX.search(t):
        return None
    for role in DECLARED_ROLES:
        if any(w in t for w in ROLE_WORDS[role]):
            if role == "head" or any(sw in t for sw in SENIOR_WORDS):
                return role
    return None


def _titles_strict() -> bool:
    from .. import client as _client
    return bool(_client.profile().titles_strict)


_STRICT_OWNER_RX = re.compile(r"\b(?:co-?)?owner\b", re.IGNORECASE)
_STRICT_FOUNDER_RX = re.compile(r"\b(?:co-?)?founder\b", re.IGNORECASE)
_STRICT_CEO_RX = re.compile(r"\b(?:ceo|chief executive(?: officer)?)\b", re.IGNORECASE)


def _strict_title_score(t: str) -> int:
    """Titles_strict clients (ProQ, MasterNow) want exactly: owner, founder, CEO / chief executive officer, then
    president, then a VP as a last resort. Nothing else is selectable. Words around the title do not matter
    ("Owner Sales", "President & ISO 9001 Small Business Expert" still count), but a different role does not
    count at all ("Operations & People Manager", "Managing Director", "Managing Partner")."""
    best = 0
    if _STRICT_OWNER_RX.search(t):
        best = 100
    if _STRICT_FOUNDER_RX.search(t):
        best = max(best, 96)
    if _STRICT_CEO_RX.search(t):
        best = max(best, 92)
    if best:
        return best
    if VICE_RX.search(t):
        return 20
    if PRESIDENT_RX.search(t):
        return 60
    return 0


def score_title(title: str | None, employees: int | None) -> int:
    """Higher is better. 0 means never select.

    The ladder, per the spec's size rule: under 50 employees the founder/CEO is the
    buyer; at 50+ it is the marketing or comms leader. Everything on IDPR's title list
    is selectable -- generic VPs included -- but ranked below the people who own the
    decision, so a company yields its best four rather than its first four.
    """
    t = (title or "").strip()
    if not t:
        return 0

    small = (employees or 0) < 50

    # Lumion spec 3.3, "include wins": a title that contains a declared buyer title is kept even when it
    # also carries an excluded word ("Owner / Lead Instructor"). Opt-in per client (client.toml include_wins).
    included = _include_wins() and _contains_declared_title(t)

    # Support staff are never the buyer, however senior the person they report to. "coordinator" is the one
    # exception for a client that DECLARED a coordinator title (Lumion: Enrollment / Admissions Coordinator).
    if ASSISTANT_RX.search(t) and not (included and not _HARD_SUPPORT_RX.search(t)):
        return 0

    # A function the client wrote down as never-contact is never the buyer either, and
    # this check has to come FIRST because the ladder below was built for IDPR, a PR
    # firm, and therefore ranks Marketing at the very top. Applied unchanged to
    # ProductEVO -- who ban Marketing, Sales, Finance, HR and Legal -- it actively
    # preferred the people they had forbidden: 6 of 20 contacts in the 2026-09-20 run
    # were banned personas, and a VP of Sales scored above a Director of Operations.
    if persona_banned(t) and not included:
        return 0

    if _titles_strict():
        return _strict_title_score(t)

    # The client's OWN title list, which its header calls "best first", ranks before
    # the generic ladder below. That ladder is IDPR's -- a PR firm, so it puts Marketing
    # at the top -- and applying it to a client whose buyer is a founder or a product
    # operator ranked a CMO above a Director of Operations. A client that declares its
    # buyers gets its buyers, in the order it wrote them down.
    pos = _declared_rank(t)
    if pos is not None:
        return 100 - pos            # first in the file scores highest

    # NOT ON THE LIST, for a client that declared one. The broad contact query returns
    # everyone at a company, and the ladder below would keep whoever ranked highest as
    # filler -- which is how Master Data Analyst, Freight Analyst, Shift Supervisor and
    # a Community Manager got copy written for them on 2026-09-22, and separately how
    # IDPR (no excluded-personas list, only a title list) got CFOs, engineers and lab
    # techs written for THEM on 2026-09-24/25 via the generic ladder's return-15 filler
    # below. A client that has written down its buyers has told us who is NOT one --
    # that does not also require a second, separate list of who to exclude.
    if TITLES_ARE_DECLARED or EXCLUDED_PERSONAS:
        # ...unless it IS a declared title that was merely too generic to RANK on.
        # _UNRANKABLE exists so a bare "VP" in the list cannot pull "VP of Sales" up
        # with it; it was never meant to exclude the word outright. "President" is the
        # FIRST entry on ProductEVO's list and this dropped it.
        t_words = set(re.findall(r"[a-z&]+", t.lower())) - _FILLER
        for declared in TITLES:
            d_words = set(re.findall(r"[a-z&]+", declared.lower())) - _FILLER
            if d_words and d_words == t_words:
                return 60
        # Same ROLE, different wording. Ranked below every exact declared title, so the
        # named buyers are still preferred and these only fill the gap.
        if not _titles_strict() and role_match(t):
            return 35
        # A bare "VP"/"President"/"Vice President" on the client's OWN list is a
        # deliberate recovery net, not a title to rank on -- see the comment above
        # PERSONA_WORDS. Honor the net here at the bottom of the ladder, exactly as the
        # generic ladder below already does for every other client: a VP or standalone
        # president of some OTHER function is in scope, just last-resort. Without this,
        # turning on strict list-matching for IDPR also silently dropped "VP of Sales"
        # to 0 -- the opposite of Troy's 2026-09-10 call that VPs of Sales, Communications
        # and IT ARE in the ICP; there is no department blacklist.
        bare = {d.lower().strip() for d in TITLES} & _UNRANKABLE
        if ("vp" in bare or "vice president" in bare) and (VICE_RX.search(t) or BARE_RX.match(t)):
            return 20
        if "president" in bare and PRESIDENT_RX.search(t) and not VICE_RX.search(t):
            return 20
        return 0

    if MARKETING_RX.search(t):
        return 90 if not small else 80
    if COMMS_RX.search(t):
        return 75
    if EXEC_RX.search(t):
        return 95 if small else 60
    # A standalone President runs the company; a Vice President does not.
    if PRESIDENT_RX.search(t) and not VICE_RX.search(t):
        return 95 if small else 60
    if DIGITAL_RX.search(t):
        return 55
    if BARE_RX.match(t):
        return 30
    # A VP of some other function -- Sales, IT, Operations. In the ICP per Troy, but
    # ranked last, so it is only reached when the company has nobody better.
    if VICE_RX.search(t):
        return 20
    return 15


def select_contact(candidates: list[quickenrich.Contact], employees: int | None
                   ) -> quickenrich.Contact | None:
    """The single best contact. Kept for callers that want exactly one."""
    picked = select_contacts(candidates, employees, limit=1)
    return picked[0] if picked else None


def select_contacts(candidates: list[quickenrich.Contact], employees: int | None,
                    limit: int | None = None) -> list[quickenrich.Contact]:
    """Up to `limit` people per company, best-first down the priority ladder.

    Multi-contact is safe only because company-level stop-on-reply is in place: the
    first reply from anyone at the company pulls their colleagues out of the sequence
    (see app/stages/stop_on_reply.py). Without that, four contacts per company would
    mean four people at the same firm forwarding each other the same pitch.

    Ordering is the ladder from score_title, so a 40-person company yields founder
    first and a 300-person company yields marketing first. Titles that score 0 --
    Sales, Engineering, Finance -- are never included, however few contacts remain.
    """
    limit = limit or settings.contacts_per_company
    scored = [(score_title(c.title, employees), c) for c in candidates]
    viable = [(sc, c) for sc, c in scored if sc > 0]
    if not viable:
        return []
    # At equal title score, take the person we have NEVER emailed. Troy, 2026-09-14:
    # "our 3000 contacts should prioritize never emailed contacts before emailed
    # contacts." Title score stays the outer key -- the ladder is what makes the
    # contact right for the campaign -- so this only decides between two people who
    # rank the same, which is where the choice is genuinely free.
    try:
        touched = db.ever_contacted_emails(c.email for _, c in viable if c.email)
    except Exception:
        touched = set()
    # Stable sort, so equal score AND equal history keep vendor order rather than
    # being reordered arbitrarily between runs.
    viable.sort(key=lambda pair: (-pair[0],
                                  1 if (pair[1].email or "").lower() in touched else 0))

    # Opt-in per client (client.toml max_vp_per_company): a client that wants owners and
    # founders first and only a few vice presidents. None for everyone else.
    try:
        from .. import client as _client
        vp_cap = _client.profile().max_vp_per_company
    except Exception:
        vp_cap = None
    vps = 0

    picked: list[quickenrich.Contact] = []
    seen: set[str] = set()
    for _, contact in viable:
        key = (contact.email or contact.linkedin_url or contact.full_name).lower()
        if key in seen:
            continue
        if vp_cap is not None and VICE_RX.search(contact.title or ""):
            if vps >= vp_cap:
                continue
            vps += 1
        seen.add(key)
        picked.append(contact)
        if len(picked) >= limit:
            break
    return picked


def _employees(company: dict) -> int | None:
    for key in ("employee_count", "staff_total", "employees", "size"):
        value = company.get(key)
        if isinstance(value, (int, float)) and value:
            return int(value)
    return None


def build(companies: list[dict], run_id: str,
          deadline: float | None = None) -> tuple[list[dict], dict]:
    """Find up to N people per company and verify their emails.

    Finding is the agreed cascade (QuickEnrich -> AI.ARK -> Apollo); verification is the
    vendored waterfall (MillionVerifier -> TryKit -> BounceBan). Hunter is no longer the
    gate: it could not resolve catch-all domains and was dropping every contact at firms
    whose mail server accepts anything.

    Order is chosen for cost: dedup and the do-not-contact list run BEFORE verification,
    because verification is the step that spends credits per address.
    """
    block = dnc.load()
    stats = {
        "contacts_found": 0,
        "contacts_dropped_dedup": 0,
        "contacts_dropped_dnc": 0,
        "contacts_no_email": 0,
        "contacts_catch_all": 0,
        "companies_no_contact": 0,
        "found_quickenrich": 0,
        "found_aiark": 0,
        "found_apollo": 0,
        "found_buzzlead": 0,
        "found_leadocean": 0,
        "apollo_skipped_companies": 0,
        "found_reused": 0,
        "companies_gateway_blocked": 0,
        # Troy, 2026-09-24: "there are discrepancies between the kept and rejected,
        # what happens to the rest?" Two real gaps in what got reported, not lost data.
        # `_one` computes both of these per company (fstats["no_first_name"],
        # fstats["dropped_seat_limit"]) but neither was ever rolled up into the run-level
        # totals, so a run's own summary could not answer the question it was asked.
        # no_first_name: found, but cannot be greeted, so never verified.
        "contacts_no_first_name": 0,
        # dropped_seat_limit: found, deduped, NAMED -- and still cut, because
        # candidates_per_company (8, how many to FIND) and contacts_per_company (4, how
        # many SEATS a company gets) are different numbers. select_contacts() trims to
        # the seat count BEFORE verification runs, so this is candidates that were never
        # going to be paid for regardless of how verification went.
        "contacts_dropped_seat_limit": 0,
    }
    def _one(company: dict) -> tuple[list[dict], dict, str | None]:
        """Find and rank contacts at one company. Returns (rows, finder stats, skip reason)."""
        domain = company["domain"]
        reason = block.blocks(domain=domain, name=company.get("name"))
        if reason:
            return [], {"qe": 0, "aiark": 0, "apollo": 0, "apollo_skipped": 0}, reason
        # Troy, 2026-09-15: nothing behind Barracuda or Mimecast goes to copy. One DNS
        # lookup here, before the finders and the paid verification (app/mx.py).
        reason = mx.blocked(domain)
        if reason:
            return [], {"qe": 0, "aiark": 0, "apollo": 0, "apollo_skipped": 0}, \
                f"{domain}: skipped, {reason}"

        # ALL the titles, not the first 12. QuickEnrich is an unlimited-credit plan, so
        # the truncation bought nothing and cost coverage: in the legal run 29 of 51
        # companies (57%) returned nobody at all while two thirds of the title list was
        # never queried. The cascade still stops as soon as it has enough candidates.
        candidates, fstats = finders.find_contacts(
            domain, MARKETING_FIRST + BARE_TITLES,
            want=settings.candidates_per_company, run_id=run_id,
            recovery_titles=BARE_TITLES,
            prefetched=company.get("_contacts"),
        )
        # Fill the seats. Medical device measured 2.05 candidates per company against
        # 4 seats -- Apollo had no credits for 412 of 583 companies -- and every empty
        # seat is a company we must find somewhere else. So the EmailBison reuse pool
        # tops up whenever the finders come back SHORT, not only when they come back
        # empty. It is a free call and the recency gate has already cleared the company.
        if len(candidates) < settings.candidates_per_company:
            have = {(c.email or "").lower() for c in candidates}
            extra = [c for c in reuse.find(domain, company.get("name"))
                     if (c.email or "").lower() not in have]
            fstats["reused"] = len(extra)
            candidates = candidates + extra
        # EmailBison requires a first name on every lead and the sequence greets by it.
        # 351 verified contacts were set aside at the push on 2026-09-14 for lacking
        # one -- paid verification for people we could never send to. Drop them here,
        # before the paid step.
        named = [c for c in candidates if (c.first_name or "").strip()]
        fstats["no_first_name"] = len(candidates) - len(named)
        candidates = named
        if not candidates:
            return [], fstats, None

        # Dedup BEFORE verification -- verification is the paid step.
        known = db.blocked_contacts(
            (c.email for c in candidates if c.email),
            (c.linkedin_url for c in candidates if c.linkedin_url),
        )
        fresh = [
            c for c in candidates
            if (c.email or "").lower() not in known
            and (db.normalize_linkedin(c.linkedin_url) or "") not in known
        ]
        fstats["found"] = len(candidates)
        fstats["deduped"] = len(candidates) - len(fresh)
        # Only the seats this company has left. A company we have emailed two people at
        # gets two more, not four -- otherwise re-entering a known company would push it
        # past the agreed per-company limit and put six people at one firm in sequence.
        used = db.company_seats_used([domain]).get(domain, 0)
        seats = max(settings.contacts_per_company - used, 0)
        if seats <= 0:
            fstats["dropped_seat_limit"] = len(fresh)
            return [], fstats, None
        pool_size = max(settings.verify_pool_per_company, seats)
        chosen = select_contacts(fresh, _employees(company), limit=pool_size) if fresh else []
        fstats["dropped_seat_limit"] = len(fresh) - len(chosen)
        rows = []
        for rank, c in enumerate(chosen):
            rows.append({
                "_rank": rank,
                "_seats": seats,
                "email": c.email,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "title": c.title,
                "linkedin_url": c.linkedin_url,
                "company_domain": domain,
                "company_name": company.get("name"),
                "company_description": company.get("description"),
                "employees": _employees(company),
                "vertical": company.get("vertical"),
            })
        return rows, fstats, None

    # Find -> verify -> bank, in SLICES of companies, and stop at the contact cap.
    # Companies arrive vertical-fit first, so the slices that matter most go first.
    # Finding for every company before verifying any was the old shape, and with
    # ~3,000 companies it meant an hour of finding before a single address was
    # banked -- and finding for ~1,700 companies the cap would never reach. Troy,
    # 2026-09-13: "safely expedite the process from company sourcing and verification
    # to contact sourcing". Each slice is banked the moment it clears, so a stop
    # between slices keeps everything verified so far.
    contacts: list[dict] = []
    total_shortlisted = 0
    with ThreadPoolExecutor(max_workers=settings.finder_workers) as pool:
        for start in range(0, len(companies), COMPANY_SLICE):
            # A DEADLINE, checked between slices. This loop is where a scale-up spends
            # its hours: 2026-09-23 took 90 minutes to walk 1,050 of 2,817 companies.
            # Left alone it runs until it finishes or the container dies -- and since
            # the review now refuses to start while another run is alive, one long
            # scale-up would block every following day's review. Each slice is already
            # banked as it clears, so stopping here keeps every contact verified so far
            # and the caller writes copy for them.
            if deadline is not None and _time.monotonic() >= deadline:
                db.log(run_id, "stage4",
                       f"time budget reached after {start} of {len(companies)} "
                       f"companies; {len(contacts)} verified contacts are banked and go "
                       f"to copy now. The rest of the audience keeps its place for the "
                       f"next run.", level="warn")
                break
            if len(contacts) >= settings.contact_cap_per_vertical:
                db.log(run_id, "stage4", f"contact cap reached with {len(contacts)} after "
                       f"{start} of {len(companies)} companies; the rest stay banked")
                break
            slice_companies = companies[start:start + COMPANY_SLICE]
            shortlist: list[dict] = []
            try:
                finders.prefetch_slice([c["domain"] for c in slice_companies],
                                       MARKETING_FIRST + BARE_TITLES,
                                       settings.candidates_per_company, run_id)
            except Exception as exc:
                db.log(run_id, "stage4", f"slice prefetch skipped: {exc}", level="warn")
            no_contact_domains: list[str] = []
            # per company: which vendors were OUT, and how many seats got filled
            misses: dict[str, tuple[list[str], int]] = {}
            for company, (rows, fstats, skip) in zip(
                    slice_companies, pool.map(_one, slice_companies)):
                if skip:
                    if "mail gateway" in skip:
                        stats["companies_gateway_blocked"] += 1
                    else:
                        stats["contacts_dropped_dnc"] += 1
                    db.log(run_id, "stage4", skip, level="warn")
                    continue
                stats["found_quickenrich"] += fstats.get("qe", 0)
                stats["found_aiark"] += fstats.get("aiark", 0)
                stats["found_apollo"] += fstats.get("apollo", 0)
                stats["found_buzzlead"] += fstats.get("buzzlead", 0)
                stats["found_leadocean"] += fstats.get("leadocean", 0)
                stats["apollo_skipped_companies"] += fstats.get("apollo_skipped", 0)
                stats["found_reused"] += fstats.get("reused", 0)
                stats["contacts_found"] += fstats.get("found", 0)
                stats["contacts_dropped_dedup"] += fstats.get("deduped", 0)
                stats["contacts_no_first_name"] += fstats.get("no_first_name", 0)
                stats["contacts_dropped_seat_limit"] += fstats.get("dropped_seat_limit", 0)
                missed = list(dict.fromkeys(fstats.get("missed_vendors") or []))
                if missed:
                    stats["companies_with_missed_vendor"] =                         stats.get("companies_with_missed_vendor", 0) + 1
                misses[company["domain"]] = (missed, len(rows))
                if not rows:
                    stats["companies_no_contact"] += 1
                    # NOT marked found. Troy, 2026-09-29: a company that came up
                    # empty while Apollo and AI Ark were both out of credit was
                    # being marked "searched" forever regardless -- so once their
                    # credit is restored, nothing ever asked that company again.
                    # Leaving found_at unset puts it straight back into
                    # unfound_kept(), the SAME surplus pool a top-up already draws
                    # from first, so a later pass retries it through whichever
                    # vendors are healthy BY THEN, no new tracking required. Scoped
                    # to genuine zero-contact companies, not a company that got
                    # some seats filled by one vendor but not all -- that partial
                    # case would need per-company vendor-availability tracking this
                    # does not have yet.
                    no_contact_domains.append(company["domain"])
                    continue
                shortlist.extend(rows)
            total_shortlisted += len(shortlist)
            # This slice has been through the finders; a resumed run must not redo
            # it -- except the zero-contact companies just held back above.
            try:
                searched = {c["domain"]: misses.get(c["domain"], ([], 0))
                            for c in slice_companies
                            if c["domain"] not in no_contact_domains}
                if searched:
                    db.mark_found_with_misses(slice_companies[0].get("vertical") or "",
                                              searched)
            except Exception as exc:
                db.log(run_id, "stage4", f"found marker not written: {exc}", level="warn")
            if not shortlist:
                continue

            # fit_before_verify (TFP, Emmanuel 2026-10-16): the website fit check runs on the companies that HAVE a contact, BEFORE any address is verified,
            # so verification credit is never spent on people at companies that will be rejected. Only about half of the companies yield a contact,
            # so this judges about half of what a fit-first order would.
            from .. import client as _client
            if _client.profile().fit_before_verify:
                from .. import company_fit
                names = {r["company_domain"]: r.get("company_name") or "" for r in shortlist}
                verdicts = company_fit.check([{"domain": d, "name": n} for d, n in names.items()], workers=12)
                passing = {d for d in names if (verdicts.get(d) or {}).get("fit") in company_fit.PASS}
                before = len(shortlist)
                shortlist = [r for r in shortlist if r["company_domain"] in passing]
                stats["contacts_dropped_fit"] = stats.get("contacts_dropped_fit", 0) + (before - len(shortlist))
                db.log(run_id, "stage4", f"fit before verify: {len(passing)} of {len(names)} companies with contacts passed; "
                       f"{before - len(shortlist)} contacts skipped before verification")
                if not shortlist:
                    continue

            verified = waterfall.verify_batch(shortlist, run_id=run_id)
            good: list[dict] = []
            for c in verified:
                if waterfall.sendable(c):
                    c["email"] = c["valid_email"]
                    good.append(c)
                    continue
                if c.get("email_status") == "catch_all":
                    stats["contacts_catch_all"] += 1
                else:
                    stats["contacts_no_email"] += 1
            # Re-run dedup on the VERIFIED address. Dedup above ran on the address the
            # finder guessed; the waterfall replaces it with the one that actually
            # resolves, and the two differ often enough to matter -- on 2026-09-14, 11
            # contacts reached the bank having been emailed as recently as 7 September,
            # one of them a week earlier. Verification is already paid for at this
            # point, so this costs one query and closes the window.
            if good:
                blocked = db.blocked_contacts((c["email"] for c in good), ())
                kept_good = [c for c in good if c["email"].lower() not in blocked]
                if len(kept_good) != len(good):
                    stats["contacts_dropped_dedup"] += len(good) - len(kept_good)
                    db.log(run_id, "stage4",
                           f"{len(good) - len(kept_good)} verified contacts dropped: the "
                           f"address the waterfall resolved is already in the recency index",
                           level="warn")
                good = kept_good
            # Seats are applied AFTER verification when a wider pool was verified: keep
            # the best-ranked people who passed, up to the company's remaining seats.
            by_company: dict[str, list[dict]] = {}
            for c in good:
                by_company.setdefault(c["company_domain"], []).append(c)
            seated: list[dict] = []
            for people in by_company.values():
                people.sort(key=lambda c: c.get("_rank", 0))
                keep = people[: people[0].get("_seats") or settings.contacts_per_company]
                stats["contacts_verified_over_seats"] = (
                    stats.get("contacts_verified_over_seats", 0) + len(people) - len(keep))
                seated.extend(keep)
            good = seated
            contacts.extend(good)
            try:
                db.reserve_contacts(good, run_id, None, status="verified")
            except Exception as exc:
                db.log(run_id, "stage4", f"slice not persisted: {exc}", level="warn")
            done = min(start + COMPANY_SLICE, len(companies))
            print(f"[stage4] {done}/{len(companies)} companies -> {total_shortlisted} candidates "
                  f"-> {len(contacts)} verified", flush=True)
            db.log(run_id, "stage4", f"{done}/{len(companies)} companies: "
                   f"{total_shortlisted} candidates, {len(contacts)} verified so far")

    if not contacts:
        db.log(run_id, "stage4", "no verified contacts", **stats)
        return [], stats

    contacts = contacts[: settings.contact_cap_per_vertical]
    stats["contacts_sendable_pre_copy"] = len(contacts)
    db.log(run_id, "stage4", f"built {len(contacts)} verified contacts", **stats)
    return contacts, stats
