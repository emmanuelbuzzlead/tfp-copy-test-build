"""Company-level enrichment and gating for the four quick-run lists (campaigns 2210 BM, 2211 HG, 2212 CN, 2213 BR), then the load.

The campaign copy merges CITY, PRODUCT_MIX, SHIPPED_PRODUCT and PLANT_COUNT. EmailBison hands out the Email 1 variants lead by lead, so any lead can get any
variant: a lead is loaded only when EVERY field its campaign's variants use is filled and valid, and the conditions the spec attaches to single variants
(coil producers, one-plant companies, no own fleet) are applied to the whole list. A field the model is not sure of stays blank and the company is held back.
Sources: clients/tfp/spec/TFP_Quick_Run_Campaign_Shell.md (gates, field rules); Emmanuel, 2026-10-27 (construction = anyone who loads flatbeds, COMPANY 2 words).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import db
from .clients import jina, llm

MODEL = "openai/gpt-oss-120b"
CAMPAIGNS = {"BM": 2210, "HG": 2211, "CN": 2212, "BR": 2213}
MAX_PER_COMPANY = 4
COMPANY_WORDS = 2

COMMON = (
    "You read a company's website text and fill a small record used to personalise a cold email about truck-loading fall protection (flatbed trailers).\n"
    "Use ONLY what the text supports. When you are not confident of a field, use null: a wrong value is worse than a blank.\n"
    "Field rules:\n"
    "- city: city of the company's largest or most prominent US or Canadian site (plant, yard or branch). City name only, Title Case, no state, no country. "
    "Fallback: headquarters city.\n"
)
SPECIFIC = {
    "BM": (
        "Company type: a building materials distributor or dealer with yards that load trucks.\n"
        '- operates_yards: "yes" only if it has physical yards or branches that load trucks, else "no"\n'
        "- branch_count: integer number of branches or yards in the US and Canada, or null\n"
        '- product_mix: its 2 main stocked product groups, lowercase, joined with "and", for example "lumber and trusses", "roofing and siding", "rebar and wire mesh"\n'
        '- has_own_delivery_fleet: "yes", "no" or "unclear" (the site mentions its own delivery trucks, boom trucks or flatbed delivery)'
    ),
    "HG": (
        "Company type: a manufacturer of heavy, long or oversized goods that ship on flatbed trailers.\n"
        '- ships_on_flatbed: "yes", "likely" or "no"\n'
        '- shipped_product: its main finished product, lowercase, PLURAL, 1 to 3 words, that reads correctly in "Your steel joists go out on flatbeds" '
        "(examples: steel joists, precast vaults, truck trailers)\n"
        "- plant_count: integer number of US and Canadian plants, or null\n"
        '- is_coil_producer: "yes" if its main product is steel or aluminum coil, else "no"'
    ),
    "CN": (
        "Company type: a construction, contracting or infrastructure company.\n"
        '- loads_flatbeds: "yes", "likely" or "no": its own people load or unload equipment, steel, pipe, materials or machines on flatbed or lowboy trailers'
    ),
    "BR": (
        "Company type: a warehousing, logistics or distribution company.\n"
        '- has_physical_facility: "yes" if it operates warehouses or yards, "no" if it is a pure broker\n'
        '- flatbed_ops: "yes", "likely" or "no": it handles flatbed or open-deck freight, yard operations, or heavy or oversized goods'
    ),
}
FORMAT = "Reply with JSON only: an object holding city and the fields above, plus reason (one short sentence of evidence)."


def ensure_table() -> None:
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS list_enrichment (
            domain TEXT NOT NULL, list_code TEXT NOT NULL, fields JSONB, gate_pass BOOLEAN, reason TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (domain, list_code))""")


def _title(s):
    s = re.sub(r",.*$", "", str(s or "")).strip()
    return s.title() if s and len(s) < 40 else None


def _words(s, lo, hi):
    s = re.sub(r"\s+", " ", str(s or "").strip().lower().strip(".\"'"))
    return s if s and lo <= len(s.split()) <= hi else None


def _int(v):
    try:
        n = int(re.sub(r"[^0-9]", "", str(v)))
        return n if 0 < n < 5000 else None
    except ValueError:
        return None


def judge(code: str, f: dict) -> tuple[bool, str, dict]:
    """(passes, reason, merge fields). Strict because any lead can get any Email 1 variant."""
    city = _title(f.get("city"))

    def yes(k):
        return str(f.get(k) or "").lower() == "yes"

    def ok(k):
        return str(f.get(k) or "").lower() in {"yes", "likely"}

    if code == "BM":
        pm, n = _words(f.get("product_mix"), 2, 5), _int(f.get("branch_count"))
        if not yes("operates_yards"):
            return False, "no yards", {}
        if not n or n < 2:
            return False, "fewer than 2 branches or unknown", {}
        if not city or not pm or " and " not in pm:
            return False, "city or product mix missing", {}
        if not yes("has_own_delivery_fleet"):
            return False, "no confirmed own delivery fleet (variant 1 says 'your own driver')", {}
        return True, "ok", {"CITY": city, "PRODUCT_MIX": pm}
    if code == "HG":
        sp, n = _words(f.get("shipped_product"), 1, 3), _int(f.get("plant_count"))
        if not ok("ships_on_flatbed"):
            return False, "does not ship on flatbeds", {}
        if str(f.get("is_coil_producer") or "").lower() != "no":
            return False, "coil producer or unknown (variant 1 is for odd shapes)", {}
        if not city or not sp or not n or n < 2:
            return False, "city, product or plant count missing, or a single plant (variant 3 needs 2+)", {}
        return True, "ok", {"CITY": city, "SHIPPED_PRODUCT": sp, "PLANT_COUNT": str(n)}
    if code == "CN":
        if not ok("loads_flatbeds"):
            return False, "does not load flatbeds", {}
        return (True, "ok", {"CITY": city}) if city else (False, "city missing", {})
    if code == "BR":
        if not yes("has_physical_facility") or not ok("flatbed_ops"):
            return False, "no facility or no flatbed operations", {}
        return (True, "ok", {"CITY": city}) if city else (False, "city missing", {})
    raise ValueError(code)


def enrich_company(code: str, domain: str, name: str) -> dict:
    from . import client
    words = tuple(client.profile().research_page_words)
    site = jina.website_text_spec(domain, words, max_pages=3, max_chars=9000) or ""
    if len(site) < 200:
        return {"gate_pass": False, "reason": "website unreadable", "fields": {}}
    user = f"Company: {name}\nDomain: {domain}\n\nWEBSITE TEXT:\n{site[:9000]}"
    f = llm.complete_json(MODEL, COMMON + SPECIFIC[code] + "\n" + FORMAT, user, 0.0, max_tokens=700, provider="research")
    if not isinstance(f, dict):
        return {"gate_pass": False, "reason": "model gave no usable answer", "fields": {}}
    passed, reason, merge = judge(code, f)
    return {"gate_pass": passed, "reason": reason if not passed else str(f.get("reason") or "ok")[:200], "fields": {**f, "_merge": merge}}


def run(code: str, workers: int = 12) -> dict:
    """Enrich every company that holds quick-run contacts for this list and has no stored verdict yet."""
    ensure_table()
    with db.cursor() as cur:
        cur.execute("""SELECT c.company_domain d, max(coalesce(s.payload->>'name', c.company_domain)) n FROM contacts c
                       LEFT JOIN staged_companies s ON s.domain = c.company_domain
                       WHERE c.status = 'quickrun_hold' AND c.source = %s
                         AND NOT EXISTS (SELECT 1 FROM list_enrichment e WHERE e.domain = c.company_domain AND e.list_code = %s)
                       GROUP BY 1""", (f"quickrun-{code}", code))
        todo = cur.fetchall()

    def one(r):
        try:
            return r["d"], enrich_company(code, r["d"], r["n"]), None
        except Exception as exc:
            return r["d"], None, f"{type(exc).__name__}: {str(exc)[:100]}"

    done = failed = passed = 0
    with ThreadPoolExecutor(workers) as pool:
        for d, out, err in pool.map(one, todo):
            if out is None:
                failed += 1
                continue                                   # no row stored: it is tried again next run
            with db.cursor() as cur:
                cur.execute("""INSERT INTO list_enrichment (domain, list_code, fields, gate_pass, reason) VALUES (%s, %s, %s, %s, %s)
                               ON CONFLICT (domain, list_code) DO UPDATE SET fields = EXCLUDED.fields, gate_pass = EXCLUDED.gate_pass,
                               reason = EXCLUDED.reason, updated_at = now()""", (d, code, json.dumps(out["fields"]), out["gate_pass"], out["reason"]))
            done += 1
            passed += bool(out["gate_pass"])
    return {"companies": len(todo), "enriched": done, "failed": failed, "passed": passed}


def load(code: str, dry_run: bool = True) -> dict:
    """Load the passing companies' held contacts (up to 4 each) into the list's campaign, COMPANY cleaned to 2 words."""
    from . import materials
    from .clients import emailbison
    from .company_name_cleaner import clean_company_name
    with db.cursor() as cur:
        cur.execute("""SELECT c.email, c.first_name, c.last_name, c.title, c.company_domain d, e.fields, coalesce(s.payload->>'name', c.company_domain) n
                       FROM contacts c JOIN list_enrichment e ON e.domain = c.company_domain AND e.list_code = %s AND e.gate_pass
                       LEFT JOIN (SELECT DISTINCT ON (domain) domain, payload FROM staged_companies) s ON s.domain = c.company_domain
                       WHERE c.status = 'quickrun_hold' AND c.source = %s ORDER BY c.company_domain, c.email""", (code, f"quickrun-{code}"))
        rows = cur.fetchall()

    def nice(s):
        s = (s or "").strip()
        return s.title() if s and (s.isupper() or s.islower()) else s

    per: dict[str, int] = {}
    contacts = []
    for r in rows:
        per[r["d"]] = per.get(r["d"], 0) + 1
        if per[r["d"]] > MAX_PER_COMPANY:
            continue
        fields = r["fields"] if isinstance(r["fields"], dict) else json.loads(r["fields"])
        company = clean_company_name(r["n"], COMPANY_WORDS)
        if materials.check_company(company, COMPANY_WORDS):
            continue
        contacts.append({"email": r["email"], "first_name": nice(r["first_name"]), "last_name": nice(r["last_name"]), "title": r["title"], "company_name": company,
                         "company_domain": r["d"], "vertical": f"QUICK RUN {code}", "company_max_words": COMPANY_WORDS, "extra_fields": fields.get("_merge") or {}})
    cid = CAMPAIGNS[code]
    if dry_run:
        return {"dry_run": True, "campaign": cid, "contacts": len(contacts), "companies": len({c["company_domain"] for c in contacts}),
                "sample": [(c["email"], c["company_name"], c["extra_fields"]) for c in contacts[:3]]}
    res = emailbison.push_leads(cid, contacts, dry_run=False)
    refused = set((res.get("refused") or {}).keys())
    ids = res.get("id_by_email") or {}
    with db.cursor() as cur:
        for c in contacts:
            e = c["email"].lower()
            if e in ids and e not in refused:
                cur.execute("UPDATE contacts SET status='pushed', eb_campaign_id=%s, eb_lead_id=%s, updated_at=now() WHERE lower(email)=%s", (cid, ids[e], e))
    return {"dry_run": False, "campaign": cid, "sent": len(contacts), "attached": res.get("attached"), "refused": res.get("refused")}
