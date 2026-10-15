"""TypeSafe System One (Jev) -- the enforcement gate as a typed judgment.

WHY THIS EXISTS. The Haiku gate in stages/s3_companies.py asks a generative model to
emit {"decision": "KEEP"|"IDPR"|"DROP"} and then parses the JSON back out. Three costs
come with that shape, and none of them are the token bill:

  1. The threshold lives inside the prompt. ENFORCE_SYSTEM spends roughly half its
     3,632 characters talking the model out of being too strict ("MISSING DATA IS NOT
     A FAILURE", "dropping everything unfamiliar is how a 1,187-company vertical became
     24"). That is calibration by paragraph, and it is not tunable without a re-run.
  2. The reply can be malformed, truncated or short a row. clients/llm.py carries a
     regex, a fence-stripper and a truncation hint for exactly that.
  3. The fallback judge disagrees. Measured 2026-09-13, gemini-2.5-flash agreed with
     haiku on keep-vs-drop 83% of the time and kept ~12% more.

Jev returns a probability distribution instead of a verdict. THE THRESHOLD MOVES INTO
CODE (see decide()), the output cannot be malformed, and re-tuning the cutoff costs
nothing because the judgment is cached evidence rather than a re-run.

Raw HTTP on purpose. There is an official `typesafe-sdk` package, but this module goes
through clients/http.request like every other vendor here, so it inherits the existing
retry/Retry-After/VendorOutage behaviour and adds no pinned dependency.

Verified against docs.typesafe.ai/api.md on 2026-09-17. NOT yet verified against the
live endpoint -- see scripts/shadow_enforce.py, which is the thing that earns it.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import settings
from .http import VendorError, request

# The shared default is a 90s read timeout and 4 attempts, sized for a generative model
# writing 8,000 tokens. A Jev judgment returns in about a second, so one that has not
# answered in 20 has failed, and waiting out 4 x 90s makes a whole run look hung.
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# Deliberately shorter than ENFORCE_SYSTEM. Everything that prompt says about missing
# data, sparse records and not dropping the unfamiliar is coaching aimed at a generative
# model's failure modes. A calibrated probability plus a code-owned cutoff replaces it:
# an uncertain company lands mid-distribution instead of being argued into a verdict.
VERDICT_CRITERIA = {
    "KEEP": "An operator in the stated vertical: it does this work itself, for its own "
            "clients. A substantial named practice area, product line or client base "
            "counts. Exclusive specialisation is NOT required.",
    "IDPR": "Not an operator in this vertical, but still a plausible US buyer of public "
            "relations: in the employee range, consumer- or B2B-facing, would want press.",
    "DROP": "A supplier, distributor, reseller, contract or private-label manufacturer, "
            "services vendor to the vertical, MLM, nonprofit, government contractor or "
            "staffing firm -- or plainly in a different business.",
}


def configured() -> bool:
    return bool(settings.typesafe_api_key)


def _post(state: Any, questions: dict[str, Any]) -> dict:
    if not settings.typesafe_api_key:
        raise VendorError("typesafe", None, "TYPESAFE_API_KEY is not set")
    data = request(
        "typesafe", "POST", URL,
        headers={"Authorization": f"Bearer {settings.typesafe_api_key}",
                 "Content-Type": "application/json"},
        json_body={"state": state, "model": MODEL, "questions": questions},
        timeout=TIMEOUT, max_attempts=2,
    )
    if not isinstance(data, dict) or "answers" not in data:
        raise VendorError("typesafe", None, f"unexpected reply: {str(data)[:300]}")
    return data


def judge(vertical, company: dict) -> dict:
    """One company, one request. Returns the RAW judgment -- no verdict applied.

    Both questions are asked together: they are independent judgments over the same
    state, so they run in one pass and cannot see each other's answers.

    Batching several companies into one request is a wash. Each question carries its own
    criteria, so twenty companies means twenty copies of VERDICT_CRITERIA -- more input
    tokens than twenty separate requests, and a single malformed company poisons the lot.
    """
    state = {
        "vertical": _no_funding(vertical.name),
        "what_it_means": vertical.rationale,
        "employee_range": f"{vertical.employee_min}-{vertical.employee_max}",
        "disqualifiers": vertical.disqualifiers,
        "company": {
            "domain": company.get("domain"),
            "name": company.get("name"),
            # Same 250 as the Haiku gate. Descriptions are ~60% of input tokens and the
            # decision turns on the first sentence or two.
            "description": (company.get("description") or "")[:250],
            "employees": company.get("employee_count") or company.get("staff_total"),
            "location": company.get("location") or company.get("country"),
            "industry": company.get("industry"),
        },
    }
    questions = {
        "verdict": {
            "type": "choice",
            "instructions": (
                "Decide whether this company belongs in the outreach list for the stated "
                "vertical. Judge only on what the record shows. A blank or missing field "
                "is not evidence against the company: the upstream search already filtered "
                "on United States location and on the employee range."
            ),
            "criteria": VERDICT_CRITERIA,
        },
        "is_operator": {
            "type": "noul",
            "instructions": (
                "Does the record show this company performs work in the stated vertical "
                "itself, rather than supplying, reselling or serving companies that do?"
            ),
            "criteria": {"true": "It operates in the vertical directly",
                         "false": "It only supplies, resells or serves that vertical"},
        },
    }
    answers = _post(state, questions)["answers"]
    verdict = answers.get("verdict", {})
    return {
        "choice": verdict.get("choice"),
        "probabilities": verdict.get("probabilities") or {},
        "confidence": verdict.get("confidence"),
        "is_operator": (answers.get("is_operator") or {}).get("noul"),
    }


# --- policy, kept separate from the judgment on purpose ----------------------------
# These are the numbers to tune against real data. Changing one does NOT require a new
# inference run: the judgment above is evidence, this is the reading of it. Start here,
# then move them once shadow_enforce.py has a few thousand rows to argue from.
KEEP_AT = 0.55          # P(KEEP) at or above this -> KEEP
IDPR_AT = 0.40          # else P(KEEP)+P(IDPR) at or above this -> IDPR (second tier)
# 0.50, not the 0.35 first written. Measured against the live endpoint 2026-09-17: a
# law firm with a BLANK description and no employee count came back KEEP p=0.66 at
# confidence 0.49, i.e. a real spread (0.66/0.24/0.10) that 0.35 would have waved
# through as a confident keep. docs.typesafe.ai/confidence.md puts the route-to-human
# line at ~0.5 and the probe agrees. Saturated cases are nowhere near this: the four
# unambiguous companies scored 0.81-1.00.
REVIEW_BELOW = 0.50     # confidence under this -> park it, do not spend enrichment


def decide(judgment: dict) -> tuple[str, str]:
    """Turn a distribution into the verdict the pipeline already understands.

    Returns (decision, reason). REVIEW is a fourth outcome the Haiku gate cannot express:
    it means the evidence was too thin to spend a QuickEnrich credit on, which is a
    different thing from "this company does not fit".
    """
    p = judgment.get("probabilities") or {}
    conf = judgment.get("confidence")
    keep, idpr = float(p.get("KEEP", 0.0)), float(p.get("IDPR", 0.0))
    if conf is not None and float(conf) < REVIEW_BELOW:
        return "REVIEW", f"low confidence {float(conf):.2f}"
    if keep >= KEEP_AT:
        return "KEEP", f"operator p={keep:.2f}"
    if keep + idpr >= IDPR_AT:
        return "IDPR", f"buyer p={keep + idpr:.2f}"
    return "DROP", f"keep p={keep:.2f}"


def _no_funding(name: str) -> str:
    """Mirror of s3_companies._no_funding -- verticals minted before 2026-09-13 are still
    NAMED "Funded X" though funding stopped being a criterion."""
    import re
    out = re.sub(r"^\s*(funded|vc[- ]backed|venture[- ]backed|series [a-d]\+?)\s+",
                 "", name, flags=re.I).strip()
    return out or name
