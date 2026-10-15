"""Loader for the Clay-ported prompt files in prompts/.

Troy's handoff note: these are raw multiline text, NOT JSON. Do not json.loads them.
Each file is:

    SOURCE: <provenance line>
    model: <openrouter model id>
    temperature: <float>
    max_tokens: <int>          # optional
    SYSTEM PROMPT (verbatim):
    ---
    <...system prompt, may contain --- rules of its own...>
    ---
    USER MESSAGE TEMPLATE...:
    <...template with {slot} placeholders...>

The system prompt is fenced by a line that is exactly `---` after the
"SYSTEM PROMPT (verbatim):" header, and closed by the LAST bare `---` before the
USER MESSAGE header. Matching the last one matters: every one of these prompts uses
`---` internally as a section rule, so a first-match parse truncates the prompt to
its first paragraph and silently ships a lobotomised system prompt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _dir(variant: str | None = None) -> Path:
    """The active client's prompt folder. PROMPTS_DIR is the single-client fallback.

    With no variant named, a client whose copy test has CONCLUDED uses the winning
    arm's prompts. That is what makes a promotion mean anything: every ordinary run
    after the test quietly writes the copy that won, with no file moved and no redeploy.
    """
    try:
        from . import client
        profile = client.profile()
        if variant is None:
            try:
                from . import db
                won = db.active_variant(profile.name)
            except Exception:
                won = None
            if won:
                variant = won
        return profile.prompts_for(variant)
    except Exception:
        return PROMPTS_DIR

_SYSTEM_HEADER = re.compile(r"^SYSTEM PROMPT \(verbatim\):\s*$", re.MULTILINE)
_USER_HEADER = re.compile(r"^USER MESSAGE TEMPLATE.*?:\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Prompt:
    name: str
    model: str
    temperature: float
    max_tokens: int | None
    system: str
    user_template: str

    def render_user(self, **slots: str) -> str:
        """Fill the {slot} placeholders in the user template.

        The templates carry annotated slots like
        `{idpr_overview — from assets/idpr_overview.md}`; the annotation is part of
        the placeholder text in the file, so match on the leading identifier only.
        """
        # Check the TEMPLATE for unfilled slots, never the rendered output. The
        # check used to run on the output, which by then contains the scraped
        # website text and the vendor JSON that were just substituted in -- so a
        # prospect whose site carried a brace token anywhere in it failed the whole
        # email. ProductEVO, 2026-09-20: nuclein.com's page held "{title" and
        # "{vendor_count", and every one of the forty sample emails died reporting
        # slots that its own prompt does not contain and never did.
        declared = set(re.findall(r"\{([a-z_0-9]+)(?:\s*[—-][^}]*)?\}",
                                  self.user_template))
        missing = sorted(declared - set(slots))
        if missing:
            raise KeyError(f"{self.name}: unfilled slots {missing}")

        out = self.user_template
        for key, value in slots.items():
            pattern = re.compile(r"\{" + re.escape(key) + r"(?:\s*[—-][^}]*)?\}")
            out, _n = pattern.subn(lambda _m: value or "", out)
            # A slot the template does not declare is simply not used. The copy stage offers every
            # slot it has (company_name, job_title, client_overview...) and each prompt takes what it needs.
        return out


def _scalar(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _leading_number(value: str | None) -> float | None:
    """`max_tokens: 300  (original Clay value was 150; raised per spec)` -> 300."""
    if value is None:
        return None
    m = re.match(r"\s*([0-9]*\.?[0-9]+)", value)
    return float(m.group(1)) if m else None


def _resolved_variant(variant: str | None) -> str | None:
    """The arm in force: the one asked for, else whichever a concluded test promoted."""
    if variant is not None:
        return variant
    try:
        from . import client, db
        return db.active_variant(client.profile().name)
    except Exception:
        return None


def load(filename: str, variant: str | None = None) -> Prompt:
    # Per FILE, so an arm that only replaces email 1 inherits the rest.
    try:
        from . import client
        path = client.profile().prompt_file(filename, _resolved_variant(variant))
    except Exception:
        path = _dir(variant) / filename
    text = path.read_text(encoding="utf-8")

    sys_header = _SYSTEM_HEADER.search(text)
    if not sys_header:
        raise ValueError(f"{filename}: no 'SYSTEM PROMPT (verbatim):' header")
    user_header = _USER_HEADER.search(text, sys_header.end())
    if not user_header:
        raise ValueError(f"{filename}: no 'USER MESSAGE TEMPLATE' header")

    body = text[sys_header.end():user_header.start()]
    fences = [m for m in re.finditer(r"^---\s*$", body, re.MULTILINE)]
    if len(fences) < 2:
        raise ValueError(f"{filename}: system prompt is not fenced by two '---' lines")
    system = body[fences[0].end():fences[-1].start()].strip("\n")
    if not system.strip():
        raise ValueError(f"{filename}: extracted system prompt is empty")

    model = _scalar(text, "model")
    temperature = _leading_number(_scalar(text, "temperature"))
    if not model or temperature is None:
        raise ValueError(f"{filename}: missing model or temperature")
    max_tokens = _leading_number(_scalar(text, "max_tokens"))

    user_template = text[user_header.end():].strip("\n")
    if not user_template.strip():
        raise ValueError(f"{filename}: empty user message template")

    return Prompt(
        name=filename,
        model=model,
        temperature=float(temperature),
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        system=system,
        user_template=user_template,
    )


def research(variant: str | None = None) -> Prompt:
    return load("prompt_research.txt", variant)


def email1(variant: str | None = None) -> Prompt:
    return load("prompt_email1.txt", variant)


def email2(variant: str | None = None) -> Prompt:
    return load("prompt_email2.txt", variant)


_FILES = ("prompt_research.txt", "prompt_email1.txt", "prompt_email2.txt")
_version_cache: dict[str, str] = {}


def version(variant: str | None = None) -> str:
    """A short fingerprint of the three prompt files as they are right now.

    Stamped on every contact whose copy they wrote. Two batches with different
    fingerprints were written by different prompts, so their reply rates can be
    compared; two with the same fingerprint were not, however much else changed.
    Hash of the file text, so an edit to one word changes it and a redeploy does not.
    """
    key = str(_dir(variant))
    if key not in _version_cache:
        h = hashlib.sha1()
        for name in _FILES:
            f = _dir(variant) / name
            if f.exists():
                h.update(f.read_bytes())
        _version_cache[key] = h.hexdigest()[:10]
    return _version_cache[key]
