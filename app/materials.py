"""MATERIALS_HAULED and COMPANY merge values for the TFP "OG copy" campaigns, to the locked field standards.

Source of truth for the rules: scripts/tfp/COPY_FIELD_STANDARDS.md (locked 2026-09-15 and 2026-09-16) and the gate scripts/tfp/preflight_copy_fields.py.
The prompt is the FACILITY prompt from scripts/tfp/bm_finalize.py and aiark_materials_hauled.py (newest, 2026-09-16): TFP only sells to companies whose
own people load trucks at their own dock, so the question is what physically crosses THAT dock. The carrier and manufacturer prompts are for other
company types and are deliberately not used here.

  {{MATERIALS_HAULED}}  at most 5 words including "and", shape "X and Y" (two concrete nouns), lowercase, no side that is only a vague word,
                        no repeated sides, no dangling modifier. Never shorten a shortened phrase: re-infer instead.
  {{COMPANY}}           app.company_name_cleaner.clean_company_name (identical to the scripts/tfp copy), at most 3 words, no legal suffix.

A company the model cannot describe is HELD, never sent with a filler phrase. The standard says a high fallback rate is a fit or prompt problem.
"""
from __future__ import annotations

import re

from .company_name_cleaner import SUFFIXES

MAX_COMPANY_WORDS = 3
MAX_MATERIALS_WORDS = 5
# Gemini Flash Lite is what the TFP scripts used (gemini-flash-lite-latest); this service reaches it through OpenRouter.
MODEL = "google/gemini-2.5-flash-lite"
TEMPERATURE = 0.1

VAGUE = {"machinery", "equipment", "components", "materials", "parts", "products", "goods", "units", "supplies", "assemblies", "items", "systems",
         "freight", "cargo", "loads", "commodities", "liquids", "parcels", "vans", "stuff"}
DANGLING = {"packaged", "palletized", "assorted", "various", "mixed", "general", "finished", "bulk", "dry", "refrigerated", "heavy", "industrial",
            "commercial", "raw", "building", "consumer", "construction", "automotive", "electrical", "metal", "steel", "food", "and", "of", "the", "for"}
CONNECTORS = {"and", "of", "the", "for", "&", "-"}
SUFFIX_SET = {s.lower() for s in SUFFIXES}
GENERIC_FALLBACKS = {"general freight and palletized goods", "palletized freight and general goods", "palletized freight and general goods"}
UNCLEAR = "unclear"

SYSTEM = (
    "You infer what physical goods cross a company's own loading docks -- the things their workers load onto and unload from trucks at their own "
    "facility, warehouse, plant, branch or yard. This may be their own manufactured product, or goods they store and distribute. Base it on the "
    "company name, description and industry. Output ONLY a short phrase in the exact shape 'X and Y' -- two concrete nouns (e.g. 'steel coils and "
    "structural beams', 'plywood and hardwood lumber', 'roofing shingles and underlayment', 'palletized groceries and dry goods'), AT MOST 5 words "
    "including 'and', all lowercase, no quotes, no extra text. Both sides must be complete noun phrases -- never end on an adjective. "
    f"If you genuinely cannot tell, output exactly: {UNCLEAR}"
)


def check_company(v: str | None) -> list[str]:
    """Problems with a COMPANY value (empty list = clean). Same rules as preflight_copy_fields.check_company."""
    out: list[str] = []
    if not v or not v.strip():
        return ["empty"]
    w = v.split()
    if len(w) > MAX_COMPANY_WORDS:
        out.append(f"{len(w)} words (max {MAX_COMPANY_WORDS})")
    if w and w[-1].lower().strip(",.") in SUFFIX_SET:
        out.append("ends in legal suffix")
    if w and w[-1].lower().strip(",.&") in CONNECTORS:
        out.append("dangling connector")
    if v.count("(") != v.count(")") or v.count("[") != v.count("]"):
        out.append("unbalanced bracket")
    return out


def check_materials(v: str | None) -> list[str]:
    """Problems with a MATERIALS_HAULED value. Same rules as preflight_copy_fields.check_materials, plus the shape rule and the fallback ban."""
    out: list[str] = []
    if not v or not v.strip():
        return ["empty"]
    if v != v.lower():
        out.append("not lowercase")
    w = v.split()
    if len(w) > MAX_MATERIALS_WORDS:
        out.append(f"{len(w)} words (max {MAX_MATERIALS_WORDS})")
    sides = v.split(" and ")
    if len(sides) != 2:
        out.append("not the shape 'X and Y'")
    else:
        left, right = sides[0].strip(), sides[1].strip()
        if not left or not right:
            out.append("empty side")
        if right in DANGLING:
            out.append(f"dangling modifier '{right}'")
        if left == right:
            out.append("repeated sides")
        if len(left.split()) == 1 and left in VAGUE and len(right.split()) == 1 and right in VAGUE:
            out.append("both sides vague")
    if v in GENERIC_FALLBACKS:
        out.append("generic fallback")
    return out


def _clean(raw: str) -> str:
    """The model's reply as a candidate phrase: first line, lowercase, no quotes or trailing punctuation, cut to 5 words."""
    first = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    first = first.strip().strip("\"'`*.").lower()
    first = re.sub(r"\s+", " ", first)
    return " ".join(first.split()[:MAX_MATERIALS_WORDS])


def infer(name: str, description: str, industry: str, attempts: int = 2) -> str | None:
    """MATERIALS_HAULED for one company, or None when the model cannot say or two tries both fail the standards."""
    from .clients import llm
    user = f"Company: {name}\nDescription: {(description or '')[:600]}\nIndustry: {industry or 'unknown'}"
    for _ in range(attempts):
        try:
            text = llm.complete(MODEL, SYSTEM, user, TEMPERATURE, max_tokens=60, provider="openrouter")
        except llm.LLMUnavailable:
            text = llm.complete(MODEL, SYSTEM, user, TEMPERATURE, max_tokens=60, provider="buzzlead")
        cand = _clean(text)
        if cand == UNCLEAR or not cand:
            return None
        if not check_materials(cand):
            return cand
    return None
