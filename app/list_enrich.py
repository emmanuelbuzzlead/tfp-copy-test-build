"""Company enrichment for the four quick-run lists (campaigns 2210 BM, 2211 HG, 2212 CN, 2213 BR), then the load.

The campaign copy merges CITY, PRODUCT_MIX, SHIPPED_PRODUCT and PLANT_COUNT. These companies were already fit-checked when they were sourced (Emmanuel, 2026-10-27:
"these companies are already fit, we just need their enrichments for copy"), so there are NO fit gates here. The only test is that every field the list's
copy merges is filled and renders: EmailBison hands out Email 1 variants lead by lead, so any lead can get any variant, and a blank would show as "your  plant".
A field the model is not sure of stays blank and the company is held back, never guessed.
Sources: clients/tfp/spec/TFP_Quick_Run_Campaign_Shell.md (field rules); COMPANY is 2 words for these campaigns.
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
PLANT_FALLBACK = "a couple of"

COMMON = (
    "You read a company's website text and fill a small record used to personalise a cold email about truck-loading fall protection (flatbed trailers).\n"
    "Use ONLY what the text supports. When you are not confident of a field, use null: a wrong value is worse than a blank.\n"
    "Field rules:\n"
    "- city: city of the company's largest or most prominent US or Canadian site (plant, yard or branch). City name only, Title Case, no state, no country. "
    "Fallback: headquarters city.\n"
)
SPECIFIC = {
    "BM": (
        '- product_mix: its 2 main stocked product groups, lowercase, joined with "and", for example "lumber and trusses", "roofing and siding", "rebar and wire mesh". '
        "This is an easy inference: take it from the site, or from the company name, description and industry when the site does not say. Give your best answer, never null."
    ),
    "HG": (
        '- shipped_product: its main finished product, lowercase, PLURAL, 1 to 3 words, that reads correctly in "Your steel joists go out on flatbeds" '
        "(examples: steel joists, precast vaults, truck trailers). This is an easy inference: take it from the site, or from the company name, description and "
        "industry when the site does not say. Give your best answer, never null.\n"
        "- plant_count: integer number of US and Canadian plants the site states, or null when it does not say"
    ),
    "CN": "",
    "BR": "",
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
    """(usable, reason, merge fields). No fit gate: usable means every field the list's copy merges is filled and reads correctly."""
    city = _title(f.get("city"))
    if not city:
        return False, "city missing", {}
    if code == "BM":
        pm = _words(f.get("product_mix"), 2, 5)
        if not pm or " and " not in pm:
            return False, "product mix missing", {}
        return True, "ok", {"CITY": city, "PRODUCT_MIX": pm}
    if code == "HG":
        sp, n = _words(f.get("shipped_product"), 1, 3), _int(f.get("plant_count"))
        if not sp:
            return False, "shipped product missing", {}
        # Variant 3 reads "with {PLANT_COUNT} plants shipping on flatbeds": an unknown or single-plant count falls back to "a couple of" (Emmanuel, 2026-10-27)
        return True, "ok", {"CITY": city, "SHIPPED_PRODUCT": sp, "PLANT_COUNT": str(n) if n and n >= 2 else PLANT_FALLBACK}
    if code in ("CN", "BR"):
        return True, "ok", {"CITY": city}
    raise ValueError(code)


def enrich_company(code: str, domain: str, name: str, description: str = "", industry: str = "") -> dict:
    from . import client
    words = tuple(client.profile().research_page_words) + ("contact", "find us", "visit")     # contact pages carry the address; 57 to 84 companies per list had no city without them
    site = jina.website_text_spec(domain, words, max_pages=3, max_chars=9000) or ""
    if len(site) < 200 and len(description or "") < 40:
        return {"gate_pass": False, "reason": "website unreadable and no description", "fields": {}}
    user = (f"Company: {name}\nDomain: {domain}\nIndustry: {industry or 'unknown'}\nDescription: {(description or '')[:600]}\n\n"
            f"WEBSITE TEXT:\n{site[:9000] or '(could not be read)'}")
    f = llm.complete_json(MODEL, COMMON + SPECIFIC[code] + "\n" + FORMAT, user, 0.0, max_tokens=700, provider="research")
    if not isinstance(f, dict):
        return {"gate_pass": False, "reason": "model gave no usable answer", "fields": {}}
    passed, reason, merge = judge(code, f)
    return {"gate_pass": passed, "reason": reason if not passed else str(f.get("reason") or "ok")[:200], "fields": {**f, "_merge": merge}}


def run(code: str, workers: int = 12) -> dict:
    """Enrich every company that holds quick-run contacts for this list and has no stored verdict yet."""
    ensure_table()
    with db.cursor() as cur:
        cur.execute("""SELECT c.company_domain d, max(coalesce(s.payload->>'name', c.company_domain)) n, max(coalesce(s.payload->>'description', '')) descr,
                              max(coalesce(s.payload->>'industry', '')) ind FROM contacts c
                       LEFT JOIN staged_companies s ON s.domain = c.company_domain
                       WHERE c.status = 'quickrun_hold' AND c.source = %s
                         AND NOT EXISTS (SELECT 1 FROM list_enrichment e WHERE e.domain = c.company_domain AND e.list_code = %s)
                       GROUP BY 1""", (f"quickrun-{code}", code))
        todo = cur.fetchall()

    def one(r):
        try:
            return r["d"], enrich_company(code, r["d"], r["n"], r["descr"], r["ind"]), None
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
    """Load the usable companies' held contacts (up to 4 each) into the list's campaign, COMPANY cleaned to 2 words."""
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


def refresh_company_names() -> dict:
    """Re-send COMPANY (cleaned to 2 words with the current rules) for every lead already loaded into the four campaigns. Upsert with patch changes only what is sent."""
    from .clients import emailbison
    from .company_name_cleaner import clean_company_name
    with db.cursor() as cur:
        cur.execute("""SELECT c.email, c.first_name, c.last_name, coalesce(s.payload->>'name', c.company_domain) n FROM contacts c
                       LEFT JOIN (SELECT DISTINCT ON (domain) domain, payload FROM staged_companies) s ON s.domain = c.company_domain
                       WHERE c.status = 'pushed' AND c.eb_campaign_id = ANY(%s)""", (list(CAMPAIGNS.values()),))
        rows = cur.fetchall()
    leads = []
    for r in rows:
        company = clean_company_name(r["n"], COMPANY_WORDS)
        nice = lambda v: (v or "").strip().title() if (v or "").isupper() or (v or "").islower() else (v or "").strip()     # same casing the load used
        leads.append({"email": r["email"], "first_name": nice(r["first_name"]), "last_name": nice(r["last_name"]), "company": company,
                      "custom_variables": [{"name": "company", "value": company}]})
    for i in range(0, len(leads), 100):
        emailbison._send_retry("POST", "/api/leads/create-or-update/multiple", {"leads": leads[i:i + 100], "existing_lead_behavior": "patch"})
    return {"leads": len(leads)}


CITY_PROMPT = ("From these Google search results for a company, give the city of its headquarters or main US or Canadian site. "
               "City name only, Title Case, no state or country. If the results do not make it clear, use null. "
               'Reply with JSON only: {"city": "..." or null}')


def fill_cities(code: str, workers: int = 8) -> dict:
    """Companies held only for a missing city: find it with a Google search (Serper) and re-run the same field checks on the stored enrichment."""
    from .clients import serper
    with db.cursor() as cur:
        cur.execute("SELECT domain, fields FROM list_enrichment WHERE list_code = %s AND NOT gate_pass AND reason = 'city missing'", (code,))
        rows = cur.fetchall()
        names = {}
        if rows:
            cur.execute("SELECT DISTINCT ON (domain) domain, payload->>'name' n FROM staged_companies WHERE domain = ANY(%s)", ([r["domain"] for r in rows],))
            names = {r["domain"]: r["n"] for r in cur.fetchall()}

    def one(r):
        try:
            text = serper.search_by_name(names.get(r["domain"]) or r["domain"], "headquarters address city", 6)
            out = llm.complete_json(MODEL, CITY_PROMPT, f"Company: {names.get(r['domain'])} ({r['domain']})" + chr(10) + text[:3000], 0.0, max_tokens=100, provider="research")
            return r, (out or {}).get("city") if isinstance(out, dict) else None
        except Exception:
            return r, None

    found = passed = 0
    with ThreadPoolExecutor(workers) as pool:
        for r, city in pool.map(one, rows):
            if not city:
                continue
            f = r["fields"] if isinstance(r["fields"], dict) else json.loads(r["fields"])
            f["city"] = city
            ok, reason, merge = judge(code, f)
            found += 1
            passed += bool(ok)
            if ok:
                f["_merge"] = merge
                with db.cursor() as cur:
                    cur.execute("UPDATE list_enrichment SET fields = %s, gate_pass = TRUE, reason = 'city from web search', updated_at = now() WHERE domain = %s AND list_code = %s",
                                (json.dumps(f), r["domain"], code))
    return {"held": len(rows), "city_found": found, "now_usable": passed}
