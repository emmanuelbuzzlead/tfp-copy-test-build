#!/usr/bin/env python3
"""Shared company-name cleaner for TFP copy.

Fixes two bugs found in the live logistics copy:
  1. The old cleaner only stripped period-terminated suffixes (" Inc." yes,
     " Inc" no), so "ALG Worldwide Logistics Inc" shipped as-is.
  2. Only one suffix was stripped per name, so stacked suffixes survived.

Then shortens to <= 3 words by dropping TRAILING generic descriptors, rather
than blind truncation (which would turn "Steel and Pipe Supply" into the
awkward "Steel and Pipe"). Never drops below 2 words.
"""
import re

# matched case-insensitively, with or without trailing period, with or
# without a preceding comma
SUFFIXES = [
    "inc", "incorporated", "llc", "l.l.c", "lllp", "llp", "lp", "l.p",
    "ltd", "limited", "corp", "corporation", "co", "company", "plc", "pc",
    "gmbh", "ag", "kg", "kgaa", "mbh", "oy", "ab", "as", "spa", "bhd",
    "nv", "bv", "sa", "srl", "pty", "pte", "ulc",
]
# dropped from the END only, to get within the word cap
TRAILING_GENERIC = [
    "logistics", "logistic", "transport", "transportation", "freight",
    "services", "service", "systems", "system", "solutions", "solution",
    "group", "holdings", "holding", "enterprises", "enterprise",
    "international", "worldwide", "industries", "industrial", "distribution",
    "distributors", "distributor", "supply", "supplies", "line", "lines",
    "express", "carriers", "carrier", "trucking", "motor", "usa", "america",
    "north", "global", "partners", "associates", "corporation",
]
MAX_WORDS = 3
MIN_WORDS = 2

_SUFFIX_RE = re.compile(
    r"[,\s]+(?:" + "|".join(re.escape(s) for s in SUFFIXES) + r")\.?\s*$",
    re.IGNORECASE,
)
_TLD_RE = re.compile(r"\.(com|net|org|biz|io|co|us|ca)$", re.IGNORECASE)

# ", A PFG Company" / ". a FIDELITONE company" / " dba Remar Supply" --
# appositives and trade names, never part of the legal name. The separator can
# be a comma OR a period ("Remar, Inc. a FIDELITONE company").
_APPOSITIVE_RE = re.compile(r"[,.]\s+an?\s+.*$", re.IGNORECASE)
_DBA_RE = re.compile(r"\s+(?:dba|d/b/a|f/k/a|fka|aka)\b.*$", re.IGNORECASE)
# "Canada Post / Postes Canada" -- bilingual or alias halves joined by a slash
_ALIAS_RE = re.compile(r"\s*[/|]\s*.*$")

# Names the 3-word cap mangles, where the real-world short form is well known.
OVERRIDES = {
    "united states postal service": "USPS",
    "united states postal service office of inspector general": "USPS",
    "the port authority of new york & new jersey": "Port Authority",
    "the port authority of new york and new jersey": "Port Authority",
    "the david j. joseph company": "David J. Joseph",
}


def clean_company_name(name: str) -> str:
    c = (name or "").strip()
    if not c:
        return ""

    ov = OVERRIDES.get(c.lower().strip(" .,"))
    if ov:
        return ov

    # drop alias halves and appositives before anything else, so the word cap
    # never spends its budget on them ("Vistar, A PFG Company" -> "Vistar")
    c = _ALIAS_RE.sub("", c).strip()
    c = _DBA_RE.sub("", c).strip()
    c = _APPOSITIVE_RE.sub("", c).strip()

    # strip domain-style tails and legal suffixes repeatedly (handles stacking
    # like "KWS Manufacturing Company, LLC")
    for _ in range(6):
        prev = c
        c = _TLD_RE.sub("", c).strip()
        c = _SUFFIX_RE.sub("", c).strip()
        c = c.rstrip(" ,.&-")
        if c == prev:
            break

    # first pass: drop trailing generic descriptors (keeps the meaningful part)
    words = c.split()
    while len(words) > MAX_WORDS and len(words) > MIN_WORDS:
        if words[-1].lower().strip(",.&") in TRAILING_GENERIC:
            words.pop()
        else:
            break

    # hard cap: 3 words max, no exceptions. Source data sometimes concatenates
    # two company names ("Oklahoma Tank Lines United Petroleum Transports"),
    # which generic-dropping alone can't fix.
    truncated = len(words) > MAX_WORDS
    words = words[:MAX_WORDS]

    # The hard cap can RE-EXPOSE a suffix that was mid-string and therefore
    # invisible to the end-anchored strip above:
    #   "Schnellecke Logistics AG & Co. KG"  -> cap -> "Schnellecke Logistics AG"
    #   "Mover Services, Inc. - Interstate Agent for Wheaton" -> "Mover Services, Inc"
    # so strip again now that a new word is final. Never go below MIN_WORDS.
    while len(words) > MIN_WORDS and words[-1].lower().strip(",.") in {
            s.lower() for s in SUFFIXES}:
        words.pop()

    # The cap can strand a bare initial ("The David J. Joseph Company" ->
    # "The David J"), which always reads as a cut-off name.
    #
    # Deliberately NOT also dropping trailing generics here. It cuts both ways:
    # it would improve "Arrow ECS North" -> "Arrow ECS" but wreck
    # "Oklahoma Tank Lines" -> "Oklahoma Tank", where "Lines" is part of the
    # real company name. A slightly long-looking name beats a wrong one.
    if truncated:
        while len(words) > MIN_WORDS and len(words[-1].strip(",.")) <= 1:
            words.pop()

    # never end on a dangling connector ("Smith and" -> "Smith")
    CONNECTORS = ("and", "of", "the", "for", "&", "-")
    while len(words) > 1 and words[-1].lower().strip(",.&") in CONNECTORS:
        words.pop()

    # Final suffix sweep. Unlike the earlier passes this may go down to a
    # single word: a name ending in a legal suffix is always wrong, and
    # "Remar, Inc" is worse than "Remar".
    while len(words) > 1 and words[-1].lower().strip(",.") in {
            s.lower() for s in SUFFIXES}:
        words.pop()

    c = " ".join(words).rstrip(" ,.&-")
    # a comma can survive on the now-final word ("Mover Services," -> "Mover Services")
    c = c.rstrip(" ,.&-")

    # truncation can leave an unclosed bracket: "CCLS (Canada Cartage"
    if c.count("(") != c.count(")"):
        c = c.split("(")[0].strip().rstrip(" ,.&-")
    if c.count("[") != c.count("]"):
        c = c.split("[")[0].strip().rstrip(" ,.&-")

    # ALL-CAPS source names read as shouting in copy -- but leave single-word
    # acronyms alone ("CCLS" must not become "Ccls")
    letters = [ch for ch in c if ch.isalpha()]
    if (" " in c and letters and all(ch.isupper() for ch in letters)
            and len(letters) > 3):
        c = c.title()

    return c or (name or "").strip()
