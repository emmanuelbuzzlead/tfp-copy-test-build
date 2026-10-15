"""Who this deployment is running for.

Troy, 2026-09-18: the engine should run for ProductEVO as well as IDPR. Everything the
pipeline does -- read performance, propose audiences, source companies, find people,
write copy, gate, push, judge -- is client-agnostic. What is NOT agnostic is a short
list of facts about the client, and until now those were scattered across seven files
as constants and hardcoded paths.

They now live in one folder per client:

    clients/<name>/
        client.toml         numbers and account ids
        pitch.md            what the client sells; both email prompts consume it
        icp.md              who to target
        exclusions.md       who never to target
        titles.txt          buyer job titles, best first, one per line
        do_not_contact.txt  their customers, partners, themselves
        prompts/            prompt_research.txt, prompt_email1.txt, prompt_email2.txt

`CLIENT=productevo` selects one. Nothing else in the pipeline changes.

A missing file falls back to the original single-client location, so a half-written
profile degrades to IDPR's rather than crashing a run. `missing()` reports what fell
back, and the runner refuses to send for a client whose profile is incomplete --
silently emailing on behalf of the wrong client is the failure this must never have.

One client per deployment. Two clients sharing a database would share the suppression
list and the 90-day recency index, which is how one client's prospects get suppressed
by another's sending.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = ROOT / "clients"

# Where each piece lived before profiles existed. Used when a profile omits a file.
LEGACY = {
    "pitch": ROOT / "assets" / "idpr_overview.md",
    "do_not_contact": ROOT / "assets" / "do_not_contact.txt",
    "prompts": ROOT / "prompts",
}

# The copy that is sent is the one thing that must never come from another client.
SENDING_REQUIRES = ("pitch", "prompts")


@dataclass(frozen=True)
class Profile:
    name: str
    display_name: str
    pitch: str
    icp: str
    exclusions: str
    fit_rule: str
    titles: tuple[str, ...]
    bare_titles: tuple[str, ...]
    prompts_dir: Path
    do_not_contact_path: Path
    workspace_id: int | None
    emailbison_key_env: str | None
    database: str | None
    slack_channel: str | None
    model_campaign_id: int | None
    employee_min: int
    employee_max: int
    default_mode: str
    base_variant: str | None = None
    scale_metric: str | None = None
    contacts_per_company: int | None = None
    verify_pool_per_company: int | None = None
    use_leadocean: bool = False
    scale_rate: float | None = None
    scale_min_sends: int | None = None
    scale_min_replies: int | None = None
    scale_min_days: int | None = None
    scale_decision_sends: int | None = None
    step_wait_days: int = 3
    recontact_days: int = 90
    title_strategy: str = "size_aware_pr"
    excluded_title_patterns: tuple[str, ...] = field(default=())
    email1_max_chars: int | None = None
    email2_max_chars: int | None = None
    email_paragraphs: int | None = None   # exact paragraph count each email must have (blank-line separated)
    copy_variants: tuple[tuple[str, str], ...] = field(default=())
    fell_back: tuple[str, ...] = field(default=())
    # The client's OWN audiences, in their priority order. Used before
    # anything is inferred from the ICP.
    verticals: tuple[str, ...] = field(default=())
    # Countries to source companies and keep contacts in (ISO2). Default US only; TFP is US and Canada (Emmanuel, 2026-10-15).
    countries: tuple[str, ...] = field(default=("US",))
    # Optional cheap first-pass fit judge (e.g. "openai/gpt-oss-120b" via BuzzLead). It only REJECTS; every company it passes is confirmed by the
    # full fit model. Measured on 112 TFP companies: 75 times cheaper, agrees with Sonnet 90 percent, but passes 12 percent of Sonnet's rejects.
    fit_screen_model: str | None = None
    # True: the screen model is the ONLY fit judge (no Sonnet confirm). TFP, Emmanuel 2026-10-15: cheaper for the long run.
    fit_screen_only: bool = False
    # Opt-in, for two clients that share one pool of firms (ProQ and MasterNow). All
    # default to off, so every other client behaves exactly as before. See
    # app/consultant_gate.py.
    #   history_file       CSV in the client's folder: firms already emailed, with dates
    #   partition_lo/hi    this client owns domains whose stable hash bucket (0-99) is in
    #                      [lo, hi); two clients with complementary ranges can never claim
    #                      the same firm
    #   max_vp_per_company at most this many VP-titled people per company
    #   emailbison_base    the host this client's workspace lives on
    history_file: str | None = None
    partition_lo: int | None = None
    partition_hi: int | None = None
    max_vp_per_company: int | None = None
    emailbison_base: str | None = None
    # True when the client's research prompt carries the fit gate (FIT_SCORE / RED_FLAGS). Then the
    # research verdict is FINAL: no 'already confirmed' override, and WEAK or any red flag drops the contact.
    research_fit_gate: bool = False
    # "serper" (the original: direct site fetch + Serper news/competitors) or "jina" (Jina site text plus the
    # vendors' company description only; Serper is never called).
    research_inputs: str = "serper"
    # research_inputs = "jina_news": Jina multi-page site text (pages chosen by these words, spec order = priority) plus Serper news for the past year.
    research_page_words: tuple[str, ...] = field(default=())
    research_max_pages: int = 5
    research_max_chars: int = 25000
    # Google searches run for each company in "jina_news" mode; {domain} and {company} are filled in. Same-site hits not already read are fetched.
    research_search_queries: tuple[str, ...] = field(default=())
    research_search_fetch: int = 2
    # [[vertical_defs]] in client.toml: each declared vertical's provider industry labels and keywords, so the
    # waterfall searches exactly those labels (no model guessing, no dependence on AI Ark to measure labels).
    vertical_defs: tuple = field(default=())
    # True: the client's DECLARED verticals, ranked by a waterfall supply probe, are always the starting point.
    # IDPR's "find a winning vertical first, then expand" flow is not used.
    declared_first: bool = False
    # What "a consultancy" means for THIS client, worded into the IS_CONSULTANCY question (MasterNow also takes
    # recruiting and IT services).
    fit_services: str = ""
    # True: ONLY the titles in titles.txt are selectable (plus the bare VP net); no "same role, different wording".
    titles_strict: bool = False
    email1_min_chars: int | None = None
    email2_min_chars: int | None = None
    # True: a title that CONTAINS one of the declared buyer titles (whole words, any order) is a buyer even when it
    # also carries an excluded-persona word. Lumion spec 3.3: "Owner / Lead Instructor" must survive "instructor".
    include_wins: bool = False
    # False: previously contacted (reused) companies are NOT fit-checked. Their supply is limited and already known, so the gate runs only
    # on NEW companies at sourcing time (Emmanuel, 2026-10-09). ProductEVO keeps the default True.
    reuse_fit_check: bool = True

    def prompts_for(self, variant: str | None = None) -> Path:
        """The prompt set for a copy variant, or the client's default set."""
        if variant:
            cand = self.prompts_dir / variant
            if cand.is_dir() and any(cand.glob("prompt_*.txt")):
                return cand
        return self.prompts_dir

    def excluded_personas(self) -> tuple[str, ...]:
        """Functions this client never contacts, from the 'Excluded personas:' line.

        ProductEVO's exclusions.md ends "Excluded personas: Marketing, Sales, Finance,
        HR, Legal." Nothing read it. The finder appends a bare "VP"/"President"/"Vice
        President" recovery net to every search, which matches any vice president at
        all, so a run for a client that bans Sales came back holding a VP of Sales, a
        VP of Finance and a Senior VP of Sales & Marketing -- 6 of 20 contacts were
        personas the client had written down as never-contact (found 2026-09-20).
        """
        import re
        m = re.search(r"excluded personas?\s*:\s*([^\n]+)", self.exclusions or "",
                      re.IGNORECASE)
        if not m:
            return ()
        words = [w.strip(" .").lower() for w in re.split(r"[,/]| and ", m.group(1))]
        return tuple(w for w in words if w)

    def prompt_file(self, filename: str, variant: str | None = None) -> Path:
        """One prompt file, falling back PER FILE within THIS CLIENT.

        Troy, 2026-09-20: "I also want to be able to hand over copy prompts to test."
        An arm is therefore allowed to be PARTIAL. Testing a new angle usually means a
        different email 1, not a different research process, so an arm holding only
        prompt_email1.txt inherits research and email 2. Handing over a new angle is
        dropping one file in a folder.

        The fallback order matters more than it looks. A client like ProductEVO has NO
        prompts directly in prompts/ -- only arm folders -- so a naive fallback to the
        single-client legacy path would have quietly served IDPR's prompts to
        ProductEVO's prospects. That is the exact failure the profiles exist to prevent,
        so the client's own base_variant is tried first and legacy is last.
        """
        if variant:
            cand = self.prompts_dir / variant / filename
            if cand.exists():
                return cand
        base = self.prompts_dir / filename
        if base.exists():
            return base
        fallback = self.base_variant or (self.variant_names()[0]
                                         if self.variant_names() else None)
        if fallback:
            cand = self.prompts_dir / fallback / filename
            if cand.exists():
                return cand
        return LEGACY["prompts"] / filename

    def prompt_source(self, filename: str, variant: str | None = None) -> str:
        """Where a prompt file actually came from: this arm, the base arm, or legacy."""
        path = self.prompt_file(filename, variant)
        try:
            rel = path.relative_to(CLIENTS_DIR)
            return str(rel.parent)
        except ValueError:
            return "LEGACY (another client's prompts)"

    def handed_arms(self) -> dict:
        """Arms handed over from Slack. Empty when there is no database to ask."""
        return _handed(self.name)

    def angle_for(self, variant: str | None) -> str:
        """An arm given as plain instructions rather than whole prompts.

        The lightest possible handover: a folder containing only angle.md, whose text is
        appended to the client's base prompts. Use it when the bet is "say it this way",
        not "replace the whole brief".
        """
        if not variant:
            return ""
        f = self.prompts_dir / variant / "angle.md"
        if f.exists():
            return "\n\n" + f.read_text(encoding="utf-8").strip()
        handed = self.handed_arms().get(variant)
        if handed and handed.get("angle"):
            return "\n\n" + handed["angle"].strip()
        return ""

    def discovered_variants(self) -> tuple[str, ...]:
        """Arms found on disk, whether or not client.toml mentions them.

        Declaring an arm in two places is a way to forget one of them. A folder holding
        prompt files or an angle.md IS an arm.
        """
        out = []
        if self.prompts_dir.is_dir():
            for d in sorted(self.prompts_dir.iterdir()):
                if d.name.startswith((".", "_")):
                    continue                  # templates and notes, not arms
                if d.is_dir() and (any(d.glob("prompt_*.txt")) or (d / "angle.md").exists()):
                    out.append(d.name)
        for name in self.handed_arms():
            if name not in out:
                out.append(name)
        return tuple(out)

    def variant_names(self) -> tuple[str, ...]:
        """Declared arms first, then any found on disk that nobody declared."""
        declared = [name for name, _ in self.copy_variants]
        for found in self.discovered_variants():
            if found not in declared:
                declared.append(found)
        return tuple(declared)

    def bet_for(self, variant: str) -> str:
        for name, bet in self.copy_variants:
            if name == variant:
                return bet
        f = self.prompts_dir / variant / "bet.txt"
        if f.exists():
            return f.read_text(encoding="utf-8").strip()[:200]
        handed = self.handed_arms().get(variant)
        if handed and handed.get("bet"):
            return handed["bet"]
        return "handed over, no stated bet"

    @property
    def is_complete_for_sending(self) -> bool:
        if any(k in self.fell_back for k in SENDING_REQUIRES):
            return False
        # Every prompt this client would actually send must resolve inside this client.
        for f in ("prompt_research.txt", "prompt_email1.txt", "prompt_email2.txt"):
            if "LEGACY" in self.prompt_source(f, None):
                return False
        return True

    def emailbison_key(self) -> str | None:
        """The client's own EmailBison key, by the env var their profile names."""
        if self.emailbison_key_env:
            return os.getenv(self.emailbison_key_env)
        return None


def _read(path: Path, fallback: Path | None = None) -> tuple[str, bool]:
    """(text, fell_back). Empty string when neither exists -- never an exception."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip(), False
    if fallback and fallback.exists():
        return fallback.read_text(encoding="utf-8").strip(), True
    return "", True


def current_name() -> str:
    """The active client. There is NO default: this build serves Lumion only, and a
    silent fallback to another client's name is how one client's settings reach another's."""
    name = (os.getenv("CLIENT") or "").strip().lower()
    if not name:
        raise RuntimeError("CLIENT is not set. Set CLIENT=lumion.")
    return name


def available() -> list[str]:
    if not CLIENTS_DIR.exists():
        return []
    return sorted(p.name for p in CLIENTS_DIR.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


_HANDED: dict[str, tuple[float, dict]] = {}
_HANDED_TTL = 60.0


def _handed(client_name: str) -> dict:
    """Arms handed over from Slack, cached briefly.

    prompt_file() consults the arm list and is called several times per email, so an
    uncached lookup would be a database round trip per prompt per lead. Sixty seconds
    is short enough that an arm added in Slack is testable immediately and long enough
    that a copy run does not notice.
    """
    import time
    hit = _HANDED.get(client_name)
    now = time.monotonic()
    if hit and now - hit[0] < _HANDED_TTL:
        return hit[1]
    try:
        from . import db
        with db.for_client(client_name):
            out = db.arms(client_name)
    except Exception:
        out = {}
    _HANDED[client_name] = (now, out)
    return out


def forget_arms() -> None:
    """Drop the arm cache, so a just-added arm is visible at once."""
    _HANDED.clear()


@lru_cache(maxsize=8)
def load(name: str | None = None) -> Profile:
    name = (name or current_name()).strip().lower()
    base = CLIENTS_DIR / name
    cfg: dict = {}
    toml_path = base / "client.toml"
    if toml_path.exists():
        with toml_path.open("rb") as fh:
            cfg = tomllib.load(fh)

    fell_back: list[str] = []
    pitch, fb = _read(base / "pitch.md", LEGACY["pitch"])
    if fb:
        fell_back.append("pitch")
    icp, fb = _read(base / "icp.md")
    if fb:
        fell_back.append("icp")
    exclusions, fb = _read(base / "exclusions.md")
    if fb:
        fell_back.append("exclusions")

    titles_raw, fb = _read(base / "titles.txt")
    if fb:
        fell_back.append("titles")
    titles = tuple(t.strip() for t in titles_raw.splitlines()
                   if t.strip() and not t.startswith("#"))

    prompts_dir = base / "prompts"
    has_direct = (prompts_dir / "prompt_email1.txt").exists()
    has_variant = prompts_dir.is_dir() and any(
        (d / "prompt_email1.txt").exists() for d in prompts_dir.iterdir() if d.is_dir())
    if not (has_direct or has_variant):
        prompts_dir = LEGACY["prompts"]
        fell_back.append("prompts")

    dnc_path = base / "do_not_contact.txt"
    if not dnc_path.exists():
        dnc_path = LEGACY["do_not_contact"]
        fell_back.append("do_not_contact")

    bare = tuple(cfg.get("bare_titles") or ("VP", "President", "Vice President"))
    return Profile(
        name=name,
        display_name=cfg.get("display_name") or name,
        pitch=pitch,
        icp=icp,
        exclusions=exclusions,
        fit_rule=cfg.get("fit_rule") or "",
        titles=titles,
        bare_titles=bare,
        prompts_dir=prompts_dir,
        do_not_contact_path=dnc_path,
        workspace_id=cfg.get("workspace_id"),
        emailbison_key_env=cfg.get("emailbison_key_env"),
        database=cfg.get("database"),
        slack_channel=cfg.get("slack_channel"),
        verticals=tuple(cfg.get("verticals") or ()),
        countries=tuple(str(c).upper() for c in (cfg.get("countries") or ["US"])),
        fit_screen_model=cfg.get("fit_screen_model") or None,
        fit_screen_only=bool(cfg.get("fit_screen_only", False)),
        model_campaign_id=cfg.get("model_campaign_id"),
        step_wait_days=int(cfg.get("step_wait_days") or 3),
        employee_min=int(cfg.get("employee_min") or 11),
        employee_max=int(cfg.get("employee_max") or 500),
        default_mode=cfg.get("default_mode") or "expand",
        base_variant=cfg.get("base_variant"),
        scale_metric=cfg.get("scale_metric"),
        contacts_per_company=cfg.get("contacts_per_company"),
        verify_pool_per_company=cfg.get("verify_pool_per_company"),
        use_leadocean=bool(cfg.get("use_leadocean", False)),
        scale_rate=cfg.get("scale_rate"),
        scale_min_sends=cfg.get("scale_min_sends"),
        scale_min_replies=cfg.get("scale_min_replies"),
        scale_min_days=cfg.get("scale_min_days"),
        scale_decision_sends=cfg.get("scale_decision_sends"),
        recontact_days=int(cfg.get("recontact_days") or 90),
        title_strategy=cfg.get("title_strategy") or "size_aware_pr",
        excluded_title_patterns=tuple(cfg.get("excluded_title_patterns") or ()),
        email1_max_chars=cfg.get("email1_max_chars"),
        email2_max_chars=cfg.get("email2_max_chars"),
        email_paragraphs=cfg.get("email_paragraphs"),
        copy_variants=tuple((v.get("name", ""), v.get("bet", ""))
                            for v in (cfg.get("copy_variants") or []) if v.get("name")),
        fell_back=tuple(fell_back),
        history_file=cfg.get("history_file"),
        partition_lo=cfg.get("partition_lo"),
        partition_hi=cfg.get("partition_hi"),
        max_vp_per_company=cfg.get("max_vp_per_company"),
        emailbison_base=cfg.get("emailbison_base"),
        research_fit_gate=bool(cfg.get("research_fit_gate", False)),
        research_inputs=str(cfg.get("research_inputs") or "serper"),
        research_page_words=tuple(str(w).lower() for w in (cfg.get("research_page_words") or ())),
        research_max_pages=int(cfg.get("research_max_pages") or 5),
        research_max_chars=int(cfg.get("research_max_chars") or 25000),
        research_search_queries=tuple(str(q) for q in (cfg.get("research_search_queries") or ())),
        research_search_fetch=int(cfg.get("research_search_fetch") if cfg.get("research_search_fetch") is not None else 2),
        vertical_defs=tuple(cfg.get("vertical_defs") or ()),
        declared_first=bool(cfg.get("declared_first", False)),
        fit_services=str(cfg.get("fit_services") or ""),
        titles_strict=bool(cfg.get("titles_strict", False)),
        email1_min_chars=cfg.get("email1_min_chars"),
        email2_min_chars=cfg.get("email2_min_chars"),
        include_wins=bool(cfg.get("include_wins", False)),
        reuse_fit_check=bool(cfg.get("reuse_fit_check", True)),
    )


def profile() -> Profile:
    return load(current_name())
