"""A measured map of AI Ark's taxonomy: how big each term is, and how much is unworked.

Troy, 2026-09-11: "it is imperative that we get around two to three thousand contacts
per vertical ... the suggested vertical should not be so niche that they yield very
low contacts."

Verticals used to be proposed from imagination and then discovered, at sourcing time,
to have no supply behind them. "Direct-to-Consumer Beauty" mapped to 834 companies;
"Funded Proptech" mapped to a 47,865-company label that was almost entirely brokerages.
This module inverts that: every industry label and NAICS code the pipeline has ever
seen -- proposed by the LLM, or carried on a fetched company record -- is measured
directly (universe size and unworked share, one cheap page each) and stored. Stage 2
then chooses adjacency FROM the entries that can actually supply a vertical, and stage
3 widens INTO them when a vertical runs dry.

A term is worth a vertical when its unworked universe sits roughly between 2,000 and
9,000. Below that it cannot fill a batch; above that it is a sector, not a vertical,
and enforcement rejects most of what it returns.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx

from . import db
from .config import settings

# Refresh a measurement after this long. Supply moves as campaigns are sent.
STALE_AFTER = timedelta(days=7)
# The band a single term needs to sit in to anchor a vertical on its own.
MIN_UNWORKED = 2000
MAX_UNIVERSE = 9000
PROBE_SIZE = 100
# A label below MIN_UNWORKED cannot anchor an audience on its own, but two or three of
# them together can, and for some clients that is the ONLY shape available. ProductEVO,
# measured 2026-09-20: its twelve real labels included Sporting Goods Manufacturing
# (929), Doll, Toy and Game Manufacturing (647) and Sporting and Athletic Goods
# Manufacturing (1,358) -- exactly the buyers it sells to, every one of them hidden from
# the audience proposer because each fell short alone. Hiding them left only broad
# catch-alls, which is how a baby-gear audience came back holding an aerospace parts
# maker and a shopping centre.
MIN_COMBINABLE = 250

SCHEMA = """
CREATE TABLE IF NOT EXISTS taxonomy (
    term        TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- 'industry' | 'naics'
    universe    INTEGER,
    fresh_rate  NUMERIC(5,4),
    unworked    INTEGER,
    seen_count  INTEGER NOT NULL DEFAULT 0,
    measured_at TIMESTAMPTZ,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (term, kind)
);
"""

# Labels named in the stage 2 prompt as known-good, so the map is never empty on a
# fresh database. Everything else is harvested from fetched records.
SEED_INDUSTRIES = [
    "Fintech", "Insurtech", "SaaS", "Law Practice", "Biotechnology Research",
    "Medical Equipment Manufacturing", "Computer and Network Security", "Real Estate",
    "Wellness and Fitness Services", "Financial Services", "Software Development",
    "Hospitals and Health Care", "Cosmetics", "Personal Care Product Manufacturing",
    "Pharmaceutical Manufacturing", "Veterinary Services", "Design Services",
    "Capital Markets", "Banking", "Insurance", "Legal Services", "Accounting",
    "Advertising Services", "Public Relations and Communications Services",
    "Retail Apparel and Fashion", "Food and Beverage Manufacturing",
    "Consumer Goods", "Manufacturing", "Renewable Energy Semiconductor Manufacturing",
    "Medical Practices", "Mental Health Care", "Staffing and Recruiting",
    "IT Services and IT Consulting", "Environmental Services", "Construction",
    "Education", "E-Learning Providers", "Travel Arrangements", "Hospitality",
    "Restaurants", "Transportation, Logistics, Supply Chain and Storage",
]


def init_schema() -> None:
    with db.cursor() as cur:
        cur.execute(SCHEMA)


def record_seen(industries: Iterable[str], naics: Iterable[str]) -> None:
    """Note terms carried on fetched company records. Cheap; called per page."""
    rows = [(t.strip().lower(), "industry") for t in industries if t and len(t.strip()) > 2]
    rows += [(str(c).strip(), "naics") for c in naics if c and len(str(c).strip()) == 6]
    if not rows:
        return
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO taxonomy (term, kind, seen_count) VALUES (%s,%s,1) "
            " ON CONFLICT (term, kind) DO UPDATE SET seen_count = taxonomy.seen_count + 1",
            rows)


class VendorUnavailable(RuntimeError):
    """The company database could not answer. Not the same as "no such label"."""


def _account(term: str, kind: str) -> dict:
    account: dict = {
        "location": {"any": {"include": ["United States"]}},
        "employeeSize": {"type": "RANGE", "range": [{"start": 11, "end": 500}]},
    }
    if kind == "naics":
        account["naics"] = {"any": {"include": [term]}}
    else:
        account["industries"] = {"any": {"include": {"mode": "WORD", "content": [term]}}}
    return account


def measure(term: str, kind: str) -> dict | None:
    """One page: universe size, and the share of the sample not emailed recently."""
    from vendor.aiark_company_search import _normalize_company
    try:
        r = httpx.post("https://api.ai-ark.com/api/developer-portal/v1/companies",
                       headers={"X-TOKEN": settings.aiark_api_key,
                                "Content-Type": "application/json"},
                       json={"account": _account(term, kind), "page": 0, "size": PROBE_SIZE},
                       timeout=90)
        if r.status_code != 200:
            # A vendor failure is NOT evidence that a label is unreal, and treating it
            # as such poisons the taxonomy with false negatives that persist. On
            # 2026-09-21 AI Ark ran out of credit mid-probe and every label came back
            # "MISS" -- including two this same code had measured an hour earlier at
            # 4,690 and 3,165 companies. Raise, so the caller knows it could not check.
            raise VendorUnavailable(
                f"company search returned {r.status_code}: {r.text[:120]}")
        d = r.json() or {}
    except VendorUnavailable:
        raise
    except Exception as exc:
        raise VendorUnavailable(f"company search unreachable: {str(exc)[:120]}") from exc
    universe = int(d.get("totalElements") or 0)
    domains = []
    for raw in d.get("content") or []:
        dom = db.normalize_domain((_normalize_company(raw).get("domain") or ""))
        if dom:
            domains.append(dom)
    blocked = db.blocked_domains(domains) if domains else {}
    fresh_rate = ((len(domains) - len(blocked)) / len(domains)) if domains else 0.0
    out = {"term": term, "kind": kind, "universe": universe,
           "fresh_rate": round(fresh_rate, 4), "unworked": int(universe * fresh_rate)}
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO taxonomy (term, kind, universe, fresh_rate, unworked, measured_at) "
            " VALUES (%s,%s,%s,%s,%s,now()) "
            " ON CONFLICT (term, kind) DO UPDATE SET universe=EXCLUDED.universe, "
            "   fresh_rate=EXCLUDED.fresh_rate, unworked=EXCLUDED.unworked, measured_at=now()",
            (term, kind, universe, out["fresh_rate"], out["unworked"]))
    return out


def refresh(limit: int = 60, run_id: str | None = None) -> int:
    """Measure unmeasured or stale terms, most-seen first. Bounded per run."""
    init_schema()
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO taxonomy (term, kind) VALUES (%s,'industry') ON CONFLICT DO NOTHING",
            [(t.lower(),) for t in SEED_INDUSTRIES])
        cur.execute(
            "SELECT term, kind FROM taxonomy "
            " WHERE measured_at IS NULL OR measured_at < %s "
            " ORDER BY measured_at NULLS FIRST, seen_count DESC LIMIT %s",
            (datetime.now(timezone.utc) - STALE_AFTER, limit))
        todo = cur.fetchall()
    done = 0
    for row in todo:
        if measure(row["term"], row["kind"]):
            done += 1
    db.log(run_id, "taxonomy", f"measured {done} of {len(todo)} stale terms")
    return done


def bootstrap(profile, run_id: str | None = None, want: int = 36,
              topic: str | None = None) -> dict:
    """Measure real taxonomy labels for a client that has never run.

    THIS IS WHY PRODUCTEVO'S FIRST RUNS FOUND NOTHING (found 2026-09-20). Audiences are
    proposed from the ICP with a MENU of labels that have been measured against the
    company database. IDPR has a 4,000-character menu built from a year of runs.
    A new client's database is empty, so the menu is empty, so the model invents labels
    -- and an invented label counts zero, is dropped by calibration, and leaves the
    vertical anchored on whatever broad catch-all survived. The search then returns a
    shopping centre, a glass contractor and an aerospace parts maker for a baby-gear
    audience, the fit gate correctly drops all twenty, and the run ends with nothing
    having spent real money to get there.

    So: ask for candidate labels from THIS client's ICP, measure every one, and keep
    the ones that are real and have unworked supply. Measurement is one search call per
    term and is the only way to tell a real label from a plausible-sounding one.
    """
    from .clients import llm

    init_schema()
    known = {r["term"] for r in menu()}
    icp = (profile.icp or "")[:4000]
    pitch = (profile.pitch or "")[:2000]
    system = (
        "You map a company's buyer onto INDUSTRY TAXONOMY LABELS used by a company "
        "database. The labels are LinkedIn-style industry names, for example "
        "'Sporting Goods Manufacturing', 'Furniture and Home Furnishings Manufacturing', "
        "'Personal Care Product Manufacturing'. Return ONLY labels you believe exist in "
        "that taxonomy -- an invented label is worse than a missing one, because it "
        "silently matches nothing. Prefer specific manufacturing and services labels "
        "over umbrella terms like 'Consumer Goods' or 'Manufacturing', which are too "
        "broad to target. Also return 6-digit NAICS codes, avoiding catch-all codes "
        "ending in 9999 or named 'All Other'. "
        'Answer as JSON: {"industries": ["..."], "naics": ["334510"]}')
    # A vertical is measured on ITS OWN terms. Asking for the client's labels in
    # general and then letting a model pick the closest from whatever came back is how
    # "Housewares & Kitchen" resolved to "Furniture and Home Furnishings Manufacturing"
    # on 2026-09-21: cookware and small appliances are not furniture, but furniture was
    # the nearest of the five labels the generic menu happened to hold. Troy spotted it
    # in the run before this one. Measure for the vertical, and the menu it chooses from
    # is about the vertical.
    scope = (f"\n\nTHIS REQUEST IS ONLY ABOUT ONE OF THEIR VERTICALS: {topic!r}.\n"
             f"Return labels whose companies belong to THAT vertical specifically. Do "
             f"not drift into a neighbouring one: kitchenware is not furniture, "
             f"sporting goods are not toys, automotive parts are not machinery."
             if topic else "")
    user = (f"CLIENT: {profile.display_name}\n\nWHAT THEY SELL:\n{pitch}\n\n"
            f"WHO THEY SELL TO:\n{icp}{scope}\n\n"
            f"Give up to {want} industry labels and up to 12 NAICS codes whose companies "
            f"are this client's BUYERS.")
    try:
        payload = llm.complete_json("anthropic/claude-haiku-4.5", system, user, 0.2,
                                    max_tokens=2000, provider="precopy")
    except Exception as exc:
        db.log(run_id, "taxonomy", f"bootstrap could not ask for labels: {exc}",
               level="warn")
        return {"asked": 0, "real": 0, "usable": 0, "terms": []}

    industries = [str(t).strip() for t in (payload.get("industries") or [])
                  if str(t).strip()][:want]
    naics = [str(c).strip() for c in (payload.get("naics") or [])
             if str(c).strip().isdigit() and len(str(c).strip()) == 6][:12]
    todo = [(t, "industry") for t in industries if t.lower() not in known]
    todo += [(c, "naics") for c in naics]

    real = usable = 0
    kept: list[str] = []
    for term, kind in todo:
        try:
            m = measure(term, kind)
        except VendorUnavailable as exc:
            db.log(run_id, "taxonomy",
                   f"bootstrap stopped: {exc}. Nothing measured, nothing recorded -- "
                   f"an unanswerable probe must not be filed as 'this label is unreal'",
                   level="warn")
            return {"asked": len(todo), "real": real, "usable": usable,
                    "terms": kept, "unavailable": str(exc)}
        if not m or not m["universe"]:
            continue                      # invented, or nothing in the United States
        real += 1
        if m["unworked"] >= MIN_UNWORKED and m["universe"] <= MAX_UNIVERSE:
            usable += 1
            kept.append(f'{term} (~{m["unworked"]:,} unworked)')
    db.log(run_id, "taxonomy",
           f"bootstrap for {profile.name}{' / ' + topic if topic else ''}: "
           f"asked {len(todo)}, {real} are real labels, "
           f"{usable} have usable supply", kept=kept[:20])
    return {"asked": len(todo), "real": real, "usable": usable, "terms": kept}


def menu(min_unworked: int = MIN_UNWORKED, max_universe: int = MAX_UNIVERSE,
         exclude: Iterable[str] = ()) -> list[dict]:
    """Terms that can anchor a vertical on their own, best supply first."""
    ex = {e.strip().lower() for e in exclude}
    with db.cursor() as cur:
        cur.execute(
            "SELECT term, kind, universe, fresh_rate, unworked FROM taxonomy "
            " WHERE measured_at IS NOT NULL AND unworked >= %s AND universe <= %s "
            " ORDER BY unworked DESC", (min_unworked, max_universe))
        return [dict(r) for r in cur.fetchall() if r["term"] not in ex]


def combinable(exclude: Iterable[str] = ()) -> list[dict]:
    """Real labels too small to anchor an audience alone, but fine two or three at a time."""
    ex = {e.strip().lower() for e in exclude}
    with db.cursor() as cur:
        cur.execute(
            "SELECT term, kind, universe, fresh_rate, unworked FROM taxonomy "
            " WHERE measured_at IS NOT NULL AND unworked >= %s AND unworked < %s "
            " ORDER BY unworked DESC", (MIN_COMBINABLE, MIN_UNWORKED))
        return [dict(r) for r in cur.fetchall() if r["term"] not in ex]


def _rows_text(rows: list[dict], limit: int) -> str:
    return "\n".join(
        f'  {r["kind"]:8} {r["term"]:44} unworked ~{r["unworked"]:,} '
        f'(universe {r["universe"]:,}, {float(r["fresh_rate"]):.0%} fresh)'
        for r in rows[:limit])


def menu_text(exclude: Iterable[str] = ()) -> str:
    """The menu rendered for a prompt. Empty string when nothing is measured yet."""
    rows = menu(exclude=exclude)
    small = combinable(exclude=exclude)
    if not rows and not small:
        return ""
    parts = []
    if rows:
        parts.append(_rows_text(rows, 40))
    if small:
        parts.append("\nSMALLER LABELS -- too few companies to carry an audience alone, "
                     "so COMBINE two or three related ones into a single audience:\n"
                     + _rows_text(small, 30))
    return "\n".join(parts)


def siblings_for(vertical_name: str, rationale: str, current: Iterable[str],
                 want: int = 2) -> list[str]:
    """Ask the LLM which measured terms belong to this vertical. Used to widen."""
    from .clients import llm
    text = menu_text(exclude=current)
    if not text:
        return []
    system = ("You map an outreach vertical onto industry taxonomy labels. Pick ONLY "
              "labels whose companies are the buyers the vertical describes -- operators, "
              "not suppliers, and not a broader sector that merely contains them. Return "
              'STRICT JSON: {"terms": ["label", ...]} with at most the number asked for. '
              'Return {"terms": []} if none genuinely fit.')
    user = (f"VERTICAL: {vertical_name}\nWHAT IT MEANS: {rationale}\n"
            f"ALREADY USED: {json.dumps(list(current))}\n\n"
            f"MEASURED TERMS (each has real supply):\n{text}\n\n"
            f"Choose up to {want} that fit this vertical.")
    from .stages.s3_companies import ENFORCE_MODEL
    try:
        payload = llm.complete_json(ENFORCE_MODEL, system, user, 0.0, max_tokens=300,
                                    provider="precopy")
    except Exception:
        return []
    terms = payload.get("terms") if isinstance(payload, dict) else None
    allowed = {r["term"] for r in menu(exclude=current)}
    return [t for t in (terms or []) if isinstance(t, str) and t.strip().lower() in allowed][:want]
