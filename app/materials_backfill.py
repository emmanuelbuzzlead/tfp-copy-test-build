"""Give every lead already in a campaign a valid {{MATERIALS_HAULED}} (Emmanuel, 2026-10-22: fuse the OG Email 1 variants into campaign 2174).

EmailBison hands each lead a random Email 1 variant, so once a variant that merges {{MATERIALS_HAULED}} is in the sequence, EVERY lead must carry a value that passes
the locked standard (app/materials.py). This reads the campaign's leads, keeps the values that already pass, infers the rest (blank, or the generic fallback
"general freight and palletized goods" that the old pipeline wrote), and patches them back with existing_lead_behavior=patch, the way scripts/tfp/patch_live_materials.py
did. COMPANY is re-cleaned the same way. A lead whose company cannot be described is NOT removed: it is listed in materials_unresolved for a person to decide.
Dry run by default; apply=True writes.
"""
from __future__ import annotations

from collections import Counter

from . import db, materials, materials_fill
from .clients import emailbison
from .company_name_cleaner import clean_company_name

PERSONAL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com"}


def _vars(lead: dict) -> dict[str, str]:
    return {(v.get("name") or "").lower(): (v.get("value") or "") for v in (lead.get("custom_variables") or [])}


def _domain(lead: dict) -> str:
    return (lead.get("email") or "").split("@")[-1].lower()


def audit(cid: int) -> dict:
    """Counts of what the campaign's leads carry now, using the same checks the loader applies. Changes nothing."""
    leads = emailbison._paged(f"/api/campaigns/{cid}/leads", None, cap=400)
    c = Counter()
    for lead in leads:
        v = _vars(lead)
        m, comp = v.get("materials_hauled", ""), v.get("company") or lead.get("company") or ""
        c["leads"] += 1
        if not m:
            c["materials_blank"] += 1
        elif materials.check_materials(m):
            c["materials_fails_standard"] += 1
        else:
            c["materials_ok"] += 1
        if materials.check_company(comp):
            c["company_fails_standard"] += 1
        if comp and comp != clean_company_name(comp):
            c["company_not_cleaned"] += 1
    return dict(c)


def _ensure_unresolved() -> None:
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS materials_unresolved (
            email TEXT PRIMARY KEY, campaign_id BIGINT, company TEXT, domain TEXT, reason TEXT, found_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def run(cid: int, apply: bool = False) -> dict:
    materials_fill.ensure_tables()
    _ensure_unresolved()
    leads = emailbison._paged(f"/api/campaigns/{cid}/leads", None, cap=400)
    todo, kept, recompany = [], 0, []
    for lead in leads:
        v = _vars(lead)
        m = v.get("materials_hauled", "")
        if m and not materials.check_materials(m):
            kept += 1
            comp = v.get("company") or lead.get("company") or ""
            cleaned = clean_company_name(comp) if comp else ""
            if cleaned and cleaned != comp and not materials.check_company(cleaned):
                recompany.append({"email": lead["email"], "first_name": lead.get("first_name") or "", "last_name": lead.get("last_name") or "",
                                  "company": cleaned, "title": lead.get("title") or "", "materials": m})       # value is fine, the name is not clean yet
        else:
            todo.append(lead)
    rows = [{"company_domain": _domain(x), "company_name": _vars(x).get("company") or x.get("company") or "", "email": x["email"]}
            for x in todo if _domain(x) and _domain(x) not in PERSONAL]
    phr, stats = materials_fill.phrases(rows) if rows else ({}, {})
    resolved, unresolved = [], []
    for lead in todo:
        dom = _domain(lead)
        p = phr.get(dom)
        v = _vars(lead)
        company = clean_company_name(v.get("company") or lead.get("company") or dom)
        if not p or materials.check_company(company):
            unresolved.append((lead, "could not say what they haul" if not p else "company name fails the standard"))
            continue
        resolved.append({"email": lead["email"], "first_name": lead.get("first_name") or "", "last_name": lead.get("last_name") or "",
                         "company": company, "title": lead.get("title") or "", "materials": p})
    resolved += recompany
    report = {"campaign": cid, "leads": len(leads), "already_valid": kept, "to_fix": len(todo), "company_recleaned": len(recompany), "resolved": len(resolved),
              "unresolved": len(unresolved),
              "applied": False, "stats": stats, "samples": [(r["company"], r["materials"]) for r in resolved[:8]]}
    if unresolved:
        with db.cursor() as cur:
            cur.executemany("""INSERT INTO materials_unresolved (email, campaign_id, company, domain, reason) VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (email) DO UPDATE SET reason = EXCLUDED.reason, found_at = now()""",
                            [(l["email"], cid, _vars(l).get("company") or l.get("company") or "", _domain(l), why) for l, why in unresolved])
    if apply and resolved:
        emailbison.ensure_custom_variables({"company", "materials_hauled"})
        for i in range(0, len(resolved), 100):
            chunk = resolved[i:i + 100]
            emailbison._send_retry("POST", "/api/leads/create-or-update/multiple", {"existing_lead_behavior": "patch", "leads": [
                {"email": r["email"], "first_name": r["first_name"], "last_name": r["last_name"], "company": r["company"], "title": r["title"],
                 "custom_variables": [{"name": "company", "value": r["company"]}, {"name": "materials_hauled", "value": r["materials"]}]} for r in chunk]})
        with db.cursor() as cur:
            cur.executemany("UPDATE contacts SET materials_hauled = %s WHERE lower(email) = lower(%s)", [(r["materials"], r["email"]) for r in resolved])
        report["applied"] = True
    return report
