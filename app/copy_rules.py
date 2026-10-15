"""Each client's own copy rules, enforced after generation.

Troy, 2026-09-18: "run those six rules. which means any other run should check their
rule and add to the current rules."

app/validator.py holds the rules every client shares: length, no em dashes, no
greeting or sign-off, no markdown, no model preamble, no character count, no refusal,
no unfilled placeholder. All thirteen were written for IDPR and they are the floor.

A client's OWN bans live in clients/<name>/copy_rules.toml and are ADDED to that floor,
never substituted for it. ProductEVO may not name Thule, may not state a tariff
percentage as fact, and may only use seven approved statistics. Nothing checked any of
that: the bans existed inside the prompt, which asks the model to behave but never
verifies that it did. The copy QA incident on 2026-09-15 is what that costs -- 15,159
emails carried a defect that was written into the prompt as forbidden.

Rule kinds, all optional:

    [[banned]]                    a pattern that must never appear
    name = "unapproved client"
    pattern = "\\\\b(Thule|Yakima|Orvis)\\\\b"

    [[banned_unless]]             a pattern allowed only when the research brief says so
    name = "Dynamic Discs outside sporting"
    pattern = "Dynamic Discs"
    unless_context = "VERTICAL:\\\\s*Sporting"

    [approved_claims]             numbers in a FIRST-PERSON claim must be on the list
    near = "\\\\b(we|our|us|ProductEVO)\\\\b"
    allow = ["17 years", "1,000+", ...]

    [reading_level]               plain-language ceiling
    max_grade = 8.0

Reading level is Flesch-Kincaid, computed here rather than pulled from a library: it
is a dozen lines and this repo pins its dependencies deliberately.
"""

from __future__ import annotations

import contextvars
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CLIENTS_DIR = Path(__file__).resolve().parent.parent / "clients"


@dataclass(frozen=True)
class Rules:
    banned: tuple[tuple[str, str], ...] = field(default=())          # (name, pattern)
    banned_unless: tuple[tuple[str, str, str], ...] = field(default=())
    banned_for_variant: tuple[tuple[str, str, tuple[str, ...]], ...] = field(default=())
    no_recipient_name: bool = False
    # (name, pattern, variants): the pattern MUST appear in the email for these arms.
    required_for_variant: tuple[tuple[str, str, tuple[str, ...]], ...] = field(default=())
    # (name, pattern with one capture group holding a list, max words per item, variants)
    list_item_max_words: tuple[tuple[str, str, int, tuple[str, ...]], ...] = field(default=())
    claims_near: str | None = None
    claims_allow: tuple[str, ...] = field(default=())
    max_grade: float | None = None
    # [allow]: things the approved copy REQUIRES that the shared floor would reject.
    allowed_spam_words: tuple[str, ...] = field(default=())
    closers_without_punctuation: tuple[str, ...] = field(default=())

    @property
    def any(self) -> bool:
        return bool(self.banned or self.banned_unless or self.banned_for_variant
                    or self.required_for_variant or self.list_item_max_words
                    or self.no_recipient_name or self.claims_allow
                    or self.max_grade)


@lru_cache(maxsize=8)
def load(client_name: str) -> Rules:
    path = CLIENTS_DIR / client_name / "copy_rules.toml"
    if not path.exists():
        return Rules()
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    claims = cfg.get("approved_claims") or {}
    reading = cfg.get("reading_level") or {}
    return Rules(
        banned=tuple((b.get("name", "banned"), b["pattern"])
                     for b in (cfg.get("banned") or []) if b.get("pattern")),
        banned_unless=tuple((b.get("name", "banned"), b["pattern"], b.get("unless_context", ""))
                            for b in (cfg.get("banned_unless") or []) if b.get("pattern")),
        banned_for_variant=tuple((b.get("name", "banned"), b["pattern"], tuple(b.get("variants") or ()))
                                 for b in (cfg.get("banned_for_variant") or []) if b.get("pattern")),
        no_recipient_name=bool((cfg.get("recipient") or {}).get("no_name_in_body")),
        required_for_variant=tuple((b.get("name", "required"), b["pattern"], tuple(b.get("variants") or ()))
                                   for b in (cfg.get("required_for_variant") or []) if b.get("pattern")),
        list_item_max_words=tuple((b.get("name", "list items too long"), b["pattern"],
                                   int(b.get("max_words", 4)), tuple(b.get("variants") or ()))
                                  for b in (cfg.get("list_item_max_words") or []) if b.get("pattern")),
        claims_near=claims.get("near"),
        claims_allow=tuple(claims.get("allow") or ()),
        max_grade=reading.get("max_grade"),
        allowed_spam_words=tuple(w.lower() for w in ((cfg.get("allow") or {}).get("spam_words") or ())),
        closers_without_punctuation=tuple(w.lower() for w in ((cfg.get("allow") or {}).get("closers_without_punctuation") or ())),
    )


def current() -> Rules:
    """The active client's rules.

    A broken rules file is raised, not swallowed. Returning "no rules" on a parse error
    is the worst outcome available: the run continues, every client-specific ban is
    silently unenforced, and nothing says so. A missing file is fine and means no extra
    rules; a malformed one is a stop.
    """
    from . import client
    return load(client.profile().name)


# --------------------------------------------------------------------------- reading

_VOWELS = "aeiouy"


def _syllables(word: str) -> int:
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return 0
    count, prev_vowel = 0, False
    for ch in word:
        is_vowel = ch in _VOWELS
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def grade_level(text: str) -> float:
    """Flesch-Kincaid grade. Lower is plainer."""
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    words = [w for w in re.findall(r"[A-Za-z']+", text)]
    if not sentences or not words:
        return 0.0
    syl = sum(_syllables(w) for w in words)
    return round(0.39 * (len(words) / len(sentences))
                 + 11.8 * (syl / len(words)) - 15.59, 1)


# --------------------------------------------------------------------------- checks

_NUMBER = re.compile(r"\b\d[\d,.]*\s*(?:%|\+|years?|months?|days?|x|M\+?|k\+?)?\b",
                     re.IGNORECASE)


# What the copy stage is writing right now: which arm, and to whom. Set per contact by
# s5_copy, so a rule can apply to one angle only or look for the recipient's own name.
_variant: contextvars.ContextVar[str | None] = contextvars.ContextVar("copy_variant", default=None)
_first_name: contextvars.ContextVar[str | None] = contextvars.ContextVar("copy_first_name", default=None)


def set_context(variant: str | None, first_name: str | None) -> None:
    _variant.set(variant)
    _first_name.set((first_name or "").strip() or None)


def check(text: str, context: str = "", rules: Rules | None = None) -> list[str]:
    """Violations of the ACTIVE CLIENT's own rules. `context` is the research brief."""
    rules = rules if rules is not None else current()
    if not rules.any or not text:
        return []
    out: list[str] = []

    for name, pattern in rules.banned:
        if re.search(pattern, text, re.IGNORECASE):
            out.append(f"client rule: {name}")

    for name, pattern, unless in rules.banned_unless:
        if re.search(pattern, text, re.IGNORECASE):
            allowed = bool(unless) and bool(re.search(unless, context or "", re.IGNORECASE))
            if not allowed:
                out.append(f"client rule: {name}")

    variant = _variant.get()
    for name, pattern, variants in rules.banned_for_variant:
        if variant in variants and re.search(pattern, text, re.IGNORECASE):
            out.append(f"client rule: {name}")

    for name, pattern, variants in rules.required_for_variant:
        if variant in variants and not re.search(pattern, text, re.IGNORECASE):
            out.append(f"client rule: {name}")

    for name, pattern, max_words, variants in rules.list_item_max_words:
        if variant not in variants:
            continue
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        items = re.split(r",\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+", m.group(1))
        long = [i.strip() for i in items
                if len(re.sub(r"^(a|an|the|some)\s+", "", i.strip(), flags=re.IGNORECASE).split()) > max_words]
        if long:
            out.append(f"client rule: {name} ({long[0][:60]!r})")

    first = _first_name.get()
    if rules.no_recipient_name and first and len(first) >= 3:
        # Addressed BY NAME ("..., Christy." / "Christy, ..."), not a name that is also a word.
        if re.search(rf"(,\s*{re.escape(first)}\b|(^|\n|[.!?]\s+){re.escape(first)},)", text):
            out.append("client rule: addresses the recipient by name in the body")

    if rules.claims_allow and rules.claims_near:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if not re.search(rules.claims_near, sentence, re.IGNORECASE):
                continue
            for m in _NUMBER.finditer(sentence):
                token = m.group(0).strip()
                if not any(ch.isdigit() for ch in token):
                    continue
                if any(token.rstrip(".,").lower() in a.lower() or
                       a.lower() in sentence.lower() for a in rules.claims_allow):
                    continue
                out.append(f"client rule: unapproved statistic {token!r} in a claim "
                           f"about the sender")
                break

    if rules.max_grade is not None:
        g = grade_level(text)
        if g > rules.max_grade:
            out.append(f"client rule: reading level {g} above {rules.max_grade}")

    return out
