"""Stage 2 -- Vertical expansion (LLM).

Adjacency here means similar BUYER BEHAVIOUR (funded, PR-hungry, founder-led,
credibility-dependent), not a similar industry name. That distinction is the whole
point of the stage and is stated explicitly in the prompt below.

Decision #7 lands here: the Series A+ funding floor applies to STARTUP verticals only.
Legal and professional-services verticals swap it for employee size plus a revenue
floor, because law firms are partnerships and essentially never take venture funding --
the original spec both required law firms in scope and applied a funding floor that
would have dropped every one of them at Stage 3.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..clients import llm

MODEL = "anthropic/claude-sonnet-5"
TEMPERATURE = 0.4

# Spec section 2, verbatim.
FALLBACK_ICP = """US-based, 11-500 employees.
Funding is a BUYER TRAIT to reason about, NOT a filter to apply. Troy, 2026-09-11:
"lose the funded, so that signal would not be a hindrance." Do not put "Funded" in a
vertical's name, do not add funding-based disqualifiers ("pre-seed", "bootstrapped",
"unfunded"), and set "funding_floor" to null unless the seed campaign itself was
explicitly a funding-signal campaign. AI Ark's funding data is sparse, so any funding
requirement mostly filters on whether a record happens to be populated. The old rule
was: Series A+ for startup/tech verticals; for LEGAL and PROFESSIONAL SERVICES it
never applied (partnerships do not raise venture rounds);
use employee size 11-500 plus a revenue floor instead.
Titles: CEO/Founder if <50 employees, CMO/VP Marketing if >=50."""

FALLBACK_EXCLUSIONS = """MSPs, VARs, IT consulting/services, government contractors, nonprofits,
staffing firms, non-US companies, pre-seed companies, MLM/direct-sales structures.
NOTE: law firms are IN scope and must never be excluded."""

def _icp() -> str:
    from .. import client
    return client.profile().icp or FALLBACK_ICP


def _exclusions() -> str:
    from .. import client
    return client.profile().exclusions or FALLBACK_EXCLUSIONS


PERFORMANCE_HISTORY = """Healthcare: top performer.
SaaS: strong. Funded Brands: strong.
CPG: weak."""

def _system() -> str:
    """Built per call, so the active client's ICP and exclusions are the ones used."""
    from .. import client
    p = client.profile()
    ICP, EXCLUSIONS = _icp(), _exclusions()
    return f"""You are a GTM strategist for {p.display_name}.
    
    You are given the verticals of campaigns that are currently WINNING on interested
    outcomes per delivered email. Propose adjacent verticals to expand into.
    
    ADJACENCY MEANS SIMILAR BUYER BEHAVIOUR, NOT SIMILAR INDUSTRY NAME.
    The buyer we want is: funded or growing, PR-hungry, often founder-led, and dependent on
    credibility to win deals. A company that looks nothing like the winner on paper but buys
    PR for the same reason is a better adjacent vertical than a near-identical industry whose
    buyers do not care about press.
    
    ICP LOCK (every proposal must satisfy this):
    {ICP}
    
    HARD EXCLUSIONS (never propose these, and list disqualifiers to enforce them):
    {EXCLUSIONS}
    
    KNOWN VERTICAL PERFORMANCE:
    {PERFORMANCE_HISTORY}
    
    NAICS. Also give 6-digit NAICS codes in "naics" -- they are a second, more precise
    taxonomy searched alongside the labels. SIX digits only: 541110 (Offices of Lawyers)
    works, the 4-digit prefix 5411 matches nothing. Give 1-3 codes that name the vertical's
    actual operators, not its suppliers.
    
    KEYWORDS. The "keywords" array is applied LOCALLY against each company's own keyword
    list, name and description, so it narrows a broad industry to the slice you mean
    ("neobank", "personal injury", "clinical trials"). Give 6-12 of them. They are free to
    use and they are what turns a broad label into a targeted vertical.
    
    Write them BROAD, and prefer ONE or TWO words. The filter keeps a company when a
    keyword appears as a phrase, or when a two-word keyword has both of its words present,
    or when a longer keyword has half its words present. Long phrases therefore cost you
    supply for nothing: "asset management boutique" and "point-of-care diagnostics testing"
    describe the same companies as "asset management" and "point-of-care" while matching
    far fewer of them. Include the plain everyday word a company would use about itself
    ("malpractice", "biotech", "wealth management") alongside the industry term, and skip
    filler words like services, solutions, firm or provider -- they are ignored anyway.
    
    But every keyword must DISTINGUISH this vertical from the wider industry it sits in.
    A keyword that every company in the parent industry would use about itself filters
    nothing. For "Funded Proptech & Real Estate Platforms" the words "real estate",
    "property" and "brokerage" describe every realtor and landlord in the taxonomy -- on
    2026-09-11 that list let 20 of 20 traditional brokerages through and enforcement then
    rejected 95% of a 4,554-company fetch. "proptech", "listing platform", "rent payments"
    and "closing software" separate the platforms from the brokerages; the generic words
    do not. Ask yourself for each keyword: would a company I am NOT targeting also use
    this word? If yes, drop it.
    
    INDUSTRY TERMS. The "industries" array is matched against a LinkedIn-style taxonomy and
    the terms are OR-ed together, so one broad term drowns a precise one. Use ONLY exact
    taxonomy labels, prefer the tight ones, and never pad with a broad sibling. Known-good
    tight labels: Fintech, Insurtech, SaaS, Law Practice, Biotechnology Research, Medical
    Equipment Manufacturing, Computer and Network Security, Real Estate, Wellness and
    Fitness Services, Financial Services (BROAD - avoid), Software Development (BROAD -
    avoid), Hospitals and Health Care (BROAD - avoid). Invented phrases like "Health Tech"
    or "Consumer Fintech" match nothing and will be discarded.
    
    Return STRICT JSON only, no prose, shaped:
    {{"verticals": [
      {{"name": "...",
    "rationale": "one or two sentences on the shared buyer behaviour",
    "industries": ["ai ark industry terms"],
    "keywords": ["ai ark keyword terms"],
    "naics": ["6-digit NAICS codes, e.g. 541110 for law offices"],
    "employee_min": 11, "employee_max": 500,
    "funding_floor": null (or "SERIES_A" ONLY for an explicit funding-signal seed),
    "revenue_floor_usd": null or a number (use this INSTEAD of funding_floor for
                          legal/professional-services verticals),
    "disqualifiers": ["specific things that mean DROP in the enforcement pass"]
      }}
    ]}}
    
    Propose between {settings.splits_per_seed_min} and {settings.splits_per_seed_max}
    sub-verticals FOR THE SINGLE SEED CAMPAIGN GIVEN. Each must be a distinct slice of that
    seed's buyer, not a restatement of it, and the slices should not overlap each other."""
    

@dataclass
class Vertical:
    name: str
    rationale: str
    industries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    employee_min: int = 11
    employee_max: int = 500
    funding_floor: str | None = None
    revenue_floor_usd: float | None = None
    disqualifiers: list[str] = field(default_factory=list)
    naics: list[str] = field(default_factory=list)
    seed_campaign: str = ""
    seed_industry: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> "Vertical":
        return cls(
            name=(raw.get("name") or "").strip(),
            rationale=(raw.get("rationale") or "").strip(),
            industries=[str(x) for x in (raw.get("industries") or [])],
            keywords=[str(x) for x in (raw.get("keywords") or [])],
            employee_min=int(raw.get("employee_min") or settings.employee_min),
            employee_max=int(raw.get("employee_max") or settings.employee_max),
            funding_floor=raw.get("funding_floor") or None,
            revenue_floor_usd=raw.get("revenue_floor_usd"),
            disqualifiers=[str(x) for x in (raw.get("disqualifiers") or [])],
            naics=[str(x).strip() for x in (raw.get("naics") or []) if str(x).strip()],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "rationale": self.rationale, "industries": self.industries,
            "keywords": self.keywords, "employee_min": self.employee_min,
            "employee_max": self.employee_max, "funding_floor": self.funding_floor,
            "revenue_floor_usd": self.revenue_floor_usd, "disqualifiers": self.disqualifiers,
            "seed_campaign": self.seed_campaign, "seed_industry": self.seed_industry,
            "naics": self.naics,
        }


def propose_for_seed(seed: dict, already_run: set[str],
                     outcomes: dict[str, dict] | None = None) -> list[Vertical]:
    """Ask the model for adjacent verticals, informed by what has already been tested.

    `outcomes` is the pipeline's memory (app/stages/s8_review.py): verticals already
    judged SCALE or DROP. Feeding it in is what stops the system proposing an avatar it
    has already proved does not resonate -- without it, a dud simply reappears once the
    90-day skip window lapses, having learned nothing from having run it.
    """
    outcomes = outcomes or {}
    winner_text = json.dumps(seed, indent=2, default=str)
    skip_text = ", ".join(sorted(already_run)) or "(none yet)"

    scaled = [v for v, o in outcomes.items() if o.get("verdict") == "scale"]
    dropped = [(v, o) for v, o in outcomes.items() if o.get("verdict") == "drop"]
    memory = ""
    if scaled:
        body = "\n".join(f"- {v}" for v in scaled)
        memory += ("\n\nVERTICALS THAT WORKED (these resonate -- propose ones that share "
                   "their buyer behaviour):\n" + body)
    if dropped:
        body = "\n".join(f"- {v}: {o.get('reason')}" for v, o in dropped)
        memory += ("\n\nVERTICAL-POLICY TESTS THAT FAILED (do not repeat the exact same "
                   "hypothesis; only revisit after the lookback window with a materially "
                   "different audience or copy rationale):\n" + body)
    # Supply-first. The menu is every taxonomy term measured to have 2,000-9,000
    # unworked companies. A vertical anchored on one of these is born with known
    # supply; one invented without reference to it is the reason "Direct-to-Consumer
    # Beauty" mapped to 834 companies and "Funded Proptech" to 47,865 brokerages.
    supply = ""
    try:
        from .. import taxonomy
        menu = taxonomy.menu_text(exclude=already_run)
        if menu:
            supply = ("\n\nMEASURED SUPPLY (industry labels and NAICS codes with real, "
                      "unworked companies behind them -- anchor every vertical on one or "
                      "more of THESE; a label not on this list is very likely to have no "
                      "supply and will be discarded):\n" + menu)
    except Exception:
        supply = ""
    user = (
        f"WINNING CAMPAIGNS (last {settings.lookback_days} days, ranked by positive "
        f"reply rate):\n{winner_text}\n\n"
        f"ALREADY RUN in the last {settings.lookback_days} days -- do not propose these "
        f"or near-duplicates of them:\n{skip_text}\n\n"
        + memory + supply +
        "\n\nPropose the adjacent verticals now."
    )
    # 5 verticals, each with industry + keyword arrays and disqualifiers, runs to
    # ~3.5k tokens. At 2000 the JSON was cut mid-object and failed to parse, which
    # looked like a model error rather than a budget one.
    # RETRYING, like the weekly run's own proposal calls in app/modes.py. This one was
    # the bare version, and on 2026-09-23 the adjacent split -- the compounding loop,
    # which had never once succeeded -- died on a single empty model reply. One unlucky
    # generation should not cost a winner its next generation of audiences.
    payload = llm.complete_json_retrying(MODEL, _system(), user, TEMPERATURE,
                                         max_tokens=8000, provider="precopy")
    raw = payload.get("verticals") if isinstance(payload, dict) else payload
    proposed = [Vertical.from_json(v) for v in (raw or []) if isinstance(v, dict)]

    # Recent tests are barred by the lookback. A loss is evidence about the policy that
    # ran, not proof that an audience can never work under a new offer/copy hypothesis.
    barred = {v.strip().lower() for v in already_run}
    fresh = [
        v for v in proposed
        if v.name and v.name.strip().lower() not in barred and v.industries
    ]
    for v in fresh:
        v.seed_campaign = seed.get("name") or ""
        v.seed_industry = seed.get("vertical_label") or seed.get("name") or ""
        try:
            from .. import keywords
            v.keywords = keywords.expand(v)
        except Exception as exc:
            print(f"[stage2] keyword fan-out skipped for {v.name!r}: {exc}", flush=True)
    return fresh[: settings.splits_per_seed_max]


_STOP = {"the", "and", "&", "of", "for", "in", "services", "solutions", "companies",
         "startups", "platforms", "firms", "brands", "tech", "technology", "b2b", "b2c",
         "consumer", "funded", "high", "growth", "digital", "practice", "practices"}


def _industry_terms(v: Vertical) -> set[str]:
    """Normalised industry vocabulary, used to spot the same audience twice."""
    terms: set[str] = set()
    for raw in list(v.industries) + [v.name]:
        for word in re.split(r"[^a-z0-9]+", (raw or "").lower()):
            if word and word not in _STOP and len(word) > 2:
                terms.add(word)
    return terms


def _is_duplicate(candidate: Vertical, kept: list[Vertical], threshold: float = 0.5) -> str | None:
    """Return the name of an already-kept vertical covering the same audience.

    Name matching is not enough. Three seeds independently proposed "Funded Fintech &
    Financial Services Startups", "Fintech & Financial Infrastructure Startups" and
    "FinTech (Consumer & B2B Financial Platforms)" -- three different strings, one
    audience. Running all three would triple-target the same companies and split the
    sample so thin that none of them reaches a fair read at the 7% gate.
    """
    mine = _industry_terms(candidate)
    if not mine:
        return None
    for other in kept:
        theirs = _industry_terms(other)
        if not theirs:
            continue
        overlap = len(mine & theirs) / min(len(mine), len(theirs))
        if overlap >= threshold:
            return other.name
    return None


def propose(winners: list[dict], already_run: set[str],
            outcomes: dict[str, dict] | None = None) -> list[Vertical]:
    """Split EVERY winning seed 3-5 ways, not the winners collectively 3-5 ways.

    Troy, 2026-09-10: "each seed campaign split into 3-5". Three seeds therefore yields
    9-15 verticals. Each seed is asked separately so its splits stay anchored to that
    seed's buyer -- asking once across all three produced a blended list that belonged
    to no particular winner and could not be attributed back when judging.

    Names are deduped across seeds: two seeds will independently suggest the obvious
    adjacent vertical, and running it twice in one cycle would double-spend on the same
    companies and split its sample in half, so neither copy would reach a fair read.
    """
    outcomes = outcomes or {}
    seen: set[str] = set()
    out: list[Vertical] = []
    for seed in winners:
        try:
            splits = propose_for_seed(seed, already_run, outcomes)
        except Exception as exc:
            print(f"[stage2] seed {seed.get('name')!r} failed to split: {exc}", flush=True)
            continue
        for v in splits:
            key = v.name.strip().lower()
            if key in seen:
                continue
            dup = _is_duplicate(v, out)
            if dup:
                print(f"[stage2] dropping {v.name!r} -- same audience as {dup!r}", flush=True)
                continue
            seen.add(key)
            out.append(v)
        # Do NOT cap on verticals_per_run here. That cap is how many a run WORKS,
        # not how many a winner is allowed to spawn. Three seeds x 3-5 splits
        # enqueue 9-15; take_from_backlog picks the next few by projected supply.
    return out
