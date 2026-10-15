"""Expand a vertical's keyword list before any company fetch.

Stage 2 asks the model for 6-12 seed keywords. Matching is local (AI Ark's keyword
filter 401s on this plan; BuzzLead TAM still benefits from a wider phrase list). A
company that describes itself as "med-mal" will miss a seed of "medical malpractice"
unless we fan the seed out into the words that company would actually use.

Fan-out is additive and capped. Originals stay first; synonyms, abbreviations and
everyday self-descriptions follow. Generic filler (services, solutions, company) is
dropped -- it matches everyone and filters nothing.
"""

from __future__ import annotations

from .config import settings

MODEL = "anthropic/claude-haiku-4.5"
TEMPERATURE = 0.2
CAP = 24

_STOP = frozenset({
    "and", "the", "for", "with", "firm", "firms", "company", "companies", "group",
    "services", "service", "solutions", "solution", "based", "provider", "providers",
    "industry", "industries", "sector", "business", "businesses",
})

SYSTEM = """You expand keyword lists for B2B company matching.

You are given a vertical name, a one-line rationale, and the seed keywords already
chosen. Return MORE keywords a company in that vertical would use about itself:
synonyms, abbreviations, product categories, job-to-be-done phrases, the everyday
word a founder would put on an About page.

Rules:
- Every keyword must DISTINGUISH this vertical from its parent industry. "real estate"
  for a proptech vertical is wrong; "listing platform" is right.
- Prefer one or two words. Long phrases match fewer records.
- Do not emit filler: services, solutions, company, provider, firm, industry.
- Keep the original seeds, then add. Deduplicate.
- 8-20 items total.

Return STRICT JSON only: {"keywords": ["..."]}"""


def _clean(words: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in words:
        k = " ".join((raw or "").strip().lower().split())
        if not k or k in seen or k in _STOP:
            continue
        tokens = [t for t in k.split() if t not in _STOP]
        if not tokens:
            continue
        seen.add(k)
        out.append(k)
        if len(out) >= CAP:
            break
    return out


def merge(seed: list[str], extra: list[str], cap: int = CAP) -> list[str]:
    """Originals first, then extras that are not already present. Pure."""
    return _clean(list(seed or []) + list(extra or []))[:cap]


def _banned_terms() -> tuple[str, ...]:
    """Words from the client's 'Excluded as a core product line' list."""
    import re
    try:
        from . import client as _client
        text = _client.profile().exclusions or ""
    except Exception:
        return ()
    # The list wraps across lines in a real exclusions.md, so read to the SENTENCE end
    # rather than the line end. ProductEVO's wraps immediately before "jewelry", which
    # is how a category they had banned in writing was never read (2026-09-21).
    m = re.search(r"excluded as a core product line\s*:\s*(.+?)\.", text, re.I | re.S)
    if not m:
        return ()
    # The words a category really appears under. "athletic wear" is apparel and contains
    # neither "apparel" nor "fashion", so the literal ban missed it (2026-09-21).
    ALSO = {
        "apparel": ["clothing", "wear", "garment", "footwear", "outerwear", "t-shirt"],
        "fashion": ["boutique", "streetwear"],
        "food": ["beverage", "snack", "grocery", "culinary ingredient"],
        "supplements": ["nutraceutical", "vitamin", "protein powder"],
        "jewelry": ["jewellery", "watch brand"],
        "smart devices": ["iot", "connected device", "smart home"],
    }
    out = []
    for w in re.split(r"[,/]| and ", m.group(1)):
        # strip() with no argument, so a wrapped list does not leave "\njewelry" --
        # which matches nothing and silently drops a category the client banned.
        w = w.strip().strip(" .").lower()
        if len(w) > 2:
            out.append(w)
            if w.endswith("s"):
                out.append(w[:-1])
            out += ALSO.get(w, [])
            out += ALSO.get(w.rstrip("s"), [])
    return tuple(dict.fromkeys(out))


def expand(vertical) -> list[str]:
    """Return the vertical's keywords plus a fan-out. On LLM failure, the seeds.

    `vertical` is duck-typed: any object with name/rationale/industries/keywords.
    """
    seed = list(getattr(vertical, "keywords", None) or [])
    if not settings.concentrate_api_key and not settings.openrouter_api_key \
            and not settings.anthropic_api_key:
        return merge(seed, [])
    # The client's exclusions belong here. These keywords go straight into the BuzzLead
    # TAM search, so a banned category in the list is a banned category in the results.
    # Measured 2026-09-21 on ProductEVO's "Sporting & Outdoor": the expansion returned
    # "apparel brand" and "athletic wear", and apparel is the first thing their
    # exclusions.md rules out.
    excl = ""
    try:
        from . import client as _client
        excl = (_client.profile().exclusions or "")[:1200]
    except Exception:
        pass
    user = (
        f"VERTICAL: {getattr(vertical, 'name', '')}\n"
        f"RATIONALE: {getattr(vertical, 'rationale', '')}\n"
        f"INDUSTRIES: {getattr(vertical, 'industries', [])}\n"
        f"SEED KEYWORDS: {seed}\n"
        + (f"\nTHIS CLIENT NEVER TARGETS THE FOLLOWING. A keyword that would surface "
           f"any of them is wrong, however close to the vertical it sounds:\n{excl}\n"
           if excl else "")
        + "Expand the keyword list now."
    )
    try:
        from .clients import llm
        payload = llm.complete_json(MODEL, SYSTEM, user, TEMPERATURE,
                                    max_tokens=800, provider="precopy")
        extra = payload.get("keywords") if isinstance(payload, dict) else payload
        extra = [str(x) for x in (extra or []) if str(x).strip()]
    except Exception:
        extra = []
    # Belt and braces: the instruction above is a request, this is a filter. A model
    # that drifts back into a banned category costs a whole TAM search.
    banned = _banned_terms()
    if banned:
        extra = [k for k in extra if not any(b in k.lower() for b in banned)]
    return merge(seed, extra)
