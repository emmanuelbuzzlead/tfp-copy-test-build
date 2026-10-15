"""One fit decision per company, made BEFORE paying to find its people, the same for every angle.

Measured 2026-09-27: the copy stage rejected ~80% of contacts as "not a fit" -- after their
people had already been found and verified -- because each angle's research prompt judged
fit on its own. The Tariff brief rejected companies the other two kept, and the same company
could pass on one call and fail on the next. So:
  - fit is judged once per company from its website, angle-neutral, and stored on
    company_funnel (fit / fit_reason / fit_vertical)
  - one judgment per company, no second opinion (dropped 2026-09-28: it changed the
    verdict on only 1.6% of rejections, for 39% more cost per rejected company)
  - sourcing only works companies that passed; the copy stage trusts the stored verdict
    instead of re-judging per angle
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from . import client, db
from .clients import llm

MODEL = "anthropic/claude-sonnet-5"   # haiku caught 4 of 24 wrong rejections, 2026-09-27
PASS = {"HIGH", "MEDIUM"}

SYSTEM = """You decide whether a company is a customer for ProductEVO, a fractional product
development and manufacturing team. Its customers are companies that design and sell THEIR OWN
physical products (a factory may build them for them).

Read the website text and answer:
- fit: HIGH, MEDIUM, LOW or NOT_FIT
  NOT_FIT only on POSITIVE evidence that the company: is a retailer, store, showroom, dealer,
  distributor, wholesaler or reseller of other brands; is a service business (installation,
  repair, contracting, design-for-hire, consulting, rental); is a contract manufacturer, job
  shop or custom fabricator that builds to other people's designs; is defunct; is based and
  run outside the United States; or its whole line is apparel, food, supplements, cosmetics
  or jewelry. Also NOT_FIT: FDA Class II or higher medical devices.
  LOW: it probably owns products but they are a small side line to one of the above.
  MEDIUM: likely a fit, but the text is thin. When you cannot tell, answer MEDIUM.
  HIGH: clearly designs and sells its own physical product line.
  {b2b_rule}
  Avoid these mistakes (a stronger model overturned 24 of 100 rejections for them, 2026-09-27):
  - A company with its OWN branded product line (catalogue, part numbers, product pages, its
    own brand name on the products) is NOT a contract manufacturer or job shop just because it
    also takes custom orders or sells components to other manufacturers.
  - A brand that sells its own products AND resells some other brands is NOT a distributor.
    Call it a distributor only when the products are mostly other companies' brands.
  - Being industrial, regulated (OSHA, UL, ASTM) or sold through dealers is not a reason to reject.
- reason: one short sentence of evidence from the text
- vertical: which ONE of these it belongs in, or "none": {verticals}

Reply with JSON only: {{"fit": "...", "reason": "...", "vertical": "..."}}"""

B2B_RULES = {
    "include": "A company that sells its own products to BUSINESSES rather than consumers is still a fit.",
    "exclude": "A company that sells its own products only to BUSINESSES (not consumers) is LOW.",
    "": "",
}


def ensure_columns() -> None:
    with db.cursor() as cur:
        cur.execute("""ALTER TABLE company_funnel ADD COLUMN IF NOT EXISTS fit TEXT,
                           ADD COLUMN IF NOT EXISTS fit_reason TEXT,
                           ADD COLUMN IF NOT EXISTS fit_vertical TEXT,
                           ADD COLUMN IF NOT EXISTS fit_checked_at TIMESTAMPTZ""")


def _system() -> str:
    """The fit prompt. A client may carry its OWN in clients/<name>/fit_prompt.txt (placeholders
    {verticals} and {b2b_rule}); without one the original ProductEVO wording is used. Lumion sells
    to schools, so judging its companies with a product-maker prompt would reject every school."""
    from . import testgrid
    cfg = testgrid.load_config() or {}
    b2b = str((cfg.get("fit") or {}).get("b2b_product_makers", "")).lower()
    verticals = list(client.profile().verticals)
    own = client.CLIENTS_DIR / client.profile().name / "fit_prompt.txt"
    template = own.read_text(encoding="utf-8") if own.exists() else SYSTEM
    return template.format(b2b_rule=B2B_RULES.get(b2b, ""), verticals=json.dumps(verticals))


def _judge(domain: str, name: str, description: str, system: str) -> dict:
    from .stages.s5_copy import _site_or_fallback
    site = _site_or_fallback(domain, name or domain)
    user = (f"Company: {name}\nDomain: {domain}\nSourcing description: {(description or '')[:600]}\n\n"
            # Half of the previous 6000 chars: tested 2026-09-28 against 200 companies,
            # agreed with the full-length read 95% of the time. Troy approved the cut.
            f"WEBSITE TEXT:\n{site[:3000]}")
    out = llm.complete_json(MODEL, system, user, 0.0, max_tokens=300, provider="precopy")
    if not isinstance(out, dict):
        return {"fit": "MEDIUM", "reason": "unparseable verdict", "vertical": "none"}
    fit = str(out.get("fit", "MEDIUM")).upper().replace(" ", "_")
    return {"fit": fit if fit in {"HIGH", "MEDIUM", "LOW", "NOT_FIT"} else "MEDIUM",
            "reason": str(out.get("reason", ""))[:300], "vertical": str(out.get("vertical", "none"))}


def check(companies: list[dict], workers: int = 10) -> dict[str, dict]:
    """Judge every company not judged yet; returns {domain: verdict} for all of them."""
    ensure_columns()
    domains = [c["domain"] for c in companies]
    with db.cursor() as cur:
        cur.execute("SELECT domain, fit, fit_reason, fit_vertical FROM company_funnel "
                    "WHERE domain = ANY(%s) AND fit IS NOT NULL", (domains,))
        done = {r["domain"]: {"fit": r["fit"], "reason": r["fit_reason"], "vertical": r["fit_vertical"]}
                for r in cur.fetchall()}
    todo = [c for c in companies if c["domain"] not in done]
    system = _system()

    def one(c: dict) -> tuple[str, dict]:
        name, desc = c.get("name") or c.get("company_name") or "", c.get("description") or ""
        try:
            v = _judge(c["domain"], name, desc, system)
            # Troy, 2026-09-28: cut the second opinion. Measured on 2026-09-27's run: it
            # changed the verdict for 134 of 8,154 rejections (1.6%), so it was costing
            # ~39% more per rejected company for a verdict that almost never moved.
        except Exception as exc:
            v = {"fit": None, "reason": f"check failed: {str(exc)[:120]}", "vertical": None}
        return c["domain"], v

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(pool.map(one, todo))
    with db.cursor() as cur:
        cur.executemany("""UPDATE company_funnel SET fit=%s, fit_reason=%s, fit_vertical=%s,
                           fit_checked_at=now() WHERE domain=%s""",
                        [(v["fit"], v["reason"], v["vertical"], d) for d, v in results.items()
                         if v["fit"]])
    done.update({d: v for d, v in results.items() if v["fit"]})
    return done


def verdict(domain: str) -> str | None:
    """The stored company-level fit, or None if never judged."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT fit FROM company_funnel WHERE domain=%s", (domain,))
            row = cur.fetchone()
        return row["fit"] if row else None
    except Exception:
        return None
