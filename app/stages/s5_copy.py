"""Stage 5 -- Research + copy, the ported Clay workflow.

Per contact: two Serper calls, a website fetch, then three LLM calls (research,
email 1, email 2) using the Clay system prompts verbatim from prompts/. Every generated
email goes through the validator; a violation regenerates (up to COPY_MAX_RETRIES),
and a contact that fails twice lands in needs_review and is excluded from the batch.

Two drop rules that are not negotiable:
  - Research report empty or all-skeleton -> DROP the contact. Generic copy is worse
    than no copy: it is what makes a whole domain look like spam.
  - Copy that fails validation twice -> needs_review, never "send it anyway".

The output contract is the strict JSON the EmailBison merge variables read:
{"PERSONALIZATION 1": ..., "PERSONALIZATION 2": ...}
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import db, experiments, prompts, validator
from .s3_companies import _no_funding
from ..clients import llm, serper
from ..clients.http import VendorOutage
from ..config import settings

OVERVIEW_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "idpr_overview.md"


def _overview() -> str:
    """The ACTIVE CLIENT's pitch. Both email prompts consume it verbatim, so this is
    the single place a run could send one client's positioning under another's name."""
    from .. import client
    text = client.profile().pitch
    if text:
        return text
    return _legacy_overview()


def _legacy_overview() -> str:
    """Static block baked into the repo (spec Stage 5 step 8). Never fetched live."""
    return OVERVIEW_PATH.read_text(encoding="utf-8")


def _profile_block(contact: dict) -> tuple[str, str]:
    """AI Ark / Quick Enrich fields standing in for the Clay LinkedIn scrapes.

    Spec Stage 5 step 4: no direct LinkedIn scraping happens off Clay, so the profile
    slots are filled from vendor profile fields instead.
    """
    company = (
        f"Name: {contact.get('company_name')}\n"
        f"Domain: {contact.get('company_domain')}\n"
        f"Employees: {contact.get('employees') or 'unknown'}\n"
        f"Vertical: {_no_funding(contact.get('vertical') or '')}\n"
        f"Description: {(contact.get('company_description') or 'unknown')[:800]}"
    )
    person = (
        f"Name: {contact.get('first_name')} {contact.get('last_name')}\n"
        f"Title: {contact.get('title')}\n"
        f"LinkedIn: {contact.get('linkedin_url') or 'not available'}"
    )
    return company, person


# Google News, the competitor search and the website fetch describe the COMPANY, not
# the person, and we take up to four contacts per company -- so without this the same
# three network calls ran four times for identical results. Measured shapes: 2,000
# contacts at 4 per company means 8,000 Serper calls and 2,000 site fetches, against
# 1,000 and 500 when cached. Keyed by domain, held for the life of the run, and
# guarded by a lock so the parallel copy workers cannot each start the same fetch.
_company_research: dict[str, tuple[str, str, str]] = {}
_research_lock = threading.Lock()


_DEAD_SITE = "Website could not be fetched."


def _site_or_fallback(domain: str, company_name: str) -> str:
    """The company's own site, or a way to still write copy when it has none.

    About 30% of the Wave 1 KEEP list has no working domain -- expired, parked, or
    dead cert (measured 2026-09-27) -- and a blank site used to mean a blank research
    report, which meant the contact was dropped even when the finders had found a real
    person there. A dead site does not mean a dead business: try a name search (the
    LinkedIn page or a press mention often outlives the domain), and if even that is
    empty, say so plainly rather than hand the model silence -- it still has the
    sourcing description in company_profile_data to work from.
    """
    site = serper.website_text(domain)
    if site != _DEAD_SITE:
        return site
    found = serper.search_by_name(company_name)
    if found:
        return f"[{domain} has no working website; found by company name instead]\n{found}"
    return (f"[{domain} has no working website, and no other public information was found. "
            f"Write from the company profile below only. If it says too little to write "
            f"from, use the fallback rule in your instructions.]")


def _company_inputs(domain: str, company_name: str) -> tuple[str, str, str]:
    with _research_lock:
        hit = _company_research.get(domain)
    if hit is not None:
        return hit
    from .. import client as _client
    if _client.profile().research_inputs == "jina":
        # Jina site text + the vendors' company description. Serper is used ONLY when Jina cannot read the
        # site (a dead or blocked domain): a name search, as the original dead-site fallback. Never news or
        # competitor searches for this client.
        from ..clients import jina
        text = jina.website_text_with_pages(domain)     # home page plus the admissions, contact and schedule pages
        if not text:
            found = serper.search_by_name(company_name)
            text = (f"[{domain} has no readable website; found by company name instead]\n{found}" if found else
                    f"[{domain} has no readable website and nothing else was found. Write from the company profile "
                    f"only. If it says too little to write from, use the fallback rule in your instructions.]")
        inputs = ("", "", text)
    else:
        inputs = (serper.news(company_name), serper.competitors(company_name),
                  _site_or_fallback(domain, company_name))
    with _research_lock:
        _company_research.setdefault(domain, inputs)
    return inputs


_research_reports: dict[str, str] = {}
_research_models: dict[str, str] = {}          # domain -> "provider/model" that wrote it
_report_lock = threading.Lock()


VERTICAL_LINE_RX = re.compile(r"^\s*vertical\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _declared_verticals() -> list[str]:
    from .. import client as _client
    return list(_client.profile().verticals)


def _needs_vertical(contact: dict) -> bool:
    v = (contact.get("vertical") or "").strip()
    return not v or v.lower().startswith("tier ")


def vertical_from_report(report: str) -> tuple[str | None, str]:
    """(declared vertical named by the research brief or None, report without that line)."""
    m = VERTICAL_LINE_RX.search(report or "")
    if not m:
        return None, report
    said = m.group(1).strip().strip("'\"*.").lower()
    short = lambda v: v.split(" (")[0].strip().lower()
    hit = (next((v for v in _declared_verticals() if v.lower() == said), None)
           or next((v for v in _declared_verticals() if short(v) == said.split(" (")[0].strip()), None))
    return hit, VERTICAL_LINE_RX.sub("", report).strip()


def research(contact: dict, variant: str | None = None, fit_confirmed: bool = False) -> str:
    """The prospect research report for this contact's COMPANY.

    Cached per domain, because the report is about the company and both email prompts
    consume only the report -- render_user(research_report, idpr_overview) takes no
    person fields at all, so two contacts at one company already receive copy built
    from the same material. Generating it per contact bought nothing and cost the most
    expensive call in the chain: measured 2026-09-14, 7.2s and 2,200 output tokens,
    repeated for every one of the ~2.4 contacts a company yields. Troy: "copy should
    not take 6 hours for 9000 contacts."
    """
    company_name = contact.get("company_name") or contact.get("company_domain") or ""
    domain = contact["company_domain"]

    cache_key = f"{variant or ''}|{domain}|{int(fit_confirmed)}"
    with _report_lock:
        hit = _research_reports.get(cache_key)
    if hit is not None:
        return hit

    news, competitors, site = _company_inputs(domain, company_name)
    company_profile, person_profile = _profile_block(contact)

    prompt = prompts.research(variant)
    user = prompt.render_user(
        website_text=site,
        company_profile_data=company_profile,
        person_profile_data=person_profile,
        serper_news_results=news,
        serper_competitor_results=competitors,
        today=__import__("datetime").date.today().strftime("%B %d, %Y"),
        cutoff=(__import__("datetime").date.today() - __import__("datetime").timedelta(days=365)).strftime("%B %Y"),
    )
    if _needs_vertical(contact):
        # Troy, 2026-09-27: the research call already reads the site, so it names the
        # vertical too -- one prompt, not a separate call. Only for leads that arrive
        # without one (tier 2 = previously emailed, never sourced into a vertical).
        user += ("\n\nAdd one more line at the very end: 'vertical: ' followed by the ONE "
                 "vertical below this company belongs in, copied exactly, or 'none':\n"
                 + "; ".join(_declared_verticals()))
    if fit_confirmed:
        # Fit is decided once per company (app/company_fit.py). Letting each angle's brief
        # re-judge it made the Tariff campaign reject companies the other two kept.
        user += ("\n\nFIT IS ALREADY CONFIRMED for this company by a separate check. Do not "
                 "write DISQUALIFY and do not rate icp_fit LOW or NOT_FIT. Write the full brief.")
    from .. import client as _client
    if _client.profile().research_fit_gate:
        user += fit_question(_client.profile().fit_services or "consulting, advisory or coaching services")
    # The Clay research prompt sets no max_tokens, and the fallback model runs to ~11k
    # characters unchecked -- which then becomes input cost twice over, in the Email 1
    # and Email 2 prompts that both consume the report.
    # The research prompt only: OpenRouter, where its model actually exists.
    report, _, model_used = llm.complete_meta(
        prompt.model, prompt.system, user, prompt.temperature,
        prompt.max_tokens or settings.research_max_tokens, provider="research")
    with _report_lock:
        _research_reports.setdefault(cache_key, report)
        _research_models.setdefault(domain, model_used)
    return report


def _protected_terms(contact: dict) -> tuple[str, ...]:
    """Names the validator must not treat as banned words (see validator docstring)."""
    return tuple(
        t for t in (
            contact.get("company_name"),
            contact.get("first_name"),
            contact.get("last_name"),
            contact.get("company_domain"),
        ) if t
    )


def _length_target(limit: int) -> int:
    """Aim retries comfortably inside the gate, not at it.

    The Clay prompts target the same number the validator enforces ("under 300-500
    characters" against a 500-char gate), so the model writes to 500 and lands just
    over: observed 590 -> 502 -> 517, failing by two characters. Telling it the count
    it produced and a target with headroom is what actually converges.
    """
    return int(limit * 0.85)


def _is_length_only(violations: list[str]) -> bool:
    return bool(violations) and all("chars >" in v for v in violations)


def _compress_user(draft: str, limit: int, extra: list[str], attempt: int = 0) -> str:
    """Ask for an EDIT of the draft, not another draft.

    Regenerating from scratch was the mistake. Measured on the medical device run of
    2026-09-11, email 2 came back at 1,414 / 1,348 / 1,304 / 1,277 / 1,255 characters
    against an 1,100 limit -- every attempt a fresh composition that re-inflated to the
    model's natural length, so four retries burned and the contact was binned. 39% of
    verified contacts died this way: 971 verified, 615 sendable.

    Handing the model its own draft and asking it to cut converges instead, because the
    task stops being "write an email" and becomes "remove 200 characters". Troy's rule
    that the three pitches are approved copy is stated as a constraint, so the cuts come
    out of prose rather than structure.
    """
    # Each round demands a deeper cut than the last. Two reasons. The prompts run at a
    # fixed temperature, so an identical compression request returns an identical draft
    # -- measured 2026-09-11, one email froze at exactly 755 characters for three
    # consecutive attempts and burned the contact. And a model asked to land just under
    # the limit lands just over it, so the ask has to aim well inside.
    ceiling = int(limit * max(0.80 - 0.06 * attempt, 0.55))
    over = len(draft) - ceiling
    note = ""
    if extra:
        note = ("\n\nAlso fix, without adding length: " + "; ".join(extra))
    return (
        f"Shorten the email below. It is {len(draft)} characters and must be under "
        f"{ceiling}, so cut at least {over} characters.\n\n"
        f"Keep ALL THREE pitches and the existing structure -- they are approved copy "
        f"and must not be dropped or merged. Take the length out of the prose: remove "
        f"qualifiers, adjectives and wind-up phrases, and tighten sentences. Do not add "
        f"anything new.{note}\n\n"
        f"Output ONLY the shortened email body, no preamble and no commentary.\n\n"
        f"--- EMAIL TO SHORTEN ---\n{draft}"
    )


NOT_FIT_RX = re.compile(r"\bthis is not a fit\b", re.IGNORECASE)
RESEARCH_NOT_FIT_RX = re.compile(r"^\s*(DISQUALIFY\b|icp_fit\s*:\s*(NOT_FIT|LOW)\b)",
                                 re.IGNORECASE | re.MULTILINE)


FIT_SCORE_RX = re.compile(r"^\s*FIT_SCORE\s*:\s*\[?\s*(STRONG|MODERATE|WEAK)\b", re.IGNORECASE | re.MULTILINE)
RED_FLAGS_RX = re.compile(r"^\s*RED_FLAGS\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
IS_CONSULTANCY_RX = re.compile(r"^\s*IS_CONSULTANCY\s*:\s*\[?\s*(YES|NO|UNKNOWN)\b", re.IGNORECASE | re.MULTILINE)
# The disqualifiers the consultant research prompt itself names. They only count when the brief ALSO rates the
# firm WEAK: a MODERATE or STRONG consultancy that merely SERVES nonprofits or government is a fit.
DISQUALIFIER_RX = re.compile(
    r"\b(non-?profit|not-for-profit|government|public[- ]sector agency|religious|church|ministry|"
    r"pre-?revenue|large agency|no active (consulting|coaching) practice)\b", re.IGNORECASE)
# A domain with no business behind it: parked, expired, for sale, dissolved. Evidence, not thin data.
DEFUNCT_RX = re.compile(
    r"\b(?:listed for sale|is for sale|domain (?:is )?parked|parked|website expired|expired \(|domain (?:is )?(?:disconnected|not connected)|"
    r"defunct|dissolved|no active business|domain isn.t connected)\b", re.IGNORECASE)


def fit_question(services: str) -> str:
    """The one line added to the END of the research request so the same call states, in a form code can read,
    whether the firm is a consultancy. Text patterns could not tell "is a consulting firm" from "rather than a
    consulting practice" (measured on 67 WEAK firms and two pending batches), so the model is asked directly."""
    return ("\n\nAdd one more line at the very end: 'IS_CONSULTANCY: ' followed by YES, NO or UNKNOWN. "
            f"YES means the firm's main business is selling {services}, in any niche; firm size does not matter for this "
            "line. NO means its main business is something else (a restaurant, manufacturer, distributor, contractor, "
            "software product company, property business or nonprofit, for example). UNKNOWN only when the website cannot "
            "be read and the name and description give no clear answer.")


def research_fit_failure(report: str) -> str | None:
    """Why the research brief says this prospect is not a fit, or None.

    The consultant research prompt IS the fit gate. A firm is dropped when the brief says its main business is NOT
    consulting/advisory/coaching (IS_CONSULTANCY: NO, worded per client in client.toml fit_services, so MasterNow
    also takes recruiting and IT services), or when it rates the firm WEAK and names one of its own disqualifiers
    (nonprofit, government, religious org, pre-revenue, large agency, no active practice) or the domain is defunct.
    MODERATE and STRONG are otherwise never dropped, and WEAK from thin data (a dead or blocked site) is still sent
    (Emmanuel, 2026-10-08). A report written before IS_CONSULTANCY existed falls back to the WEAK rules alone.
    """
    is_c = IS_CONSULTANCY_RX.search(report)
    if is_c and is_c.group(1).upper() == "NO":
        return "main business is not consulting, advisory or coaching"
    fit = FIT_SCORE_RX.search(report)
    if not fit or fit.group(1).upper() != "WEAK":
        return None
    flags_m = RED_FLAGS_RX.search(report)
    flags = flags_m.group(1) if flags_m else ""
    hit = DISQUALIFIER_RX.search(flags)
    if hit:
        return f"WEAK + red flag: {hit.group(0).lower()}"
    if DEFUNCT_RX.search(flags):
        return "WEAK and the domain has no business behind it"
    return None


def _generate_with_retry(prompt, user: str, check, protected: tuple[str, ...],
                         label: str, run_id: str, email: str,
                         limit: int, context: str = "") -> tuple[str | None, list[str], str | None]:
    """Returns (text, violations, "provider/model" that wrote the accepted text)."""
    last_violations: list[str] = []
    last_text = ""
    model_used: str | None = None
    # State the budget on the FIRST attempt, not just in retries. The Clay prompt says
    # "under 300-500 characters" and the validator enforces 500, so the model writes to
    # the number it was given and lands just over: measured 43% of verified contacts in
    # the legal run failed here and were binned after two retries -- a bigger loss than
    # anything in sourcing.
    budget = (f"\n\nHARD LIMIT: {limit} characters. Aim for about {_length_target(limit)} "
              f"so you land inside it. Count characters, not words.")
    _paras = validator.required_paragraphs()
    if _paras:
        budget += (f" Write exactly {_paras} paragraphs, separated by one blank line; a closing line "
                   f"such as 'Let me know.' belongs at the end of the last paragraph, not on its own.")
    for attempt in range(settings.copy_max_retries + 1):
        # Once there is a draft that is only too long, EDIT it rather than start over.
        if last_text and any("chars >" in v for v in last_violations):
            extra = [v for v in last_violations if "chars >" not in v]
            raw, _, model_used = llm.complete_meta(
                prompt.model, prompt.system,
                _compress_user(last_text, limit, extra, attempt),
                prompt.temperature, prompt.max_tokens, provider="copy")
            text = validator.normalize(raw)
        else:
            nudge = budget
            if last_violations:
                # Tell the model exactly what tripped, or the retry reproduces the fault.
                nudge = ("\n\nYour previous attempt was rejected for: "
                         + "; ".join(last_violations)
                         + ". Rewrite it so none of those apply. Output only the email body.")
            raw, _, model_used = llm.complete_meta(
                prompt.model, prompt.system, user + nudge,
                prompt.temperature, prompt.max_tokens, provider="copy")
            text = validator.normalize(raw)
        if NOT_FIT_RX.search(text) and len(text) < 120:
            # The prompt's own instruction for a bad prospect. It is a verdict about the
            # company, so retrying only buys the same answer -- and on 2026-09-27 three of
            # twelve sample emails shipped this sentence as the email itself.
            return None, ["not a fit (declared by the copy prompt)"], model_used
        verdict = check(text, protected, context)
        if verdict.ok:
            return text.strip(), [], model_used
        # Keep the SHORTER of the two drafts to compress from -- a compression pass that
        # overshot should not hand the next attempt a longer starting point.
        if not last_text or len(text) < len(last_text) or not _is_length_only(last_violations):
            last_text = text
        last_violations = verdict.violations
        db.log(run_id, "stage5",
               f"{email}: {label} attempt {attempt + 1} rejected", level="warn",
               violations=last_violations, chars=len(text))
    return None, last_violations, model_used


def generate(contact: dict, run_id: str) -> tuple[dict | None, str]:
    """Returns (contact_with_copy, reason). contact is None when the contact is dropped."""
    email = contact["email"]
    overview = _overview()
    protected = _protected_terms(contact)
    experiment_id = experiments.COPY_EXPERIMENT
    variant = contact.get("copy_variant") or experiments.assign(email, experiment_id)
    # Two kinds of variant. A client testing whole messages (ProductEVO) keeps a prompt
    # FOLDER per variant, and that folder is the treatment -- appending an instruction
    # on top would contaminate copy that was approved as written. A client tuning one
    # message (IDPR) has a single folder, and the variant is an appended instruction.
    from .. import client as _client
    _prof = _client.profile()
    # Three kinds of arm, and the angle is checked FIRST because an arm handed over
    # from Slack has no folder at all -- it lives in the database, so asking "does it
    # have its own prompts" would miss it entirely.
    _angle = _prof.angle_for(variant)
    _own_prompts = _prof.prompts_for(variant) != _prof.prompts_dir
    if _angle:
        # Plain instructions appended to the client's base prompts.
        treatment = _angle
    elif _own_prompts:
        # Whole approved prompts ARE the treatment; a nudge on top contaminates them.
        treatment = ""
    else:
        treatment = experiments.copy_instruction(variant)
    _v = variant if _own_prompts else None

    from .. import company_fit as _company_fit
    _fit = _company_fit.verdict(contact.get("company_domain") or "")
    _reused_unchecked = (not _prof.reuse_fit_check) and contact.get("vertical") == "Tier 2 - previously emailed"
    if _fit in ("LOW", "NOT_FIT") and not _reused_unchecked:
        return None, f"not a fit (company check: {_fit})"
    _fit_ok = _fit in _company_fit.PASS and not _prof.research_fit_gate
    # A reused (previously contacted) company is not fit-checked when the client turned that off, so the research brief must not
    # reject it either: treat it as confirmed. (wave1_fill tags these with this vertical label.)
    if _reused_unchecked:
        _fit_ok = True
    try:
        # Troy, 2026-09-27: the company fit check (Sonnet) is THE gate. The haiku research brief
        # rejected ~60% of companies that check passed, clear fits included (Zonex, Instagrid),
        # so once the company check has passed, research does not re-judge. Research still
        # judges companies the check has not seen.
        report = research(contact, _v, fit_confirmed=True) if _fit_ok else research(contact, _v)
    except VendorOutage:
        raise          # not this contact's fault; the whole stage must stop
    except Exception as exc:
        return None, f"research call failed: {exc}"

    if not validator.research_is_usable(report):
        return None, "research report empty or all-skeleton"
    if _prof.research_fit_gate:
        why = research_fit_failure(report)
        if why:
            return None, f"not a fit (research brief: {why})"
    if RESEARCH_NOT_FIT_RX.search(report):
        if not _fit_ok:
            return None, "not a fit (research brief)"
        report = RESEARCH_NOT_FIT_RX.sub("", report)   # company check already passed it
    _tagged_vertical = None
    if _needs_vertical(contact):
        _tagged_vertical, report = vertical_from_report(report)
    from .. import copy_rules as _copy_rules
    _copy_rules.set_context(variant, contact.get("first_name"))

    p1 = prompts.email1(_v)
    _who = dict(company_name=contact.get("company_name") or contact.get("company_domain") or "",
                job_title=contact.get("title") or "")
    user1 = p1.render_user(research_report=report, client_overview=overview, **_who) + treatment
    body1, violations1, model1 = _generate_with_retry(
        p1, user1, validator.validate_email1, protected, "email1", run_id, email,
        settings.email1_max_chars, context=report,
    )
    if not body1:
        return None, f"email 1 failed validation: {'; '.join(violations1)}"

    p2 = prompts.email2(_v)
    user2 = p2.render_user(email_1_output=body1, research_report=report,
                           client_overview=overview, **_who) + treatment
    body2, violations2, model2 = _generate_with_retry(
        p2, user2, validator.validate_email2, protected, "email2", run_id, email,
        settings.email2_max_chars, context=report,
    )
    if not body2:
        return None, f"email 2 failed validation: {'; '.join(violations2)}"

    # NAMING SOMEBODY ELSE'S COMPANY. 8 of 33 emails did it on 2026-09-22, and one told
    # Arsenal Strength it wanted to help ROGUE -- their direct competitor. A weak email
    # costs a reply; that one costs the account. Checked here rather than in the
    # validator because this is the only place the prospect's own company is in scope.
    for slot, body in (("email 1", body1), ("email 2", body2)):
        other = validator.wrong_company(body, contact.get("company_name"),
                                        contact.get("company_domain"))
        if other:
            return None, (f"{slot} names {other!r}, which is not "
                          f"{contact.get('company_name') or 'this prospect'}")

    enriched = dict(contact)
    enriched["research_report"] = report
    if _tagged_vertical:
        enriched["vertical"] = _tagged_vertical
    enriched["personalization_1"] = body1
    enriched["personalization_2"] = body2
    # Provenance. Which prompt files, and which provider/model, wrote this row -- so
    # a later reply rate can be attributed to the copy that earned it.
    enriched["prompt_version"] = prompts.version(_v)
    with _report_lock:
        enriched["research_model"] = _research_models.get(contact["company_domain"])
    enriched["copy_model"] = (model1 if model1 == model2 or not model2
                              else f"{model1}|{model2}")
    enriched["experiment_id"] = experiment_id
    enriched["copy_variant"] = variant
    enriched["title_segment"] = experiments.title_segment(contact.get("title"))
    return enriched, "ok"


def run(contacts: list[dict], run_id: str) -> tuple[list[dict], dict]:
    """Generate copy for a batch, in parallel.

    Each contact costs two Serper calls, a site fetch and three LLM calls, and measured
    end to end that is about 80 seconds. Sequentially a single 2,500-lead vertical would
    take 56 hours, so a three-vertical run could not finish inside a week. The work is
    almost entirely waiting on other people's APIs, so it parallelises cleanly.
    """
    ready: list[dict] = []
    stats = {"contacts_needs_review": 0, "contacts_dropped_research": 0, "copy_generated": 0}
    done = 0
    total = len(contacts)

    with ThreadPoolExecutor(max_workers=settings.copy_workers) as pool:
        futures = {pool.submit(generate, c, run_id): c for c in contacts}
        for fut in as_completed(futures):
            contact = futures[fut]
            done += 1
            try:
                result, reason = fut.result()
            except VendorOutage:
                # A vendor is out of credit or rejecting the key. Every remaining
                # contact would fail the same way and be filed as needs_review with a
                # misleading reason -- 20 Proptech contacts on 2026-09-11 when
                # Concentrate ran dry, and 3,058 on 2026-09-14 when Serper did, each
                # with its verification already paid for. Stop the pool, keep what is
                # already persisted.
                for f in futures:
                    f.cancel()
                raise
            except Exception as exc:
                stats["contacts_needs_review"] += 1
                db.log(run_id, "stage5", f"{contact['email']}: copy failed -- {exc}",
                       level="warn")
                try:        # park it, so the next pass does not pick the same contact again
                    db.mark_needs_review(contact["email"], f"copy failed: {str(exc)[:150]}")
                except Exception:
                    pass
                continue
            if result:
                ready.append(result)
                stats["copy_generated"] += 1
                # Persist immediately. Copy that exists only in this list is copy that a
                # later failure deletes -- which is exactly how 615 finished emails were
                # lost on 2026-09-11. One write per contact is cheap against the three
                # LLM calls that produced it.
                try:
                    db.save_drafts([result], run_id)
                except Exception as exc:
                    db.log(run_id, "stage5",
                           f"{contact['email']}: draft not persisted -- {exc}", level="warn")
            elif reason.startswith("research report empty"):
                stats["contacts_dropped_research"] += 1
                db.log(run_id, "stage5", f"{contact['email']}: dropped -- {reason}")
            else:
                stats["contacts_needs_review"] += 1
                db.mark_needs_review(contact["email"], reason)
                db.log(run_id, "stage5", f"{contact['email']}: needs_review -- {reason}",
                       level="warn")
            if done % 25 == 0 or done == total:
                print(f"[stage5] copy {done}/{total} "
                      f"({stats['copy_generated']} ok)", flush=True)

    stats["contacts_sendable"] = len(ready)
    db.log(run_id, "stage5", f"{len(ready)} contacts have approved copy", **stats)
    return ready, stats


def output_contract(contact: dict) -> dict[str, str]:
    """The strict JSON shape the EmailBison merge variables read (spec Stage 5 step 9)."""
    return {
        "PERSONALIZATION 1": contact.get("personalization_1") or "",
        "PERSONALIZATION 2": contact.get("personalization_2") or "",
    }


def gate_companies(companies: list[dict], vertical_name: str, run_id: str) -> list[dict]:
    """Stage 3b for a research_fit_gate client: the research prompt decides, per company, BEFORE contacts.

    One research call per company (cached by domain, so the later copy stage reuses it for free), run in
    parallel. A company whose brief names a disqualifier on RED_FLAGS is dropped here, so no contact lookup or
    email verification is paid for it. A research error keeps the company: a vendor blip must not drop a fit.
    """
    def _one(c: dict):
        pseudo = {"company_name": c.get("name") or c.get("domain"), "company_domain": c["domain"],
                  "company_description": c.get("description"), "employees": c.get("employees") or c.get("employee_count"),
                  "first_name": "", "last_name": "", "title": "Founder or Owner", "vertical": vertical_name}
        try:
            return c, research_fit_failure(research(pseudo)), None
        except VendorOutage:
            raise
        except Exception as exc:
            return c, None, str(exc)[:100]

    kept, dropped, errors = [], [], 0
    with ThreadPoolExecutor(max_workers=settings.copy_workers) as pool:
        for fut in as_completed([pool.submit(_one, c) for c in companies]):
            c, why, err = fut.result()
            if err:
                errors += 1
            if why:
                dropped.append((c["domain"], why))
            else:
                kept.append(c)
    order = {c["domain"]: i for i, c in enumerate(companies)}
    kept.sort(key=lambda c: order[c["domain"]])
    db.log(run_id, "stage3b", f"{vertical_name}: research gate kept {len(kept)} of {len(companies)} companies, "
           f"dropped {len(dropped)}, {errors} research errors", dropped_sample=dropped[:10])
    return kept
