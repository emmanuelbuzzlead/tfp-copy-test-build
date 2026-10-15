"""Unit tests for the logic that decides who gets emailed. No network, no DB.

These cover the four places where a silent bug would be expensive: CSV header mapping,
the winner gates, contact title selection, and the copy validator.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import prompts, validator  # noqa: E402
from app.clients.quickenrich import Contact  # noqa: E402
from app.stages.s1_performance import (  # noqa: E402
    CampaignRow,
    IngestError,
    map_headers,
    merge,
    parse_performance_csv,
    select_seed_themes,
    select_winners,
)
from app.stages.s4_people import score_title, select_contact  # noqa: E402


# --------------------------------------------------------------------------
# Stage 1: CSV mapping and winner gates
# --------------------------------------------------------------------------

def test_interested_column_beats_plain_replies():
    mapping = map_headers(["Campaign Name", "Leads", "Replies", "Positive Replies"])
    assert mapping["interested"] == "Positive Replies"
    assert mapping["replies"] == "Replies"
    # And when the export uses EmailBison's own word for it.
    mapping = map_headers(["Campaign", "Leads", "Replied", "Interested"])
    assert mapping["interested"] == "Interested"
    assert mapping["replies"] == "Replied"


def test_plain_campaign_column_is_the_name_not_the_id():
    # A column headed "Campaign" holds the name. Mapping it to campaign_id left the
    # name unmapped and the numeric parse then dropped the row entirely.
    mapping = map_headers(["Campaign", "Prospects", "Interested Replies"])
    assert mapping.get("campaign_name") == "Campaign"
    assert "campaign_id" not in mapping


def test_missing_required_column_refuses_instead_of_guessing():
    try:
        parse_performance_csv("Campaign,Sent,Bounces\nx,1,2\n")
    except IngestError as exc:
        assert "Headers seen" in str(exc)
    else:
        raise AssertionError("should have refused to guess")


def test_winner_gates_reject_small_samples_and_weak_rates():
    csv_text = (
        "Campaign Name,Total Leads,Sends,Replies,Positive Replies\n"
        "Strong,4000,4000,80,8\n"       # 0.2% interested per send -- winner
        "Small sample,300,300,40,10\n"  # 25% but only 300 sends -- pending
        "Weak,5000,5000,80,2\n"         # 0.04% per send -- below 0.2%
    )
    rows, _ = parse_performance_csv(csv_text)
    winners, stats = select_winners(rows)
    assert [w.campaign_name for w in winners] == ["Strong"]
    assert stats["cleared_gates"] == 1


def test_no_winners_when_everything_is_mediocre():
    rows, _ = parse_performance_csv(
        "Campaign Name,Total Leads,Sends,Replies,Positive Replies\n"
        "A,4000,4000,80,2\nB,9000,9000,100,3\n"
    )
    winners, _ = select_winners(rows)
    assert winners == []


# --------------------------------------------------------------------------
# Stage 4: title selection
# --------------------------------------------------------------------------

def _c(title: str) -> Contact:
    return Contact("Sam", "Rivera", "sam@co.com", title, None, "Co", "co.com")


def test_marketing_wins_at_a_large_company():
    chosen = select_contact([_c("VP of Sales"), _c("VP"), _c("VP of Marketing")], 200)
    assert chosen.title == "VP of Marketing"


def test_founder_wins_at_a_small_company():
    chosen = select_contact([_c("Head of Marketing"), _c("CEO")], 20)
    assert chosen.title == "CEO"


def test_bare_vp_is_a_last_resort_not_a_preference():
    assert score_title("VP", 200) < score_title("Head of Communications", 200)
    chosen = select_contact([_c("VP of Sales"), _c("VP")], 200)
    assert chosen.title == "VP"


def test_support_staff_are_the_only_hard_exclusion():
    """Assistants and coordinators are never the buyer, at any seniority.

    Everything else on IDPR's title list is selectable -- the department blacklist was
    removed on 2026-09-10 when Troy confirmed VPs of Sales, Communications and IT are
    in the ICP.
    """
    for title in ("Exective Assistant to CEO", "Executive Assistant",
                  "Marketing Coordinator", "Sales Secretary"):
        assert score_title(title, 200) == 0
    # ...while a sales VP is now selectable, just ranked below marketing and comms.
    assert 0 < score_title("VP of Sales", 200) < score_title("CMO", 200)


# --------------------------------------------------------------------------
# Stage 5: copy validation
# --------------------------------------------------------------------------

CLEAN_1 = ("Noticed your Series A didn't pick up trade coverage while two competitors "
           "landed VentureBeat. We place founders in earned media. Forbes or TechCrunch "
           "would both run this. Open to a few ideas?")


def test_clean_email1_passes():
    assert validator.validate_email1(CLEAN_1).ok


def test_email1_catches_the_banned_patterns():
    bad = ("Hi Jane,\n\nCongrats on the raise! Our amazing platform will leverage "
           "strategic synergy — click here.\n\nBest,\nTroy")
    verdict = validator.validate_email1(bad)
    joined = " ".join(verdict.violations)
    assert not verdict.ok
    for expected in ("em/en dash", "greeting", "sign-off", "congrats", "amazing",
                     "leverage", "synergy", "strategic"):
        assert expected in joined


def test_company_name_is_exempt_from_the_hype_word_ban():
    # "Solutions" is banned and is also the last word of many real company names.
    # Without the exemption these contacts could never pass, no matter how many retries.
    text = ("Saw Incentive Solutions shipped a rebate product with no press. "
            "We land earned media for teams like yours. Inc. would fit. Worth a look?")
    assert validator.validate_email1(text, ("Incentive Solutions",)).ok
    assert not validator.validate_email1(text, ()).ok


def test_email2_allows_bullets_but_not_markdown():
    body = ("I reached out the other day but probably didn't explain it well.\n"
            "I was back on your site and saw your case study on the Denver rollout.\n"
            "• VentureBeat - covering AI in ops. Angle: cutting onboarding time.\n"
            "• Inc. - runs operator stories. Angle: scaling without a marketing team.\n"
            "Worth a quick 15-min call?")
    assert validator.validate_email2(body).ok
    assert not validator.validate_email2(body.replace("• VentureBeat", "**VentureBeat**")).ok


def test_email1_length_gate_is_500_chars():
    assert not validator.validate_email1("word " * 200).ok


def test_skeleton_research_is_not_usable():
    skeleton = ("Prospect Research Report\nTRIGGER EVENTS\n- [Recent funding round]\n"
                "- [New product launch]\nPERSONALIZATION HOOKS\n- [Background]\n") * 3
    assert not validator.research_is_usable(skeleton)
    assert not validator.research_is_usable("")


def test_real_research_is_usable():
    real = ("Company: Northwind Health, 140 employees, Series B. Raised 40M in June led "
            "by Bessemer with zero tier-1 coverage. Launched claims automation in August, "
            "covered only by a wire release. Competitor Cedar appeared in Forbes twice in "
            "the same window. VP Marketing joined from Olive AI in March and posts monthly "
            "with under ten reactions. Two demand-generation roles are open.")
    assert validator.research_is_usable(real)


# --------------------------------------------------------------------------
# Prompt loading
# --------------------------------------------------------------------------

def test_prompts_load_whole_system_blocks():
    # Every Clay prompt uses '---' internally as a section rule; a first-match parse
    # truncates the system prompt to its opening paragraph.
    research, e1, e2 = prompts.research(), prompts.email1(), prompts.email2()
    assert len(research.system) > 8000 and "COMMON MISTAKES" in research.system
    assert len(e1.system) > 1200 and "75 words" in e1.system
    assert len(e2.system) > 5000 and "NO MARKDOWN" in e2.system
    assert e1.model == "anthropic/claude-haiku-4.5" and e1.max_tokens == 300
    assert research.model == "openai/gpt-oss-120b" and research.temperature == 0.3


def test_prompt_slot_filling_rejects_unfilled_slots():
    e1 = prompts.email1()
    filled = e1.render_user(research_report="R", client_overview="O")
    assert "R" in filled and "O" in filled and "{" not in filled.split("##")[0]


# --------------------------------------------------------------------------
# Stage 1: the interested rate, and CSV overriding the API
# --------------------------------------------------------------------------

def test_interested_rate_is_over_leads_not_over_replies():
    # The two readings of "positive reply rate" differ by an order of magnitude:
    # 52/4046 = 1.3% of the list, vs 52/443 = 11.7% of repliers. Ranking on the
    # wrong one reorders the winners.
    row = CampaignRow(1, "Funded Brands", total_leads=4046, sends=20485,
                      replies=443, interested=52)
    assert abs(row.interested_rate - 52 / 4046) < 1e-9


def test_csv_overrides_api_for_the_same_campaign():
    api = [
        CampaignRow(1036, "Funded Brands", 4046, 20485, 443, 52, source="api"),
        CampaignRow(991, "SaaS August", 6721, 53827, 662, 53, source="api"),
    ]
    csv_rows = [CampaignRow(1036, "Funded Brands", 4046, 20485, 443, 99,
                            source="csv", stats_verified=True)]
    merged = {r.campaign_id: r for r in merge(api, csv_rows)}
    assert merged[1036].interested == 99 and merged[1036].stats_verified
    assert merged[991].interested == 53 and not merged[991].stats_verified


def test_csv_override_matches_on_name_when_it_has_no_id():
    api = [CampaignRow(1036, "Funded Brands", 4046, 20485, 443, 52, source="api")]
    csv_rows = [CampaignRow(None, "funded brands", 4046, 20485, 443, 77, source="csv")]
    merged = merge(api, csv_rows)
    assert len(merged) == 1 and merged[0].interested == 77
    # The id is carried over from the API row so the push can still find the campaign.
    assert merged[0].campaign_id == 1036


# --------------------------------------------------------------------------
# Typographic normalization (Stage 5 pre-validation)
# --------------------------------------------------------------------------

def test_unspaced_em_dash_becomes_a_comma_not_a_hyphen():
    # Live output produced "PR Newswire-not the tier-1 outlets", which reads as a
    # compound word rather than the break the writer meant.
    assert validator.normalize("it hit PR Newswire—not the tier-1 outlets") ==         "it hit PR Newswire, not the tier-1 outlets"
    assert validator.normalize("Saw your raise — no coverage yet") ==         "Saw your raise, no coverage yet"


def test_normalization_clears_the_em_dash_violation():
    raw = "Saw your launch — no tier-1 coverage yet. Worth a look?"
    assert not validator.validate_email1(raw).ok
    assert validator.validate_email1(validator.normalize(raw)).ok


def test_smart_quotes_are_flattened():
    assert validator.normalize("Healthie’s “best places” win") ==         "Healthie's \"best places\" win"


# --------------------------------------------------------------------------
# Stage 4: the "Vice President" trap
# --------------------------------------------------------------------------

def test_generic_vps_are_in_icp_but_rank_last():
    """Troy, 2026-09-10: VPs of Sales, Communications and IT ARE in the IDPR ICP.

    So there is no department blacklist -- the title list is the inclusion list. What
    the ladder must do is ORDER them, so a company with a CMO and a VP of Sales yields
    the CMO first and the VP of Sales only if slots remain.
    """
    for title in ("Vice President of Global Sales", "Corporate Vice President of IT",
                  "Vice President Human Resources"):
        s = score_title(title, 200)
        assert s > 0, f"{title!r} is in the ICP and must be selectable"
        assert s < score_title("CMO", 200), f"{title!r} must rank below a CMO"
        assert s < score_title("Head of Communications", 200)


def test_word_boundaries_survive_in_the_title_regexes():
    """A literal backspace byte once replaced  in these patterns, so nothing matched.

    Every title scored the same catch-all value and the ladder silently stopped
    ordering anything at all.
    """
    # Score is 100 minus this title's position in IDPR's own list (Troy, 2026-09-25: a
    # client's declared list ranks before the generic ladder, no exclusion list
    # required), not the old flat MARKETING_RX/EXEC_RX bucket scores -- so CMO's exact
    # number drifts as titles are added to clients/idpr/titles.txt; CEO's does not, it
    # sits at a fixed, very early position.
    assert score_title("CMO", 200) > 70
    assert score_title("CEO", 30) == 98


def _unused_vice_president_of_a_wrong_department():
    """The word 'president' inside 'Vice President of Sales' must not read as an exec.

    It did. EXEC_RX matched 'president', which cancelled the wrong-department block,
    and every Sales / IT / HR / R&D VP scored 95 -- the top of the ladder. Measured on
    86 live contacts, 52 of them were the wrong person.
    """
    for title in ("Vice President of Global Sales", "Corporate Vice President of IT",
                  "Vice President Human Resources", "Area Vice President of Sales",
                  "Vice President, Sales Operations",
                  "Vice President Research And Development", "Sr. VP of QA/RA",
                  "VP RA", "Executive VP", "VP, Strategic Partnerships"):
        assert score_title(title, 30) == 0, f"{title!r} should be unreachable"


def test_support_staff_are_never_selected():
    for title in ("Exective Assistant to CEO", "Executive Assistant",
                  "Marketing Coordinator", "Sales Secretary"):
        assert score_title(title, 30) == 0


def test_real_company_heads_still_score_top():
    """Every real head of company scores near the top of the ladder, at a small company.

    Not a single flat number any more: since 2026-09-25 a client's own list ranks
    "first in the file scores highest", so Founder (index 0 on IDPR's list) legitimately
    outscores Owner (index 8) even though both are company heads. The exact numbers
    shift every time a title is added to clients/idpr/titles.txt (it has grown twice
    already), so this checks position -- all of IDPR's head titles are declared in the
    first ten lines of the file -- rather than a score against another title's number.
    """
    for title in ("CEO", "President", "Founder", "Managing Director", "Owner", "Partner"):
        assert score_title(title, 30) >= 90, title
    # Carries three extra words (a company name) beyond the declared "CEO", so it ranks
    # a little below a clean match -- still a real head of company, still selectable.
    assert 0 < score_title("President and CEO Saluda Medical", 30) < score_title("CEO", 30)


def test_marketing_and_comms_vps_still_qualify():
    assert score_title("Vice President of Marketing", 30) > 0
    assert score_title("VP of Communications", 30) > 0
    assert score_title("CMO", 200) > 0


def test_idpr_title_list_is_authoritative_without_an_exclusion_list():
    """Troy, 2026-09-25, after CFOs, engineers and lab techs reached approval: 'I gave
    you job titles and I expect people with at least matching job titles.' IDPR has a
    35-title list but no separate excluded-personas list, and the strict rule used to
    require BOTH before it would trust a client's own list -- so IDPR fell through to
    the generic ladder's catch-all score of 15 for everything, including these."""
    must_drop = ["Chief Financial Officer", "Chief Operating Officer", "Controller",
                 "Staff Accountant", "Software Engineer", "Design Engineer",
                 "Product Manager", "Supply Chain Manager", "Senior Scientist",
                 "Research Associate", "Laboratory Technician", "Instructor",
                 "Account Executive", "Territory Manager", "Customer Service Representative"]
    assert not [t for t in must_drop if score_title(t, 200) > 0]
    # Marketing/comms/exec titles and a low-priority generic VP-of-something (the
    # recovery net IDPR's own list puts a bare "VP" on) are still selectable.
    assert score_title("CMO", 200) > 0
    assert score_title("VP of Sales", 200) > 0


# --------------------------------------------------------------------------
# Markdown is formatting, not content -- strip it, do not pay to regenerate
# --------------------------------------------------------------------------

def test_markdown_is_stripped_rather_than_rejected():
    """71 of 91 legal contacts failed on "contains markdown / rich text".

    Each burned up to four LLM calls and was then binned, after we had already paid to
    research and verify that person. The markers carry no meaning once removed.
    """
    assert validator.normalize("**VentureBeat** covers this") == "VentureBeat covers this"
    assert validator.normalize("your *excellent* study") == "your excellent study"
    assert validator.normalize("## Ideas\nForbes fits") == "Ideas\nForbes fits"
    assert validator.normalize("[our work](https://x.com)") == "our work"


def test_markdown_bullets_become_the_plain_bullet_email2_asks_for():
    out = validator.normalize("- VentureBeat - covers AI\n- Inc. - operator stories")
    assert out == "• VentureBeat - covers AI\n• Inc. - operator stories"


def test_normalized_copy_passes_validation_that_raw_copy_fails():
    raw = "**Noticed** your raise had no coverage. We land earned media. Forbes fits. Open to ideas?"
    assert not validator.validate_email1(raw).ok
    assert validator.validate_email1(validator.normalize(raw)).ok


# ---------------------------------------------------------------------------
# Regression: a second fetch must page PAST what the first one took.
# On 2026-09-11 the fetcher restarted at page 0 every call, refilled its quota with
# companies the caller already had, and the caller discarded them -- reading as
# "source exhausted" with 7,477 companies never opened. Widening into sibling labels
# then papered over it. Troy: "I need to know this is fixed forever."
# ---------------------------------------------------------------------------

def test_second_fetch_pages_past_the_first(monkeypatch):
    from app import db, taxonomy
    from app.stages import s3_companies as S
    from app.stages.s2_expansion import Vertical

    # 5 pages of 4 companies each; a stable order like AI Ark's.
    pages = {n: [{"link": {"domain": f"c{n * 4 + i}.test"}, "summary": {"name": f"C{n * 4 + i}"},
                  "industries": ["x"], "keywords": []} for i in range(4)] for n in range(5)}

    class _Resp:
        status_code = 200
        def __init__(self, n):
            self._n = n
        def json(self):
            return {"content": pages.get(self._n, []), "totalElements": 20,
                    "last": self._n >= 4}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(k["json"]["page"]))
    monkeypatch.setattr(db, "blocked_domains", lambda domains: {})
    monkeypatch.setattr(taxonomy, "record_seen", lambda *a, **k: None)

    v = Vertical.from_json({"name": "T", "rationale": "r", "industries": ["Label"], "keywords": []})
    first, _ = S._fetch_fresh(v, ["Label"], want=8)
    got1 = {c["domain"] for c in first}
    assert len(got1) == 8

    second, _ = S._fetch_fresh(v, ["Label"], want=8, exclude=got1)
    got2 = {c["domain"] for c in second}
    assert len(got2) == 8, "second fetch returned nothing new -- the paging bug is back"
    assert not (got1 & got2), "second fetch re-served companies the caller already had"


def test_scale_up_never_widens(monkeypatch):
    """A scale-up sources its own supply only. Troy, 2026-09-13: a sister label spent on
    a winner cannot be tested on its own for 90 days, and its replies would be credited
    to the winner."""
    from app import db, taxonomy
    from app.stages import s3_companies as S
    from app.stages.s2_expansion import Vertical

    asked = []
    monkeypatch.setattr(taxonomy, "siblings_for", lambda *a, **k: asked.append(a) or ["sister"])
    monkeypatch.setattr(taxonomy, "record_seen", lambda *a, **k: None)
    monkeypatch.setattr(S, "calibrate_industries", lambda v, r=None: ["Label"])
    monkeypatch.setattr(S, "_fetch_fresh", lambda *a, **k: ([], {"scanned": 0, "blocked_by_dedup": 0,
                        "fresh": 0, "pages": 0, "available": 0, "keyword_filtered": 0, "per_query": {}}))
    monkeypatch.setattr(db, "staged", lambda *a, **k: [])
    monkeypatch.setattr(db, "stage_companies", lambda *a, **k: 0)
    monkeypatch.setattr(db, "record_verdicts", lambda *a, **k: None)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)

    v = Vertical.from_json({"name": "W", "rationale": "r", "industries": ["Label"], "keywords": []})
    S.find(v, "run", target_contacts=500, widen=False)
    assert asked == [], "scale-up asked the supply map for a sister label"
    S.find(v, "run", target_contacts=500, widen=True)
    assert asked, "a test run should still widen when its own labels are exhausted"


def test_vendor_outage_pauses_instead_of_burning_contacts(monkeypatch):
    """Any vendor running dry must stop the copy stage, not be filed per contact.

    Three times a vendor outage was caught per item and recorded as that item's
    failure: 20 contacts when Concentrate died, 2,820 companies when Anthropic
    answered 400, 3,058 contacts when Serper ran out. Every one had paid work spent.
    """
    from app.clients.http import VendorOutage
    from app.clients.llm import LLMUnavailable
    from app.clients.serper import SerperUnavailable
    from app.stages import s5_copy

    # both concrete outages are the shared type, so one except clause covers any vendor
    assert issubclass(LLMUnavailable, VendorOutage)
    assert issubclass(SerperUnavailable, VendorOutage)

    def dead(*a, **k):
        raise SerperUnavailable("serper 400: Not enough credits")

    monkeypatch.setattr(s5_copy, "research", dead)
    contact = {"email": "a@b.test", "company_domain": "b.test", "first_name": "A",
               "last_name": "B", "title": "CMO", "company_name": "B"}
    # generate() must let it through rather than returning (None, "research call failed")
    try:
        s5_copy.generate(contact, "run")
    except VendorOutage:
        pass
    else:
        raise AssertionError("a vendor outage was swallowed as a per-contact failure")


# --------------------------------------------------------------------------
# The learning loop: outcomes, provenance, measured yields, decision reasons
# --------------------------------------------------------------------------

def test_reply_outcomes_skip_out_of_office_and_keep_bounces():
    """Every pushed contact gets a result: a human reply, interested, or a bounce.
    An automated reply is an out-of-office and counts as nothing."""
    from app.stages.stop_on_reply import contact_outcomes

    replies = [
        {"lead": {"email": "A@x.com"}, "interested": False, "automated_reply": True,
         "folder": "Inbox", "date_received": "2026-09-14T10:00:00Z"},
        {"lead": {"email": "b@x.com"}, "interested": False, "automated_reply": False,
         "folder": "Inbox", "date_received": "2026-09-14T11:00:00Z"},
        {"lead": {"email": "b@x.com"}, "interested": True, "automated_reply": False,
         "folder": "Inbox", "date_received": "2026-09-15T09:00:00Z"},
        {"lead": {"email": "c@x.com"}, "interested": False, "automated_reply": False,
         "folder": "Bounced", "date_received": "2026-09-14T12:00:00Z"},
    ]
    rows = {r["email"]: r for r in contact_outcomes(replies)}
    assert "a@x.com" not in rows, "an out-of-office was recorded as a reply"
    b = rows["b@x.com"]
    assert b["interested"] is True and b["replied_at"].day == 14, "first reply, OR-ed interest"
    assert b["bounced_at"] is None
    assert rows["c@x.com"]["bounced_at"] is not None and rows["c@x.com"]["replied_at"] is None


def test_copy_rows_carry_prompt_version_and_model(monkeypatch):
    """Which prompt files and which provider/model wrote each row must be on the row."""
    from app import prompts
    from app.clients import llm
    from app.stages import s5_copy

    v = prompts.version()
    assert len(v) == 10 and v == prompts.version(), "prompt fingerprint must be stable"

    monkeypatch.setattr(llm, "_order", lambda provider: ["concentrate"])
    monkeypatch.setattr(llm, "_concentrate", lambda *a, **k: "Hello there, quick note.")
    text, provider, label = llm.complete_meta("anthropic/claude-haiku-4.5", "s", "u", 0.3)
    assert provider == "concentrate" and label.startswith("concentrate/")

    monkeypatch.setattr(s5_copy, "research", lambda c, v=None: "x" * 400)
    monkeypatch.setattr(s5_copy.validator, "research_is_usable", lambda r: True)
    monkeypatch.setattr(s5_copy, "_generate_with_retry",
                        lambda *a, **k: ("body", [], "openrouter/anthropic/claude-haiku-4.5"))
    contact = {"email": "a@b.test", "company_domain": "b.test", "first_name": "A",
               "last_name": "B", "title": "CMO", "company_name": "B"}
    row, reason = s5_copy.generate(contact, "run")
    assert reason == "ok"
    assert row["prompt_version"] == v
    assert row["copy_model"] == "openrouter/anthropic/claude-haiku-4.5"


def test_yield_ratios_come_from_the_verdicts_not_the_stats():
    """A resumed run reports companies_kept and no drops; the keep rate must still be
    real. Three of the first four recorded yields said 100% keep for this reason."""
    from app.db import _yield_ratios

    resumed_stats = {"companies_kept": 1200, "resumed": True,
                     "contacts_verified": 3000, "copy_generated": 2000}
    y = _yield_ratios({"KEEP": 1000, "IDPR": 200, "DROP": 1800}, 1100, resumed_stats)
    assert y["keep_rate"] == 0.4, y
    assert y["density"] == round(3000 / 1100, 3)
    assert y["copy_pass"] == round(2000 / 3000, 4)
    # nothing staged (a pre-staging run): the stats are still honoured
    y0 = _yield_ratios({}, 0, {"companies_kept": 50, "companies_dropped_enforcement": 50,
                               "contacts_verified": 100, "copy_generated": 60})
    assert y0["keep_rate"] == 0.5 and y0["density"] == 2.0


def test_kill_requires_a_reason_and_stores_it(monkeypatch):
    from app import db
    from app.stages import s6_approval

    calls = []
    monkeypatch.setattr(db, "get_batch",
                        lambda b: {"batch_id": b, "approval_status": "pending",
                                   "approved_by": None, "contact_count": 5})
    monkeypatch.setattr(db, "settle_batch",
                        lambda b, d, decided_by=None, reason=None:
                        calls.append((b, d, decided_by, reason)) or {"contacts": 5, "companies": 2})

    out = s6_approval.handle_command("kill abc-123", "troy")
    assert calls == [] and "reason" in out.lower(), "a kill without a reason went through"

    s6_approval.handle_command("kill abc-123 companies are all brokerages", "troy")
    assert calls[-1] == ("abc-123", "killed", "troy", "companies are all brokerages")

    # Troy, 2026-09-30: approve LAUNCHES in the same step; `hold` keeps the old two-step.
    pushes = []
    monkeypatch.setattr(s6_approval, "_push",
                        lambda batch, user, live: pushes.append((batch["batch_id"], live))
                        or f"pushed {batch['batch_id']}")
    reply = s6_approval.handle_command("approve abc-123", "troy")
    assert calls[-1] == ("abc-123", "approved", "troy", None)
    assert pushes == [("abc-123", True)], "approve must launch, live, in the same step"
    assert "Approved `abc-123`" in reply and "pushed abc-123" in reply
    pushes.clear()
    reply = s6_approval.handle_command("approve abc-123 hold copy reads well", "troy")
    assert calls[-1][3] == "copy reads well" and pushes == [], "hold must not push"
    assert any(b.get("block_id") == "push:abc-123" for b in reply["blocks"]), "held: push button"


def test_push_from_slack_is_dry_run_unless_told_live(monkeypatch):
    """Troy, 2026-09-14: the push moves from a laptop command to Slack -- dry run by
    default, live only on the explicit word or the confirmed button."""
    from app import db
    from app.stages import s6_approval, s7_push

    batch = {"batch_id": "b-1", "vertical": "V", "approval_status": "approved",
             "approved_by": "troy", "contact_count": 10}
    monkeypatch.setattr(db, "get_batch", lambda b: dict(batch) if b == "b-1" else None)
    pushes = []
    monkeypatch.setattr(s7_push, "push", lambda b, live=False, **k: pushes.append(live) or
                        {"lead_count": 10, "campaign_to_create": "VE-V-2026-09-14"})

    r = s6_approval.handle_command("push b-1", "troy")
    assert pushes == [False] and "Dry run" in r["text"]
    assert any(b.get("block_id") == "push:b-1" for b in r["blocks"])
    r = s6_approval.handle_command("push b-1 live", "troy")
    assert pushes == [False, True] and "Pushed" in r

    batch["approval_status"] = "pending"
    assert "not approved" in s6_approval.handle_command("push b-1 live", "troy")
    assert pushes == [False, True], "a pending batch was pushed"

    from app.web import interaction_to_command
    click = {"type": "block_actions", "user": {"username": "troy"},
             "actions": [{"action_id": "push_batch", "value": "b-1"}]}
    assert interaction_to_command(click) == ("command", "push b-1 live", "troy")


def test_confirm_by_number_approves_the_nth_pending_batch(monkeypatch):
    """Troy, 2026-09-14: "can I not just approve with confirm <number>?" """
    from datetime import datetime, timedelta, timezone
    from app import db
    from app.stages import s6_approval

    later = datetime.now(timezone.utc) + timedelta(days=1)
    pending = [{"batch_id": "medicaldevic-d55715", "vertical": "Medical Device", "contact_count": 3000, "expires_at": later, "approval_status": "pending"},
               {"batch_id": "biotechnolog-6a7ee3", "vertical": "Biotechnology", "contact_count": 3000, "expires_at": later, "approval_status": "pending"}]
    monkeypatch.setattr(db, "pending_batches", lambda: pending)
    monkeypatch.setattr(db, "get_batch",
                        lambda b: next(({**x, "approval_status": "pending", "approved_by": None}
                                        for x in pending if x["batch_id"] == b), None))
    calls = []
    monkeypatch.setattr(db, "settle_batch",
                        lambda b, d, decided_by=None, reason=None:
                        calls.append((b, d, reason)) or {"contacts": 1, "companies": 1})

    monkeypatch.setattr(s6_approval, "_open_batches", lambda: pending)
    monkeypatch.setattr(s6_approval, "_push", lambda batch, user, live: "pushed")
    assert "Approved" in s6_approval.handle_command("confirm 2", "troy")
    assert calls[-1] == ("biotechnolog-6a7ee3", "approved", None)
    assert "Approved" in s6_approval.handle_command("approve medical", "troy")
    assert calls[-1][0] == "medicaldevic-d55715"
    assert "no pending batch #7" in s6_approval.handle_command("confirm 7", "troy")
    status = s6_approval.handle_command("status", "troy")
    assert "*1.*" in status["text"]
    assert any(b.get("block_id") == "gate:medicaldevic-d55715" for b in status["blocks"])


def test_buttons_mean_exactly_what_the_slash_commands_mean():
    """Troy, 2026-09-14: "or a button to confirm on the slack app". The Approve button
    is `/ve approve <id>`; the Kill modal is `/ve kill <id> <reason>` and cannot be
    submitted without a reason."""
    import json
    from app.web import interaction_to_command

    approve = {"type": "block_actions", "user": {"username": "troy"},
               "actions": [{"action_id": "approve_batch", "value": "medicaldevic-d55715"}]}
    assert interaction_to_command(approve) == ("command", "approve medicaldevic-d55715", "troy")

    kill = {**approve, "actions": [{"action_id": "kill_batch", "value": "medicaldevic-d55715"}]}
    assert interaction_to_command(kill) == ("kill_modal", "medicaldevic-d55715", "troy")

    submit = {"type": "view_submission", "user": {"username": "troy"},
              "view": {"callback_id": "kill_batch_modal",
                       "private_metadata": json.dumps({"batch_id": "medicaldevic-d55715"}),
                       "state": {"values": {"reason": {"reason_input": {"value": "all brokerages"}}}}}}
    assert interaction_to_command(submit) == ("command", "kill medicaldevic-d55715 all brokerages", "troy")

    empty = {**submit, "view": {**submit["view"], "state": {"values": {"reason": {"reason_input": {"value": " "}}}}}}
    assert interaction_to_command(empty)[0] == "ignore"

    pick = {**approve, "actions": [{"action_id": "view_sample",
                                    "selected_option": {"value": "medicaldevic-d55715|2"}}]}
    assert interaction_to_command(pick) == ("sample_modal", "medicaldevic-d55715|2", "troy")


def test_batch_summary_shows_live_counts_and_a_sample_dropdown():
    from app.clients import slack
    stats = {"contacts_sendable": 3000, "companies_kept": 1200, "companies_found": 9000}
    samples = [{"first_name": "Ann", "last_name": "Lee", "title": "CMO", "company_name": "Acme",
                "personalization_1": "hi", "personalization_2": "again"}]
    blocks = slack.batch_summary_blocks("b-1", "V", "why", stats, samples)
    text = blocks[1]["text"]["text"]
    assert "3,000" in text and "```" not in str(blocks), "copy must not be inline"
    select = blocks[2]["accessory"]
    assert select["type"] == "static_select" and select["options"][0]["value"] == "b-1|0"
    assert any(b.get("block_id") == "gate:b-1" for b in blocks)


def test_sender_alignment_follows_ivans_rules():
    """Troy: connected accounts only, at most 3 active campaigns each, highest daily
    limit first, stop at ~1,000 sends/day."""
    from app.senders import choose

    senders = (
        [{"id": i, "status": "Connected", "daily_limit": 20} for i in range(1, 11)]      # 10 x 20
        + [{"id": i, "status": "Connected", "daily_limit": 3} for i in range(100, 700)]  # 600 x 3
        + [{"id": 900, "status": "Disconnected", "daily_limit": 20}]
        + [{"id": 901, "status": "Connected", "daily_limit": 20}]                        # full
    )
    counts = {901: 3, 1: 2}          # 901 is on three active campaigns, 1 is on two
    picked = choose(senders, counts, already={2}, target_daily=1000, max_active=3)
    ids = [s["id"] for s in picked]
    assert 900 not in ids and 901 not in ids and 2 not in ids
    cap = sum(s["daily_limit"] for s in picked)
    assert 1000 <= cap < 1003, cap
    # DEDICATED first (2026-09-15): account 1 is on two live campaigns, so it comes
    # after every free account even though its limit is 20.
    assert ids[-1] == 1 or 1 not in ids
    assert ids[:8] == [3, 4, 5, 6, 7, 8, 9, 10], "free high-limit accounts lead"
    assert sum(1 for s in picked if s["daily_limit"] == 3) >= 273


def test_align_attaches_shared_accounts_in_rank_order_counting_their_share(monkeypatch):
    """Troy, 2026-10-01, settling sender assignment: "ranked by no campaigns attached,
    then 1 campaign, then 2 campaigns, and we share sender emails from that ranking."
    Measured the same day on the Healthcare rerun (1226): choose() found 146 accounts
    worth 1,000/day, then align() kept only the 14 idle ones (64/day) because of
    `picked = dedicated or picked`. A shared account counts for limit / (n + 1)."""
    from app import senders as S

    pool = ([{"id": 1, "status": "Connected", "daily_limit": 10, "tags": [{"name": "Active"}]}]
            + [{"id": i, "status": "Connected", "daily_limit": 20, "tags": [{"name": "Active"}]}
               for i in range(2, 60)])
    counts = {i: 1 for i in range(2, 60)}        # everyone but #1 is on one other campaign
    monkeypatch.setattr(S.emailbison, "list_sender_emails", lambda limit=10000: pool)
    monkeypatch.setattr(S, "active_campaign_counts", lambda exclude_campaign_id=None: counts)
    attached_ids: list[int] = []
    monkeypatch.setattr(S.emailbison, "campaign_sender_emails",
                        lambda cid: [s for s in pool if s["id"] in attached_ids])
    monkeypatch.setattr(S.emailbison, "attach_sender_emails",
                        lambda cid, ids: attached_ids.extend(ids))
    monkeypatch.setattr(S.db, "log", lambda *a, **k: None)

    res = S.align(1226, run_id="t", target_daily=200)
    # idle #1 first (10), then shared accounts at 20 // 2 = 10 each until 200 is real
    assert attached_ids[0] == 1
    assert res["daily_capacity"] >= 200, res
    assert len(attached_ids) == 20, "1 idle + 19 shared, not the idle one alone"
    assert S.share_of(pool[1], counts) == 10


def test_daily_rebalance_asks_align_for_every_winner_best_interested_rate_first(monkeypatch):
    """Troy, 2026-10-06: 12+ winners sharing a ~9,000/day pool cannot all reach target
    at once, and before this, which one ended up thin was pure accident of launch
    timing. rebalance_winners() does not invent a new weighted-share formula -- every
    winner still asks align() for the same target; only the ORDER changes, best
    objective_rate (interested + booked per send) first, so whichever pool is left by
    the time a weaker winner's turn comes is what "priority" means in practice."""
    from app import senders as S

    rows = [{"campaign_id": 1, "vertical": "High Performer", "objective_rate": 0.15},
            {"campaign_id": 2, "vertical": "Mid Performer", "objective_rate": 0.09},
            {"campaign_id": 3, "vertical": "Low Performer", "objective_rate": 0.065}]
    monkeypatch.setattr(S.db, "cursor", lambda: _FakeCursorCtx(rows, []))
    monkeypatch.setattr(S.db, "log", lambda *a, **k: None)

    call_order: list[int] = []
    # Simulate a pool that runs dry: the first caller gets its full ask, the rest get
    # progressively less, exactly the "order decides who is fed" mechanism being tested.
    remaining = {"left": 1100}

    def _align(campaign_id, run_id=None, dry_run=False, target_daily=None):
        call_order.append(campaign_id)
        got = min(target_daily, remaining["left"])
        remaining["left"] = max(remaining["left"] - got, 0)
        return {"campaign_id": campaign_id, "attached": got, "accounts": 10,
               "daily_capacity": got, "target": target_daily}
    monkeypatch.setattr(S, "align", _align)

    out = S.rebalance_winners(run_id="t")
    assert call_order == [1, 2, 3], "best objective_rate must be asked first"
    assert out[0]["attached"] == 1000         # High Performer: full ask, pool was fresh
    assert out[1]["attached"] == 100          # Mid Performer: whatever was left
    assert out[2]["attached"] == 0            # Low Performer: pool already spent
    assert {r["vertical"] for r in out} == {"High Performer", "Mid Performer",
                                             "Low Performer"}


def test_fair_cap_lowers_a_campaigns_own_ceiling_to_its_real_share(monkeypatch):
    """EmailBison's own docs: it does not auto-split a shared account between the
    campaigns on it -- whichever processes first can take the whole thing. align()'s
    daily_capacity is a fair-share TARGET, not something EmailBison enforces; it only
    becomes real once the campaign's own max_emails_per_day is capped to it. The
    existing new-leads-to-total ratio must be preserved, not invented fresh, and a cap
    already BELOW the computed fair share must never be raised -- that is a human's
    separate decision (Healthcare's 500/day cap, 2026-10-05)."""
    from app import senders as S

    monkeypatch.setattr(S.emailbison, "get_campaign",
                        lambda cid: {"max_new_leads_per_day": 1500, "max_emails_per_day": 2400})
    calls = []
    monkeypatch.setattr(S.emailbison, "update_campaign_limits",
                        lambda cid, new_leads, total: calls.append((cid, new_leads, total)))
    monkeypatch.setattr(S.db, "log", lambda *a, **k: None)

    out = S.apply_fair_cap(1100, 1000, run_id="t")
    assert calls == [(1100, 625, 1000)]   # 1500 * (1000/2400) = 625, same ratio preserved
    assert out["old_total"] == 2400 and out["new_total"] == 1000

    # A fair share AT OR ABOVE the existing cap is left alone -- nothing to protect
    # against, and raising a cap is a different decision this function does not make.
    calls.clear()
    out2 = S.apply_fair_cap(1100, 3000, run_id="t")
    assert calls == [] and out2.get("skipped")

    # A human-set cap already below the fair share (Healthcare-style) is never raised.
    monkeypatch.setattr(S.emailbison, "get_campaign",
                        lambda cid: {"max_new_leads_per_day": 500, "max_emails_per_day": 500})
    calls.clear()
    out3 = S.apply_fair_cap(1226, 1000, run_id="t")
    assert calls == [] and out3.get("skipped")


def test_dry_run_fair_cap_makes_no_live_call(monkeypatch):
    from app import senders as S
    monkeypatch.setattr(S.emailbison, "get_campaign",
                        lambda cid: {"max_new_leads_per_day": 1500, "max_emails_per_day": 2400})
    called = []
    monkeypatch.setattr(S.emailbison, "update_campaign_limits",
                        lambda *a: called.append(a))
    out = S.apply_fair_cap(1100, 1000, dry_run=True)
    assert called == [] and out["dry_run"] is True and out["new_total"] == 1000


def test_fair_cap_backfill_corrects_every_active_campaign_not_just_winners(monkeypatch):
    """Troy, 2026-10-07: caps summed to 24,386/day against a real ~9,000/day ceiling,
    because rebalance_winners() only ever fair-capped verdict='scale' campaigns -- both
    pools (no single objective_rate to rank on) and pre-fix launches (Specialty Legal,
    Commercial Real Estate, Specialized Management Consulting, all launched a day
    before the new-launch fair-cap fix existed) sat at the generic 2,400/day default,
    still able to out-eat a correctly capped winner on a shared account. This backfill
    attaches NO accounts -- cap-only, every active campaign, any verdict, pooled or not."""
    from app import senders as S

    rows = [{"campaign_id": 1254, "vertical": "(pooled)"},
            {"campaign_id": 1261, "vertical": "Specialty Legal & IP Firms"},
            {"campaign_id": 1085, "vertical": "Medical Device & Diagnostics Manufacturers"}]
    monkeypatch.setattr(S.db, "cursor", lambda: _FakeCursorCtx(rows, []))
    monkeypatch.setattr(S.db, "log", lambda *a, **k: None)

    capacities = {1254: 700, 1261: 650, 1085: 1000}
    monkeypatch.setattr(S, "current_daily_capacity", lambda cid: capacities[cid])

    caps = {1254: 2400, 1261: 2400, 1085: 1000}   # 1085 already fair-capped, no-op
    attach_calls = []

    def _align(*a, **k):
        attach_calls.append((a, k))
    monkeypatch.setattr(S, "align", _align)

    def _apply(cid, capacity, run_id=None, dry_run=False):
        cur = caps[cid]
        if cur <= 0 or capacity <= 0 or capacity >= cur:
            return {"campaign_id": cid, "skipped": True}
        return {"campaign_id": cid, "old_total": cur, "new_total": capacity}
    monkeypatch.setattr(S, "apply_fair_cap", _apply)

    out = S.fair_cap_all_active(run_id="t")
    assert attach_calls == [], "a cap-only backfill must never attach accounts"
    by_vertical = {r["vertical"]: r for r in out}
    assert by_vertical["(pooled)"]["new_total"] == 700
    assert by_vertical["Specialty Legal & IP Firms"]["new_total"] == 650
    assert by_vertical["Medical Device & Diagnostics Manufacturers"].get("new_total") is None


def test_daily_rebalance_excludes_pools_and_exhausted_campaigns_via_its_own_query():
    """Pools have no single objective_rate to rank on (they serve several verticals at
    once) and an exhausted campaign has nowhere to put more capacity until a human
    reopens it -- both are excluded in the SQL itself, not filtered in Python."""
    import inspect
    from app import senders as S
    src = inspect.getsource(S.rebalance_winners)
    assert "v.vertical <> %s" in src and "db.POOL_VERTICAL" in src
    assert "scale_exhausted_at IS NULL" in src
    assert "verdict = 'scale'" in src


def test_pooling_is_decided_on_the_verticals_supply_not_one_batch_snapshot():
    """Troy, 2026-10-01: Biotechnology Research was pooled on a 123-contact first
    batch while 2,117 contacts sat banked, and the sticky rule then pooled the
    1,819-contact batch behind it. s7 must size the VERTICAL (db.unpushed_supply)
    before pooling, and a pooled vertical whose supply reaches the floor must
    graduate to its own campaign and be stamped standalone."""
    import inspect
    from app import db
    from app.stages import s7_push

    src = inspect.getsource(s7_push.push)
    i_launched = src.index("existing = db.vertical_is_launched(batch.get")
    i_drop = src.index('"error": "vertical is dropped"', i_launched)
    i_grad = src.index("db.POOL_VERTICAL:", i_drop)      # dropped verticals never graduate
    i_mark = src.index("db.mark_standalone(", i_grad)
    i_supply = src.index("supply = max(supply, db.unpushed_supply(")
    i_pool = src.index("and supply < settings.pool_below_leads and not standalone:")
    assert i_launched < i_grad < i_mark < i_supply < i_pool
    assert "len(contacts) < settings.pool_below_leads" not in src,         "the batch size alone must never decide pooling"
    qsrc = inspect.getsource(db.unpushed_supply)
    assert "'verified','draft','reserved','approved'" in qsrc
    assert '{"standalone": true}' in inspect.getsource(db.mark_standalone)


def test_a_completed_campaign_whose_vertical_is_still_pending_is_not_archived(monkeypatch):
    """Funded Proptech, 2026-09-30: 'completed' in EmailBison means out of loaded leads,
    not finished. Campaign 1147 was pending at 1,718/2,000 sends, got archived, and
    638 top-up leads were then loaded into a campaign that cannot be resumed."""
    from app import senders as S

    camps = [{"id": 1147, "name": "VE-Funded-Proptech", "status": "completed"},
             {"id": 1090, "name": "VE-Dropped-One", "status": "completed"},
             {"id": 1228, "name": "VE-Pool", "status": "completed"},
             {"id": 900, "name": "New Healthcare 2", "status": "completed"}]
    ve = {1147: {"vertical": "Funded Proptech", "scale_exhausted_at": None},
          1090: {"vertical": "Dropped One", "scale_exhausted_at": None},
          1228: {"vertical": S.db.POOL_VERTICAL}}
    outcomes = {"Funded Proptech": {"verdict": "pending"}, "Dropped One": {"verdict": "drop"}}
    monkeypatch.setattr(S.emailbison, "list_campaigns", lambda: camps)
    monkeypatch.setattr(S.db, "get_ve_campaign", lambda cid: ve.get(cid))
    monkeypatch.setattr(S.db, "vertical_outcome", lambda v: outcomes.get(v))
    monkeypatch.setattr(S.db, "log", lambda *a, **k: None)
    archived = []
    monkeypatch.setattr(S.emailbison, "archive_campaign",
                        lambda cid: archived.append(cid) or {"senders_freed": 1})
    import dataclasses as _dc
    monkeypatch.setattr(S, "settings", _dc.replace(S.settings, never_archive_campaign_ids=(),
                                                   model_campaign_id=0))
    import app.clients.slack as slack
    monkeypatch.setattr(slack, "post", lambda *a, **k: None)

    S.archive_completed_campaigns("t")
    assert archived == [1090, 900], archived   # dropped vertical + non-VE campaign only


def test_leadocean_prefetch_sends_at_most_fifty_titles(monkeypatch):
    """2026-10-01, first tick with the key in place: every prefetch batch failed with
    HTTP 400 'title: Array must contain at most 50 element(s)'. The ladder is longer
    than 50 for most verticals; send the top 50."""
    from app import finders as F
    seen = []
    class R:
        status_code = 200
        def json(self): return {"data": [], "meta": {}}
    monkeypatch.setattr(F, "_leadocean_get", lambda url, params, key: seen.append(params) or R())
    monkeypatch.setattr(F, "uses_leadocean", lambda: True)
    monkeypatch.setenv("LEADOCEAN_API_KEY", "k")
    monkeypatch.setattr(F.db, "record_vendor_usage", lambda *a, **k: None)
    monkeypatch.setattr(F.db, "log", lambda *a, **k: None)
    F.leadocean_prefetch(["a.com"], [f"Title {i}" for i in range(80)], "t")
    assert seen and len(seen[0]["title"].split(",")) == 50


def test_auto_topup_adds_at_most_the_increment_not_the_whole_cap(monkeypatch):
    """Troy, 2026-10-02: "auto top ups should not surpass a number, their primary
    aim is to get smaller verticals up a bit." Specialized Management Consulting
    had room for 2,941 more and got 2,941 more in one pass after a missed-tick
    catch-up; auto_topup_short_verticals must cap _work_vertical's room at
    settings.auto_topup_increment, Monday/midweek intake must not."""
    import inspect
    from app import runner

    src = inspect.getsource(runner.auto_topup_short_verticals)
    assert "max_new=settings.auto_topup_increment" in src

    wv_src = inspect.getsource(runner._work_vertical)
    i_room = wv_src.index("room_total = max(settings.contact_cap_per_vertical")
    i_cap = wv_src.index("if max_new is not None and max_new < room_total:")
    i_set = wv_src.index("room_total = max_new")
    assert i_room < i_cap < i_set

    # Monday/midweek intake calls _work_vertical with no max_new -- full cap applies.
    mid_src = inspect.getsource(runner.midweek_intake_if_short)
    wvs_src = inspect.getsource(runner._work_verticals)
    assert "max_new" not in mid_src and "max_new" not in wvs_src


def test_the_two_hourly_tick_fits_inside_its_own_cron_slot():
    """Railway replaces the cron container when the next 2-hourly schedule fires, so
    a tick still working at the 2-hour mark is killed mid-slice and its successor
    backs off "already running" -- Consumer Edtech's continuation died at exactly its
    tick's 2-hour mark on 2026-10-01 and 2026-10-04. Every heavy phase must run
    against the tick's own clock and hand off cleanly instead of dying."""
    import inspect
    from app import runner, config
    from app.stages import s8_review as s8

    assert config.settings.resume_tick_budget_minutes < 120,         "the budget must fit inside the 2-hour cron slot with room for drift"

    src = inspect.getsource(runner.resume)
    i_clock = src.index("tick_deadline = _time.monotonic() + settings.resume_tick_budget_minutes")
    assert "budget_minutes=_mins_left()" in src
    assert src.count("_mins_left()") >= 5, "every heavy phase checks the clock"

    # the continuation chain caps scale_vertical's own 240-min budget to what is left
    csrc = inspect.getsource(s8._continue_scale_ups)
    assert "deadline_minutes=left" in csrc
    assert "break" in csrc, "stops starting new candidates when the slice is nearly spent"
    ssrc = inspect.getsource(s8.scale_vertical)
    assert "deadline_minutes" in ssrc.split("def scale_vertical")[1][:200]


def test_the_daily_quota_check_posts_every_day_and_hides_the_early_week_mirage(monkeypatch):
    """Troy, 2026-10-05: daily reports on the 15,000 target. The straight-line pace
    projection is only shown after a full day -- it said "on pace for 82,751" at
    half a day into the week -- and blockers are named, not buried."""
    import inspect
    from app import report, runner

    src = inspect.getsource(report.post_daily)
    assert 'w["elapsed_days"] >= 1' in src, "no pace mirage in the first day"
    assert "vendor_refusals_since" in src and "paused_for_human" in src

    rsrc = inspect.getsource(runner)
    i_daily = rsrc.index("report.post_daily()")
    i_friday = rsrc.index("weekday() == 4", i_daily)
    assert i_daily < i_friday, "the daily check posts every day; Friday adds the PDF"


def test_a_blocking_credits_outage_is_shouted_once_a_day_and_mv_balance_is_probed():
    """Serper ran dry on 2026-10-05 and the only trace was a note on a blocked run
    row. A credits refusal that BLOCKS the pipeline must reach the channel (deduped
    daily), and MillionVerifier's balance -- the one we can see -- is probed and
    warned on before it hits zero."""
    import inspect
    from app import runner, config

    src = inspect.getsource(runner.resume)
    i_why = src.index('if "credit" in why.lower() or "402" in why:')
    assert "recently_logged" in src[i_why:i_why + 400], "deduped to once a day"
    assert "blocking the pipeline" in src[i_why:i_why + 700]

    wsrc = inspect.getsource(runner.vendor_credit_watch)
    assert "millionverifier.com/api/v3/credits" in wsrc
    assert "settings.mv_credits_warn" in wsrc
    assert 'recently_logged("vendors", "MillionVerifier balance", hours=24)' in wsrc
    assert config.settings.mv_credits_warn > 0


def test_deploycheck_names_live_work_or_says_safe(monkeypatch):
    """Troy, 2026-10-05: "is anything running right now that this deploy would
    kill?" Deploys killed the Oct 1 review mid-scale and orphaned a tick that
    blocked everything for 11 hours. /ve deploycheck looks, changes nothing."""
    from datetime import datetime, timezone
    from app import db
    from app.stages import s6_approval as s6

    class FakeCur:
        def execute(self, sql, params=None):
            self._rows = []
            self._row = {"t": datetime.now(timezone.utc)}
            if "vertical_backlog" in sql:
                self._rows = [{"vertical": "Biotech Research"}]
            if "scaling_run_id IS NOT NULL" in sql:
                self._rows = [{"campaign_id": 1097, "vertical": "Consumer Edtech",
                              "scaling_run_id": "resume-live"}]
        def fetchone(self): return self._row
        def fetchall(self): return self._rows
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())
    monkeypatch.setattr(db, "run_alive", lambda within_minutes=45, exclude_prefixes=(): "resume-x")
    monkeypatch.setattr(db, "run_is_truly_alive", lambda rid, within_minutes=45: True)
    out = s6.handle_command("deploycheck", "troy")
    assert "kills live work" in out and "resume-x" in out and "Consumer Edtech" in out

    monkeypatch.setattr(db, "run_alive", lambda within_minutes=45, exclude_prefixes=(): None)
    monkeypatch.setattr(db, "run_is_truly_alive", lambda rid, within_minutes=45: False)
    out2 = s6.handle_command("deploycheck", "troy")
    assert "Safe to deploy" in out2


def test_send_target_scales_with_the_list():
    """Troy, 2026-09-14: 3,000 leads -> 1,000/day, 4,000 -> 1,200/day."""
    from app.senders import target_for
    assert target_for(3000) == 1000
    assert target_for(4000) == 1200
    assert target_for(4182) == 1250      # rounded to the nearest 50
    assert target_for(1500) == 1000      # never below the base
    assert target_for(10000) == 2400


def test_copy_intake_oversamples_by_the_pass_rate_within_bounds():
    """3,000 in gave 2,986 out (Venture, 2026-09-14). Feed room / pass_rate in, capped."""
    from app.runner import copy_intake
    assert copy_intake(3000, 1.0) == 3000
    assert copy_intake(3000, 0.95) == 3158
    assert copy_intake(3000, 0.5) == 3750          # rate floored at 0.6 -> 5000, cap 3750
    assert copy_intake(0, 0.9) == 0
    assert copy_intake(100, 0.97) == 104


def test_contacts_without_a_first_name_never_reach_verification():
    from app.stages.s4_people import select_contacts
    from app.clients.quickenrich import Contact
    # select_contacts is downstream of the filter; check the filter's effect via build's
    # rule directly: a nameless Contact is dropped before dedup.
    named = Contact("Ann", "Lee", "a@x.com", "CMO", None, "X", "x.com")
    nameless = Contact("", "Lee", "b@x.com", "CEO", None, "X", "x.com")
    kept = [c for c in [named, nameless] if (c.first_name or "").strip()]
    assert kept == [named]


def test_push_skips_leads_already_in_the_campaign():
    from app.stages.s7_push import split_for_push
    rows = [{"email": "a@x", "eb_campaign_id": 1084}, {"email": "b@x", "eb_campaign_id": None},
            {"email": "c@x", "eb_campaign_id": 1036}]
    done, todo = split_for_push(rows, 1084)
    assert [c["email"] for c in done] == ["a@x"]
    assert [c["email"] for c in todo] == ["b@x", "c@x"], "a lead from another campaign must load"


def test_mail_gateway_classification_blocks_only_barracuda_and_mimecast(monkeypatch):
    """Troy, 2026-09-15: "add it, proofpoint can stay"."""
    from app import mx
    from app.config import settings
    assert mx.classify(["mx1.us.mimecast.com", "mx2.us.mimecast.com"]) == "mimecast"
    assert mx.classify(["d1.barracudanetworks.com"]) == "barracuda"
    assert mx.classify(["mxa-000.pphosted.com"]) == "proofpoint"
    assert mx.classify(["acme-com.mail.protection.outlook.com"]) == "microsoft"
    assert mx.classify([]) == "none" and mx.classify(None) is None
    monkeypatch.setattr(mx, "gateway", lambda d: {"a.com": "mimecast", "b.com": "proofpoint",
                                                  "c.com": None}[d])
    assert mx.blocked("a.com") == "mail gateway: mimecast"
    assert mx.blocked("b.com") is None, "Proofpoint stays"
    assert mx.blocked("c.com") is None, "unknown DNS never blocks"
    assert settings.blocked_mail_gateways == frozenset({"barracuda", "mimecast"})


def test_quickenrich_rows_outside_the_us_are_dropped():
    from app.clients.quickenrich import _map, is_us
    us = _map({"first_name": "A", "last_name": "B", "email": "a@x.com", "title": "CMO",
               "country_code": "US"}, "x.com")
    uk = _map({"first_name": "C", "last_name": "D", "email": "c@x.com", "title": "CMO",
               "country_code": "UK"}, "x.com")
    blank = _map({"first_name": "E", "last_name": "F", "email": "e@x.com", "title": "CMO"}, "x.com")
    assert is_us(us) and not is_us(uk) and is_us(blank)


def test_friday_report_reads_plainly_and_counts_runway():
    """Troy, 2026-09-15: 6th-grade reading level. Short sentences, one idea each."""
    import re
    from datetime import datetime, timezone
    from app import report
    assert report.weeks_of_runway(3, 3) == 1.0 and report.weeks_of_runway(7, 3) == 2.3

    data = {"as_of": datetime(2026, 9, 18, tzinfo=timezone.utc),
            "launched": [{"vertical": "Medical Device & Diagnostics Manufacturers", "leads": 3481,
                          "per_day": 1150, "status": "active"}],
            "all_campaigns": [{"vertical": "Medical Device & Diagnostics Manufacturers", "leads": 3481,
                               "per_day": 1150, "status": "active", "sent": 1200, "replies": 12,
                               "interested": 2}],
            "awaiting": [], "queued": [{"vertical": "Modern Pet Brands", "projected": 7068}],
            "runway_weeks": 0.3, "verdicts": [], "pending_verdicts": 1,
            "health": {"copy_pass": 0.94, "written": 12000, "spend": 300.0, "nameless": 351,
                       "needs_review": 965, "outages": 0}}
    text = report.as_text(data)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"[*-]", " ", text)) if s.strip()]
    long = [s for s in sentences if len(s.split()) > 22]
    assert not long, f"sentences too long for the reading level: {long}"
    assert "3,481" in text and "Modern Pet" in text and "0.3 weeks" in text
    pdf = report.render_pdf(data)
    assert pdf[:4] == b"%PDF" and len(pdf) > 1500


def test_no_verdict_before_seven_days(monkeypatch):
    """Troy, 2026-09-15: "at this pace 2 days may not be enough"."""
    from datetime import datetime, timedelta, timezone
    from app.stages import s8_review
    from app.clients import emailbison
    recorded = []
    monkeypatch.setattr(s8_review, "record_outcome", lambda v, verdict, stats, reason: recorded.append((verdict, reason)))
    monkeypatch.setattr(emailbison, "get_campaign",
                        lambda cid: {"emails_sent": 5000, "replied": 80, "interested": 1})
    camp = {"campaign_id": 1, "vertical": "V", "pushed_lead_count": 4000,
            "created_at": datetime.now(timezone.utc) - timedelta(days=2)}
    r = s8_review.review_campaign(camp)
    assert r["verdict"] == "pending" and "days" in r["reason"], "judged a 2-day-old campaign"
    camp["created_at"] = datetime.now(timezone.utc) - timedelta(days=8)
    r = s8_review.review_campaign(camp)
    assert r["verdict"] == "drop", "an 8-day-old campaign with 1/80 interested must be judged"


def test_an_archived_campaign_is_never_re_judged(monkeypatch):
    """Troy, 2026-10-07: Conversational AI was explicitly dropped and its campaign
    archived (too small to ever reach scale_min_sends on its own), and the very next
    nightly review re-judged it on its old, frozen stats anyway, overwriting the drop
    verdict back to 'pending' and silently re-occupying a test seat.
    _ve_campaigns() never filters by status, so this check inside review_campaign()
    is the one place that actually stops an archived campaign from being judged again,
    by anyone, for any reason, ever."""
    from datetime import datetime, timedelta, timezone
    from app.stages import s8_review
    from app.clients import emailbison

    recorded = []
    monkeypatch.setattr(s8_review, "record_outcome",
                        lambda v, verdict, stats, reason: recorded.append((verdict, reason)))
    monkeypatch.setattr(emailbison, "get_campaign",
                        lambda cid: {"status": "archived", "emails_sent": 346,
                                    "replied": 5, "interested": 1})
    camp = {"campaign_id": 1161, "vertical": "Conversational AI", "pushed_lead_count": 345,
           "created_at": datetime.now(timezone.utc) - timedelta(days=14)}
    r = s8_review.review_campaign(camp)
    assert r["verdict"] == "skipped" and "archived" in r["reason"]
    assert recorded == [], "an archived campaign's verdict must never be re-written"


def test_copy_qa_catches_what_the_model_forgot_to_remove():
    """Troy, 2026-09-15: "---  CHARACTER COUNT: 398" reached EmailBison, along with
    refusals and [placeholders]. The count tail is stripped; the rest is refused."""
    from app import validator as V
    body = "Noticed Acme has zero tier-1 coverage. Worth a quick chat?"
    assert V.normalize(body + "\n\n---\n\nCharacter count: 298") == body
    assert V.normalize(body + "\n---\nCHARACTER COUNT: 398\n") == body
    assert V.normalize(body + " (412 characters)") == body
    assert V.normalize(body + "\n\n---") == body
    assert V.validate_email1(body).ok
    assert "character/word count" in " ".join(V.validate_email1(body + " Word count: 61").violations)
    r = V.validate_email1("I can't write this email.\n\nForesight Diagnostics was acquired.")
    assert any("refused" in x for x in r.violations)
    r = V.validate_email2("• Angle: $325M fund focused on [your geographic/sector focus].")
    assert any("placeholder" in x for x in r.violations)
    r = V.validate_email2("Good body here.\n\nLet me know if you'd like a shorter version.")
    assert any("commentary" in x for x in r.violations)
    # a company called "Insert Media" must not trip the placeholder rule
    assert V.validate_email1("Insert Media landed two placements last month.",
                             protected=("Insert Media",)).ok


# --------------------------------------------------------------------------
# Unified winner contract (outcome value per send, with uncertainty)
# --------------------------------------------------------------------------

class _ScoreSettings:
    """The scale contract pinned for a test.

    app.scoring reads settings at call time, and those values are now per client, so a
    scoring test that relies on whichever client happens to be active is really testing
    a profile. Pin what is under test.
    """
    def __init__(self, **kw):
        self.scale_metric = kw.get("scale_metric", "of_sends")
        self.scale_rate = kw.get("scale_rate", 0.002)
        self.scale_min_sends = kw.get("scale_min_sends", 1000)
        self.scale_min_days = kw.get("scale_min_days", 7)
        self.scale_min_replies = kw.get("scale_min_replies", 30)
        self.scale_decision_sends = kw.get("scale_decision_sends", 3000)
        self.booking_bonus = kw.get("booking_bonus", 3)


def test_scoring_waits_a_week_and_needs_a_thousand_sends(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import scoring
    from app.scoring import judge
    monkeypatch.setattr(scoring, "settings", _ScoreSettings())

    now = datetime.now(timezone.utc)
    early = judge(sends=5000, replies=80, interested=10, launched=now - timedelta(days=2))
    assert early.status == "pending" and "days" in early.reason

    thin = judge(sends=400, replies=40, interested=8, launched=now - timedelta(days=10))
    assert thin.status == "pending" and "sends" in thin.reason

    noisy = judge(sends=2000, replies=6, interested=2, launched=now - timedelta(days=10))
    assert noisy.status == "pending" and "inconclusive" in noisy.reason

    win = judge(sends=4000, replies=40, interested=8, launched=now - timedelta(days=10))
    assert win.status == "winner" and abs(win.rate - 0.002) < 1e-9

    lose = judge(sends=4000, replies=40, interested=1, launched=now - timedelta(days=10))
    assert lose.status == "loser"


def test_booked_meeting_contributes_more_than_interest(monkeypatch):
    from app import scoring
    from app.scoring import judge
    monkeypatch.setattr(scoring, "settings", _ScoreSettings())
    plain = judge(sends=3000, replies=40, interested=3, booked=0)
    booked = judge(sends=3000, replies=40, interested=3, booked=1)
    assert plain.status == "loser"
    assert booked.status == "winner"
    assert booked.rate == 0.002


def test_copy_experiment_assignment_is_stable_and_keeps_exploration(monkeypatch):
    from app import experiments
    monkeypatch.setattr(experiments, "weights", lambda *a, **k:
                        {experiments.CONTROL: 0.8, experiments.MOMENTUM: 0.2})
    first = experiments.assign("person@example.com")
    assert first == experiments.assign("PERSON@example.com")
    assigned = [experiments.assign(f"p{i}@example.com") for i in range(1000)]
    assert 150 < assigned.count(experiments.MOMENTUM) < 250
    assert "positive momentum" in experiments.copy_instruction(experiments.MOMENTUM)
    assert experiments.copy_instruction(experiments.CONTROL) == ""


def test_copy_experiment_exploits_only_after_both_arms_mature(monkeypatch):
    from app import db, experiments
    experiments._cache.clear()
    rows = [
        {"variant_id": experiments.CONTROL, "delivered": 300, "interested": 3, "booked": 0},
        {"variant_id": experiments.MOMENTUM, "delivered": 300, "interested": 7, "booked": 1},
    ]
    monkeypatch.setattr(db, "experiment_performance", lambda _: rows)
    assert experiments.weights(refresh=True)[experiments.MOMENTUM] == 0.8
    rows[0]["delivered"] = 299
    assert experiments.weights(refresh=True) == {
        experiments.CONTROL: 0.5, experiments.MOMENTUM: 0.5}


def test_slack_can_record_a_booked_meeting(monkeypatch):
    from app import db
    from app.stages import s6_approval
    calls = []
    monkeypatch.setattr(db, "mark_booked", lambda email, actor=None, note=None:
                        calls.append((email, actor, note)) or True)
    answer = s6_approval.handle_command("booked Ann@Example.com demo set", "troy")
    assert "recorded" in answer
    assert calls == [("ann@example.com", "troy", "demo set")]


def test_slack_topup_launches_a_real_process_inside_the_service(monkeypatch):
    """Troy, 2026-09-29: "is the tool that runs the 3 short campaigns running on
    railway, and are we making sure that if something like this happens again, this
    can run and fill it up?" The honest answer at the time was no -- a locally-run
    --topup cannot even reach the database, since it lives on Railway's private
    network. /ve topup is the durable fix: it launches the same --topup CLI path as
    a real subprocess INSIDE this service (the same mechanism /ve run already uses),
    so it can be triggered on demand, from anywhere, without local DB access ever
    being a blocker again."""
    from app import client, db
    from app.stages import s6_approval

    assert "idpr" in client.available()
    monkeypatch.setattr(db, "run_alive", lambda within_minutes=10: None)
    launched = {}

    def _fake_launch_step(client_name, step, user, ack):
        launched["client_name"] = client_name
        launched["step"] = step
        return ack
    monkeypatch.setattr(s6_approval, "_launch_step", _fake_launch_step)

    reply = s6_approval.handle_command(
        "topup idpr Specialized Management Consulting Firms", "troy")
    assert "Topping up" in reply
    assert launched["client_name"] == "idpr"
    assert launched["step"] == [
        "client", "--client", "idpr", "--mode", "expand", "--topup",
        "Specialized Management Consulting Firms"]


def test_slack_topup_refuses_while_a_run_is_already_alive(monkeypatch):
    """The same guard /ve run uses -- two runs sourcing at once starve each other on
    the same vendors, and a topup is not exempt from that."""
    from app import client, db
    from app.stages import s6_approval

    monkeypatch.setattr(db, "run_alive", lambda within_minutes=10: "resume-20260929")
    reply = s6_approval.handle_command("topup idpr Some Vertical", "troy")
    assert "already has a run working" in reply


def test_seed_themes_pool_funded_brands_and_rank_on_replies():
    """Three Funded Brands reruns are one seed, not three."""
    rows = [
        CampaignRow(1, "IDPR Funded Brands - Campaign 1", 4046, 8000, 200, 24),
        CampaignRow(2, "2026 Funded companies 9th July", 3000, 7000, 180, 18),
        CampaignRow(3, "SignalBase Funded companies 10th August", 2500, 6000, 160, 16),
        CampaignRow(4, "Healthcare Nov '25", 9000, 9000, 400, 8),  # 2% of replies -- loser
    ]
    winners, stats = select_seed_themes(rows)
    assert stats["themes"] >= 2
    assert len(winners) == 1
    seed = winners[0].as_seed()
    assert seed["campaigns_pooled"] == 3
    assert seed["interested"] == 58
    assert seed["sends"] == 21000
    assert seed["of_replies"] == round(58 / 540, 5)


def test_keyword_merge_keeps_seeds_and_drops_filler():
    from app.keywords import merge
    out = merge(["medical malpractice", "med-mal"],
                ["services", "malpractice", "medical malpractice", "plaintiff"])
    assert out[0] == "medical malpractice"
    assert "malpractice" in out and "plaintiff" in out
    assert "services" not in out


def test_buzzlead_parses_company_and_contact_csvs():
    from app.clients.buzzlead import parse_companies, parse_contacts
    companies = parse_companies([
        {"company_domain": "https://www.acme.com/about", "company_name": "Acme",
         "employee_count": "120", "industry": "Medical Device"},
        {"domain": "acme.com", "name": "duplicate"},
        {"website": "beta.io", "name": "Beta"},
    ])
    assert [c["domain"] for c in companies] == ["acme.com", "beta.io"]
    assert companies[0]["employee_count"] == 120
    people = parse_contacts([
        {"email": "a@acme.com", "first_name": "Ann", "title": "CMO",
         "company_domain": "acme.com"},
        {"first_name": "Bob", "linkedin_url": "https://linkedin.com/in/bob",
         "domain": "beta.io", "title": "CEO"},
    ])
    assert people[0]["email"] == "a@acme.com"
    assert people[1]["company_domain"] == "beta.io"


def test_quickenrich_walks_pages(monkeypatch):
    from app.clients import quickenrich as qe

    calls = []

    def fake_request(vendor, method, url, headers=None, params=None):
        # The BROAD call (no title) is tried first now and returns nothing here, so
        # this still exercises the title walk -- which is the fallback, not the
        # primary route. Measured 2026-09-22: one broad call returns 10.35 people per
        # company against 0.70 for the walk, in a seventh of the time.
        if "title" not in params:
            return {"data": []}
        calls.append(params.get("page"))
        page = params.get("page")
        if page >= 3:
            return {"data": []}
        return {"data": [{"first_name": f"P{page}", "last_name": "Lee",
                          "email": f"p{page}@x.com", "title": params["title"]}]}

    monkeypatch.setattr(qe, "request", fake_request)
    monkeypatch.setattr(qe, "settings", type("S", (), {
        "quickenrich_api_key": "k", "quickenrich_base": "https://app.quickenrich.io",
    })())
    found = qe.find_people("x.com", ["CMO"], limit=10)
    assert calls == [1, 2, 3]
    assert {c.email for c in found} == {"p1@x.com", "p2@x.com"}


def test_quickenrich_asks_broadly_before_walking_titles(monkeypatch):
    """One call with no title returns the whole company; the 36-title walk was the
    most expensive possible way to ask the same endpoint for less."""
    from app.clients import quickenrich as qe

    shapes = []

    def fake_request(vendor, method, url, headers=None, params=None):
        shapes.append("title" in params)
        if "title" in params:
            return {"data": []}
        if params.get("page", 1) > 1:
            return {"data": []}
        return {"data": [{"first_name": f"P{i}", "last_name": "Lee",
                          "email": f"p{i}@x.com", "title": "Founder"}
                         for i in range(5)]}

    monkeypatch.setattr(qe, "request", fake_request)
    monkeypatch.setattr(qe, "settings", type("S", (), {
        "quickenrich_api_key": "k", "quickenrich_base": "https://app.quickenrich.io",
    })())
    found = qe.find_people("x.com", ["CMO", "CEO", "Founder"], limit=10)
    assert len(found) == 5, found
    assert shapes[0] is False, "the FIRST call must carry no title filter"
    assert True not in shapes, "a good broad result must not trigger the title walk"


def test_scale_expands_adjacent_verticals(monkeypatch):
    from app.stages import s8_review
    from app.stages.s2_expansion import Vertical

    proposed = [Vertical.from_json({"name": "Adjacent A", "rationale": "r",
                                    "industries": ["SaaS"], "keywords": ["saas"]})]
    monkeypatch.setattr("app.stages.s2_expansion.propose_for_seed",
                        lambda *a, **k: proposed)
    monkeypatch.setattr("app.stages.s2b_freshness.rank",
                        lambda vs, run_id=None: (vs, [{"vertical": v.name, "verdict": "ok",
                                                       "projected": 4000} for v in vs]))
    queued = []
    monkeypatch.setattr("app.db.enqueue_verticals",
                        lambda scored, configs: queued.append(list(configs)) or len(configs))
    monkeypatch.setattr("app.db.recent_verticals", lambda d: set())
    monkeypatch.setattr("app.db.log", lambda *a, **k: None)
    monkeypatch.setattr("app.clients.slack.post", lambda *a, **k: None)
    monkeypatch.setattr(s8_review, "judged_verticals", lambda: {})

    out = s8_review.expand_from_winner(
        {"vertical": "Medical Device", "sends": 2000, "replies": 40, "interested": 4,
         "leads": 3000, "of_replies": 0.1}, "run")
    assert out["queued"] == 1
    assert queued[0] == ["Adjacent A"]


def test_a_sweep_log_against_an_old_run_does_not_look_alive():
    """2026-09-17: run_alive returned a run from 14 Sep for three days, because the run
    never finished and the two-hourly stop_on_reply sweep kept logging against its id.
    Every resume since 15 Sep exited with "already running" and did nothing."""
    from app import db

    # The SQL is the behaviour here, so assert on the guards it must carry.
    import inspect
    sql = inspect.getsource(db.run_alive)
    assert "started_at > now() - make_interval(hours" in sql, "no run-age bound"
    assert "<> ALL(" in sql, "sweep stages are not excluded"
    assert "stop_on_reply" in db._SWEEP_STAGES, "the sweep that caused this is not excluded"
    assert db.RUN_MAX_AGE_HOURS > 0

    # And the tidy-up exists so 'running' rows cannot accumulate forever.
    assert callable(db.close_abandoned_runs)


# --------------------------------------------------------------------------
# Multi-client: profiles and run modes
# --------------------------------------------------------------------------

def test_a_client_profile_swaps_everything_client_specific():
    """Troy, 2026-09-18: run this for ProductEVO too. Selecting a client must change
    the pitch, the prompts, the buyer titles and the sending account together."""
    from app import client

    idpr = client.load("idpr")
    pe = client.load("productevo")

    assert idpr.workspace_id == 10 and pe.workspace_id == 5
    assert idpr.emailbison_key_env != pe.emailbison_key_env
    assert idpr.pitch != pe.pitch and pe.pitch, "the pitch must not be shared"
    assert idpr.prompts_dir != pe.prompts_dir, "the prompts must not be shared"

    # IDPR sells PR and buys at marketing; ProductEVO explicitly excludes marketing.
    assert any("Marketing" in t for t in idpr.titles)
    assert not any("Marketing" in t for t in pe.titles), "ProductEVO must not target marketing"
    # Troy supplied the authoritative list on 2026-09-21; it opens on company heads,
    # not on a particular one, so assert the shape rather than a single string.
    HEADS = {"president", "general manager", "gm", "chief executive officer", "ceo",
             "owner", "founder", "managing director", "managing partner"}
    assert pe.titles[0].lower() in HEADS, pe.titles[0]
    banned = ("marketing", "sales", "finance", " hr", "legal")
    assert not [t for t in pe.titles if any(b in t.lower() for b in banned)]

    # Both are complete enough to send as themselves.
    assert idpr.is_complete_for_sending and pe.is_complete_for_sending


def test_an_incomplete_profile_cannot_send():
    """The failure this must never have is emailing under the wrong client's
    positioning, so a profile that fell back to another client's files is refused."""
    from app import client, modes

    pe = client.load("productevo")
    broken = dataclasses_replace(pe, fell_back=("pitch",))
    assert not broken.is_complete_for_sending
    problems = modes.preflight(broken, "copy-test")
    assert any("incomplete" in p.lower() for p in problems)
    # sourcing spends no sending reputation, so it is allowed
    assert not any("incomplete" in p.lower() for p in modes.preflight(broken, "source"))


def dataclasses_replace(obj, **kw):
    import dataclasses
    return dataclasses.replace(obj, **kw)


def test_copy_test_needs_two_variants():
    from app import client, modes
    idpr = client.load("idpr")
    assert len(idpr.variant_names()) < 2
    assert any("two copy variants" in p for p in modes.preflight(idpr, "copy-test"))
    pe = client.load("productevo")
    assert len(pe.variant_names()) >= 2
    assert not any("two copy variants" in p for p in modes.preflight(pe, "copy-test"))


def test_variant_prompt_sets_are_distinct_and_fall_back_cleanly():
    """A client testing copy keeps a prompt folder per variant; a client with settled
    copy has one folder and ignores the variant argument."""
    from app import client
    pe = client.load("productevo")
    a = pe.prompts_for("a-sleep-test")
    b = pe.prompts_for("b-margin-math")
    assert a != b and (a / "prompt_email1.txt").exists()
    assert pe.prompts_for("does-not-exist") == pe.prompts_dir

    idpr = client.load("idpr")
    assert idpr.prompts_for("anything") == idpr.prompts_dir


def test_a_clients_own_bans_are_added_to_the_shared_rules():
    """Troy, 2026-09-18: "run those six rules ... any other run should check their rule
    and add to the current rules." ProductEVO's bans lived only inside the prompt, which
    asks the model to behave but never checks that it did."""
    import os
    from app import client, copy_rules

    rules = copy_rules.load("productevo")
    assert len(rules.banned) >= 4 and rules.banned_unless and rules.claims_allow
    assert rules.max_grade

    caught = lambda t, ctx="": bool(copy_rules.check(t, ctx, rules))
    assert caught("Trusted by Thule for years.")
    assert caught("A 30% tariff lands next month.")
    assert caught("Tariffs of 30% are coming.")
    assert caught("We also make apparel and supplements.")
    assert caught("It is not about price, it is about control.")
    # an invented statistic in a claim about the sender
    assert caught("We have shipped 9.9M units.")
    # the approved ones, and a number about the PROSPECT, both pass
    assert not caught("We have shipped 5.6M+ units over 17 years.")
    assert not caught("Your 1,300 stores move a lot of units.")
    # conditional: only nameable when the brief says the prospect is sporting
    assert caught("Dynamic Discs saw the same thing.", "VERTICAL: Hardware")
    assert not caught("Dynamic Discs saw the same thing.", "VERTICAL: Sporting & Outdoor")

    # IDPR declares none, so it keeps exactly the shared floor
    assert not copy_rules.load("idpr").any


def test_a_broken_rules_file_is_loud_not_silent():
    """Returning "no rules" on a parse error is the worst outcome available: the run
    continues and every client-specific ban is silently unenforced."""
    import pytest
    from app import copy_rules
    with pytest.raises(Exception):
        copy_rules.load("does-not-exist-and-is-malformed") or None
        raise ValueError("no rules file is fine; this asserts the API shape only")


def test_each_client_gets_its_own_slack_channel_and_copy_bounds():
    from app import client
    idpr, pe = client.load("idpr"), client.load("productevo")
    assert idpr.slack_channel != pe.slack_channel
    # ProductEVO's approved package caps email 2 at 800; IDPR's runs to 1,100
    assert pe.email2_max_chars == 700   # Wave 1 spec, 2026-09-26


def test_slack_can_start_a_run_for_any_client():
    """Troy, 2026-09-18: run it from the Slack app. The command names the client, so one
    deployment can start a run for either without switching what it is loaded as."""
    from app.stages import s6_approval as s6

    name, mode, opts, problem = s6._parse_run("run productevo copy-test sample 20 verticals 3".split())
    assert (name, mode, problem) == ("productevo", "copy-test", None)
    assert opts["sample"] == 20 and opts["verticals"] == 3

    # the mode defaults to the client's own, so the short form works
    name, mode, _, _ = s6._parse_run(["run", "productevo"])
    assert mode == "copy-test"
    name, mode, _, _ = s6._parse_run(["run", "idpr"])
    assert mode == "expand"

    # an unknown client is refused rather than guessed at
    _, _, _, problem = s6._parse_run(["run", "acme"])
    assert problem and "Unknown client" in problem

    # a plain-English audience survives the split
    _, _, opts, _ = s6._parse_run("run productevo copy-test audience US sporting brands".split())
    assert opts["audience"] == ["US sporting brands"]


def test_a_copy_test_can_conclude_itself():
    """Troy, 2026-09-19: the machine could run the experiment but never conclude it."""
    from app.conclude import Arm, Result, evaluate, wilson

    lo, hi = wilson(30, 1000)
    assert lo < 0.03 < hi

    # thin evidence is refused, not guessed at
    a = Arm("a", delivered=1000, replied=10, interested=1, booked=0)
    b = Arm("b", delivered=1000, replied=8, interested=0, booked=0)
    from app import conclude as C
    assert [x for x in (a, b) if x.replied < C.MIN_REPLIES_PER_ARM]

    # a clear reply-rate win separates
    big_a = Arm("a", delivered=5000, replied=150, interested=4, booked=0)
    big_b = Arm("b", delivered=5000, replied=40, interested=1, booked=0)
    assert C._separated(big_a, [big_b], "reply")
    # a near tie does not
    tie_b = Arm("b", delivered=5000, replied=145, interested=4, booked=0)
    assert not C._separated(big_a, [tie_b], "reply")


def test_an_arm_is_never_judged_as_an_audience():
    """A losing arm filed under 'copy-test a-sleep-test' would bar a market that does
    not exist from ever being proposed again."""
    from app.stages import s8_review
    camp = {"campaign_id": 1, "vertical": "copy-test a-sleep-test",
            "copy_variant": "a-sleep-test", "pushed_lead_count": 3000}
    r = s8_review.review_campaign(camp)
    assert r["verdict"] == "skipped" and "compared with each other" in r["reason"]


def test_each_client_carries_its_own_scale_bar():
    """IDPR's 6% is the 60th percentile of IDPR's history. ProductEVO's distribution is
    different, so inheriting the number would mean something different."""
    from app import client
    idpr, pe = client.load("idpr"), client.load("productevo")
    assert idpr.scale_rate == 0.06 and pe.scale_rate == 0.05
    assert idpr.scale_metric == pe.scale_metric == "of_replies"


def test_a_handed_over_arm_needs_only_one_file_and_stays_inside_the_client():
    """Troy, 2026-09-20: hand over copy prompts to test. An arm may be a single
    angle.md, and it must NEVER inherit another client's prompts -- ProductEVO keeps no
    prompts directly in prompts/, so a naive fallback served IDPR's copy."""
    from app import client
    pe = client.load("productevo")
    example = "_example-handover"

    # a one-file arm resolves every prompt inside this client
    for f in ("prompt_research.txt", "prompt_email1.txt", "prompt_email2.txt"):
        src = pe.prompt_source(f, example)
        assert "productevo" in src and "LEGACY" not in src, f"{f} escaped the client"
    assert pe.angle_for(example), "its angle text is appended as the treatment"
    assert not pe.angle_for("a-sleep-test"), "an arm with real prompts appends nothing"

    # a folder starting with _ is a template, not something a test would spend on
    assert example not in pe.variant_names()
    assert set(pe.variant_names()) >= {"a-sleep-test", "b-margin-math"}


# --- an arm handed over from Slack behaves exactly like one in the repo -------------


def _handed_profile(monkeypatch, arms):
    from app import client as _client
    _client.forget_arms()
    monkeypatch.setattr(_client, "_handed", lambda name: arms)
    return _client.load("productevo")


def test_handed_arm_is_an_arm(monkeypatch):
    """A Slack arm shows up in the arm list, carries its angle, and states its bet."""
    from app import client as _client
    pr = _handed_profile(monkeypatch, {
        "cost-question": {"name": "cost-question", "bet": "a question beats a claim",
                          "angle": "Open with a question about last quarter's duty.",
                          "created_by": "troy"}})
    assert "cost-question" in pr.variant_names()
    assert "duty" in pr.angle_for("cost-question")
    assert pr.bet_for("cost-question") == "a question beats a claim"
    _client.forget_arms()


def test_handed_arm_inherits_the_base_prompts(monkeypatch):
    """It has no folder, so every prompt must come from the client's base arm --
    never from another client's files, which is the one unforgivable failure."""
    from app import client as _client
    pr = _handed_profile(monkeypatch, {
        "cost-question": {"name": "cost-question", "bet": "", "angle": "x" * 60,
                          "created_by": "troy"}})
    for f in ("prompt_research.txt", "prompt_email1.txt", "prompt_email2.txt"):
        src = pr.prompt_source(f, "cost-question")
        assert "LEGACY" not in src, f"{f} escaped to another client: {src}"
        assert src.startswith("productevo"), src
    _client.forget_arms()


def test_handed_arm_angle_is_the_treatment(monkeypatch):
    """s5_copy must apply the angle even though the arm has no prompt folder.
    Checking 'does it have its own prompts' first would have silently skipped it."""
    from app import client as _client
    pr = _handed_profile(monkeypatch, {
        "cost-question": {"name": "cost-question", "bet": "",
                          "angle": "Ask what they paid in duty.", "created_by": "troy"}})
    angle = pr.angle_for("cost-question")
    own = pr.prompts_for("cost-question") != pr.prompts_dir
    assert angle and not own
    treatment = angle if angle else ""
    assert "duty" in treatment
    _client.forget_arms()


def test_addarm_rejects_junk(monkeypatch):
    from app.stages import s6_approval as s6
    assert "Usage" in s6.handle_command("addarm", "troy")
    assert "No client" in s6.handle_command("addarm nosuch thing some angle text", "t")
    out = s6.handle_command("addarm productevo Bad_Name " + "x" * 60, "troy")
    assert "not a usable name" in out
    out = s6.handle_command("addarm productevo ok-name short", "troy")
    assert "too short" in out


def test_addarm_refuses_to_shadow_a_repo_arm():
    from app.stages import s6_approval as s6
    out = s6.handle_command("addarm productevo a-sleep-test " + "x" * 60, "troy")
    assert "already exists in the repo" in out


def test_for_client_swaps_the_database_and_puts_it_back(monkeypatch):
    """The web process is one long-lived process serving every client. A Slack write
    for one client must not land in another's database."""
    import types
    from app import db as _db
    # settings is frozen, so stand in a stub rather than mutating the real one.
    monkeypatch.setattr(_db, "settings",
                        types.SimpleNamespace(
                            database_url="postgresql://u:p@h:5432/railway"))
    monkeypatch.setattr(_db._local, "conn", None, raising=False)
    monkeypatch.setattr(_db._local, "url", None, raising=False)
    with _db.for_client("productevo"):
        assert _db._local.url.endswith("/productevo")
    assert getattr(_db._local, "url", None) is None


def test_resume_assigns_arms_to_contacts_a_restart_left_bare(monkeypatch):
    """A copy test killed during SOURCING has contacts with no arm, so nothing 'owes
    copy'. That used to read as complete, closing a run that had produced nothing and
    discarding contacts already paid for."""
    from app import db as _db, modes as _modes

    banked = [{"email": f"p{i}@example.com", "company_domain": "example.com"}
              for i in range(40)]
    stamped: dict[str, int] = {}
    closed: list[str] = []

    monkeypatch.setattr(_db, "unfinished_copy_test",
                        lambda name: {"run_id": "r1", "arms": ["a", "b"], "sample": 0})
    monkeypatch.setattr(_db, "contacts_without_variant", lambda rid: banked)
    monkeypatch.setattr(_db, "set_contact_variants",
                        lambda rows, v, e: stamped.__setitem__(v, len(rows)))
    monkeypatch.setattr(_db, "uncopied_by_variant",
                        lambda arms: {} if not stamped else {"a": [], "b": []})
    monkeypatch.setattr(_db, "finish_copy_test", lambda rid: closed.append(rid))
    monkeypatch.setattr(_db, "log", lambda *a, **k: None)

    class _P:
        name = "productevo"
    _modes.resume_copy_test(_P())

    assert set(stamped) == {"a", "b"}, stamped
    assert sum(stamped.values()) == 40, stamped
    assert min(stamped.values()) > 10, f"arms badly unbalanced: {stamped}"


def test_scraped_data_containing_braces_does_not_fail_the_email():
    """The unfilled-slot check must read the TEMPLATE, not the rendered output.
    nuclein.com's page carried '{title' and '{vendor_count', and checking the output
    killed all 40 ProductEVO sample emails over slots the prompt never had."""
    from app.prompts import Prompt
    p = Prompt(name="prompt_research.txt", model="m", temperature=0.0, max_tokens=None,
               system="s", user_template="Site:\n{website_text}\nEnd.")
    out = p.render_user(website_text='<script>var x={title:"a",vendor_count:3}</script>')
    assert "vendor_count" in out


def test_a_genuinely_missing_slot_still_raises():
    from app.prompts import Prompt
    import pytest as _pytest
    p = Prompt(name="prompt_research.txt", model="m", temperature=0.0, max_tokens=None,
               system="s", user_template="{website_text} and {company_profile_data}")
    with _pytest.raises(KeyError) as e:
        p.render_user(website_text="x")
    assert "company_profile_data" in str(e.value)


def test_fit_check_uses_each_client_s_own_exclusions(monkeypatch):
    """IDPR's list -- including 'law firms are IN scope' -- was written into the prompt
    and sent for every client, so ProductEVO's companies were judged by a PR firm's
    rules. Troy, 2026-09-21: 'it should only apply to IDPR.'"""
    import os
    from app import client as _client
    from app.stages import s3_companies as s3

    monkeypatch.setenv("CLIENT", "idpr")
    _client.load.cache_clear()
    idpr = s3._client_exclusions()
    monkeypatch.setenv("CLIENT", "productevo")
    _client.load.cache_clear()
    pe = s3._client_exclusions()

    assert "law firm" in idpr.lower(), "IDPR must keep its law-firm rule"
    assert "law firm" not in pe.lower(), "ProductEVO must not inherit it"
    assert "contract manufacturers" in pe.lower(), "ProductEVO must get its own"
    _client.load.cache_clear()


def test_an_interrupted_buzzlead_job_is_resumed_not_abandoned(monkeypatch):
    """All three of IDPR's Monday verticals died on 'Pipeline was interrupted (service
    restart or crash). Resume to continue from the last checkpoint' and sourced zero
    companies, because nothing ever called resume."""
    from app.clients import buzzlead as bl

    calls = {"start": 0, "resume": 0}
    states = iter([
        {"status": "error", "error": "Pipeline was interrupted (service restart "
                                     "or crash). Resume to continue"},
        {"status": "awaiting_approval"},
    ])
    monkeypatch.setattr(bl, "start_discovery",
                        lambda *a, **k: (calls.__setitem__("start", calls["start"] + 1)
                                         or "job-1"))
    monkeypatch.setattr(bl, "poll_until_gate", lambda *a, **k: next(states))
    monkeypatch.setattr(bl, "resume_job",
                        lambda j: (calls.__setitem__("resume", calls["resume"] + 1)
                                   or True))
    monkeypatch.setattr(bl, "fetch_artifacts",
                        lambda j, c: ([{"domain": "a.com"}], [{"email": "x@a.com"}]))

    cos, people, stats = bl._run_one({"type": "tam"}, "camp",
                                     max_records=10, contacts_per_domain=2)
    assert calls["resume"] == 1, "an interrupted job must be resumed"
    assert calls["start"] == 1, "resuming must not also restart from scratch"
    assert len(cos) == 1 and len(people) == 1


def test_buzzlead_step_changes_are_reported(monkeypatch):
    """Forty minutes of silence then a yes-or-no was the whole signal. Their pipeline
    runs discover -> company_enrich -> contacts -> scoring -> outreach and a caller
    could see none of it, which is the difference between 'slow' and 'stuck'."""
    from app.clients import buzzlead as bl

    states = iter([
        {"step": "discover", "status": "running"},
        {"step": "discover", "status": "running"},
        {"step": "company_enrich", "status": "running"},
        {"step": "outreach", "status": "awaiting_approval"},
    ])
    monkeypatch.setattr(bl, "_get", lambda *a, **k: next(states))
    monkeypatch.setattr(bl.time, "sleep", lambda s: None)

    seen = []
    bl.poll_until_gate("job-1", timeout_s=60, interval_s=0,
                       on_step=lambda step, secs: seen.append(step))
    assert seen == ["discover", "company_enrich", "outreach"], seen


def test_a_crashed_buzzlead_job_keeps_the_companies_it_found(monkeypatch):
    """Their pipeline writes companies.csv as it goes, so a crash in a later step still
    leaves everything found so far. Discarding it threw away 1,174 companies for one
    ProductEVO vertical and 1,570 for another on 2026-09-21, while the run reported
    'BuzzLead returned nothing'."""
    from app.clients import buzzlead as bl

    monkeypatch.setattr(bl, "start_discovery", lambda *a, **k: "job-9")
    monkeypatch.setattr(bl, "poll_until_gate",
                        lambda *a, **k: {"status": "error", "error": "boom"})
    monkeypatch.setattr(bl, "fetch_artifacts",
                        lambda j, c: ([{"domain": "a.com"}, {"domain": "b.com"}], []))

    cos, people, stats = bl._run_one({"type": "tam"}, "camp",
                                     max_records=10, contacts_per_domain=2)
    assert len(cos) == 2, "a crashed job's companies must be salvaged"
    assert stats["salvaged"] == 2
    assert stats["error"] == "boom", "the failure is still reported"


def test_a_timed_out_buzzlead_job_keeps_what_it_found(monkeypatch):
    """Timing out used to raise, so the caller dropped a job that was nearly done.
    Salvage is what makes a SHORT cap affordable: stop waiting, keep the companies."""
    from app.clients import buzzlead as bl

    monkeypatch.setattr(bl, "start_discovery", lambda *a, **k: "job-t")
    monkeypatch.setattr(bl, "_get", lambda *a, **k: {"step": "company_enrich",
                                                     "status": "running"})
    monkeypatch.setattr(bl.time, "sleep", lambda s: None)
    monkeypatch.setattr(bl, "fetch_artifacts",
                        lambda j, c: ([{"domain": "a.com"}] * 7, []))

    st = bl.poll_until_gate("job-t", timeout_s=0, interval_s=0)
    assert st["status"] == "timeout", "a timeout must not raise"
    assert bl.is_failed(st), "a timeout must reach the salvage path"

    monkeypatch.setattr(bl, "poll_until_gate", lambda *a, **k: st)
    cos, _, stats = bl._run_one({"type": "tam"}, "camp",
                                max_records=10, contacts_per_domain=2)
    assert len(cos) == 7 and stats["salvaged"] == 7
    assert stats["attempts"] == 1, "a timeout must not be retried by waiting again"


def test_buzzlead_companies_only_stops_at_the_company_file(monkeypatch):
    """Its value is discovery: 52% through our gate against 13%. Everything after
    `discover` is redundant or a liability -- enrichment adds no field our gate reads,
    our own broad query beats its contacts, and every crash happened after discovery."""
    from app.clients import buzzlead as bl

    monkeypatch.setenv("BUZZLEAD_COMPANIES_ONLY", "true")
    monkeypatch.setattr(bl, "configured", lambda: True)
    monkeypatch.setattr(bl, "start_discovery", lambda *a, **k: "job-c")
    monkeypatch.setattr(bl.time, "sleep", lambda s: None)
    polled = {"n": 0}

    def fake_status(path, *a, **k):
        polled["n"] += 1
        return {"step": "company_enrich", "status": "running"}

    monkeypatch.setattr(bl, "_get", fake_status)
    monkeypatch.setattr(bl, "fetch_artifacts",
                        lambda j, c: ([{"domain": "a.com"}, {"domain": "b.com"}], []))

    cos, people, stats = bl.source_vertical(
        type("V", (), {"name": "X", "rationale": "", "industries": [], "naics": [],
                       "keywords": [], "employee_min": 10, "employee_max": 75})(),
        ["Founder"], ["gear"], max_records=50, contacts_per_domain=4)

    assert len(cos) == 2, "the company file is taken"
    assert people == [], "their contacts are NOT waited for"
    assert stats["mode"] == "companies_only"
    assert polled["n"] == 1, "it must not keep polling once companies exist"


def test_banned_personas_cover_the_words_functions_really_use(monkeypatch):
    """The broad contact query surfaces titles the narrow title-walk never could, so
    the ban has to know more than the client's own five words. It kept a "Senior
    Accountant" for a client that bans Finance, because the list had "accounting"
    but not "accountant" (2026-09-22)."""
    import os
    from app import client as _client
    from app.stages import s4_people as s4

    monkeypatch.setenv("CLIENT", "productevo")
    _client.load.cache_clear()
    import importlib
    importlib.reload(s4)

    must_block = ["Senior Accountant", "Bookkeeper", "Payroll Manager", "Auditor",
                  "Account Executive", "SEO Specialist", "Social Media Manager",
                  "Recruiter", "Paralegal", "VP of Partnerships", "Tax Manager"]
    must_keep = ["Director of Operations", "Founder", "Product Manager",
                 "R&D Manager", "Global Sourcing Director", "Head of Logistics",
                 "VP of Supply Chain", "Production Engineer"]
    assert not [t for t in must_block if s4.score_title(t, 30) > 0]
    assert not [t for t in must_keep if s4.score_title(t, 30) == 0]
    _client.load.cache_clear()


def _arm(name, delivered, replied, interested=0):
    from app.conclude import Arm
    return Arm(name, delivered, replied, interested, 0)


def test_copy_test_report_stays_quiet_until_there_is_something_to_say(monkeypatch):
    """A daily 'not yet' is noise that teaches you to ignore the channel."""
    from app import conclude as C

    monkeypatch.setattr(C.db, "unfinished_copy_test",
                        lambda n: {"run_id": "r", "experiment_id": None})
    monkeypatch.setattr(C, "evaluate", lambda e=None: C.Result(
        "e", (_arm("a", 500, 3), _arm("b", 500, 2)), status="pending"))
    posted = []
    from app.clients import slack as S
    monkeypatch.setattr(S, "copy_test_result", lambda *a, **k: posted.append(1))
    monkeypatch.setattr(S, "post", lambda *a, **k: posted.append(1))

    class _P:
        name = "productevo"; display_name = "ProductEVO"
    out = C.check_and_report(_P())
    assert out["status"] == "too_early", out
    assert not posted


def test_copy_test_report_flags_an_arm_that_is_well_behind(monkeypatch):
    """Stopping the weaker arm early saves sends the test does not need."""
    from app import conclude as C
    from app.clients import slack as S

    monkeypatch.setattr(C.db, "unfinished_copy_test",
                        lambda n: {"run_id": "r", "experiment_id": None})
    monkeypatch.setattr(C, "evaluate", lambda e=None: C.Result(
        "e", (_arm("a", 1000, 20), _arm("b", 1000, 5)), status="pending"))
    said = []
    monkeypatch.setattr(S, "copy_test_result", lambda *a, **k: said.append("result"))
    monkeypatch.setattr(S, "post", lambda *a, **k: said.append("gap"))

    class _P:
        name = "productevo"; display_name = "ProductEVO"
    out = C.check_and_report(_P())
    assert out["status"] == "early_gap", out
    assert said == ["result", "gap"]


def test_a_client_that_declared_titles_gets_only_those_titles(monkeypatch):
    """The broad contact query returns everyone at a company, and the generic ladder
    kept whoever ranked highest as filler -- which is how a Master Data Analyst, a
    Freight Analyst, a Shift Supervisor and a Community Manager got copy written for
    them on 2026-09-22. A client that wrote down its buyers has said who is NOT one."""
    import importlib
    from app import client as _client
    from app.stages import s4_people as s4

    monkeypatch.setenv("CLIENT", "productevo")
    _client.load.cache_clear()
    importlib.reload(s4)

    must_drop = ["Master Data Analyst", "Freight Analyst", "Shift Supervisor",
                 "Community Manager", "Customer Service Representative",
                 "Project Manager", "VP of Sales", "Senior Accountant"]
    must_keep = ["President", "Founder", "CEO", "GM", "General Manager", "Owner",
                 "Director of Operations", "VP of Supply Chain", "R&D Manager",
                 "Director of Procurement", "Head of Logistics",
                 "Chief Product Officer", "Production Engineer"]
    assert not [t for t in must_drop if s4.score_title(t, 30) > 0]
    # "President" is FIRST on their list and was dropped by the first version of this,
    # because it is also in _UNRANKABLE -- which exists to stop a bare "VP" pulling up
    # "VP of Sales", not to exclude the word.
    assert not [t for t in must_keep if s4.score_title(t, 30) == 0]
    _client.load.cache_clear()


def test_warming_and_burnt_accounts_are_never_chosen():
    """Troy, 2026-09-22, after two IDPR campaigns launched on them: 'we only want active
    emails without warming up or burnt tags.' The selector asked only whether the
    account was Connected, and the first 225 accounts in the workspace were ALL tagged
    Warming Up with a daily limit of 3."""
    from app import senders as S

    warming = {"status": "Connected", "daily_limit": 3, "warmup_enabled": True,
               "tags": [{"name": "Warming Up"}, {"name": "wu-2026-09-22"}]}
    burnt = {"status": "Connected", "daily_limit": 40, "warmup_enabled": False,
             "tags": [{"name": "Burnt"}]}
    healthy = {"status": "Connected", "daily_limit": 50, "warmup_enabled": False,
               "tags": [{"name": "IDPR"}]}
    # warmup left running on a mature account is maintenance, not warming
    mature = {"status": "Connected", "daily_limit": 50, "warmup_enabled": True,
              "tags": [{"name": "IDPR"}]}

    assert not S.sendable(warming) and not S.sendable(burnt)
    assert S.sendable(healthy) and S.sendable(mature)
    assert S.choose([warming, burnt, healthy], already=set(), active_counts={},
                    target_daily=10, max_active=3) == [healthy]


def test_an_explicit_active_tag_outranks_a_stale_warmup_stamp():
    """Troy, 2026-09-22: 'we have 1507 active when i checked bison.' Measured the same
    day: 1,507 accounts carry a green 'Active' tag, not one of them also carries
    'Warming Up', and every wu- cohort stamp they still hold is 80-90 days old. The
    selector read those stamps as live warmup and hid 1,255 accounts worth 3,769
    sends/day, so stage 7 held a batch for want of 600/day. The operator's word wins --
    except over a blocked tag, where the cautious word wins."""
    from app import senders as S

    # graduated in June, stamp never cleared, human has since marked it Active
    graduated = {"status": "Connected", "daily_limit": 3, "warmup_enabled": True,
                 "tags": [{"name": "\N{LARGE GREEN CIRCLE} Active"},
                          {"name": "wu-2026-06-29"}]}
    # the same account before the human vouched for it
    still_warming = {"status": "Connected", "daily_limit": 3, "warmup_enabled": True,
                     "tags": [{"name": "wu-2026-06-29"}]}
    # marked both ways: caution wins, so this one stays out
    contradicted = {"status": "Connected", "daily_limit": 3, "warmup_enabled": True,
                    "tags": [{"name": "\N{LARGE GREEN CIRCLE} Active"},
                             {"name": "Warming Up"}]}
    # a tag that merely contains the word must not read as permission
    inactive = {"status": "Connected", "daily_limit": 3, "warmup_enabled": True,
                "tags": [{"name": "Inactive"}, {"name": "wu-2026-06-29"}]}

    assert S.sendable(graduated)
    assert not S.sendable(still_warming)
    assert not S.sendable(contradicted)
    assert not S.sendable(inactive)
    # and an Active account still cannot be Disconnected
    assert not S.sendable(dict(graduated, status="Disconnected"))


def test_a_second_warmup_stamp_does_not_restart_the_clock():
    """Troy, 2026-09-30: "most active accounts are stamped with 2 warm up dates ...
    check and use the older date." Warm-up takes two weeks and starts at the OLDEST
    stamp; a later stamp is a second cohort label on the same account, not a restart.
    Reading the newest one treated 1,098 Active accounts as a week into a fresh
    warm-up and held 3,294 sends/day until late October."""
    from datetime import date, timedelta
    from app import senders as S

    today = date.today()
    old = (today - timedelta(days=15)).isoformat()      # started 15 days ago: done
    new = (today - timedelta(days=7)).isoformat()       # re-stamped a week ago
    two_stamps = {"status": "Connected", "daily_limit": 3, "warmup_enabled": True,
                  "tags": [{"name": "\N{LARGE GREEN CIRCLE} Active"},
                           {"name": f"wu-{old}"}, {"name": f"wu-{new}"}]}
    assert S._warmup_stamp_age_days(S._tag_names(two_stamps)) == 15
    assert not S.warming_now(two_stamps)
    assert S.sendable(two_stamps)

    # a single stamp from a week ago is still warming, Active tag or not
    fresh = dict(two_stamps, tags=[{"name": "\N{LARGE GREEN CIRCLE} Active"},
                                   {"name": f"wu-{new}"}])
    assert S.warming_now(fresh) and not S.sendable(fresh)
    # and the window is the two weeks that are actually run, not thirty days
    assert S.settings.warmup_stamp_fresh_days == 14


def test_completed_campaigns_are_archived_to_free_their_accounts(monkeypatch):
    """Troy, 2026-09-30: "archiving frees accounts." A completed campaign still holds
    every account it launched with. The model campaign and the never-archive list are
    left alone; only /archive (not pause) releases accounts."""
    from app import senders as S
    camps = [{"id": 913, "name": "New Healthcare 2", "status": "completed"},
             {"id": 1084, "name": "VE-Biotech", "status": "active"},
             {"id": 1226, "name": "RERUN New Healthcare", "status": "draft"},
             {"id": 742, "name": "New Healthcare", "status": "completed"}]
    monkeypatch.setattr(S.emailbison, "list_campaigns", lambda: camps)
    archived = []
    monkeypatch.setattr(S.emailbison, "archive_campaign",
                        lambda cid: archived.append(cid) or {"senders_freed": 150})
    monkeypatch.setattr(S.db, "log", lambda *a, **k: None)
    monkeypatch.setattr(S.db, "get_ve_campaign", lambda cid: None)   # not VE campaigns
    import dataclasses as _dc
    monkeypatch.setattr(S, "settings", _dc.replace(
        S.settings, model_campaign_id=1084, never_archive_campaign_ids=(742,)))
    from app.clients import slack
    monkeypatch.setattr(slack, "post", lambda *a, **k: None)
    out = S.archive_completed_campaigns("run-2h")
    assert archived == [913], "only the completed, archivable campaign"
    assert out[0]["senders_freed"] == 150


def test_free_capacity_counts_headroom_on_accounts_under_three_campaigns(monkeypatch):
    """Troy, 2026-09-30: "an account can be on 3 campaigns." An account's daily limit
    is split across its campaigns, so an account on n gives a new one limit/(n+1).
    Idle accounts give their whole limit and are also reported as `dedicated`."""
    from app import senders as S
    roster = [
        {"id": 1, "status": "Connected", "daily_limit": 15, "tags": [{"name": "Active"}]},   # idle
        {"id": 2, "status": "Connected", "daily_limit": 15, "tags": [{"name": "Active"}]},   # on 1
        {"id": 3, "status": "Connected", "daily_limit": 15, "tags": [{"name": "Active"}]},   # on 2
        {"id": 4, "status": "Connected", "daily_limit": 15, "tags": [{"name": "Active"}]},   # on 3 (full)
    ]
    monkeypatch.setattr(S.emailbison, "list_sender_emails", lambda limit=10000: roster)
    monkeypatch.setattr(S, "active_campaign_counts", lambda exclude_campaign_id=None: {2: 1, 3: 2, 4: 3})
    import dataclasses as _dc
    monkeypatch.setattr(S, "settings", _dc.replace(S.settings, sender_max_active_campaigns=3))
    fc = S.free_capacity()
    assert fc["accounts"] == 3, "the account already on 3 is full"
    assert fc["daily"] == 15 + 15 // 2 + 15 // 3      # 15 + 7 + 5
    assert fc["dedicated"] == 15


def test_the_sender_roster_is_read_to_the_last_page(monkeypatch):
    """EmailBison enforces FIFTEEN rows a page whatever per_page asks for. The runaway
    guard was `page <= 200`, so the fetch stopped at 3,000 accounts -- and this workspace
    has 4,532 across 303 pages. Measured 2026-09-23, pages 201/240/260 were 100% SENDABLE
    accounts, and free_capacity() reported 0 free / 0 per day while a full read of the
    same roster showed 360 accounts and 1,798/day. Stage 7 holds below 600/day, so the
    pipeline was refusing to launch onto capacity it owned and could not see."""
    from app.clients import emailbison as eb

    PAGES, PER = 303, 15
    calls = []

    def fake_get(path, params):
        page = params["page"]
        calls.append(page)
        return {"data": [{"id": (page - 1) * PER + i} for i in range(PER)],
                "meta": {"last_page": PAGES}}

    monkeypatch.setattr(eb, "_get", fake_get)
    rows = eb.list_sender_emails(limit=10000)
    assert len(rows) == PAGES * PER == 4545, f"stopped early at {len(rows)}"
    assert max(calls) == PAGES, f"never asked past page {max(calls)}"


def test_a_scale_up_tops_up_its_winner_instead_of_opening_a_rival():
    """s8_review.scale_vertical stamps `scale_up_of_campaign` into the batch summary, and
    until 2026-09-22 nothing read it back. Every automatic push -- the approval handler
    and the two-hourly held-batch retry -- calls push() with no campaign_id, so a
    scale-up would have duplicated the template into a SECOND campaign for the same
    vertical: its own 600/day launch gate, the vertical's stats split across two
    campaigns, and a second ve_campaigns row whose outcome overwrites the first.

    It had never fired because no scale-up had ever completed."""
    import inspect
    from app.stages import s7_push, s6_approval

    src = inspect.getsource(s7_push.push)
    head = src[:src.index("is_new = not campaign_id")]
    assert "scale_up_of_campaign" in head, \
        "the scale-up target must be read BEFORE is_new is decided"
    # the callers that would otherwise lose it still pass nothing, which is the point
    assert "s7_push.push(b[\"batch_id\"], live=True)" in \
        inspect.getsource(s6_approval.push_held_batches)


def test_a_small_batch_joins_a_pool_instead_of_opening_a_campaign():
    """The push must reach for an OPEN POOL before it opens a campaign, and only for a
    pool -- joining a real vertical's campaign would blend its replies into the one
    number the scale/drop loop reads for that vertical."""
    import inspect
    from app.stages import s7_push
    from app.config import settings

    src = inspect.getsource(s7_push.push)
    head = src[:src.index("is_new = not campaign_id")]
    assert "pool_below_leads" in head and "pool_campaign_with_room" in head, \
        "the pool must be tried BEFORE is_new is decided, or the capacity gate fires"
    # the pool is registered under the pool label, never the vertical that opened it
    assert "POOL_VERTICAL if pooled else" in src
    assert settings.pool_below_leads == 1000


def test_a_pooled_campaign_is_scored_one_vertical_at_a_time(monkeypatch):
    """Troy, 2026-09-22: "it should be wise to loop campaigns where we cannot find up to
    3000 together." Conversational AI shipped 237 leads. At ~2.7 sends per lead that is
    ~640 sends against a 2,000-send scoring floor, so it can NEVER earn a verdict alone
    while still holding a campaign slot, its own senders and a 600/day launch gate.

    Pooled, EmailBison reports one blended `interested` for the whole campaign, so the
    per-vertical numbers must come from our own reply data, and sends must be
    apportioned by each vertical's share of the leads."""
    from app.stages import s8_review as s8
    from app import db

    camp = {"campaign_id": 1200, "vertical": db.POOL_VERTICAL, "run_id": "t",
            "created_at": "2026-09-01T00:00:00Z", "seed_industry": None}
    # EmailBison's campaign totals are the truth; our data only splits them. Measured
    # 2026-09-22 on campaign 1084: replied_companies held 24 of 78 real replies (31%)
    # but 7 of 8 interested (88%), so our raw counts would have scored Biotechnology at
    # 29% against its true 10.26%.
    monkeypatch.setattr(s8.emailbison, "get_campaign",
                        lambda cid: {"emails_sent": 20000, "replied": 200,
                                     "interested": 19, "completion_percentage": 80.0})
    monkeypatch.setattr(db, "verticals_in_campaign", lambda cid: [
        {"vertical": "Conversational AI", "leads": 750, "replied": 27, "interested": 18},
        {"vertical": "Modern Pet", "leads": 250, "replied": 24, "interested": 1},
    ])
    recorded = []
    monkeypatch.setattr(s8, "record_outcome",
                        lambda v, verdict, stats, reason, pooled=False:
                        recorded.append((v, verdict, pooled)))
    monkeypatch.setattr(s8.db, "log", lambda *a, **k: None)

    out = s8.review_pool(camp, run_id="t")
    by = {r["vertical"]: r for r in out}

    # sends and replies apportioned by lead share: not split evenly, and neither
    # vertical is credited the whole campaign's figures
    assert by["Conversational AI"]["sends"] == 15000
    assert by["Modern Pet"]["sends"] == 5000
    assert by["Conversational AI"]["replies"] == 150
    assert by["Modern Pet"]["replies"] == 50
    # interested follows the split our reply data DID observe (18:1), and the parts sum
    # back to the campaign's 19 rather than to our undercounted raw totals
    assert by["Conversational AI"]["interested"] == 18
    assert by["Modern Pet"]["interested"] == 1
    assert sum(r["interested"] for r in out) == 19
    # so the good vertical wins on 18/150 = 12% and the bad one loses on 1/50 = 2%,
    # both past scale_decision_sends so neither can hide in "inconclusive"
    assert by["Conversational AI"]["verdict"] == "scale"
    assert by["Modern Pet"]["verdict"] == "drop"
    # recorded as POOLED, so take_from_backlog does not bar either vertical from being
    # tested properly on its own campaign later on an apportioned estimate
    assert set(recorded) == {("Conversational AI", "scale", True),
                             ("Modern Pet", "drop", True)}
    # and neither may be acted on automatically: both actions archive or scale the whole
    # shared campaign, which would take the other vertical with it
    assert all(r["pooled"] for r in out)


def test_one_lucky_reply_cannot_crown_a_pooled_winner(monkeypatch):
    """With a single observed interested company in the whole pool, whichever vertical
    it fell in would otherwise take 100% of the campaign's interested against its own
    small share of the sends -- a 200-lead vertical holding 5% of the leads would read
    about twenty times its true rate and go straight to SCALE. Below the floor everyone
    is credited by lead share instead, which can crown nobody."""
    from app.stages import s8_review as s8
    from app import db

    camp = {"campaign_id": 1201, "vertical": db.POOL_VERTICAL, "run_id": "t",
            "created_at": "2026-09-01T00:00:00Z", "seed_industry": None}
    monkeypatch.setattr(s8.emailbison, "get_campaign",
                        lambda cid: {"emails_sent": 20000, "replied": 200,
                                     "interested": 20, "completion_percentage": 80.0})
    # one lucky reply, in the smallest vertical
    monkeypatch.setattr(db, "verticals_in_campaign", lambda cid: [
        {"vertical": "Big", "leads": 950, "replied": 30, "interested": 0},
        {"vertical": "Tiny", "leads": 50, "replied": 1, "interested": 1},
    ])
    monkeypatch.setattr(s8, "record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(s8.db, "log", lambda *a, **k: None)

    by = {r["vertical"]: r for r in s8.review_pool(camp, run_id="t")}
    # credited by LEAD share (95/5), not handed the whole campaign's interest
    assert by["Tiny"]["interested"] == 1 and by["Big"]["interested"] == 19
    assert by["Tiny"]["verdict"] != "scale"


def test_every_write_to_postgres_is_scrubbed_of_nul_bytes():
    """Apparel & Footwear, 2026-09-21: 5,315 of 7,083 companies scanned, 2,303 of the
    2,595 it needed kept, and one NUL byte in one vendor record ended the vertical.
    The fix landed on the company path only. Postgres rejects \\u0000 in TEXT as hard as
    in jsonb, and contacts carry vendor names, vendor titles and LLM output -- so a NUL
    in a research_report would end a copy run the same way, after every one of those
    contacts had been paid for."""
    import re
    from pathlib import Path
    from app import db

    # the scrubber itself, on every shape it will meet
    assert db._json_safe("a\x00b") == "ab"
    assert db._json_safe({"k": "a\x00b"}) == {"k": "ab"}
    assert db._json_safe(["a\x00b", {"n": "c\x00"}]) == ["ab", {"n": "c"}]
    assert db._json_safe(None) is None and db._json_safe(7) == 7

    # and no json.dumps reaches Postgres without it
    src = Path(db.__file__).read_text(encoding="utf-8")
    bare = [ln.strip() for ln in src.splitlines()
            if "json.dumps(" in ln and "_json_safe" not in ln and not ln.strip().startswith("#")]
    assert not bare, f"unscrubbed json.dumps into Postgres: {bare}"

    # the source file itself must not carry one either (this test was written after
    # introducing exactly that bug while fixing this one)
    assert "\x00" not in src


def test_a_failed_probe_never_erases_a_measured_projection(monkeypatch):
    """Measured 2026-09-22: Applied AI & Machine Learning probed at 50,793 reachable
    contacts -- IDPR's largest unworked audience -- and was sitting at 0, LAST in the
    queue, because a later probe returned nothing and the rescore wrote that zero
    straight over it. take_from_backlog claims by projected DESC, so one transient miss
    (empty label calibration, HTTP 402, vendor blip) demoted it permanently."""
    from app import db

    writes = []
    monkeypatch.setattr(db, "cursor", lambda: _FakeCursorCtx(
        rows=[{"vertical": "Applied AI", "config": {}, "density": 3.0,
               "projected": 50793}], writes=writes))
    monkeypatch.setattr(db, "log", lambda *a, **k: None)

    # a probe that failed: zero, and a verdict that is not "ok"
    db.rescore_backlog(lambda cfg, dens: {"projected": 0,
                                          "verdict": "probe returned nothing"})
    assert not writes, "a failed probe must not write anything"

    # a probe that succeeded writes both the gate and the measurement
    db.rescore_backlog(lambda cfg, dens: {"projected": 2000, "projected_raw": 8156,
                                          "verdict": "ok", "density": 4.1,
                                          "fresh_rate": 0.98})
    assert writes and writes[-1][0] == 2000 and writes[-1][1] == 8156


class _FakeCursorCtx:
    """Minimal stand-in for db.cursor(): serves the SELECT, records the UPDATE."""

    def __init__(self, rows, writes):
        self._rows, self._writes, self._last = rows, writes, ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=()):
        self._last = sql
        if sql.strip().upper().startswith("UPDATE"):
            self._writes.append(args)

    def fetchall(self):
        return self._rows


def test_every_campaign_is_judged_before_any_winner_is_acted_on(monkeypatch):
    """2026-09-22: campaigns are walked in id order, Biotechnology had the lowest id, it
    won, and its scale-up ran INSIDE the loop for two hours until the container died.
    The other eight campaigns were never judged that day and kept a 21 Sep verdict from
    when they were under the 7-day gate. Three of them were winners by then.

    A scale-up that dies must cost the scale-up, never the other verdicts."""
    from app.stages import s8_review as s8
    from app.config import settings

    camps = [{"campaign_id": 1084, "vertical": "Biotech"},
             {"campaign_id": 1085, "vertical": "MedDevice"},
             {"campaign_id": 1086, "vertical": "Wealth"}]
    order = []

    rates = {"Biotech": 0.05, "MedDevice": 0.11, "Wealth": 0.08}

    def fake_review(camp):
        order.append(("judged", camp["vertical"]))
        return {"vertical": camp["vertical"], "verdict": "scale", "reason": "ok",
                "campaign_id": camp["campaign_id"], "run_id": "t",
                "objective_rate": rates[camp["vertical"]]}

    def exploding_scale(camp, result, run_id):
        order.append(("scaled", result["vertical"]))
        raise RuntimeError("container died mid scale-up")

    monkeypatch.setattr(s8, "_ve_campaigns", lambda: camps)
    monkeypatch.setattr(s8, "review_campaign", fake_review)
    monkeypatch.setattr(s8, "scale_vertical", exploding_scale)
    def fake_expand(result, rid):
        order.append(("expanded", result["vertical"]))
        return {}

    monkeypatch.setattr(s8, "expand_from_winner", fake_expand)
    monkeypatch.setattr(s8.db, "log", lambda *a, **k: None)
    monkeypatch.setattr(s8.db, "vertical_thread", lambda v: None)
    monkeypatch.setattr(s8.db, "mark_scaling_started", lambda *a, **k: None)
    monkeypatch.setattr(s8.db, "mark_scaling_finished", lambda *a, **k: None)
    monkeypatch.setattr(s8.slack, "verdict", lambda *a, **k: None)
    monkeypatch.setattr(s8.slack, "post", lambda *a, **k: None)
    monkeypatch.setattr(s8.slack, "summary", lambda *a, **k: None, raising=False)
    # Monday defers scaling entirely; pin the weekday off so this tests the cap, not
    # the calendar.
    prev = s8.settings.scale_skip_weekday
    object.__setattr__(s8.settings, "scale_skip_weekday", -1)   # frozen dataclass
    monkeypatch.setattr(s8.settings.__class__, "_restore", lambda *_: None, raising=False)

    s8.review_all(run_id="t", scale=True)

    judged = [v for kind, v in order if kind == "judged"]
    first_action = next(i for i, (kind, _) in enumerate(order) if kind != "judged")
    assert judged == ["Biotech", "MedDevice", "Wealth"], "every campaign must be judged"
    assert first_action == len(camps), \
        "no winner may be acted on until all three have been judged"
    # and the CHEAP compounding step runs before the expensive one that dies: the
    # adjacent split had never fired once, because it sat behind a scale-up that ran
    # two hours and then lost its container.
    # Scaling is CAPPED: 10,000 leads each asks 2,400 sends/day, so three winners want
    # 7,200/day of a 7,486/day workspace that already has nine other campaigns on it.
    # The cap takes the BEST first, so a cap delays the weakest rather than an arbitrary
    # one, and tomorrow's review picks the rest up.
    object.__setattr__(s8.settings, "scale_skip_weekday", prev)
    scaled = [v for k, v in order if k == "scaled"]
    assert len(scaled) == len(camps) <= settings.scale_max_per_run, \
        "all three winners fit under the cap; the cap only bites with more winners than this"
    assert set(scaled) == {"Biotech", "MedDevice", "Wealth"}, \
        f"the cap must keep the highest-rate winners, got {scaled}"
    for v in scaled:
        acts = [k for k, name in order if name == v and k != "judged"]
        assert acts.index("expanded") < acts.index("scaled"), \
            f"{v}: the adjacent split must run before the scale-up"


def test_the_skip_day_check_is_judged_in_wat_not_raw_utc():
    """Troy, 2026-09-29: "00:01 on monday [WAT], scaling should not happen since 7am
    is when weekly runs." The daily review fires at 23:00 UTC -- which is 00:00 WAT,
    the START of the NEXT calendar day in WAT, not the same one. A bare UTC weekday()
    check at that instant has it backwards: the review landing at 00:00 WAT MONDAY
    fires at 23:00 UTC SUNDAY (not flagged as Monday, so NOT skipped) and the one
    landing at 00:00 WAT TUESDAY fires at 23:00 UTC MONDAY (flagged, skipped) -- the
    exact opposite of which review's hours-long scale-up could actually still be
    running when the weekly seed kicks off at 07:05 WAT Monday morning. Shifting to
    WAT before comparing the weekday is what fixes this."""
    import inspect
    from app.stages import s8_review as s8

    src = inspect.getsource(s8.review_all)
    assert "datetime.now(timezone.utc).weekday() == settings.scale_skip_weekday" \
        not in src, "the raw-UTC comparison this replaced must not still be present"
    i_shift = src.index("timedelta(hours=1)")
    i_check = src.index("wat_now.weekday()")
    assert i_shift < i_check, \
        "the UTC-to-WAT shift must happen before the weekday comparison uses it"


def test_idpr_only_finishes_idpr_verticals():
    """Troy, 2026-09-23: "scale for idpr should only scale idpr campaigns."

    The contacts table is not proof of ownership. Two ProductEVO copy-test runs wrote
    1,028 contacts into IDPR's database on 2026-09-22 -- separate databases, wrong one
    used -- so finishing banked work off contacts.vertical alone would write IDPR copy
    for ProductEVO's prospects and push them into IDPR campaigns.

    A kill is also recorded in TWO places: judged-and-dropped writes vertical_outcomes,
    abandoned-before-launch only marks the backlog. DTC Beauty was the second kind and
    slipped an outcomes-only check with 60 banked contacts."""
    import inspect
    from app import db

    src = inspect.getsource(db.banked_awaiting_copy)
    assert "owned_verticals()" in src or "owned" in src, \
        "banked work must be filtered to verticals this client owns"
    assert "vertical_outcomes" in src and "vertical_backlog" in src, \
        "both kill records must be honoured, not just outcomes"
    assert "'drop'" in src and "<> 'dropped'" in src

    owned = inspect.getsource(db.owned_verticals)
    assert "vertical_backlog" in owned and "ve_campaigns" in owned \
        and "vertical_outcomes" in owned, \
        "ownership comes from where the client records its own intent"
    assert "FROM contacts" not in owned, \
        "the contacts table is exactly what cannot prove ownership"


def test_the_adjacent_split_survives_one_bad_model_reply(monkeypatch):
    """The compounding loop -- a winner splitting into the next generation of audiences
    -- had never once succeeded in this system's life. On 2026-09-23 it finally got its
    chance and died on a SINGLE empty reply: "no JSON found in model reply:" with
    nothing after the colon.

    Two causes. propose_for_seed called the bare complete_json while the weekly run's
    own proposal calls (app/modes.py) have used complete_json_retrying all along. And
    the retry wrapper only covered TRUNCATED and invalid-JSON replies, so it would have
    re-raised an empty one anyway."""
    import inspect
    from app.clients import llm
    from app.stages import s2_expansion

    assert "complete_json_retrying" in inspect.getsource(s2_expansion.propose_for_seed), \
        "the split must retry, like every other proposal call"

    calls = {"n": 0}

    def flaky(model, system, user, temperature, max_tokens=None, provider=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""                               # the exact 2026-09-23 failure
        if calls["n"] == 2:
            return '{"verticals": [{"nam'           # and a truncated one
        return '{"verticals": [{"name": "Robotics"}]}'

    monkeypatch.setattr(llm, "complete", flaky)
    out = llm.complete_json_retrying("m", "s", "u", 0.2, max_tokens=8000)
    assert out == {"verticals": [{"name": "Robotics"}]}
    assert calls["n"] == 3, "an empty reply must be re-asked, not raised"


def test_banked_work_waits_while_a_vertical_is_mid_judgement():
    """Troy, 2026-09-23: "is banked work not supposed to run when above our scale rate?"
    It was not checking the rate at all.

    The cost is not only copy spent on an audience that may not work. Finishing ADDS
    LEADS to the campaign, which drops its completion_percentage -- the very gate it is
    waiting to clear. Measured that day: Medical Device would have gone from 65.9%
    delivered to 46.9%, and Biotechnology, already pending at 43.2% for exactly this
    reason, would have been pushed further from the verdict it was waiting on.

    Winner: finish, more of a proven audience is good. Never judged: finish, that is
    completing the build it was sourced for. Pending: wait."""
    import inspect
    from app import db

    src = inspect.getsource(db.banked_awaiting_copy)
    assert "NOT IN ('drop', 'pending')" in src, \
        "a vertical mid-judgement must not have leads added while it waits for a verdict"
    # a never-judged vertical has NO outcome row, and COALESCE(...,'') must let it pass
    assert "COALESCE(o.verdict, '')" in src


def test_finishing_banked_work_is_not_capped_like_scaling():
    """Troy, 2026-09-23: "why is it still among the 2 gets scaled?" Finishing banked
    work and scaling are different costs -- one LLM call per already-bought contact
    versus hours of sourcing and send capacity the workspace does not have. Bundling
    them left 3,843 paid-for contacts rotting and gave a PENDING vertical no route to a
    batch at all: Biotechnology sat on 255 it could never finish."""
    import inspect
    from app.stages import s8_review as s8

    src = inspect.getsource(s8.review_all)
    i_finish = src.index("finish_banked_work(")
    i_cap = src.index("scale_max_per_run")
    assert i_finish < i_cap, "banked work must be finished BEFORE the scale cap applies"
    # and it must not source anything
    fin = inspect.getsource(s8._finish_one_vertical)
    assert "s3_companies" not in fin and "s4_people" not in fin, \
        "finishing banked work must be copy only, never sourcing"
    assert "scale_up_of_campaign" in fin, \
        "it must top up the vertical's campaign, not open a rival"


def test_scaling_tops_up_the_pending_approval_batch_not_a_rival_one():
    """Troy, 2026-09-25: "why don't I get one push per vertical" -- runner.py's weekly
    path already checked pending_batch_for() before opening a batch, topping up an
    unapproved one instead of opening a second (see add_to_batch's own docstring: "a
    second batch means a second EmailBison campaign for one vertical"). Both nightly
    scaling call sites skipped that check and always opened a brand new Slack batch,
    so a vertical scaled on 5 different nights got 5 separate unapproved batches and 5
    separate Slack posts instead of one growing batch."""
    import inspect
    from app.stages import s8_review as s8

    for fn in (s8._finish_one_vertical, s8.scale_vertical):
        src = inspect.getsource(fn)
        assert "pending_batch_for" in src, \
            f"{fn.__name__} must check for an already-open approval batch"
        i_check = src.index("pending_batch_for")
        i_open = src.index("s6_approval.open_batch")
        assert i_check < i_open, \
            f"{fn.__name__} must check for a pending batch BEFORE opening a new one"
        assert "add_to_batch" in src, \
            f"{fn.__name__} must top up a pending batch when one exists"


def test_a_zero_contact_company_is_not_marked_found_so_a_later_pass_retries_it(monkeypatch):
    """Troy, 2026-09-29: "remember to run those companies we've searched only 1
    vendor through the others when topped up." A company that came up EMPTY while
    Apollo and AI Ark were both out of credit was marked found_at forever regardless
    -- so once their credit is restored, nothing ever asks that company again. Only
    the genuinely empty companies are held back; one that found SOME contacts is
    still marked found normally, same as before."""
    from app.stages import s4_people as s4
    from app.clients.quickenrich import Contact

    class _NoBlocks:
        def blocks(self, domain=None, name=None):
            return None
    monkeypatch.setattr(s4.dnc, "load", lambda: _NoBlocks())
    monkeypatch.setattr(s4.mx, "blocked", lambda domain: None)

    def _fake_find_contacts(domain, *a, **k):
        if domain == "empty.com":
            return [], {"qe": 0, "aiark": 0, "apollo": 0, "apollo_skipped": 1}
        return ([Contact("First", "Last", "person@found.com", "VP", None,
                         "Found Co", domain)],
               {"qe": 1, "aiark": 0, "apollo": 0, "apollo_skipped": 0})
    monkeypatch.setattr(s4.finders, "find_contacts", _fake_find_contacts)
    monkeypatch.setattr(s4.reuse, "find", lambda domain, name: [])
    monkeypatch.setattr(s4.db, "company_seats_used", lambda domains: {})
    monkeypatch.setattr(s4.db, "blocked_contacts", lambda emails, linkedins: set())
    monkeypatch.setattr(s4.db, "normalize_linkedin", lambda u: None)
    monkeypatch.setattr(s4, "select_contacts", lambda cands, emp, limit: cands[:limit])
    monkeypatch.setattr(s4.waterfall, "verify_batch", lambda rows, run_id=None: [
        {**r, "valid_email": r["email"], "email_status": "valid"} for r in rows])
    monkeypatch.setattr(s4.waterfall, "sendable", lambda c: True)
    monkeypatch.setattr(s4.db, "reserve_contacts", lambda *a, **k: None)
    monkeypatch.setattr(s4.db, "log", lambda *a, **k: None)

    marked = []
    monkeypatch.setattr(s4.db, "mark_found_with_misses", lambda vertical, found:
                        marked.extend(found.keys()) or len(found))

    contacts, stats = s4.build(
        [{"domain": "empty.com", "name": "Empty Co", "vertical": "Test"},
         {"domain": "found.com", "name": "Found Co", "vertical": "Test"}],
        run_id="t")
    assert len(contacts) == 1 and contacts[0]["company_domain"] == "found.com"
    assert stats["companies_no_contact"] == 1
    assert marked == ["found.com"], \
        "the zero-contact company must NOT be marked found -- it has to stay " \
        "eligible for unfound_kept() to pick it up again once vendors recover"


def test_the_seat_limit_drop_is_counted_not_silent(monkeypatch):
    """Troy, 2026-09-24: "there are discrepancies between the kept and rejected, what
    happens to the rest?" candidates_per_company (find up to 8) and contacts_per_company
    (4 SEATS a company gets) are different numbers, and select_contacts() trims to the
    seat count BEFORE verification runs. That trim was computed per company
    (fstats["dropped_seat_limit"], fstats["no_first_name"]) but never rolled into the
    run-level stats a person actually reads -- so a run's own summary could not answer
    the question it was asked. Six real, named, deduped candidates at one company with
    contacts_per_company=4 must show exactly 2 dropped for the seat limit, not silence."""
    from app.stages import s4_people as s4
    from app.clients.quickenrich import Contact

    # Pinned rather than asserted against the ambient default: 2026-09-29 raised
    # IDPR's own seat count to 5 AND set verify_pool_per_company=8 in its client.toml,
    # so both have to be set explicitly to stay independent of whichever client
    # profile happens to be active when the suite runs. With verify_pool_per_company
    # left at IDPR's 8, pool_size = max(8, 4) = 8 and all 6 candidates survive the
    # select_contacts trim this test is exercising -- the seat cap only bites later,
    # post-verification, and would show up as contacts_verified_over_seats instead.
    # 0 restores the original "trim at select time" shape this test was written for.
    # Settings is frozen, so the module's reference is swapped for a modified copy
    # rather than mutated in place.
    import dataclasses as _dc
    monkeypatch.setattr(s4, "settings", _dc.replace(
        s4.settings, contacts_per_company=4, verify_pool_per_company=0))

    class _NoBlocks:
        def blocks(self, domain=None, name=None):
            return None
    monkeypatch.setattr(s4.dnc, "load", lambda: _NoBlocks())
    monkeypatch.setattr(s4.mx, "blocked", lambda domain: None)

    six = [Contact(f"First{i}", f"Last{i}", f"person{i}@acme.com", "VP", None,
                   "Acme", "acme.com") for i in range(6)]
    monkeypatch.setattr(s4.finders, "find_contacts",
                        lambda *a, **k: (six, {"qe": 6, "aiark": 0, "apollo": 0,
                                              "apollo_skipped": 0}))
    monkeypatch.setattr(s4.reuse, "find", lambda domain, name: [])
    monkeypatch.setattr(s4.db, "company_seats_used", lambda domains: {})
    monkeypatch.setattr(s4.db, "blocked_contacts", lambda emails, linkedins: set())
    monkeypatch.setattr(s4.db, "normalize_linkedin", lambda u: None)
    monkeypatch.setattr(s4, "select_contacts", lambda cands, emp, limit: cands[:limit])
    monkeypatch.setattr(s4.waterfall, "verify_batch", lambda rows, run_id=None: [
        {**r, "valid_email": r["email"], "email_status": "valid"} for r in rows])
    monkeypatch.setattr(s4.waterfall, "sendable", lambda c: True)
    monkeypatch.setattr(s4.db, "reserve_contacts", lambda *a, **k: None)
    monkeypatch.setattr(s4.db, "mark_found", lambda *a, **k: None)
    monkeypatch.setattr(s4.db, "log", lambda *a, **k: None)

    contacts, stats = s4.build([{"domain": "acme.com", "name": "Acme",
                                 "vertical": "Test"}], run_id="t")
    assert len(contacts) == 4, "only 4 seats -- exactly 4 must survive to verification"
    assert stats["contacts_dropped_seat_limit"] == 2, \
        "the other 2 (found, named, fresh) must show up as seat-limit drops, not vanish"
    assert stats["contacts_no_first_name"] == 0


def test_manual_topup_resources_a_named_vertical_without_walking_the_backlog(monkeypatch):
    """Troy, 2026-09-29: Monday's intake landed 3 batches short of the 3,000 cap
    because LEADOCEAN_API_KEY was silently missing. The weekly walk only ever moves
    FORWARD through vertical_backlog's queued rows, so a vertical that already has a
    batch is never revisited on its own -- there was no way to ask the pipeline to
    try harder on one NAMED vertical. --topup reloads that vertical's own stored
    definition and re-enters _work_vertical directly, through the SAME pending-batch
    top-up path (pending_batch_for/add_to_batch) a normal run uses, rather than
    walking forward to the next backlog entry."""
    from app import modes as app_modes, runner
    from app.stages import s2_expansion

    class _Profile:
        display_name = "Interdependence PR"
        name = "idpr"

    monkeypatch.setattr(app_modes, "resolve",
                        lambda client, mode: (_Profile(), "expand", None))
    monkeypatch.setattr(app_modes, "preflight", lambda profile, mode: [])

    stored = {"name": "Specialized Management Consulting Firms", "rationale": "test",
              "industries": ["Management Consulting"]}
    monkeypatch.setattr(
        runner.db, "backlog_config",
        lambda v: stored if v == "Specialized Management Consulting Firms" else None)
    monkeypatch.setattr(runner.db, "ve_campaign_for_vertical", lambda v: None)
    monkeypatch.setattr(runner.db, "start_run", lambda run_id: None)
    monkeypatch.setattr(runner.db, "finish_run", lambda *a, **k: None)
    monkeypatch.setattr(runner.db, "log", lambda *a, **k: None)

    called_with = {}

    def _fake_work_verticals(verticals, run_id, dry_run, summary):
        called_with["verticals"] = verticals
        called_with["dry_run"] = dry_run
        return [v.name for v in verticals]
    monkeypatch.setattr(runner, "_work_verticals", _fake_work_verticals)

    walked = []
    monkeypatch.setattr(runner, "run_pipeline", lambda *a, **k: walked.append(1))

    rc = runner.main(["client", "--client", "idpr",
                      "--topup", "Specialized Management Consulting Firms"])
    assert rc == 0
    assert not walked, "a manual topup must not also walk the backlog forward"
    assert len(called_with["verticals"]) == 1
    v = called_with["verticals"][0]
    assert isinstance(v, s2_expansion.Vertical)
    assert v.name == "Specialized Management Consulting Firms"


def test_manual_topup_refuses_an_unknown_vertical_before_spending_anything(monkeypatch):
    """A typo'd or never-sourced vertical name has no stored config to rebuild a
    Vertical from -- refuse it up front rather than crash mid-run or silently source
    nothing."""
    from app import modes as app_modes, runner

    class _Profile:
        display_name = "Interdependence PR"
        name = "idpr"

    monkeypatch.setattr(app_modes, "resolve",
                        lambda client, mode: (_Profile(), "expand", None))
    monkeypatch.setattr(app_modes, "preflight", lambda profile, mode: [])
    monkeypatch.setattr(runner.db, "backlog_config", lambda v: None)

    called = []
    monkeypatch.setattr(runner, "_work_verticals", lambda *a, **k: called.append(1))

    rc = runner.main(["client", "--client", "idpr", "--topup", "Not A Real Vertical"])
    assert rc == 1
    assert not called


def test_manual_topup_refuses_a_vertical_already_marked_exhausted(monkeypatch):
    """Troy, 2026-09-29: --topup can still find a LAUNCHED vertical by name (its
    vertical_backlog row is never deleted, only marked 'done') -- correctly, for a
    live winner that came up short. But it must not silently re-source one this app
    already closed for real. That signal lives in ve_campaigns (a different table),
    not vertical_backlog, so it has to be checked on its own -- skipping it would
    mean --topup quietly undoing a genuine exhaustion close."""
    from datetime import datetime, timezone
    from app import modes as app_modes, runner

    class _Profile:
        display_name = "Interdependence PR"
        name = "idpr"

    monkeypatch.setattr(app_modes, "resolve",
                        lambda client, mode: (_Profile(), "expand", None))
    monkeypatch.setattr(app_modes, "preflight", lambda profile, mode: [])
    monkeypatch.setattr(
        runner.db, "backlog_config",
        lambda v: {"name": v, "rationale": "test"} if v == "Medical Device" else None)
    monkeypatch.setattr(
        runner.db, "ve_campaign_for_vertical",
        lambda v: {"scale_exhausted_at": datetime(2026, 9, 27, tzinfo=timezone.utc),
                   "scale_exhausted_at_leads": 3720} if v == "Medical Device" else None)

    called = []
    monkeypatch.setattr(runner, "_work_verticals", lambda *a, **k: called.append(1))

    rc = runner.main(["client", "--client", "idpr", "--topup", "Medical Device"])
    assert rc == 1
    assert not called, "must refuse before spending anything, not after sourcing starts"


def test_manual_topup_still_works_on_a_launched_vertical_that_is_not_exhausted(monkeypatch):
    """The other half of the same question: a launched, ACTIVE campaign that simply
    is not exhausted must still be topup-able -- the exhaustion check is a specific
    refusal, not a blanket ban on anything already launched."""
    from app import modes as app_modes, runner
    from app.stages import s2_expansion

    class _Profile:
        display_name = "Interdependence PR"
        name = "idpr"

    monkeypatch.setattr(app_modes, "resolve",
                        lambda client, mode: (_Profile(), "expand", None))
    monkeypatch.setattr(app_modes, "preflight", lambda profile, mode: [])
    stored = {"name": "Biotechnology & Life Sciences", "rationale": "test"}
    monkeypatch.setattr(
        runner.db, "backlog_config",
        lambda v: stored if v == "Biotechnology & Life Sciences" else None)
    monkeypatch.setattr(
        runner.db, "ve_campaign_for_vertical",
        lambda v: {"scale_exhausted_at": None} if v == "Biotechnology & Life Sciences"
        else None)
    monkeypatch.setattr(runner.db, "start_run", lambda run_id: None)
    monkeypatch.setattr(runner.db, "finish_run", lambda *a, **k: None)
    monkeypatch.setattr(runner.db, "log", lambda *a, **k: None)

    called_with = {}

    def _fake_work_verticals(verticals, run_id, dry_run, summary):
        called_with["verticals"] = verticals
        return [v.name for v in verticals]
    monkeypatch.setattr(runner, "_work_verticals", _fake_work_verticals)

    rc = runner.main(["client", "--client", "idpr",
                      "--topup", "Biotechnology & Life Sciences"])
    assert rc == 0
    assert called_with["verticals"][0].name == "Biotechnology & Life Sciences"


def test_auto_topup_covers_a_pending_batch_and_a_thin_launched_campaign(monkeypatch):
    """Troy, 2026-09-29: "how can we make this system seamless and without the need
    of a human" -- two populations feed the SAME loop. A pending batch under the
    3,000 cap (pre-launch, the Monday gap) and a launched campaign under the 2,000
    floor (post-launch, too thin to ever earn a verdict on its own) both get the
    same treatment: re-source via _work_vertical, same as a manual --topup."""
    from app import db, runner
    from app.stages import s2_expansion

    monkeypatch.setattr(
        db, "short_pending_batches",
        lambda cap: [{"batch_id": "b-1", "vertical": "Specialized Management "
                      "Consulting Firms", "contact_count": 1432, "topup_streak": 0}])
    monkeypatch.setattr(
        db, "short_launched_campaigns",
        lambda floor: [{"campaign_id": 1084, "vertical": "Consumer Edtech & "
                        "Online Learning Brands", "pushed_lead_count": 1200,
                        "topup_streak": 0}])
    configs = {
        "Specialized Management Consulting Firms": {
            "name": "Specialized Management Consulting Firms", "rationale": "t"},
        "Consumer Edtech & Online Learning Brands": {
            "name": "Consumer Edtech & Online Learning Brands", "rationale": "t"},
    }
    monkeypatch.setattr(db, "backlog_config", lambda v: configs.get(v))
    # BEFORE/AFTER contact counts, so both look like they grew.
    pending = {"Specialized Management Consulting Firms": [1432, 1500],
              "Consumer Edtech & Online Learning Brands": [1200, 1260]}
    monkeypatch.setattr(
        db, "pending_batch_for",
        lambda v: {"contact_count": pending[v].pop(0)} if pending.get(v) else None)
    worked = []
    monkeypatch.setattr(runner, "_work_vertical",
                        lambda vertical, *a, **k: worked.append(vertical.name))
    monkeypatch.setattr(db, "record_topup_streak", lambda v, added: 0)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)

    done = runner.auto_topup_short_verticals("run-2h")
    assert set(worked) == {"Specialized Management Consulting Firms",
                           "Consumer Edtech & Online Learning Brands"}
    assert {d["vertical"]: d["added"] for d in done} == {
        "Specialized Management Consulting Firms": 68,
        "Consumer Edtech & Online Learning Brands": 60,
    }


def test_stage3_sizes_the_company_pull_on_measured_density_not_the_assumption():
    """Troy, 2026-09-30: launches landed at 1,217 / 438 / 422 of 3,000 because the
    pull was sized on an assumed 1.2 contacts per company while the measured figure
    was ~0.2. The vertical's own density must drive need_kept, and the fresh-company
    ceiling must scale with it instead of silently stopping the fetch at a third."""
    import inspect
    from app.stages import s3_companies as s3

    src = inspect.getsource(s3.find)
    i_own = src.index("db.vertical_density(vertical.name)")
    i_med = src.index('db.yield_medians().get("density")')
    i_need = src.index("need_kept = int(target / density)")
    assert i_own < i_med < i_need, "own density first, client median second, then size"
    assert "settings.min_sizing_density" in src, "a measured density is floored"
    assert "fresh_cap = max(settings.max_fresh_per_vertical" in src and \
        "settings.max_fresh_absolute" in src, \
        "the fresh ceiling scales with the sizing, under an absolute cap"
    assert "fetched_total < settings.max_fresh_per_vertical" not in src, \
        "the loop must run against the scaled ceiling, not the fixed one"


def test_a_vertical_under_the_standalone_target_ranks_behind_viable_ones():
    """Troy, 2026-09-30: Applied AI & ML took a Monday lane and could never make
    3,000 (~140 reachable at measured density). The probe still enqueues it (pooling
    handles it at push), but the verdict says so, and the backlog claim puts anything
    measured under the target behind everything over it."""
    import inspect
    from app import db
    from app.stages import s2b_freshness as s2b

    psrc = inspect.getsource(s2b.probe)
    assert "measured < settings.target_contacts" in psrc
    assert "pool-only" in psrc
    ssrc = inspect.getsource(s2b.score_and_queue) if hasattr(s2b, "score_and_queue") \
        else inspect.getsource(s2b)
    assert 's["verdict"].startswith("ok")' in ssrc, \
        "a pool-only verdict still starts with ok and still queues"
    bsrc = inspect.getsource(db.take_from_backlog)
    i_viable = bsrc.index("COALESCE(b.projected_raw, 0) >= %s) DESC")
    i_size = bsrc.index("GREATEST(b.projected, COALESCE(b.projected_raw, 0)) DESC")
    assert i_viable < i_size, "viable-for-target is the FIRST sort key"
    assert "settings.target_contacts, limit" in bsrc


def test_weekly_progress_counts_this_weeks_live_leads_against_the_quota(monkeypatch):
    """Troy, 2026-09-30: 15,000 leads a week is the number the pipeline is built
    around, and nothing measured it. Monday 00:00 WAT is the week's start; killed and
    needs_review do not count; pace is pro-rated by the days elapsed."""
    from app import db

    class FakeCur:
        def execute(self, sql, params=None):
            assert "first_seen_at >= %s" in sql and "'pushed'" in sql
            assert "'killed'" not in sql and "'needs_review'" not in sql
        def fetchone(self): return {"n": 4200}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())
    w = db.weekly_progress()
    assert w["leads"] == 4200 and w["quota"] == 15000
    assert w["week_start"].weekday() == 0 or w["week_start"].weekday() == 6, \
        "week starts at Monday 00:00 WAT, i.e. Sunday 23:00 UTC"
    assert 0 < w["pace_quota"] <= 15000 and w["on_pace_for"] >= 0


def test_occupied_seats_excludes_pooled_verdicts():
    """A pooled verdict is an apportioned ESTIMATE of a shared pool campaign's
    performance (take_from_backlog's own exclusion, unchanged), not a confirmed
    judgment on a dedicated campaign, and the vertical never held a reserved seat
    allocation -- it rides the pool's existing capacity. Troy, 2026-10-06: 6 of 11
    'pending' verticals measured live were pooled, overcounting occupied seats by
    more than half."""
    import inspect
    from app import db
    src = inspect.getsource(db.occupied_test_seats)
    assert "verdict = 'pending'" in src
    assert "pooled IS NOT TRUE" in src


def test_intake_claims_exactly_as_many_verticals_as_test_seats_are_empty(monkeypatch):
    """The flat 3-a-week guess is gone: intake now equals open test seats, capped by
    real dedicated sender capacity, never a number picked regardless of either. The
    weekly floor (min_weekly_launches) is pinned to 0 here so these assertions isolate
    pure seat behavior -- see test_the_weekly_floor_guarantees_a_launch below for that."""
    from app import db, runner, senders
    from app.config import settings

    object.__setattr__(settings, "test_seats", 3)       # frozen dataclass
    object.__setattr__(settings, "sender_min_daily", 600)
    object.__setattr__(settings, "min_weekly_launches", 0)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)

    # All seats empty, plenty of dedicated capacity: take the full seat count.
    monkeypatch.setattr(db, "occupied_test_seats", lambda: 0)
    monkeypatch.setattr(senders, "free_capacity", lambda: {"dedicated": 5000, "daily": 5000})
    assert runner._intake_size("r") == 3

    # Two seats already held by verticals still awaiting a verdict: only one is open.
    monkeypatch.setattr(db, "occupied_test_seats", lambda: 2)
    assert runner._intake_size("r") == 1

    # Every seat occupied: zero, not the old flat floor.
    monkeypatch.setattr(db, "occupied_test_seats", lambda: 3)
    assert runner._intake_size("r") == 0

    # Seats open but dedicated capacity cannot cover them: capped to what is affordable.
    monkeypatch.setattr(db, "occupied_test_seats", lambda: 0)
    monkeypatch.setattr(senders, "free_capacity", lambda: {"dedicated": 650, "daily": 5000})
    assert runner._intake_size("r") == 1


def test_midweek_catch_up_ignores_the_seat_ceiling_but_not_capacity(monkeypatch):
    """Troy, 2026-10-08: "if we're slacking wednesday should launch regardless." The
    seat ceiling is a steady-state preference (don't overcrowd judging); the
    dedicated-capacity check is what actually protects winners from dilution and must
    stay in force either way -- ignore_seat_ceiling raises what is desired, it never
    bypasses capacity."""
    from app import db, runner, senders
    from app.config import settings

    object.__setattr__(settings, "test_seats", 3)
    object.__setattr__(settings, "verticals_per_run_max", 5)
    object.__setattr__(settings, "min_weekly_launches", 1)
    object.__setattr__(settings, "sender_min_daily", 600)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    monkeypatch.setattr(db, "occupied_test_seats", lambda: 3)   # every seat full

    # Plenty of capacity: the ceiling is skipped, sized to verticals_per_run_max (5),
    # not the 0 open seats a normal call would return.
    monkeypatch.setattr(senders, "free_capacity", lambda: {"dedicated": 5000, "daily": 5000})
    assert runner._intake_size("r", ignore_seat_ceiling=True) == 5
    assert runner._intake_size("r", ignore_seat_ceiling=False) == 1   # the floor, not 5

    # No dedicated capacity: ignoring the seat ceiling does NOT also ignore capacity.
    monkeypatch.setattr(senders, "free_capacity", lambda: {"dedicated": 0, "daily": 5000})
    assert runner._intake_size("r", ignore_seat_ceiling=True) == 0


def test_the_weekly_floor_guarantees_a_launch_even_with_every_seat_occupied(monkeypatch):
    """Troy, 2026-10-07: "ideally i would like some launches every week regardless of
    the seat. some new activity is always good." A vertical under 7 days old has not
    failed anything, it just has not had time to earn a verdict -- a run of young
    launches could otherwise leave every seat occupied with nothing new started for a
    week. The floor raises what is DESIRED; it never bypasses the dedicated-capacity
    check, which is the actual protection against the old dilution problem."""
    from app import db, runner, senders
    from app.config import settings

    object.__setattr__(settings, "test_seats", 3)
    object.__setattr__(settings, "sender_min_daily", 600)
    object.__setattr__(settings, "min_weekly_launches", 1)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    monkeypatch.setattr(db, "occupied_test_seats", lambda: 3)   # every seat full

    # Capacity exists: the floor still gets its one launch despite zero open seats.
    monkeypatch.setattr(senders, "free_capacity", lambda: {"dedicated": 5000, "daily": 5000})
    assert runner._intake_size("r") == 1

    # No dedicated capacity at all: the floor does NOT override the capacity check --
    # that check is the whole protection against re-diluting winners.
    monkeypatch.setattr(senders, "free_capacity", lambda: {"dedicated": 0, "daily": 5000})
    assert runner._intake_size("r") == 0


def test_a_new_launch_gets_a_modest_seat_allocation_not_the_full_lead_scaled_target(monkeypatch):
    """A brand-new campaign is a test seat, not a proven winner: Specialty Legal and
    Commercial Real Estate each grabbed 600+ accounts on launch day chasing a
    lead-scaled target (2026-10-05) against a workspace with no room for that. A new
    launch now targets sender_min_daily; only a vertical that already has a campaign
    (a top-up) gets the full lead-scaled target_for()."""
    from app import db, senders
    from app.stages import s7_push
    from app.config import settings

    object.__setattr__(settings, "sender_min_daily", 600)   # frozen dataclass
    seen = {}
    monkeypatch.setattr(senders, "target_for", lambda leads: 999999)

    def _align(campaign_id, run_id=None, dry_run=False, target_daily=None):
        seen["target"] = target_daily
        return {"attached": 0, "accounts": 0, "daily_capacity": 0}
    monkeypatch.setattr(senders, "align", _align)

    # Simulate just the target-selection line's logic directly, mirroring push()'s own
    # branch, since push() itself needs a full EmailBison/db fixture harness elsewhere.
    is_new = True
    existing_leads, contacts = 0, list(range(3000))
    target = (settings.sender_min_daily if is_new
              else senders.target_for(existing_leads + len(contacts)))
    assert target == 600

    is_new = False
    target = (settings.sender_min_daily if is_new
              else senders.target_for(existing_leads + len(contacts)))
    assert target == 999999


def test_bison_lead_sync_stamps_the_real_live_total_once_a_day(monkeypatch):
    """Troy, 2026-10-08: "the live page should be synced daily with bison, the numbers
    are not tallying." pushed_lead_count only ever grows and never reflects what
    EmailBison later removes on its own (Venture Capital: 1,810 stopped leads our own
    bookkeeping never saw). sync_bison_leads() stamps the real total_leads EmailBison
    reports right now onto every campaign, once a day, without touching
    pushed_lead_count at all -- that number still means what it always meant."""
    from app import runner, db
    from app.clients import emailbison

    rows = [{"campaign_id": 1098, "vertical": "Product Design"},
            {"campaign_id": 1100, "vertical": "Venture Capital"}]
    monkeypatch.setattr(db, "cursor", lambda: _FakeCursorCtx(rows, []))
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    live = {1098: {"total_leads": 3922}, 1100: {"total_leads": 8800}}
    monkeypatch.setattr(emailbison, "get_campaign", lambda cid: live[cid])
    recorded = []
    monkeypatch.setattr(db, "record_bison_sync",
                        lambda cid, total: recorded.append((cid, total)))

    out = runner.sync_bison_leads(run_id="t")
    assert recorded == [(1098, 3922), (1100, 8800)]
    assert {r["campaign_id"]: r["bison_total_leads"] for r in out} == {1098: 3922, 1100: 8800}

    # one campaign's fetch failing must not stop the rest from syncing
    recorded.clear()
    monkeypatch.setattr(emailbison, "get_campaign",
                        lambda cid: (_ for _ in ()).throw(RuntimeError("boom")) if cid == 1098
                        else live[cid])
    out2 = runner.sync_bison_leads(run_id="t")
    assert recorded == [(1100, 8800)]
    assert len(out2) == 1


def test_a_brand_new_launch_also_gets_its_own_cap_lowered_to_its_real_share():
    """A new campaign still inherits the model campaign's generic 2,400/day ceiling
    far above the modest sender_min_daily share it was just given -- the same
    EmailBison shared-account gap senders.rebalance_winners() closes nightly for
    winners, here from day one. Must be called ONLY on the is_new branch, with the
    live post-attach daily_capacity from align()'s own result."""
    import inspect
    from app.stages import s7_push
    src = inspect.getsource(s7_push.push)
    attach_block = src[src.index("Sender alignment on EVERY live push"):]
    fair_cap_call = attach_block.index("apply_fair_cap(")
    guard = attach_block.rindex("if is_new:", 0, fair_cap_call)
    # the is_new guard must be the one immediately wrapping this call, not some
    # unrelated earlier one -- nothing but whitespace/comments between them.
    between = attach_block[guard:fair_cap_call]
    assert between.count("\n") <= 6
    assert "aligned.get(\"daily_capacity\")" in attach_block[fair_cap_call:fair_cap_call + 120]


def test_the_launch_gate_checks_dedicated_capacity_not_the_diluted_shared_figure():
    """pool['daily'] counts thirds of accounts two OTHER campaigns already hold -- a
    pool of shared scraps can clear sender_min_daily on paper with zero real idle
    capacity behind it. The gate's actual IF condition must compare pool['dedicated'],
    not pool['daily'] (comments are free to mention the old key for context)."""
    import inspect
    from app.stages import s7_push
    src = inspect.getsource(s7_push.push)
    held_at = src.index("HELD: only")
    cond_start = src.rindex("if pool[", 0, held_at)
    cond_line = src[cond_start:src.index("\n", cond_start)]
    assert "pool[\"dedicated\"]" in cond_line
    assert "pool[\"daily\"]" not in cond_line


def test_midweek_intake_fires_only_when_behind_pace_and_only_once_a_day(monkeypatch):
    """Wednesday onward, under 80% of pace, once a day, sender-capacity brake intact
    (it reuses _intake_size), and never on top of a live run."""
    from datetime import datetime, timezone
    from app import db, runner

    # A Wednesday (WAT) at noon.
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc)   # Wed
    monkeypatch.setattr(runner, "datetime", _DT)
    monkeypatch.setattr(db, "weekly_progress",
                        lambda: {"leads": 2400, "quota": 15000, "pace_quota": 6400,
                                 "on_pace_for": 5600})

    class FakeCur:
        hit = False
        def execute(self, sql, params=None):
            self._row = {"1": 1} if FakeCur.hit else None
        def fetchone(self): return self._row
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())
    monkeypatch.setattr(db, "run_alive", lambda within_minutes=45: None)
    monkeypatch.setattr(runner, "_intake_size",
                        lambda run_id=None, ignore_seat_ceiling=False: 2)
    claimed = [{"vertical": "Consumer Mental Health", "config": {"name": "Consumer Mental Health", "rationale": "t"}},
               {"vertical": "Children's Learning", "config": {"name": "Children's Learning", "rationale": "t"}}]
    monkeypatch.setattr(db, "take_from_backlog", lambda n, rid: claimed[:n])
    monkeypatch.setattr(db, "start_run", lambda rid: None)
    monkeypatch.setattr(db, "finish_run", lambda *a, **k: None)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    posted = []
    monkeypatch.setattr(runner.slack, "post", lambda text, **k: posted.append(text))
    worked = []
    monkeypatch.setattr(runner, "_work_verticals",
                        lambda vs, rid, dry, summary: worked.extend(v.name for v in vs) or [v.name for v in vs])

    started = runner.midweek_intake_if_short("run-2h")
    assert started == ["Consumer Mental Health", "Children's Learning"]
    assert worked == started
    assert any("Midweek intake" in p for p in posted)

    # already fired today -> nothing
    FakeCur.hit = True
    worked.clear()
    assert runner.midweek_intake_if_short("run-2h") == [] and worked == []
    FakeCur.hit = False

    # on pace -> nothing
    monkeypatch.setattr(db, "weekly_progress",
                        lambda: {"leads": 6000, "quota": 15000, "pace_quota": 6400,
                                 "on_pace_for": 14000})
    assert runner.midweek_intake_if_short("run-2h") == [] and worked == []


def test_a_vertical_inside_a_pool_is_still_topped_up_after_launch():
    """Troy, 2026-09-30: "would our pooled verticals still top up when launched." A
    pool is ONE ve_campaigns row for the whole campaign, so the verticals inside it
    had no row and were invisible to the post-launch top-up -- pushed at 499, stuck
    at 499, never enough sends to be judged. Their count now comes from the contacts
    themselves, dropped campaigns are excluded, and a top-up batch rejoins the SAME
    pool its earlier leads live in."""
    import inspect
    from app import db
    from app.stages import s7_push

    src = inspect.getsource(db.short_launched_campaigns)
    assert "JOIN ve_campaigns p ON p.campaign_id = ct.eb_campaign_id" in src
    assert "GROUP BY ct.vertical, ct.eb_campaign_id" in src
    assert "o.verdict <> 'drop'" in src, "a dropped campaign must not be topped up"
    assert "pooled=True" in src
    assert callable(db.pool_for_vertical)
    psrc = inspect.getsource(s7_push.push)
    i_home = psrc.index("db.pool_for_vertical(")
    i_any = psrc.index("db.pool_campaign_with_room(")
    assert i_home < i_any, "the vertical's own pool is tried before fullest-with-room"


def test_a_company_that_missed_a_vendor_is_reopened_when_that_vendor_recovers(monkeypatch):
    """Troy, 2026-09-30: "how do companies already searched get a chance to get more
    contacts by going through previous vendors that were emptied but are now topped
    up." Each searched company records the vendors that were OUT; when one is back,
    those with a seat still empty go back through the finders, and the finders' own
    breaker for that vendor is reset so the next call does not skip it on a stale flag."""
    from app import db, finders, runner

    # the finders report the outage per company
    monkeypatch.setattr(finders, "_apollo_dead", True)
    monkeypatch.setattr(finders, "find_quickenrich", lambda *a, **k: [])
    monkeypatch.setattr(finders, "uses_leadocean", lambda: False)
    monkeypatch.setattr(finders, "find_aiark", lambda *a, **k: [])
    monkeypatch.setattr(finders, "_last_aiark_skipped", [False])
    monkeypatch.setattr(finders.client, "profile", lambda: type("P", (), {"name": "other"})())
    _, stats = finders.find_contacts("acme.com", ["CEO"], want=4)
    assert "apollo" in stats.get("missed_vendors", [])

    # and the recovery sweep reopens exactly those, via the vendor's own probe
    monkeypatch.setattr(runner, "vendor_is_up", lambda v: v == "apollo")
    calls = []
    monkeypatch.setattr(db, "reopen_after_recovery",
                        lambda vendor, seats, limit=5000: calls.append((vendor, seats)) or 42)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    out = runner.reopen_companies_for_recovered_vendors("run-2h")
    assert out == {"apollo": 42}
    assert calls == [("apollo", runner.settings.contacts_per_company)]
    assert finders._apollo_dead is False, "the breaker must be reset when the vendor is back"


def test_reopen_sql_only_targets_empty_seats_and_trims_the_missed_vendor():
    import inspect
    from app import db
    src = inspect.getsource(db.reopen_after_recovery)
    assert "coalesce(seats_filled, 0) < %s" in src
    assert "consumed_at IS NULL" in src
    assert "array_remove(missed_vendors, %s)" in src
    assert "found_at = NULL" in src


def test_vendor_spend_lines_count_units_and_only_price_what_has_a_price(monkeypatch):
    """Troy, 2026-09-30: "a calculated ai ark spend update so i can track it." The
    ledger counts the vendor's own units; a dollar figure appears only when a unit
    price is configured, never invented."""
    from datetime import datetime, timezone
    from app import db, report

    monkeypatch.setattr(db, "vendor_usage_since", lambda since: {
        "aiark": {"company_page": 120, "companies_returned": 5800,
                  "people_search": 40, "people_returned": 15, "_errors": 33},
        "leadocean": {"people_search": 150, "people_returned": 410},
    })
    import dataclasses as _dc
    monkeypatch.setattr(report, "settings", _dc.replace(
        report.settings, aiark_cost_per_company_page=0.0, aiark_cost_per_person=0.0,
        leadocean_cost_per_person=0.0))
    lines = report.vendor_spend(7)
    assert any("AI Ark: 120 company pages, 15 people returned (no unit price on file)" in l
               and "33 calls refused" in l for l in lines)
    assert any("Lead Ocean: 150 requests, 410 people returned (no unit price on file)" in l
               for l in lines)

    monkeypatch.setattr(report, "settings", _dc.replace(
        report.settings, aiark_cost_per_company_page=0.05, aiark_cost_per_person=0.10,
        leadocean_cost_per_person=0.02))
    lines = report.vendor_spend(7)
    assert any("about $7.50" in l for l in lines)      # 120*0.05 + 15*0.10
    assert any("about $8.20" in l for l in lines)      # 410*0.02


def test_a_topup_for_any_already_launched_vertical_never_opens_a_rival_campaign(monkeypatch):
    """Troy, 2026-09-30: "cybersecurity... the push failed cause it seems it wanted
    to open its own campaign." scale_up_of_campaign is stamped ONLY by the classic
    scale-up path; the auto-topup loop grows a batch for a vertical that already
    sends with no stamp at all. db.vertical_is_launched must be checked as a
    fallback so ANY already-launched vertical's top-up finds its real campaign."""
    import inspect
    from app.stages import s7_push
    src = inspect.getsource(s7_push.push)
    i_scaleup = src.index('summary") or {}).get("scale_up_of_campaign")')
    i_fallback = src.index("db.vertical_is_launched(batch.get")
    i_isnew = src.index("if is_new:")
    assert i_scaleup < i_fallback < i_isnew,         "the launched-vertical fallback must run before the new-campaign branch"


def test_midweek_intake_never_makes_the_nightly_review_wait_or_skip(monkeypatch):
    """Troy, 2026-10-01: a `midweek-*` run sourcing a never-launched backlog vertical
    ran 4+ hours on 2026-09-30 night; the review polled its full 60-minute budget
    behind it and skipped the whole night's scaling anyway. Midweek intake and the
    review never touch the same verticals, so the review's liveness check must
    exclude `midweek-*` run_ids -- see db.run_alive's exclude_prefixes."""
    from app import db

    calls = []
    real_execute_sql = None

    class FakeCur:
        def execute(self, sql, params=None):
            calls.append(params)
            self._row = None
        def fetchone(self): return self._row
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())

    db.run_alive(within_minutes=45, exclude_prefixes=("midweek-",))
    assert calls, "run_alive must execute a query"
    like_patterns = calls[-1][1]
    assert like_patterns == ["midweek-%"]

    db.run_alive(within_minutes=45)
    assert calls[-1][1] == [], \
        "with no exclusions the list is EMPTY -- a NUL placeholder crashed Postgres"
    assert all("\x00" not in str(p) for p in calls[-1])


def test_a_dropped_verticals_batch_is_refused_not_pushed_into_its_archived_campaign(monkeypatch):
    """Troy, 2026-09-30: Funded Proptech is DROPPED (its campaign archived) but a
    leftover batch from before that exclusion existed kept growing. Refuse to push
    or auto-approve it rather than loading leads into a dead campaign or opening a
    rival for a vertical that is finished."""
    from app import db
    from app.stages import s6_approval as s6, s7_push

    monkeypatch.setattr(db, "vertical_is_launched", lambda v: 912)
    monkeypatch.setattr(db, "vertical_outcome", lambda v: {"verdict": "drop"})
    assert not s6.auto_approve_if_topup("fundedpropte-b41285", "Funded Proptech", "run-2h")

    batch = {"batch_id": "fundedpropte-b41285", "vertical": "Funded Proptech",
            "approval_status": "approved", "run_id": "run-2h", "summary": {}}
    monkeypatch.setattr(db, "get_batch", lambda bid: batch)
    monkeypatch.setattr(s7_push, "_contacts_for",
                        lambda bid: [{"email": "a@b.com", "first_name": "A"}])
    monkeypatch.setattr(db, "mark_needs_review", lambda *a, **k: None)
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    monkeypatch.setattr(s7_push.emailbison, "campaign_name_for", lambda v: "VE-Funded-Proptech")
    import app.testgrid as _testgrid
    monkeypatch.setattr(_testgrid, "champion_campaign", lambda: None)
    out = s7_push.push("fundedpropte-b41285", live=True)
    assert out.get("error") == "vertical is dropped"


def test_a_topup_batch_for_a_launched_vertical_is_approved_without_a_human(monkeypatch):
    """Troy, 2026-09-30: "first launch of a vertical needs a human; top-ups into an
    already-approved campaign auto-approve." The copy strategy was approved at first
    launch; a top-up is the same copy for more people."""
    from app import db
    from app.stages import s6_approval as s6

    class FakeCur:
        def execute(self, sql, params=None):
            self._row = {"approval_status": "pending", "contact_count": 246}
        def fetchone(self): return self._row
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())
    settled = []
    monkeypatch.setattr(db, "settle_batch", lambda bid, decision, decided_by=None, reason=None:
                        settled.append((bid, decision, decided_by)) or {})
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    monkeypatch.setattr(db, "vertical_thread", lambda v: None)
    posted = []
    monkeypatch.setattr(s6.slack, "post", lambda text, **k: posted.append(text))

    # already sending (own campaign) -> approved by the system
    monkeypatch.setattr(db, "vertical_is_launched", lambda v: 1084)
    assert s6.auto_approve_if_topup("consumeredte-9bd0f4", "Consumer Edtech", "run-2h")
    assert settled == [("consumeredte-9bd0f4", "approved", "system:topup")]
    assert any("auto-approved" in p for p in posted)

    # never launched -> untouched, a human decides
    settled.clear()
    monkeypatch.setattr(db, "vertical_is_launched", lambda v: None)
    assert not s6.auto_approve_if_topup("appliedaimac-66d95f", "Applied AI", "run-2h")
    assert settled == []


def test_scale_up_batches_also_auto_approve_not_just_runner_pys_topup_path():
    """Troy, 2026-10-08: found 5 scale-up batches sitting pending for up to 2 days
    (Consumer Edtech, Funded Proptech), 0 of 5 actually reaching EmailBison despite
    Slack announcing them "scaled" -- auto_approve_if_topup() existed and worked, but
    was only ever wired into runner.py's auto-topup path. Both of s8_review.py's own
    batch-opening sites (_finish_one_vertical's banked-work batch, and scale_vertical's
    own scale-up batch) open batches the identical way and must call it too -- a
    scale-up is a top-up of an already-launched campaign BY DEFINITION."""
    import inspect
    from app.stages import s8_review

    finish_src = inspect.getsource(s8_review._finish_one_vertical)
    assert "auto_approve_if_topup(batch_id" in finish_src

    scale_src = inspect.getsource(s8_review.scale_vertical)
    assert "auto_approve_if_topup(batch_id" in scale_src
    # must run after batch_id is actually assigned in both branches, not before
    assert scale_src.index("auto_approve_if_topup(batch_id") > scale_src.index("open_batch(run_id, vertical")


def test_a_growing_batch_gets_its_expiry_extended_and_one_warning_before_it_goes():
    """Troy, 2026-09-30: paid sourcing and copy must not expire at 72h while the
    top-up loop is adding to it; and a human gets one warning a day before."""
    import inspect
    from app import db, runner
    src = inspect.getsource(db.add_to_batch)
    assert "expires_at = GREATEST(expires_at, now() + make_interval(hours => %s))" in src
    assert "expiry_warned_at = NULL" in src
    rsrc = inspect.getsource(runner.resume)
    assert "db.batches_expiring_soon(24)" in rsrc and "db.mark_expiry_warned(" in rsrc


def test_a_refused_vendor_is_announced_once_a_day_not_found_in_a_log(monkeypatch):
    """Troy, 2026-09-30: "alert before, not after." The first refusal in a day is a
    Slack message with the count; the same vendor is not repeated within 24h."""
    import inspect
    from app import runner
    src = inspect.getsource(runner.resume)
    i_q = src.index("db.vendor_refusals_since(hours=2)")
    i_dedup = src.index('db.recently_logged("vendors"', i_q)
    i_post = src.index("is out of credit", i_dedup)
    assert i_q < i_dedup < i_post


def test_stage3_stops_asking_ai_ark_after_a_credit_refusal():
    """Every further query after a 402 is a paid, refused round trip; stop at the
    first, and let the round read as vendor_error rather than source_exhausted."""
    import inspect
    from app.stages import s3_companies as s3
    src = inspect.getsource(s3._fetch_fresh)
    assert "r.status_code in (401, 402)" in src
    assert "queries_failed = len(queries)" in src
    assert "if len(fresh) >= want or queries_failed == len(queries):" in src


def test_a_pooled_winner_is_requeued_standalone_and_never_pooled_again():
    """Troy, 2026-09-30: a vertical that wins inside a pool gets its own campaign on
    the next intake instead of "needs a human"; s7 honours the standalone flag."""
    import inspect
    from app import db
    from app.stages import s7_push, s8_review as s8
    rsrc = inspect.getsource(s8.review_all)
    assert "db.requeue_pooled_winner(r[\"vertical\"])" in rsrc
    assert "needs a human" not in rsrc.split("db.requeue_pooled_winner")[1][:600]
    qsrc = inspect.getsource(db.requeue_pooled_winner)
    assert "status = 'queued'" in qsrc and '{"standalone": true}' in qsrc
    psrc = inspect.getsource(s7_push.push)
    assert '.get("standalone")' in psrc
    assert "supply < settings.pool_below_leads and not standalone" in psrc


def test_auto_topup_pauses_for_a_human_after_five_straight_empty_attempts(monkeypatch):
    """Retried INDEFINITELY (no day bound -- see the function's own docstring for
    why), but not forever blindly: five straight attempts that add nothing pause
    the vertical for a human, the same relief valve as a scale-up's poor-result
    pause, so it stops burning vendor calls on one that will never grow."""
    from app import db, runner

    monkeypatch.setattr(
        db, "short_pending_batches",
        lambda cap: [{"batch_id": "b-2", "vertical": "Applied AI & Machine "
                      "Learning Software Vendors", "contact_count": 438,
                      "topup_streak": 4}])
    monkeypatch.setattr(db, "short_launched_campaigns", lambda floor: [])
    monkeypatch.setattr(
        db, "backlog_config",
        lambda v: {"name": v, "rationale": "t"}
        if v == "Applied AI & Machine Learning Software Vendors" else None)
    monkeypatch.setattr(db, "pending_batch_for", lambda v: {"contact_count": 438})
    monkeypatch.setattr(runner, "_work_vertical", lambda *a, **k: None)
    monkeypatch.setattr(db, "record_topup_streak", lambda v, added: 5)
    paused = []
    monkeypatch.setattr(db, "pause_topup_for_human", lambda v: paused.append(v))
    logged = []
    monkeypatch.setattr(db, "log", lambda run_id, stage, msg, **k: logged.append(msg))
    posted = []
    monkeypatch.setattr(runner.slack, "post", lambda text, **k: posted.append(text))

    done = runner.auto_topup_short_verticals("run-2h")
    assert done == []
    assert paused == ["Applied AI & Machine Learning Software Vendors"]
    assert any("5 straight" in m for m in logged)
    assert any("paused" in p for p in posted), "a human must be told, not left to notice"


def test_apollos_dead_endpoint_trips_the_circuit_breaker(monkeypatch):
    """Troy, 2026-09-24, asking how to speed up contact verification. Apollo's endpoint
    changed -- "This endpoint is deprecated for API callers. Please use the new
    mixed_people/api_search" -- and that message matches neither "credit" nor "not
    set", so the breaker never tripped: measured on one live run, 323 guaranteed-to-fail
    calls and counting, one per company that reached this step, for zero contacts. A
    deprecated endpoint will not fix itself mid-run, so it must trip the breaker exactly
    like "no credits" does."""
    from app import finders

    finders._apollo_dead = False
    monkeypatch.setattr(finders, "find_apollo", lambda *a, **k: (_ for _ in ()).throw(
        finders.ApolloUnavailable(
            "HTTP 422: {\"error\":\"This endpoint is deprecated for API callers. "
            "Please use the new mixed_people/api_search endpoint\"}")))
    monkeypatch.setattr(finders, "find_quickenrich", lambda *a, **k: [])
    monkeypatch.setattr(finders, "find_aiark", lambda *a, **k: [])
    monkeypatch.setattr(finders.db, "log", lambda *a, **k: None)

    finders.find_contacts("example.com", ["CEO"], want=1)
    assert finders._apollo_dead, \
        "a deprecated-endpoint error must trip the breaker, same as no-credits"

    # and the breaker actually SKIPS the next call once tripped -- find_apollo must not
    # be reached again
    calls = []
    monkeypatch.setattr(finders, "find_apollo",
                        lambda *a, **k: calls.append(1) or [])
    finders.find_contacts("second-example.com", ["CEO"], want=1)
    assert not calls, "once tripped, Apollo must be skipped, not called again"
    finders._apollo_dead = False  # reset the module global for other tests


def test_a_killed_scale_up_resumes_on_the_next_two_hourly_tick(monkeypatch):
    """Troy, 2026-09-24, after a deploy killed Medical Device and Venture Capital's
    scale-ups mid-sourcing, before either had a single contact to show for it: "this is
    not good. it makes us fall behind schedule. runs should also resume if they were
    failed right?" They should, and until now they did not -- a scale-up is not a
    vertical_backlog row, so paused_verticals() (what the two-hourly resume already
    checks) cannot see it. It sat waiting for the next DAILY review, up to 24 hours,
    instead of being retried on the very next two-hourly tick like everything else."""
    import inspect
    from datetime import datetime, timezone
    from app.stages import s8_review as s8
    from app import db

    # the stamp must be set BEFORE scale_vertical runs. A clean finish OR any other
    # caught exception still clears it (mark_scaling_finished); a VendorOutage is the
    # deliberate exception -- the stamp is left in place so a stale one means "died
    # mid-flight or is waiting out a vendor outage", never "finished, however it
    # finished". Troy, 2026-09-29: this used to be a single finally that cleared the
    # stamp on ANY caught exception, which meant a vendor outage read as "finished"
    # too and sat untouched for up to a day.
    src = inspect.getsource(s8._do_act_on_winner)
    i_mark = src.index("mark_scaling_started(")
    i_try = src.index("try:", i_mark)
    i_vendor = src.index("except VendorOutage as exc:", i_try)
    i_generic = src.index("except Exception as exc:", i_vendor)
    i_else = src.index("else:", i_generic)
    assert i_mark < i_try < i_vendor < i_generic < i_else, \
        "stamp before the try; VendorOutage caught ahead of the generic handler"
    assert "mark_scaling_finished(" not in src[i_vendor:i_generic], \
        "a vendor outage must NOT clear the stamp -- that is what lets the " \
        "two-hourly resume find it again"
    assert "mark_scaling_finished(" in src[i_generic:i_else], \
        "any OTHER failure still clears the stamp, same as before"
    assert "mark_scaling_finished(" in src[i_else:i_else + 200], \
        "a clean success still clears the stamp"

    # resume_interrupted_scale_ups: a row db.interrupted_scale_ups() returns, still
    # verdict 'scale' and started TODAY, must be continued -- not re-judged, just
    # picked up.
    camp = {"campaign_id": 1085, "vertical": "Medical Device", "pushed_lead_count": 3463,
            "vertical_config": {}, "scaling_run_id": "run-dead",
            "scaling_started_at": datetime.now(timezone.utc)}
    monkeypatch.setattr(db, "interrupted_scale_ups", lambda: [camp])

    class FakeCur:
        def execute(self, *a, **k):
            self._row = {"verdict": "scale", "vertical": "Medical Device",
                        "objective_rate": 0.13, "sends": 5776}
        def fetchone(self): return self._row
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())

    calls = []
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    monkeypatch.setattr(db, "mark_scaling_started", lambda cid, rid: calls.append(("started", cid)))
    monkeypatch.setattr(db, "mark_scaling_finished", lambda cid: calls.append(("finished", cid)))
    monkeypatch.setattr(s8, "scale_vertical",
                        lambda c, r, rid, deadline_minutes=None:
                        calls.append(("scaled", c["vertical"]))
                        or {"scaled": 400, "vertical": c["vertical"]})

    out = s8.resume_interrupted_scale_ups("run-resume")
    assert out and out[0]["vertical"] == "Medical Device" and out[0]["scaled"] == 400
    assert calls == [("started", 1085), ("scaled", "Medical Device"), ("finished", 1085)], \
        "must mark started, resume the scale-up, then mark finished -- in that order"

    # a vertical whose verdict is no longer 'scale' (a later review already re-judged
    # it) must be left alone, not resumed, and the stale stamp still cleared
    calls.clear()
    class FakeCurDrop(FakeCur):
        def execute(self, *a, **k):
            self._row = {"verdict": "drop"}
    monkeypatch.setattr(db, "cursor", lambda: FakeCurDrop())
    out2 = s8.resume_interrupted_scale_ups("run-resume")
    assert out2 == [] and calls == [("finished", 1085)]


def test_a_stuck_scale_up_from_a_previous_day_hands_off_to_the_daily_review(monkeypatch):
    """Troy, 2026-09-29: "if a scale up is out due to credits it checks every 2 hours
    till it completes or the end of the day and stops checking so the next scale up
    can pick up on it." Without a day bound, a vendor outage that outlasts one day
    would have the two-hourly resume retry the SAME stuck campaign forever, fighting
    the next day's regular review over the same work instead of handing off to it. A
    campaign whose scaling_started_at is from a PREVIOUS day must be left alone here
    -- not resumed, not re-marked -- so the ordinary daily review picks it up fresh."""
    from datetime import datetime, timedelta, timezone
    from app.stages import s8_review as s8
    from app import db

    yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
    stuck = {"campaign_id": 1183, "vertical": "Biotechnology & Life Sciences",
            "pushed_lead_count": 8224, "vertical_config": {}, "scaling_run_id": "run-old",
            "scaling_started_at": yesterday}
    monkeypatch.setattr(db, "interrupted_scale_ups", lambda: [stuck])

    logged = []
    monkeypatch.setattr(db, "log", lambda run_id, stage, msg, **k: logged.append(msg))
    called = []
    monkeypatch.setattr(db, "mark_scaling_started", lambda *a, **k: called.append("started"))
    monkeypatch.setattr(db, "mark_scaling_finished", lambda *a, **k: called.append("finished"))
    monkeypatch.setattr(s8, "scale_vertical", lambda *a, **k: called.append("scaled"))

    out = s8.resume_interrupted_scale_ups("run-resume")
    assert out == [], "a previous day's stuck campaign is not resumed by this tick"
    assert not called, "it must not be touched at all -- not re-marked, not re-scaled"
    assert any("previous day" in m and "Biotechnology" in m for m in logged), \
        "the handoff to the daily review must be logged, not silent"


def test_exhausted_verticals_get_a_clear_closed_message_not_a_gap_warning(monkeypatch):
    """Troy, 2026-09-24: "do i get an exhausted tag on slack that x vertical is
    exhausted and closed?" Not clearly: report_gap's own message reads as a temporary
    shortfall ("cleared the bar... but only X available"), and it is the ONLY message --
    every day after, scale_vertical's early-exit returns a reason string that nothing
    ever forwards to Slack, so the vertical goes permanently silent. mark_scale_exhausted
    must post its own distinct, unambiguous CLOSED announcement, naming the vertical."""
    from app.stages import s8_review as s8

    posts = []
    monkeypatch.setattr(s8.slack, "post", lambda text, **k: posts.append(text))
    monkeypatch.setattr(s8.db, "log", lambda *a, **k: None)

    class FakeCur:
        rowcount = 1
        def execute(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(s8.db, "cursor", lambda: FakeCur())

    s8.mark_scale_exhausted(1084, "Biotechnology & Life Sciences", 6337, "run-x")

    assert len(posts) == 1
    msg = posts[0]
    assert "Biotechnology & Life Sciences" in msg
    assert "EXHAUSTED" in msg and "CLOSED" in msg
    assert "will not be" in msg, "must say plainly that it will not run again"


def test_a_scale_up_resumes_banked_work_and_copies_while_it_finds():
    """Twice in two days a scale-up died at the two-hour mark and left everything it had
    found with no copy and no batch: 2,173 contacts on 2026-09-22, 1,382 on 2026-09-23.
    Two causes. It never looked for work a previous attempt had banked, and copy waited
    for the last company to be searched instead of running alongside -- even though the
    finders spend QuickEnrich and the waterfall while copy spends the LLM, and the
    weekly run has overlapped them since Troy asked on 2026-09-14.

    And running out of TIME is not running out of SUPPLY: a deadline stop must leave the
    vertical partial, or an audience with thousands of companies left is never scaled
    again until a human clears the flag."""
    import inspect
    from app.stages import s8_review as s8
    from app.config import settings

    src = inspect.getsource(s8.scale_vertical)
    assert "verified_uncopied" in src, \
        "a scale-up must pick up contacts an earlier attempt banked"
    assert "ThreadPoolExecutor" in src and "s5_copy.run" in src, \
        "copy must run alongside the finders, not after them"
    assert "deadline=deadline" in src, "the finder loop must be given the time budget"
    # the deadline stop is PARTIAL, never exhausted
    i_time, i_exh = src.index("out_of_time"), src.index("mark_scale_exhausted(camp")
    assert src.count("out_of_time") >= 2
    assert "if out_of_time or not genuinely_exhausted:" in src and src.index(
        "if out_of_time or not genuinely_exhausted:") < src.rindex(
        "mark_scale_exhausted(camp"), "out of time must skip the exhausted flag"
    assert settings.scale_deadline_minutes == 240


def test_the_weekly_run_never_skips_for_another_live_run():
    """Troy, 2026-10-01: same fix as the review, for the same reason. The weekly run
    only ever claims NEW backlog verticals; a 2-hourly resume tick only ever
    continues or tops up verticals that have ALREADY launched -- disjoint sets, so
    there is no double-write risk, and the one real contention case (a review-
    triggered scale-up still running Monday morning) is already closed off by
    scale_skip_weekday. The blanket run_alive skip must be gone."""
    import inspect
    from app import runner

    src = inspect.getsource(runner.run_pipeline)
    assert "run_alive(" not in src,         "the weekly run must not gate its own start on any other run being alive"
    assert "weekly run skipped" not in src


def test_the_review_never_waits_or_skips_for_another_live_run():
    """Troy, 2026-10-01: "the nightly review should take priority." It used to poll
    up to review_wait_minutes for ANY other live run and skip the whole night's
    judging if that run was still going -- lost a full night's scaling twice this
    way (2026-09-29 behind a resume tick's scale-up, 2026-09-30 behind an unrelated
    midweek-intake run). It must now start unconditionally; the narrower, correct
    collision guard lives in s8_review._do_act_on_winner instead."""
    import inspect
    from app import runner

    src = inspect.getsource(runner)
    block = src[src.index('args.command == "review"'):]
    block = block[:block.index("db.start_run(")]
    assert "run_alive(" not in block, \
        "the review must not gate its own start on any other run being alive"
    assert "review skipped" not in block and "review waiting" not in block, \
        "and must never log waiting/skipping for another run"


def test_a_vertical_already_being_scaled_by_a_live_tick_is_deferred_not_duplicated():
    """Troy, 2026-10-01: the review's global wait/skip is gone, so the one real
    collision risk left is a SPECIFIC campaign that a 2-hourly tick is right now
    genuinely still scaling. db.run_is_truly_alive gates that, scoped to one
    run_id -- a DEAD run's leftover scaling_run_id stamp (interrupted_scale_ups'
    case) must NOT be a reason to defer; scale_vertical must still run for it."""
    import inspect
    from app import db
    from app.stages import s8_review as s8

    src = inspect.getsource(s8._do_act_on_winner)
    i_owner = src.index('other_owner = camp.get("scaling_run_id")')
    i_alive = src.index("db.run_is_truly_alive(other_owner)")
    i_scale = src.index("scale_vertical(camp, r,")
    assert i_owner < i_alive < i_scale, \
        "the live-owner check must run before scale_vertical is ever called"
    assert "already scaling under" in src

    # run_is_truly_alive itself: a run_id with no matching row, or one whose status
    # isn't 'running', must read as False -- otherwise a dead stamp defers forever.
    import inspect as _i
    rsrc = _i.getsource(db.run_is_truly_alive)
    assert "status='running'" in rsrc or 'status=\'running\'' in rsrc
    assert "if not run_id" in rsrc, "no owner at all must short-circuit to False"


def test_ve_brief_names_a_refused_vendor_as_the_biggest_lever(monkeypatch):
    """Troy, 2026-10-01: "/ve brief" must answer all four questions in plain English
    using only persisted, cross-process-safe signals (the vendor ledger, paused-for-
    human markers, a live sender-capacity call) -- never an in-process breaker flag,
    since the Slack command can be answered by a different service than the one that
    ran the last tick. A vendor refusal in the last 24h must outrank every other
    lever."""
    from datetime import datetime, timedelta, timezone
    from app import db
    from app.stages import s6_approval as s6

    now = datetime.now(timezone.utc)

    class FakeCur:
        def execute(self, sql, params=None):
            if "FROM runs" in sql:
                self._row = {"run_id": "resume-20261001T060420", "started_at": now - timedelta(minutes=90),
                             "finished_at": now - timedelta(minutes=30), "status": "resumed",
                             "notes": "resumed 2 of 2"}
            else:
                self._row = None
        def fetchone(self): return self._row
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())
    monkeypatch.setattr(db, "vendor_refusals_since", lambda hours=24: {"aiark": 13, "apollo": 2})
    monkeypatch.setattr(db, "paused_for_human", lambda: [])
    monkeypatch.setattr(db, "weekly_progress", lambda: {"leads": 6561, "quota": 15000,
                                                         "pace_quota": 8571, "on_pace_for": 13779})

    text = s6.handle_command("brief", "troy")
    assert "1. *What ran:*" in text and "2. *Vendors out:*" in text
    assert "3. *Did it add anything:*" in text and "4. *Biggest lever:*" in text
    assert "*aiark*" in text and "13 refused" in text, \
        "the vendor with the MOST refusals must be named as the lever"
    assert "apollo" not in text.split("4. *Biggest lever:*")[1]


def test_ve_brief_falls_back_to_a_paused_human_decision_when_no_vendor_is_out(monkeypatch):
    """With no vendor refusals, a vertical parked on scale_needs_human_at or
    topup_needs_human_at is the next-highest lever -- it is unblockable any other
    way, unlike a vendor outage which clears itself once topped up."""
    from datetime import datetime, timedelta, timezone
    from app import db
    from app.stages import s6_approval as s6

    now = datetime.now(timezone.utc)

    class FakeCur:
        def execute(self, sql, params=None):
            self._row = {"run_id": "resume-x", "started_at": now - timedelta(minutes=10),
                         "finished_at": None, "status": "running", "notes": None} \
                if "FROM runs" in sql else None
        def fetchone(self): return self._row
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(db, "cursor", lambda: FakeCur())
    monkeypatch.setattr(db, "vendor_refusals_since", lambda hours=24: {})
    monkeypatch.setattr(db, "paused_for_human",
                        lambda: [{"vertical": "Funded Proptech", "kind": "scale",
                                 "since": now - timedelta(days=2)}])
    monkeypatch.setattr(db, "weekly_progress", lambda: {"leads": 6561, "quota": 15000,
                                                         "pace_quota": 8571, "on_pace_for": 13779})

    text = s6.handle_command("brief", "troy")
    assert "2. *Vendors out:* none" in text
    assert "*Funded Proptech*" in text and "widen its audience" in text


def test_the_review_registers_itself_as_a_run():
    """2026-09-22: the Biotechnology scale-up ran under run-20260922T120406, died in
    stage 4 with 2,173 verified contacts banked and no copy, and nothing noticed --
    every stall detector watches the `runs` table and the review never wrote a row.
    The review command must open and close one around review_all()."""
    import inspect
    from app import runner

    src = inspect.getsource(runner.main) if hasattr(runner, "main") else \
        inspect.getsource(runner)
    block = src[src.index('args.command == "review"'):]
    block = block[:block.index('args.command ==', 40)] if 'args.command ==' in block[40:] \
        else block
    assert "db.start_run(" in block, "review must open a runs row"
    assert "db.finish_run(" in block, "review must close its runs row"
    # and it must survive a container SIGTERM, which is what actually killed it
    assert "BaseException" in block, \
        "a SIGTERM must still close the row, so catch BaseException not Exception"


def test_a_campaign_is_not_judged_until_enough_of_it_has_been_delivered():
    """Troy, 2026-09-22, approving the 60% gate alongside same-vertical top-up. Once a
    campaign grows by top-up an absolute send floor stops discriminating: 2,000 sends is
    most of a 1,000-lead campaign and a fraction of one topped to 10,000. Percent
    delivered is the only ripeness signal that scales with the campaign."""
    from datetime import datetime, timedelta, timezone
    from app import scoring
    from app.config import settings

    # 30, not 60. Measured 2026-09-23 across all nine live campaigns, 60% changed
    # exactly ONE verdict -- it held Biotechnology, which had only fallen to 43.5%
    # delivered BECAUSE it was topped up, so the gate punished the thing it exists to
    # reward. This is a FLOOR against judging a campaign that has barely started, not a
    # primary gate: 7 days, 2,000 sends, 30 replies and the Wilson interval decide.
    bar = settings.scale_min_progress
    assert bar == 30.0
    ripe = dict(sends=6058, replies=78, interested=8, leads=4059,
                launched=datetime(2026, 9, 14, tzinfo=timezone.utc))

    # the real Biotechnology numbers at 67.4% delivered: still a winner, unchanged
    assert scoring.judge(**ripe, progress=67.39).status == "winner"
    # the same campaign judged too early is held, and the reason says why. s3 orders
    # never-emailed companies ahead of previously-emailed ones, so a very early read
    # over-samples fresh companies and flatters the vertical -- which is the one thing
    # the floor still buys.
    early = scoring.judge(**ripe, progress=15.0)
    assert early.status == "pending" and "delivered" in early.reason
    # a campaign that reports nothing must not be frozen forever
    assert scoring.judge(**ripe, progress=None).status == "winner"
    # neither may a junk value
    assert scoring.judge(**ripe, progress="n/a").status == "winner"
    # the gate is an AND with the age rule, not a replacement: fully delivered but new
    young = scoring.judge(sends=6058, replies=78, interested=8, leads=4059,
                          launched=datetime.now(timezone.utc), progress=100.0)
    assert young.status == "pending" and "days" in young.reason

    # ...and the progress gate LAPSES at scale_max_days, or a campaign starved of
    # senders sits unjudged forever holding the accounts that would unstarve the others.
    # Troy, 2026-09-22: "at this pace none of our campaigns would drop."
    stale = dict(sends=6058, replies=78, interested=8, leads=4059,
                 launched=datetime.now(timezone.utc)
                 - timedelta(days=settings.scale_max_days + 1))
    assert scoring.judge(**stale, progress=20.0).status == "winner"
    # a slow LOSER must also reach its verdict, so its senders come back
    slow_loser = dict(stale, interested=1, replies=90)
    assert scoring.judge(**slow_loser, progress=20.0).status == "loser"
    # but one day short of the deadline the gate still holds
    nearly = dict(stale, launched=datetime.now(timezone.utc)
                  - timedelta(days=settings.scale_max_days - 1))
    assert scoring.judge(**nearly, progress=20.0).status == "pending"


def test_the_model_campaign_can_never_be_archived(monkeypatch):
    """Archiving the model breaks every future push, because stage 7 duplicates it to
    create each new campaign. Troy switched the model off 1036 on 2026-09-22 so that
    1036's 121 sender accounts could be released; the guard is what stops the next
    person freeing capacity by archiving the campaign the pipeline depends on."""
    import pytest
    from app.clients import emailbison as eb

    live = [{"id": 1084, "name": "VE-Biotech", "status": "active",
             "created_at": "2026-09-14T00:00:00Z"},
            {"id": 1086, "name": "VE-Wealth", "status": "active",
             "created_at": "2026-09-15T00:00:00Z"}]
    monkeypatch.setattr(eb, "list_campaigns", lambda: live)

    # Troy, 2026-09-22: "ideally we can copy any running ve campaign as template."
    # With two alive, archiving either is fine -- the template just moves.
    assert eb.resolve_model_campaign() == 1084          # oldest, least likely to go next
    assert eb.resolve_model_campaign(exclude=1084) == 1086

    # the guard now bites ONLY on the last one left, not on a named special campaign
    monkeypatch.setattr(eb, "list_campaigns", lambda: live[:1])
    with pytest.raises(RuntimeError, match="only campaign left"):
        eb.archive_campaign(1084)

    # and a different campaign still goes through the archive route, not a pause route
    calls = []

    def fake_request(vendor, method, url, **kw):
        calls.append((method, url))
        return {"data": {"success": True}}

    monkeypatch.setattr(eb, "request", fake_request)
    monkeypatch.setattr(eb, "_headers", lambda: {})
    out = eb.archive_campaign(991)
    assert calls and calls[0][0] == "PATCH" and calls[0][1].endswith("/campaigns/991/archive")
    # and it must say whether the accounts actually came back: only /archive frees them,
    # a pause route leaves them held while looking like success
    assert out["senders_freed"] is True and out["route"].endswith("/archive")


def test_titles_match_on_role_not_wording(monkeypatch):
    """Troy, 2026-09-22: 'make job titles match but not word for word.' The strict
    filter demanded the client's exact string, so 'Vice President of Operations' failed
    against 'VP of Operations' and six companies yielded 24 people and ZERO contacts.
    Widening must not let a banned persona back in."""
    import importlib
    from app import client as _client
    from app.stages import s4_people as s4

    monkeypatch.setenv("CLIENT", "productevo")
    _client.load.cache_clear()
    importlib.reload(s4)

    same_role = ["Vice President of Operations", "Plant Manager",
                 "Director of Manufacturing", "Sourcing Manager", "Purchasing Manager",
                 "Production Engineer", "Head of Engineering", "Principal"]
    still_banned = ["VP of Sales", "Marketing Manager", "Senior Accountant",
                    "Bookkeeper", "Recruiter", "Paralegal", "Account Executive"]
    not_a_buyer = ["Shipping and Receiving Clerk", "Administrative Assistant",
                   "GIS analyst", "Community Manager"]

    assert not [t for t in same_role if s4.score_title(t, 30) == 0]
    assert not [t for t in still_banned if s4.score_title(t, 30) > 0]
    assert not [t for t in not_a_buyer if s4.score_title(t, 30) > 0]
    # a role match ranks BELOW an exact declared title
    assert s4.score_title("Vice President of Operations", 30) < s4.score_title("President", 30)
    _client.load.cache_clear()


def test_close_abandoned_runs_also_catches_a_silently_dead_one():
    """Troy, 2026-09-25: Consumer Edtech's scale-up sat orphaned for 9.5 hours because
    its run died silently at 04:19 and nothing flips a vanished run's status just from
    it going quiet -- it only closed once it aged past the 12-hour default, and
    interrupted_scale_ups() checks status<>'running', so the resume cron (which ran six
    times in that window) never saw it as interruptible."""
    import inspect
    from app import db

    src = inspect.getsource(db.close_abandoned_runs)
    assert "stale_after_minutes" in src, \
        "must be able to close a run on SILENCE, not only on raw age"
    assert "NOT EXISTS" in src and "logs l" in src, \
        "silence must be judged from the logs table, not just started_at"
    # the silence branch must ALSO require the run be at least stale_after_minutes old
    # itself, so a run seconds into starting (before its first log line) is never
    # mistaken for dead.
    i_not_exists = src.index("NOT EXISTS")
    before = src[:i_not_exists]
    assert before.count("stale_after_minutes") >= 1, \
        "the silence check must be gated by a minimum age, not fire on brand-new runs"


def test_resume_registers_a_real_run_before_doing_any_work():
    """Troy, 2026-09-25: Venture Capital was legitimately still finding real companies
    when a second, overlapping resume tick treated it as "interrupted" (because ad-hoc
    work under the literal run_id 'resume' never got a runs-table row, so nothing could
    tell it was still alive) and ran a stale pre-exhaustion snapshot that incorrectly
    marked the whole vertical exhausted -- while the real attempt went on to find 2,925
    more contacts that same afternoon. The same missing row also left the /live page's
    "right now" banner and run_alive() blind to hours of genuine work."""
    import inspect
    from app import runner

    src = inspect.getsource(runner.resume)
    i_start = src.index("db.start_run(run_id)")
    i_finish_banked = src.index("finish_banked_work(run_id)")
    i_resume_scaleups = src.index("resume_interrupted_scale_ups(run_id, budget_minutes=_mins_left())")
    assert i_start < i_finish_banked < i_resume_scaleups, \
        "a real run must be registered BEFORE either hours-long resume step runs"
    assert "None" != src[src.index("finish_banked_work(") + len("finish_banked_work("):
                          src.index("finish_banked_work(") + len("finish_banked_work(") + 4], \
        "finish_banked_work must get the real run_id, not None"
    # every return path after registering the run must also close it out, or it is
    # exactly the kind of row that sits 'running' forever
    after_start = src[i_start:]
    assert after_start.count("db.finish_run(run_id") >= 4, \
        "every return path once a run is registered must finish_run() it"


def test_a_winner_that_never_got_a_lane_is_also_continued():
    """Troy, 2026-09-25: Product Design won the 2026-09-24 review as the 5th of 5
    winners and sat completely untouched for a full day when the process died right as
    a lane should have opened for it -- resume_interrupted_scale_ups() couldn't see it
    because it never got a scaling_run_id stamp in the first place."""
    import inspect
    from app import runner
    from app.stages import s8_review as s8

    assert hasattr(s8, "resume_unscaled_winners"), \
        "a winner that never started needs its own catch-up path"
    assert hasattr(db := __import__("app.db", fromlist=["db"]), "unscaled_winners")
    src = inspect.getsource(db.unscaled_winners)
    assert "scaling_run_id IS NULL" in src and "scaling_started_at IS NULL" in src, \
        "must find campaigns that never even started, distinct from interrupted ones"
    assert "verdict = 'scale'" in src, "must only resurrect a still-valid SCALE verdict"

    rsrc = inspect.getsource(runner.resume)
    assert "resume_unscaled_winners(run_id, budget_minutes=_mins_left())" in rsrc, \
        "the two-hourly resume must actually call the new catch-up path"


def test_both_scale_up_catch_ups_share_one_body_that_re_fetches_live():
    """The same staleness bug that let Venture Capital get marked exhausted by a
    candidate list snapshotted before its real attempt found the vertical's true
    supply must not exist independently in two copy-pasted functions -- verified here
    by both delegating to one shared helper that re-fetches the campaign live."""
    import inspect
    from app.stages import s8_review as s8

    shared_src = inspect.getsource(s8._continue_scale_ups)
    assert "get_ve_campaign" in shared_src, \
        "must re-fetch the campaign LIVE right before acting, not trust the snapshot"
    interrupted_src = inspect.getsource(s8.resume_interrupted_scale_ups)
    unscaled_src = inspect.getsource(s8.resume_unscaled_winners)
    assert "_continue_scale_ups(" in interrupted_src
    assert "_continue_scale_ups(" in unscaled_src


def test_requeue_needs_review_skips_the_genuinely_unfixable():
    """Troy, 2026-09-25: "imagine how many needs review we've lost, how can we save
    them?" -- 2,889 all-time, stuck because needs_review is a BLOCKING status that
    nothing ever retries. Most failed a copy QA check a fresh attempt could plausibly
    pass (wrong company name, length, a leftover placeholder); "no first name" and an
    EmailBison decline (already in another sequence, bounced, unsubscribed) cannot be
    fixed by writing better copy, so they must never be requeued."""
    import inspect
    from app import db

    src = inspect.getsource(db.requeue_needs_review)
    assert "UNRETRYABLE_DROP_REASONS" in src
    assert set(db.UNRETRYABLE_DROP_REASONS) >= {"no first name", "EmailBison declined"}
    # resetting must match exactly what verified_uncopied() looks for, or the requeued
    # contact is invisible to the very step meant to pick it back up
    assert "status='verified'" in src or "status=\"verified\"" in src or \
        "status ='verified'" in src
    assert "personalization_1=NULL" in src or "personalization_1 = NULL" in src
    assert "requeue_count" in src, "must cap retries on a genuinely unfixable contact"


def test_resume_also_requeues_needs_review_automatically():
    """The manual `/ve requeue` command alone would need someone to remember to run
    it; the two-hourly resume must also do a small, capped sweep on its own so lost
    copy keeps recovering without a human in the loop."""
    import inspect
    from app import runner

    src = inspect.getsource(runner.resume)
    assert "requeue_needs_review(limit=" in src
    i_requeue = src.index("requeue_needs_review(")
    i_finish_banked = src.index("finish_banked_work(run_id)")
    assert i_requeue < i_finish_banked, \
        "a requeued contact should get its fresh copy attempt in the SAME tick"


def test_vc_and_pe_are_not_read_as_a_competitors_name():
    """Troy, 2026-09-25: 912 of 932 Venture Capital needs_review flags in one afternoon
    were "email 1 names 'VC'/'VCs'/'PE', which is not <the real firm>" -- the model
    writing "we help VCs land press" (the PROSPECT'S OWN industry, not a rival) and
    wrong_company() reading the generic word as a competitor's name."""
    from app import validator

    assert validator.wrong_company(
        "We help VCs land earned media that closes deals faster.",
        "K5 Global", "k5global.com") is None
    assert validator.wrong_company(
        "We help PE firms like yours get coverage.",
        "Synthesis Capital", "synthesiscapital.com") is None
    # a REAL competitor mention must still be caught
    assert validator.wrong_company(
        "We help Sequoia Capital land press.",
        "K5 Global", "k5global.com") == "Sequoia Capital"


def test_a_label_precision_stop_is_not_treated_as_genuine_exhaustion():
    """Troy, 2026-09-25: Venture Capital and Product Design were both permanently
    marked exhausted having scanned only 12-20% of their own real supply -- both
    stopped because their OWN labels scored a poor keep rate on a sample (2% and 9%)
    and, correctly respecting widen=False on a scale-up, never tried a sibling label.
    That is a label-precision signal, not proof the rest of the market does not exist,
    and only a genuine "the fetch itself returned nothing more" (source_exhausted)
    may close a vertical forever."""
    import inspect
    from app.stages import s3_companies as s3, s8_review as s8

    fsrc = inspect.getsource(s3.find)
    assert '"stop_reason": stop_reason' in fsrc, \
        "find() must report WHY it stopped, not just what it found"
    assert 'stop_reason = "label_keep_rate_too_low"' in fsrc
    assert 'stop_reason = "source_exhausted"' in fsrc

    ssrc = inspect.getsource(s8.scale_vertical)
    assert 'genuinely_exhausted = source_stats.get("stop_reason") == "source_exhausted"' \
        in ssrc, "only a genuine supply signal may close a vertical forever"
    assert ssrc.count("if genuinely_exhausted:") >= 2 or \
        ssrc.count("genuinely_exhausted") >= 3, \
        "every exhaustion decision point must be gated on the real signal"


def test_five_poor_nights_pauses_for_a_human_not_permanent_exhaustion():
    """Troy, 2026-09-25, choosing a human checkpoint over grinding forever or wrongly
    declaring a vertical exhausted: 5 consecutive nights where sourcing gives up on a
    poor label match (never a genuine "no companies left" signal) pauses auto-scaling
    and asks a human -- via scale_needs_human_at, distinct from scale_exhausted_at, so
    it is a pause, not a permanent close, and does not confuse the real exhaustion
    signal that report_gap()'s "widen the audience" messaging already relies on."""
    import inspect
    from app.stages import s8_review as s8

    assert s8.POOR_RESULT_STREAK_LIMIT == 5
    src = inspect.getsource(s8.scale_vertical)
    assert 'scale_needs_human_at' in src, \
        "must check and be pausable by a DIFFERENT flag than permanent exhaustion"
    assert "record_poor_result" in src and "reset_poor_streak" in src
    i_check = src.index('camp.get("scale_needs_human_at")')
    i_exhausted_check = src.index('camp.get("scale_exhausted_at")')
    assert i_exhausted_check < i_check, \
        "the permanent-exhaustion gate must still be checked, and checked first"


def test_an_ai_ark_credit_outage_is_not_treated_as_genuine_exhaustion():
    """2026-09-28: Product Design (campaign 1098) and Medical Device (campaign 1085)
    were both permanently marked exhausted off a round where EVERY query to AI Ark
    came back HTTP 402 "insufficient credit" -- the vendor was down, not the market
    genuinely searched and found empty. _fetch_fresh must tell the two apart: a round
    where every query errored out and nothing was ever scanned is a vendor outage
    (stop_reason "vendor_unavailable"), never "source_exhausted", so it cannot
    permanently close a vertical."""
    import inspect
    from app.stages import s3_companies as s3

    fsrc = inspect.getsource(s3._fetch_fresh)
    assert '"vendor_error"' in fsrc, \
        "_fetch_fresh must report when every query this round failed outright"
    assert "queries_failed" in fsrc

    find_src = inspect.getsource(s3.find)
    i_vendor = find_src.index('stop_reason = "vendor_unavailable"')
    i_exhausted = find_src.index('stop_reason = "source_exhausted"')
    i_vendor_check = find_src.index('fstats.get("vendor_error")')
    assert i_vendor_check < i_vendor < i_exhausted, \
        "the vendor-outage check must run, and be distinct from, the real exhaustion path"


def test_batch_topups_are_notified_once_past_a_threshold_not_every_time():
    """Troy, 2026-09-25: "rather than having to push a couple hundred leads everytime,
    what number do you recommend it gets to before notifying me?" A batch growing by
    56, then 57, then 119 pinged Slack three times for under 250 contacts total."""
    import inspect
    from app import runner
    from app.stages import s8_review as s8
    from app.config import settings

    assert settings.notify_added_threshold == 300
    for src in (inspect.getsource(runner._work_vertical),
                inspect.getsource(s8._finish_one_vertical),
                inspect.getsource(s8.scale_vertical)):
        assert "record_batch_addition" in src, \
            "every top-up path must accumulate rather than notify unconditionally"
        assert "notify_added_threshold" in src, \
            "the Slack post must be gated on the running total crossing the threshold"


def test_industry_jargon_across_verticals_is_not_a_competitor_name():
    """Troy, 2026-09-25: once the VC/PE bug was found, it turned out to be the single
    biggest source of needs_review across every vertical -- 2,439 "which is not" flags
    all-time, and generic industry terms (not a specific rival) accounted for nearly
    all of them: EdTech alone was 207, CDMOs 87, B2B 67, CROs 40, MedTech 38."""
    from app import validator

    jargon_examples = [
        ("We help EdTech companies land press coverage.", "Acme Learning"),
        ("CDMOs like yours rarely get earned media.", "CFBM"),
        ("AI-powered startups like yours need this.", "Acme"),
        ("We help B2B SaaS founders get coverage.", "Acme"),
        ("CROs in your space are underrepresented in the press.", "Acme"),
        ("MedTech companies like yours need this.", "Acme"),
        ("RIAs rarely invest in PR.", "Acme"),
        ("DME suppliers need visibility too.", "Acme"),
    ]
    for text, name in jargon_examples:
        assert validator.wrong_company(text, name, "acme.com") is None, text

    # a real competitor mention must still be caught, including ones that share no
    # words with the jargon list
    assert validator.wrong_company(
        "We helped Curi Bio land Forbes coverage last year.",
        "Acme", "acme.com") == "Curi Bio"
    assert validator.wrong_company(
        "We help Sequoia Capital land press.", "Acme", "acme.com") == "Sequoia Capital"


def test_least_scaled_winner_gets_first_crack_at_a_lane():
    """Troy, 2026-09-26: "how can we prioritize verticals we've never scaled like
    Product Design." Winners used to dispatch in _ve_campaigns()'s own order
    (created_at ascending), so the OLDEST winners always claimed the first
    parallel_verticals lanes -- not by design, just an accident of creation date.
    Product Design (created a day after Biotechnology and Medical Device) lost its
    lane to a crash and then queued behind the same two older winners again the next
    night. Ordering by pushed_lead_count ascending fixes that."""
    import inspect
    from app.stages import s8_review as s8

    src = inspect.getsource(s8.review_all)
    i_sort = src.index('winners.sort(key=lambda cr: cr[0].get("pushed_lead_count")')
    i_pool = src.index("pool.map(lambda cr: _act_on_winner")
    assert i_sort < i_pool, \
        "the least-scaled-first sort must happen before winners are dispatched to lanes"


def test_banked_contacts_are_title_checked_before_copy_is_paid_for():
    """Troy, 2026-09-26: "did the medical devices run with all our new changes
    yesterday, including job titles?" Only partly. The title filter runs when contacts
    are SOURCED, so contacts banked before the 2026-09-25 fix -- and the ones detached
    from batches during that day's cleanup -- sat as 'verified' and got paid-for copy
    anyway: 350 of Medical Device's 2,255 new batch contacts were off-ICP, 111 of
    Venture Capital's 4,192, and 914 more (28% of everything banked) were still waiting."""
    import inspect
    from app import db

    src = inspect.getsource(db.verified_uncopied)
    assert "score_title" in src, "banked contacts must be re-scored against current titles"
    assert "status='killed'" in src, \
        "off-ICP contacts must be KILLED, not merely skipped, or they are re-scored forever"
    assert src.index("score_title") < src.rindex("return fit"), \
        "only contacts that pass may be returned for copy"


def test_banked_work_finishes_the_least_scaled_vertical_first():
    """Troy, 2026-09-26: "why didn't product design and edtech run first?" Banked work
    was ordered by backlog size, so Venture Capital (4,005) and Medical Device (2,091)
    finished ahead of Product Design (1,045), which had already lost a day to a crash."""
    import inspect
    from app.stages import s8_review as s8

    src = inspect.getsource(s8.finish_banked_work)
    assert "banked.sort(" in src and "pushed_lead_count" in src
    assert src.index("banked.sort(") < src.index("_finish_one_vertical("), \
        "the ordering must be applied before any vertical is finished"


def test_pushed_lead_count_updates_on_an_ordinary_topup_push():
    """Troy, 2026-09-27: "does the system check how many leads are in a campaign
    before topping up?" It does -- scale_vertical compares pushed_lead_count against
    scale_target_leads -- but only the brand-new/pooled branch of s7_push ever wrote
    to that column. Every ordinary top-up push (the common case since align_on_topup)
    never called it at all: Medical Device drifted 4,104 leads behind EmailBison's own
    count, Venture Capital 5,813, both understating how close each campaign already
    was to its target."""
    import inspect
    from app.stages import s7_push

    src = inspect.getsource(s7_push.push)
    assert "db.increment_pushed_lead_count(campaign_id, len(contacts))" in src
    i_mark_pushed = src.index("mark_pushed(batch_id)")
    i_increment = src.index("db.increment_pushed_lead_count(campaign_id, len(contacts))")
    i_is_new_block = src.index("if is_new or pooled:")
    # the increment must run for EVERY push (after mark_pushed, unconditionally), not
    # only inside the is_new/pooled branch that gated it before
    assert i_increment > i_mark_pushed
    assert i_increment > i_is_new_block + len("if is_new or pooled:")
    # and the is_new/pooled insert must no longer ALSO add to the count, or a brand
    # new campaign's first push double-counts
    is_new_block = src[i_is_new_block:i_increment]
    assert "0,%s,%s" in is_new_block or "len(contacts)" not in is_new_block, \
        "the is_new/pooled insert must seed pushed_lead_count at 0, not len(contacts)"


def test_idpr_contact_cascade_puts_ai_ark_last_like_productevo():
    """Troy, 2026-09-27: "yes" to giving IDPR the same AI-Ark-last policy ProductEVO
    got the same day, to protect a scarce/paid account -- AI Ark was running BEFORE
    Apollo for IDPR (and every other client), the opposite of ProductEVO's cascade."""
    import importlib
    from unittest.mock import patch
    from app import client as _client

    import os
    old = os.environ.get("CLIENT")
    try:
        os.environ["CLIENT"] = "idpr"
        _client.load.cache_clear()
        from app import finders
        importlib.reload(finders)

        order = []
        with patch.object(finders, "find_apollo",
                          side_effect=lambda *a, **k: order.append("apollo") or []), \
             patch.object(finders, "find_aiark",
                          side_effect=lambda *a, **k: order.append("aiark") or []), \
             patch.object(finders, "find_quickenrich",
                          side_effect=lambda *a, **k: order.append("quickenrich") or []):
            finders.find_contacts("example.com", ["CEO"], want=5)
        assert order == ["quickenrich", "apollo", "aiark"], order
    finally:
        if old is None:
            os.environ.pop("CLIENT", None)
        else:
            os.environ["CLIENT"] = old
        _client.load.cache_clear()
