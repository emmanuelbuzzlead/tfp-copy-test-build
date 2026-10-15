"""Stage 5 step 10 -- post-generation copy QA.

Every rule here comes from the spec or from the Clay prompts themselves. A violation
triggers regeneration (max COPY_MAX_RETRIES, default 2); a contact that fails twice
goes to the needs_review bucket and is excluded from the batch, never sent as-is.

Two rules are deliberately narrower than a naive reading of the spec:

1. Hype words are ignored inside the prospect's own company or person name. "Solutions"
   is a banned word and also the last word of a great many real company names
   (Incentive Solutions, Selective Search...). A blanket substring ban would send
   every such contact to needs_review forever, and no retry could fix it -- the model
   has to write the company's name.

2. Email 2 is REQUIRED to allow the bullet character. Its Clay prompt mandates
   plain-text bullets, so "no rich text" here means markdown/asterisks/backticks,
   not the literal bullet the prompt asks for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import settings

# Spec section 4 step 10 list, plus the fluff words the Email 2 prompt itself bans.
HYPE_WORDS = (
    "amazing", "game-changing", "game changing", "revolutionary", "innovative",
    "excited", "leverage", "synergy", "strategic", "solutions",
)

# "congrats on the raise" openers, explicitly called out in the spec.
CONGRATS_RX = re.compile(
    r"\b(congrat\w*|congratulations)\b[^.!?\n]{0,40}\b(raise|round|funding|series|seed)\b",
    re.IGNORECASE,
)

GREETING_RX = re.compile(
    r"^\s*(hi|hey|hello|dear|good\s+(morning|afternoon|evening))\b[^\n]{0,40}$",
    re.IGNORECASE | re.MULTILINE,
)

SIGNOFF_RX = re.compile(
    r"^\s*(best|regards|best regards|thanks|thank you|cheers|sincerely|warmly|"
    r"talk soon|all the best)\s*[,.!]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Markdown / rich text. The bullet char is handled separately per email.
MARKDOWN_RX = re.compile(r"(\*\*|__|`|^#{1,6}\s|^\s*[-*]\s+|\[[^\]]+\]\([^)]+\))", re.MULTILINE)

EM_DASH_RX = re.compile(r"[—–]")

# Classic spam triggers, reusing the spam/spintax QA rules from the existing chain.
SPAM_WORDS = (
    "free", "guarantee", "guaranteed", "risk-free", "no obligation", "act now",
    "limited time", "click here", "buy now", "cash", "cheap", "discount", "earn money",
    "100%", "urgent", "winner", "congratulations you", "special promotion",
)

# The model's own bookkeeping, appended after the email: "---\n\nCharacter count: 398".
# 2026-09-15: 15,159 of 18,830 loaded first emails carried it, and 1,946 were SENT with
# it before Troy saw one in EmailBison. The prompt says "under 500 characters" and the
# model reports back. Stripped in normalize(); anything left is a violation.
TRAILING_META_RX = re.compile(
    r"(\s*(-{3,}|_{3,}|\*{3,})\s*)*\s*\(?\s*(charac?ter|char|word|token)s?\s*count\s*[:=]?\s*"
    r"\d{1,5}\s*(chars?|characters|words)?\s*\)?\s*$"
    r"|(\s*(-{3,}|_{3,}|\*{3,})\s*)*\s*\(\s*\d{2,4}\s*(chars?|characters|words)\s*\)\s*$",
    re.IGNORECASE,
)

# The rule line the model draws above its bookkeeping, left behind once the count
# itself is cut. Stripped in normalize(), never a violation on its own.
TRAILING_SEP_RX = re.compile(r"\s*(-{3,}|_{3,}|\*{3,})\s*$")

# A count anywhere in the body, not just in a tail. "Word count: 61" mid-paragraph is
# the same leak as "---\nCharacter count: 398" at the end: the model reporting back on
# the brief inside the email the prospect receives.
COUNT_ANYWHERE_RX = re.compile(
    r"\b(charac?ter|char|word|token)s?\s*count\s*[:=]?\s*\d{1,5}"
    r"|\(\s*\d{2,4}\s*(chars?|characters|words)\s*\)",
    re.IGNORECASE,
)

# Copy that argues the prospect OUT of replying. Not a refusal -- the model writes a
# real email, addressed to a real person, that tells them to ignore it. The message on
# this violation is deliberate: when this fires often for one client it is a TARGETING
# problem, not a wording problem, and the answer is to fix who is being emailed.
DISQUALIFY_RX = re.compile(
    r"\b("
    r"(probably|may|might|likely) not (be )?(a |the )?(good |right )?fit|"
    r"(i'?m|we'?re|that'?s|this is) not (the |a )?(right |good )?(fit|lane|match) for|"
    r"not (the |a )?(right |good )?(fit|lane) for (you|your)|"
    r"(that'?s|this is) not our lane|not our (lane|world|space)|"
    r"we'?d just slow you down|we (can'?t|cannot) help (you|your)|"
    r"(i'?ll|let me) get out of your inbox|"
    r"(send|pass) them my way|if you know (a|any|someone)|"
    r"good luck with|whoever you pick|"
    r"you'?re not (a |the )?(good |right )?fit|"
    r"not (the )?right for (you|your)|"
    r"(feel free to |please )?(ignore|disregard|delete) (this|my|it)|"
    r"if (this|that) (isn'?t|is not) relevant|"
    r"no (need|reason) to (reply|respond)|"
    r"(you can|just) ignore (this|me)|"
    r"sorry (for|to) (the |)(bother|bothering|interrupt)"
    r")",
    re.IGNORECASE,
)

# The model talking ABOUT the job instead of doing it. This is the third distinct shape
# the same failure takes, and the one a phrase blocklist keeps missing, because every
# sentence is ordinary English:
#
#   "BLANCO America is disqualified from outreach. The research brief states it is
#    foreign-owned... There is no email to write here. Remove this prospect from the
#    list... If you have a US-founded brand, I'm ready to write the cold email."
#
# Measured 2026-09-22: that body cleared REFUSAL_RX and DIRECTIVE_RX and was APPROVED
# for sending. It refuses without a refusal word and instructs without a label.
#
# So this rule is not a phrase ban. It keys on VOCABULARY THE PROSPECT NEVER SEES. A
# cold email to Elena at BLANCO does not contain "research brief", "prospect", "cold
# email", "Email 1" or "outreach list" -- those words belong to the briefing, not to
# the message. Any of them means the model is addressing US, whatever it claims.
META_RX = re.compile(
    r"\b("
    r"research brief|the brief states|per the brief|"
    r"cold email|outreach list|prospect list|"
    r"this (prospect|lead)\b|the (prospect|lead) (is|does|has)|"
    r"email (1|2|one|two)\b|"
    r"(is|are) disqualified|disqualified (from|because)|"
    r"does not fit|do not fit|doesn'?t fit|don'?t fit|not a fit for|"
    r"outside (the |our )?(scope|icp|criteria)|"
    r"no email (to write|will be written|is needed)|there is no email|"
    r"remove (this|them|it) from|"
    r"i'?m ready to write|i am ready to write|ready to write the|"
    r"please provide (a|another|me)"
    r")",
    re.IGNORECASE,
)

DIRECTIVE_RX = re.compile(
    r"(^|\n)\s*(disqualify|do not send|don'?t send|skip this (lead|prospect|contact)|"
    r"remove from (the )?(outbound|list|campaign)|action\s*:|reason\s*:|"
    r"recommendation\s*:|verdict\s*:|note to (self|team|the team)|"
    # No trailing \b. A label ends in a colon, and a colon followed by a space is not a
    # word boundary, so "Action: skip this lead" slipped straight through.
    r"flag(ged)? for review|no follow-?up warranted|not a (valid|real) prospect)",
    re.IGNORECASE,
)

REFUSAL_RX = re.compile(
    r"\b("
    # flat refusals
    r"i (can(?:no|')t|won'?t|will not|cannot) (write|draft|create|produce|send|do|help)|"
    r"i'?m (unable|not able) to|i am (unable|not able) to|"
    r"i cannot send an email|i won'?t be able to|"
    r"unable to (write|generate|create|produce) (this|an?|the)|"
    # polite preambles that precede a refusal
    r"i appreciate (the|you|your) (research|test|testing|flag|pushback|clarity|"
    r"correction|context|honesty|candor|candour|question|challenge)|"
    r"i appreciate you (testing|sharing|flagging|pushing)|"
    r"(respectfully )?declin(e|ing) (to )?(write|draft|send)|"
    r"i'?m going to (respectfully )?decline|"
    # meta commentary about the task rather than an email
    r"i need to hold the line|i have to hold the line|"
    r"i don'?t have (an email|the email|enough (information|context|detail))|"
    r"i need the actual email|there'?s nothing (to|here)|"
    r"instead of writing (this|the)|rather than (write|writing|send|sending) (this|it)|"
    # verdicts about fit, which are a refusal wearing a reason
    r"(there'?s|there is) no fit|has no fit|no fit here|not a fit for|"
    r"would be a template blast|"
    r"as an ai\b"
    r")",
    re.IGNORECASE,
)
# Unfilled template slots the model left for a human: "[your geographic/sector focus]",
# "{{first_name}}", "[Company]". 136 loaded on 2026-09-15.
PLACEHOLDER_RX = re.compile(
    r"\[(your|insert|name|company|first|last|geographic|sector|industry|city|date|x{2,})[^\]]*\]"
    r"|\{\{?\s*[a-z_][a-z_ ]*\}?\}|\bTBD\b|\[\.\.\.\]",
    re.IGNORECASE,
)
# Commentary after the email: "Let me know if you'd like...", "This version keeps...".
TRAILING_COMMENTARY_RX = re.compile(
    r"\n\s*\(?(let me know if|hope this (helps|works)|this (email|version|draft) (is|comes|stays|keeps)|"
    r"i(?:'ve| have) kept|note that|feel free to adjust)",
    re.IGNORECASE,
)

MODEL_PREAMBLE_RX = re.compile(
    r"^\s*(here'?s|here is|sure[,!]|certainly[,!]|i'?ve (written|drafted)|below is)\b",
    re.IGNORECASE,
)


@dataclass
class Verdict:
    ok: bool
    violations: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def normalize(text: str) -> str:
    """Fix the mechanical violations instead of paying a model to retry them.

    An em dash or a smart quote is a typographic artifact, not a flaw in what the email
    says. Observed live: the model reproduced em dashes on every retry even when told
    exactly what had been rejected, so regenerating burned three calls per contact and
    still failed. Rewriting the character costs nothing and changes no meaning. Content
    rules -- length, hype words, greetings, sign-offs -- are NOT auto-fixed, because
    those do require the model to say something different.
    """
    out = (text or "").strip()
    # Every em/en dash becomes a comma, spaced or not. Mapping the unspaced case to a
    # hyphen instead produced "PR Newswire-not the tier-1 outlets" and "Healthie's
    # story-AI for dietitians" in live output: it reads as a compound word rather than
    # the break the writer meant. A comma is right in both positions.
    out = re.sub(r"\s*[—–]\s*", ", ", out)
    out = re.sub(r",\s*,", ",", out)          # in case the dash already followed a comma
    out = re.sub(r"\s+([,.!?])", r"\1", out)  # no space before punctuation
    out = out.replace("‘", "'").replace("’", "'")
    out = out.replace("“", '"').replace("”", '"')
    out = out.replace("…", "...")

    # Markdown is formatting, not content, so strip it rather than paying for a rewrite.
    # It was the single largest copy loss: 71 of 91 legal contacts tripped "contains
    # markdown / rich text", each burning up to four LLM calls before being binned. The
    # emphasis markers carry no meaning once removed, and a markdown bullet becomes the
    # plain bullet Email 2's own prompt asks for.
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out, flags=re.DOTALL)   # **bold**
    out = re.sub(r"__(.+?)__", r"\1", out, flags=re.DOTALL)       # __bold__
    out = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", out)  # *italic*
    out = re.sub(r"`([^`]*)`", r"\1", out)                        # `code`
    out = re.sub(r"^#{1,6}\s+", "", out, flags=re.MULTILINE)      # # heading
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)            # [text](url)
    out = re.sub(r"^\s*[-*]\s+", "• ", out, flags=re.MULTILINE)   # - item  ->  • item
    out = re.sub(r"[ \t]{2,}", " ", out)
    # The model's trailing bookkeeping is not part of the email. Cut it, and the rule
    # line above it, however many times they stack.
    for _ in range(3):
        out = TRAILING_META_RX.sub("", out)
        out = TRAILING_SEP_RX.sub("", out)
    return out.strip()



# Press the copy legitimately cites. A prospect written up in Forbes is a personalisation
# WIN, not a competitor mention. Measured 2026-09-22: the first version of this rule sent
# 29 of 40 contacts to needs_review, and the names it objected to were CNET, Forbes,
# TechCrunch, Wired, Fast Company, ProSoundWeb and Mix Magazine.
_MEDIA = {
    "cnet", "forbes", "techcrunch", "wired", "fast company", "engadget", "gizmodo",
    "the verge", "verge", "wsj", "wall street journal", "new york times", "nyt",
    "bloomberg", "reuters", "inc", "entrepreneur", "business insider", "cnbc",
    "prosoundweb", "mix magazine", "home improvement today", "popular mechanics",
    "good housekeeping", "wirecutter", "consumer reports", "gear patrol", "outside",
}
_MEDIA_WORDS = ("magazine", "journal", "review", "reviews", "weekly", "daily", "today",
                "times", "post", "news", "gazette", "digest", "report", "wire", "tv",
                "podcast", "blog", "press")

# Being written about is not being compared. "featured in X", "named by X", "X called it"
# all name a publication, never a competitor.
_COVERAGE_RX = re.compile(
    r"(featur\w+|cover\w+|review\w+|writ\w+|publish\w+|nam\w+|list\w+|rank\w+|"
    r"profil\w+|interview\w+|mention\w+|spotlight\w+|award\w+)\s+(in|by|on|at|for)?\s*$",
    re.IGNORECASE,
)

# Words that look like a brand but are not the prospect's competitor: places the copy
# legitimately names, calendar words, and the client's own vocabulary.
_NOT_A_COMPANY = {
    "asia", "china", "vietnam", "india", "mexico", "taiwan", "thailand", "cambodia",
    "europe", "european", "america", "american", "us", "usa", "u.s.", "d.c.", "dc",
    "washington", "canada", "germany", "german", "italy", "italian", "japan", "korea",
    "colorado", "nashville", "north", "south", "east", "west", "january", "february",
    "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday",
    "productevo", "buzzlead", "interdependence", "idpr", "i", "im", "ive", "id",
    "honestly", "curious", "worth", "every", "but", "if", "so", "and", "the", "here",
    # generic words the first version treated as brands
    "diy", "signs", "sign", "reliable", "quality", "custom", "pro", "plus", "direct",
    "amazon", "walmart", "target", "costco", "home depot", "lowes",   # channels, not rivals
    # Troy, 2026-09-25: 912 of 932 Venture Capital needs_review flags this run were
    # "email 1 names 'VC'/'VCs'/'PE', which is not <the real firm>" -- the model
    # writing "we help VCs land press" (the PROSPECT's own industry, not a rival) and
    # this check reading "VCs" as a competitor's name. "GP"/"LP" are deliberately NOT
    # added here even though they are the same kind of generic finance shorthand: both
    # are also common literal SUFFIXES of real fund names ("Smith Capital LP"), so
    # exempting them would hide a genuine competitor mention instead of a false one.
    "vc", "vcs", "pe",
    # Troy, 2026-09-25: the same bug, once found, turned out to be the single biggest
    # source of needs_review across every vertical -- 2,439 "which is not" flags
    # all-time, and these generic INDUSTRY terms (not a specific rival) accounted for
    # nearly all of them: EdTech alone was 207. Verified each is the prospect's own
    # industry, not a company, before adding -- a real competitor mention
    # ("Curi Bio", "Wild Bioscience", "LPL Financial") stays caught.
    "edtech", "cdmo", "cdmos", "b2b", "cros", "medtech", "rias", "dme", "saas", "ai",
    "oem", "oems", "o&p", "ceo", "m&a", "r&d", "l&d", "etf", "otcqb",
    "managing director",
}

# Where a competitor actually shows up: as the thing being helped, worked with, or
# switched to. NOT "like X" or "for X" -- those catch press and product lines, which is
# how the first version rejected three quarters of a real run. A licensing partner or a
# magazine named in passing is the prospect's own context and must survive.
_COMPANY_SLOT_RX = re.compile(
    r"\b(help|helping|helped|work(?:ed|ing)? with|partner(?:ed|ing)? with|"
    r"switch(?:ed|ing)? (?:to|from)|mov(?:e|ed|ing) (?:to|from)|"
    r"source (?:from|through)|instead of|rather than|over at)\s+"
    r"([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,2})",
)


_LEGAL_SUFFIX = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "group",
                 "holdings", "international", "usa", "america", "brands", "industries"}


def _own_tokens(company_name: str | None, company_domain: str | None) -> set[str]:
    """Every way this prospect's own name can legitimately be written.

    Measured 2026-09-22: the first version flagged 'SeaVee' at Sea Vee Boats, 'S&W' at
    S&W Manufacturing, 'Plast-O-Matic' at Plastomatic and 'SFI' at Stainless Fabricators
    Inc -- every one of them the prospect's OWN name, just spaced, hyphenated or
    abbreviated differently. Eight of thirty-five blocks were this, so the squashed form
    and the acronym both have to count as the company itself.
    """
    own: set[str] = set()
    name = company_name or ""
    words = re.findall(r"[A-Za-z0-9&]+", name)
    for token in (w.lower() for w in words):
        own.add(token)
        own.add(token.rstrip("s"))          # "Sign" must match "Signs"
    squashed = "".join(w.lower() for w in words)
    if squashed:
        own.add(squashed)
        # without the legal tail: "swmanufacturingcompanyinc" -> "swmanufacturing"
        core = [w.lower() for w in words if w.lower() not in _LEGAL_SUFFIX]
        if core:
            own.add("".join(core))
            # The acronym real people use. Which words count differs by company --
            # Stainless Fabricators Inc is SFI (legal tail dropped) but Accurate
            # Manufactured Products Group is AMPG (Group kept, Inc dropped) -- so build
            # one for EVERY trailing trim, not just the fully stripped form.
            low_words = [w.lower() for w in words]
            for cut in range(len(low_words), 1, -1):
                part = low_words[:cut]
                if len(part) > 1:
                    own.add("".join(w[0] for w in part))
                if part[-1] not in _LEGAL_SUFFIX:
                    break
    root = re.sub(r"^www\.", "", (company_domain or "").lower()).split(".")[0]
    if root:
        own.add(root)
        own.update(t for t in re.findall(r"[a-z]{4,}", root))
    return own


def _is_own(cand_words: list[str], own: set[str]) -> bool:
    """Is this candidate just the prospect's name in another shape?"""
    low = [w.lower() for w in cand_words]
    if any(w in own or w.rstrip("s") in own for w in low):
        return True
    squashed = "".join(low)
    if not squashed:
        return False
    if squashed in own or squashed.rstrip("s") in own:
        return True
    # "SeaVee" against "Sea Vee Boats": the brand people actually say is the first part
    # of the registered name. Five characters keeps this from matching on a shared
    # syllable -- "Pro" or "Tech" would otherwise excuse a genuine competitor.
    return any(len(o) >= 5 and (o.startswith(squashed) or squashed.startswith(o))
               for o in own if len(squashed) >= 5)


def wrong_company(text: str, company_name: str | None,
                  company_domain: str | None) -> str | None:
    """The brand this email offers to help, when it is not the prospect's.

    Returns the offending name, or None. Deliberately narrow: it fires only where a
    competitor actually appears -- "help ROGUE", "work with Acme", "instead of Brand X".
    Press coverage, channels and the prospect's own product lines are not competitors,
    and rejecting them costs real contacts for nothing.
    """
    if not text:
        return None
    own = _own_tokens(company_name, company_domain)
    for m in _COMPANY_SLOT_RX.finditer(text):
        cand = m.group(2).strip(" .,'")
        words = re.findall(r"[A-Za-z0-9&]+", cand)
        if not words:
            continue
        low = [w.lower() for w in words]
        joined = " ".join(low)
        if joined in _MEDIA or any(w in _MEDIA for w in low):
            continue
        if any(w in _MEDIA_WORDS for w in low):
            continue
        if joined in _NOT_A_COMPANY or any(w in _NOT_A_COMPANY for w in low):
            continue
        if _is_own(words, own):
            continue
        # "featured in Forbes" names a publication, not a rival.
        if _COVERAGE_RX.search(text[:m.start()][-60:]):
            continue
        # a lone capitalised word right after a sentence end is the next sentence
        if len(words) == 1 and text[:m.start(2)].rstrip().endswith((".", "?", "!")):
            continue
        return cand
    return None

def _strip_protected(text: str, protected: tuple[str, ...]) -> str:
    """Blank out the prospect's own names so their words don't trip word bans."""
    out = text
    for term in protected:
        term = (term or "").strip()
        if len(term) < 3:
            continue
        out = re.sub(re.escape(term), " ", out, flags=re.IGNORECASE)
    return out


def _common_checks(text: str, protected: tuple[str, ...],
                   context: str = "") -> list[str]:
    v: list[str] = []
    scrubbed = _strip_protected(text, protected)

    if EM_DASH_RX.search(text):
        v.append("contains an em/en dash")
    if GREETING_RX.search(text):
        v.append("contains a greeting line")
    if SIGNOFF_RX.search(text):
        v.append("contains a sign-off line")
    if MODEL_PREAMBLE_RX.search(text):
        v.append("starts with a model preamble instead of the email body")
    if CONGRATS_RX.search(text):
        v.append("uses a 'congrats on the raise' opener")
    if COUNT_ANYWHERE_RX.search(text):
        v.append("contains a character/word count")
    if REFUSAL_RX.search(text):
        v.append("model refused to write the email")
    if DIRECTIVE_RX.search(text):
        v.append("the body is an instruction to us, not an email to the prospect")
    if META_RX.search(text):
        v.append("the body discusses the assignment instead of writing to the prospect")
    if DISQUALIFY_RX.search(text):
        v.append("the email talks the prospect out of it; the audience is probably "
                 "wrong for this client, not the wording")
    if PLACEHOLDER_RX.search(scrubbed):
        v.append("contains an unfilled placeholder")
    if TRAILING_COMMENTARY_RX.search(text):
        v.append("contains commentary after the email")

    for word in HYPE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", scrubbed, re.IGNORECASE):
            v.append(f"hype word: {word!r}")
    try:
        from . import copy_rules as _cr
        _allowed = set(_cr.current().allowed_spam_words)
    except Exception:
        _allowed = set()
    for word in SPAM_WORDS:
        if word in _allowed:
            continue
        if re.search(rf"\b{re.escape(word)}\b", scrubbed, re.IGNORECASE):
            v.append(f"spam word: {word!r}")

    # The active client's OWN bans, on top of everything above -- never instead of it.
    # `context` is the research brief, which is what lets a conditional rule ask "is
    # this prospect actually in the vertical that permits naming that client?".
    try:
        from . import copy_rules
        v += copy_rules.check(text, context)
    except Exception as exc:
        # A broken rules file must be loud: silently skipping a client's bans is how
        # an unapproved claim reaches a prospect.
        v.append(f"client rules could not be applied: {str(exc)[:120]}")
    return v


def _looks_cut_off(text: str) -> bool:
    """A completion that stopped mid-sentence: the last character isn't sentence-ending
    punctuation or a closing quote.

    Found 2026-09-27: two of nine TARIFF samples came back at 185/191 characters, ending
    mid-word ("...tired of gambling margins on polit"), with no CTA -- and no existing
    rule caught it, because both were well under the length CEILING and there is no
    floor. Neither a provider outage nor a length violation, so nothing upstream would
    have retried it; it would have shipped as written. Punctuation, not length, is the
    reliable signal: real short copy in this codebase's own test fixtures runs as low as
    56 characters and always ends in a real sentence.
    """
    tail = text.rstrip()
    if tail[-1:] in ".!?\"'":
        return False
    # An approved closer that carries no final punctuation (ProQ's Email 1 ends 'Let me know').
    try:
        from . import copy_rules as _cr
        return not any(tail.lower().endswith(c) for c in _cr.current().closers_without_punctuation)
    except Exception:
        return True


def _min_chars(which: int) -> int | None:
    try:
        from . import client
        return getattr(client.profile(), f"email{which}_min_chars", None)
    except Exception:
        return None


def _below_min(text: str, which: int) -> bool:
    lo = _min_chars(which)
    return bool(lo) and len(text) < lo


def required_paragraphs() -> int | None:
    """The exact paragraph count the active client's emails must have, or None (no rule)."""
    try:
        from . import client
        n = getattr(client.profile(), "email_paragraphs", None)
        return int(n) if n else None
    except Exception:
        return None


def _paragraph_violation(text: str) -> str | None:
    """Emmanuel, 2026-10-08: every ProQ / MasterNow email 1 and email 2 has exactly two
    paragraphs, separated by one blank line. A closer on its own line ("Let me know.") is
    a paragraph, so it has to be folded into the one before it."""
    want = required_paragraphs()
    if not want:
        return None
    got = len([p for p in re.split(r"\n[ \t]*\n", text.strip()) if p.strip()])
    if got != want:
        return (f"has {got} paragraph{'s' if got != 1 else ''}; it must be exactly {want}, "
                f"separated by one blank line")
    return None


def validate_email1(text: str, protected: tuple[str, ...] = (),
                    context: str = "") -> Verdict:
    text = (text or "").strip()
    v: list[str] = []
    if not text:
        return Verdict(False, ["empty"])
    if len(text) > settings.email1_max_chars:
        v.append(f"{len(text)} chars > {settings.email1_max_chars} limit")
    elif _below_min(text, 1):
        v.append(f"{len(text)} chars < the {_min_chars(1)} minimum; write a fuller email")
    elif _looks_cut_off(text):
        v.append("looks cut off mid-sentence (too short or no closing punctuation)")
    if MARKDOWN_RX.search(text):
        v.append("contains markdown / rich text")
    if "•" in text:
        # Email 1's structure is prose; bullets are an Email 2 device.
        v.append("email 1 should not use bullets")
    _pv = _paragraph_violation(text)
    if _pv:
        v.append(_pv)
    v += _common_checks(text, protected, context)
    return Verdict(not v, v)


def validate_email2(text: str, protected: tuple[str, ...] = (),
                    context: str = "") -> Verdict:
    text = (text or "").strip()
    v: list[str] = []
    if not text:
        return Verdict(False, ["empty"])
    if len(text) > settings.email2_max_chars:
        v.append(f"{len(text)} chars > {settings.email2_max_chars} limit")
    elif _below_min(text, 2):
        v.append(f"{len(text)} chars < the {_min_chars(2)} minimum; write a fuller email")
    elif _looks_cut_off(text):
        v.append("looks cut off mid-sentence (too short or no closing punctuation)")
    # Its prompt mandates plain-text bullets, so only ban true markdown here.
    if re.search(r"(\*\*|__|`|^#{1,6}\s|\[[^\]]+\]\([^)]+\))", text, re.MULTILINE):
        v.append("contains markdown / rich text")
    _pv = _paragraph_violation(text)
    if _pv:
        v.append(_pv)
    v += _common_checks(text, protected, context)
    return Verdict(not v, v)


def research_is_usable(report: str) -> bool:
    """Spec: empty or all-blank research -> DROP the contact, never send generic copy.

    The Clay research prompt is a bracketed template, so a model that found nothing
    tends to echo the skeleton back with its [placeholders] intact. That is an empty
    report wearing a costume, and it must not reach the copy stage.
    """
    text = (report or "").strip()
    if len(text) < 200:
        return False
    # Strip the template's own furniture and see whether anything real is left.
    body = re.sub(r"\[[^\]]{0,120}\]", "", text)          # unfilled [placeholders]
    body = re.sub(r"[#*>\-✅⚠️❌\U0001F4C4|]", "", body)
    body = re.sub(r"\b(TRIGGER EVENTS|PERSONALIZATION HOOKS|Company Overview|"
                  r"Media Presence Gap|LinkedIn Signal|Qualifier Status|"
                  r"Prospect Research Report|Company|Size|Stage|N/?A|unknown|none found)\b",
                  "", body, flags=re.IGNORECASE)
    return len(re.sub(r"\s+", " ", body).strip()) >= 150
