"""Stage 2b -- score proposed verticals on how much UNWORKED supply they actually have.

Stage 2 picks verticals on buyer behaviour alone, which is the right criterion for
whether an audience will convert and no guide at all to whether it can be filled. Those
are different questions, and on this workspace they diverge hard. Measured 2026-09-10,
first 100 US 11-500 results per term against the 223k-contact history:

    Fintech                       3,133 universe, 13% fresh
    Computer and Network Security 2,782 universe, 33% fresh
    Biotechnology Research        5,428 universe, 61% fresh
    Design Services               5,963 universe, 85% fresh
    Veterinary Services           3,015 universe, 91% fresh
    Capital Markets                 134 universe, 95% fresh

Fintech and Biotech are similarly sized and Biotech yields roughly five times the
workable companies, purely because IDPR has already mined fintech. A vertical proposed
without this check can be a perfectly good audience that cannot be filled -- which is
exactly how the first live run produced one contact from 503 fintech companies.

The probe is deliberately cheap: one page of 100 per vertical, no enforcement pass and
no enrichment, so a vertical is rejected before it costs anything real.
"""

from __future__ import annotations

from .. import db
from ..config import settings
from .s2_expansion import Vertical

SAMPLE_SIZE = 100
DENSITY_SAMPLE = 5        # companies to probe for contact density (QuickEnrich is free)
ASSUMED_KEEP_RATE = 0.35  # share surviving the enforcement pass, measured across runs
# A run spent on a vertical that yields a few dozen contacts is a run not spent on one
# that yields thousands. Raised from 250 on 2026-09-11 after DTC Beauty passed the gate
# and delivered 61 contacts. Note this only became a safe number to enforce once probe()
# started measuring the same universe stage 3 searches -- gating hard on a figure that
# was eight times too small would have rejected good verticals.
MIN_PROJECTED = 2000

# A probe that FAILED, as opposed to one that measured a small audience. The difference
# matters to db.rescore_backlog: a failure must keep whatever was measured before, and a
# real "this is mined out" must be allowed to lower it. Naming them stops the rescore
# guessing from a string.
PROBE_FAILED_NO_TERMS = "no usable search terms"
PROBE_FAILED_NOTHING = "probe returned nothing"
PROBE_FAILURES = (PROBE_FAILED_NO_TERMS, PROBE_FAILED_NOTHING)

# Below this share of the sample being unworked, a vertical cannot reach its lead target
# no matter how well it is targeted, so it is not worth spending an enforcement pass on.
MIN_FRESH_RATE = 0.25
# And a universe smaller than this cannot supply a full vertical even at 100% fresh.
MIN_UNIVERSE = 400


def _sample_density(domains: list[str]) -> float:
    """Average findable contacts per company, from a few live QuickEnrich lookups."""
    from ..finders import find_quickenrich
    from .s4_people import BARE_TITLES, MARKETING_FIRST

    if not domains:
        return 1.0
    titles = MARKETING_FIRST + BARE_TITLES
    found = 0
    for d in domains:
        try:
            found += len(find_quickenrich(d, titles, settings.candidates_per_company))
        except Exception:
            pass
    return max(found / len(domains), 0.05)


def probe(vertical: Vertical, run_id: str | None = None,
          known_density: float | None = None) -> dict:
    """Sample one page and report universe size, fresh rate and projected supply."""
    import httpx
    from vendor.aiark_company_search import _normalize_company

    from .s3_companies import _account_filter, _keyword_hit, calibrate_industries

    industries = calibrate_industries(vertical, run_id)
    naics = vertical.naics or []
    if not industries and not naics:
        return {"vertical": vertical.name, "universe": 0, "fresh_rate": 0.0,
                "projected": 0, "verdict": PROBE_FAILED_NO_TERMS}

    # Measure supply the way STAGE 3 measures it: one query per NAICS code and one per
    # industry term, unioned. Sending them together asks AI Ark for the INTERSECTION --
    # it ANDs the two taxonomies -- and that is a different, much smaller pool than the
    # one sourcing will actually walk. Measured 2026-09-11 on DTC Beauty: the combined
    # filter reported 834 companies where stage 3 went on to scan 6,892. A gate judging
    # a vertical on an eighth of its real supply is not judging the same vertical.
    queries = [_account_filter(vertical, None, [code]) for code in naics]
    queries += [_account_filter(vertical, [term], None) for term in industries]

    universe = 0
    rows: list[dict] = []
    seen: set[str] = set()
    for account in queries:
        try:
            r = httpx.post("https://api.ai-ark.com/api/developer-portal/v1/companies",
                           headers={"X-TOKEN": settings.aiark_api_key,
                                    "Content-Type": "application/json"},
                           json={"account": account, "page": 0,
                                 "size": max(SAMPLE_SIZE // len(queries), 25)},
                           timeout=90)
            if r.status_code != 200:
                continue
            d = r.json() or {}
        except Exception:
            continue
        universe += int(d.get("totalElements") or 0)
        for raw in (d.get("content") or []):
            c = _normalize_company(raw)
            key = (c.get("domain") or "").lower()
            if key and key not in seen:
                seen.add(key)
                rows.append(c)

    if not universe:
        return {"vertical": vertical.name, "universe": 0, "fresh_rate": 0.0,
                "projected": 0, "verdict": PROBE_FAILED_NOTHING}
    for c in rows:
        c["domain"] = db.normalize_domain(c.get("domain") or "")
    rows = [c for c in rows if c["domain"]]

    matched = [c for c in rows if _keyword_hit(c, vertical.keywords)]
    kw_rate = (len(matched) / len(rows)) if rows else 0.0

    blocked = db.blocked_domains(c["domain"] for c in matched)
    fresh_rate = ((len(matched) - len(blocked)) / len(matched)) if matched else 0.0

    # Contact DENSITY, sampled on a handful of real companies. This is what decides the
    # true cost of a vertical: measured 2026-09-10, medical device returned 2.4
    # candidates per company and law firms 0.74, so the same 2,000 contacts cost ~1,700
    # companies in one vertical and ~9,500 in the other. QuickEnrich is an
    # unlimited-credit plan, so asking is free and only costs a little wall clock.
    # Density sampling is the expensive part of a probe (five companies through the
    # full title list on QuickEnrich); re-scoring an already-queued vertical reuses
    # the density it was measured with -- measured 2026-09-11, three re-probes with
    # fresh sampling ran past ten minutes, which is not a cost the runner can carry.
    density = (known_density if known_density
               else _sample_density([c["domain"] for c in matched[:DENSITY_SAMPLE]]))

    # Universe, narrowed by the keyword filter, by what is unworked, by the share that
    # survives enforcement, and finally by how many people each company actually yields.
    # The keep rate is MEASURED from recorded runs once two or more exist (db.yield_
    # medians); the 0.35 constant is only the cold-start value. The live density probe
    # stays, since it is per-vertical, but a measured median guards against a five-
    # company sample landing on an outlier.
    learned = db.yield_medians()
    keep_rate = learned.get("keep_rate", ASSUMED_KEEP_RATE)
    if learned.get("density"):
        density = (density + learned["density"]) / 2
    projected = int(universe * kw_rate * fresh_rate * keep_rate * density)

    # AI Ark's ranked list-head is what IDPR has already emailed. A low fresh_rate
    # here means THAT index is mined, not that the vertical has no companies left --
    # DiscoLike lookalikes and keyword TAM behind the BuzzLead API still have supply.
    # Troy, 2026-09-17: do not call TAM saturated on an AI Ark head-of-list read.
    buzzlead_on = bool(settings.buzzlead_api_key)
    if buzzlead_on and fresh_rate < MIN_FRESH_RATE:
        projected = int(universe * kw_rate * keep_rate * density)

    # What the probe actually measured, captured BEFORE MIN_PROJECTED floors it below.
    # `projected` is a GATE: it is raised to the floor so a BuzzLead-backed vertical
    # still enqueues, because the probe cannot see a lookalike/TAM universe. That makes
    # it useless as a RANK -- on 2026-09-22 five queued verticals sat at exactly 2,000
    # and Behavioral & Mental Health, measured at 8,156, was indistinguishable from
    # Clinical Trial's real 2,000, so Monday's pick fell to row order.
    measured = projected

    if universe < MIN_UNIVERSE and not buzzlead_on:
        verdict = f"universe too small ({universe})"
    elif fresh_rate < MIN_FRESH_RATE and not buzzlead_on:
        verdict = f"only {fresh_rate:.0%} unworked -- already mined"
    elif projected < MIN_PROJECTED and not buzzlead_on:
        verdict = f"only ~{projected} contacts reachable ({density:.1f} per company)"
    elif projected < MIN_PROJECTED and buzzlead_on:
        # Still enqueue: BuzzLead lookalike/TAM is not the universe this probe measured.
        verdict = "ok"
        projected = max(projected, MIN_PROJECTED)
    else:
        verdict = "ok"
    # STANDALONE-VIABLE? Troy, 2026-09-30: Applied AI & ML took a Monday lane and
    # could never make 3,000 -- 713 companies passed the fit check, worth ~140
    # contacts at the measured density. The probe already knew (~projected_raw), but
    # nothing compared it to the launch target, so a vertical that can only ever be
    # pooled was ranked and run like one that can carry a campaign. It still queues
    # (pooling handles it at push), but it no longer outranks a viable one for a
    # lane, and the verdict says so where a human reads it.
    if verdict == "ok" and measured < settings.target_contacts:
        verdict = (f"ok, pool-only: ~{measured:,} reachable, under the "
                   f"{settings.target_contacts:,} standalone target")

    return {"vertical": vertical.name, "universe": universe, "sampled": len(rows),
            "keep_rate_used": round(keep_rate, 3),
            "keyword_rate": round(kw_rate, 3), "fresh_rate": round(fresh_rate, 3),
            "density": round(density, 2), "projected": projected,
            "projected_raw": measured, "verdict": verdict}


def rank(verticals: list[Vertical], run_id: str | None = None
         ) -> tuple[list[Vertical], list[dict]]:
    """Drop verticals that cannot be filled, and order the rest by workable supply."""
    scored = []
    for v in verticals:
        s = probe(v, run_id)
        scored.append((s, v))
        db.log(run_id, "stage2b",
               f"{v.name}: universe {s['universe']:,}, {s.get('fresh_rate', 0):.0%} fresh, "
               f"{s.get('density', 0):.1f} contacts/company, "
               f"~{s['projected']:,} reachable -- {s['verdict']}")

    keep = [(s, v) for s, v in scored if s["verdict"].startswith("ok")]
    keep.sort(key=lambda pair: pair[0]["projected"], reverse=True)
    return [v for _, v in keep], [s for s, _ in scored]
