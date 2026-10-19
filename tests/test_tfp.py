"""TFP build: the spec's prompts load through the engine, the copy rules catch the spec's bans, the client profile is what Emmanuel set."""
import pytest

from app import client, copy_rules, prompts

ANGLES = ["a5-load-type", "a7-carrier-liability", "a10-peer-proof"]


def test_profile_is_tfp_with_the_agreed_firmographics():
    p = client.profile()
    assert (p.workspace_id, p.model_campaign_id, p.database) == (93, 2103, "tfp")
    assert p.employee_min == 100 and p.countries == ("US", "CA") and p.contacts_per_company == 5
    assert p.reuse_fit_check is True          # TFP fit-checks every company: no detach in EmailBison
    assert [c[0] for c in p.copy_variants] == ANGLES
    assert len(p.verticals) == 17 and "Construction and Infrastructure" in p.verticals and "Defense and Aerospace Manufacturing" in p.verticals


@pytest.mark.parametrize("angle", ANGLES)
def test_each_angle_has_research_email1_and_email2_prompts(angle):
    r, e1, e2 = prompts.research(angle), prompts.email1(angle), prompts.email2(angle)
    assert r.model == "openai/gpt-oss-120b" and e1.model == e2.model == "anthropic/claude-haiku-4.5"
    for pr in (e1, e2):
        assert "P1:" in pr.system and "INEOS" not in pr.system and "SKIP RULE" not in pr.system and "NEVER MENTION" in pr.system
        assert "Do NOT start with a greeting" in pr.system or "Do not start with a greeting" in pr.system
    assert "{research_report}" in e1.user_template and "{email_1_output}" in e2.user_template


def bad(text):
    return copy_rules.check(text)


def test_spec_bans_are_enforced():
    assert bad("You can have the free checklist.\nWant it?")   # spam-trigger word free
    assert bad("INEOS Composites uses it.\nWant the story?")
    assert bad("First line!\nSecond line!\nWant it?")                                   # one exclamation at most
    assert bad("We built it and we love it.\nOur panels fit.\nWant it?")                # we/our/us at most once
    assert bad("Open to a quick call?")                                                 # meeting ask, spam word, quick call
    assert bad("Nucor runs 4 sites: all differ.\nWant it?")                             # colon in the opening line


def test_a_clean_email_passes_the_tfp_rules():
    ok = ("Four service centers means four yards with four different dock layouts.\n\n"
          "Steel Dynamics ran into that across its flat rolled plants and ended up with one approach sized to each site.\n\n"
          "Worth sending how they rolled it out?")
    assert bad(ok) == []


def test_eaton_may_be_named_and_the_settlement_figure_is_allowed():
    assert bad("A fall at an Eaton Steel plant ended in a $1.4M settlement.\nAre carrier drivers covered by your setup?") == []


def test_tfp_hard_rules_are_switched_on_and_company_is_three_words():
    p = client.profile()
    assert p.mx_blocklist is True and p.company_custom_field is True
    from app.clients import emailbison
    lead = emailbison._lead_payload({"email": "a@x.com", "first_name": "A", "company_name": "ALG Worldwide Logistics Inc", "company_domain": "x.com"})
    assert lead["custom_fields"]["COMPANY"] == "ALG Worldwide Logistics" or len(lead["custom_fields"]["COMPANY"].split()) <= 3


def test_mx_filter_drops_gateway_contacts_before_copy(monkeypatch):
    from app import mx, wave1_fill
    monkeypatch.setattr(mx, "blocked", lambda d: "mimecast" if d == "acme.com" else None)
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, *a, **k): pass
    monkeypatch.setattr(wave1_fill.db, "cursor", lambda: Cur())
    monkeypatch.setattr(wave1_fill.db, "log", lambda *a, **k: None)
    keep = wave1_fill.mx_filter([{"email": "a@acme.com", "company_domain": "acme.com"}, {"email": "c@ok.com", "company_domain": "ok.com"}], "r1")
    assert [c["email"] for c in keep] == ["c@ok.com"]


def test_testrun_is_a_web_job_with_resumable_state(monkeypatch):
    from app import testrun
    assert testrun.LOCK == "testrun" and callable(testrun.run_safe)
    monkeypatch.setattr(testrun, "_ensure", lambda: None)
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): self.n = a[0]
        def fetchone(self): return {"n": 7}
    monkeypatch.setattr(testrun.db, "cursor", lambda: Cur())
    assert testrun._bank() == 7


def test_fit_before_verify_is_on_and_documented():
    assert client.profile().fit_before_verify is True and client.profile().skip_source_gate is True
    import inspect
    from app.stages import s4_people
    src = inspect.getsource(s4_people.build)
    assert src.index("fit_before_verify") < src.index("waterfall.verify_batch(shortlist")   # the fit check comes before any address is verified


def test_runway_report_is_compact_and_recommends(monkeypatch):
    from app import runway
    n = dict(goal=4500, target=1500, loaded=232, loaded_by={"A5": 64, "A7": 82, "A10": 86}, per_hour=40, bank=764, held=10, sourced=5113, searched=2185,
             left=2928, with_contacts=728, contact_rows=1236, fit_failed=375, cps=0.57, potential=2250.0)
    monkeypatch.setattr(runway, "numbers", lambda: n)
    out = runway.report()
    assert len(out.splitlines()) == 6 and "5,113 sourced" in out and "43% used" in out and "Next:" in out
    assert "more companies are needed" in runway.recommendation(n) and "Copy is the slower side" in runway.recommendation(n)


def test_copy_chain_can_be_overridden_and_rotated(monkeypatch):
    from app.clients import llm
    monkeypatch.setattr(llm, "_has", lambda p: True)
    monkeypatch.setattr(llm, "_is_down", lambda p: False)
    monkeypatch.setenv("LLM_COPY_CHAIN", "openrouter,buzzlead,concentrate")
    monkeypatch.delenv("LLM_COPY_ROTATE", raising=False)
    assert llm._order("copy") == ["openrouter", "buzzlead", "concentrate"]
    monkeypatch.setenv("LLM_COPY_ROTATE", "true")
    firsts = {llm._order("copy")[0] for _ in range(4)}
    assert firsts == {"openrouter", "buzzlead"}                       # the load is split across both accounts
    assert llm._order("research")[0] == "buzzlead"                    # research stays where it is fastest


def test_runway_page_renders_in_plain_words(monkeypatch):
    from app import runway, runway_page
    n = dict(goal=4500, target=1500, loaded=239, loaded_by={"A5": 77, "A7": 98, "A10": 64}, per_hour=173, bank=974, held=103, sourced=5059, searched=2583,
             left=2476, with_contacts=797, contact_rows=1300, fit_failed=682, cps=0.5, potential=2166.0)
    monkeypatch.setattr(runway_page, "numbers", lambda: n)
    monkeypatch.setattr(runway_page, "_targets", lambda: {"target": 1500, "angles": {"A5": 2103, "A7": 2106, "A10": 2107}})
    monkeypatch.setattr(runway_page, "held_reasons", lambda: [("Email came out longer than the cap, even after retries", 40)])
    monkeypatch.setattr(runway_page, "_vertical_rows", lambda: [{"v": "Food and Beverage Production", "sourced": 373, "searched": 114, "with_contacts": 57}])
    html = runway_page.page_html()
    assert "239 of 4,500 leads loaded (5%)" in html and "What I recommend" in html and "Contacts ready for copy" in html and 'http-equiv="refresh"' in html


def test_own_product_and_proof_names_do_not_trip_the_wrong_company_check():
    from app import validator
    body = "Steel Dynamics put NoFalls on its trucks after the same problem.\nWorth a look at how Eaton Steel did it?"
    assert validator.wrong_company(body, "J Walter Miller Co", "jwmiller.com") is None


def test_everyday_words_are_no_longer_spam_but_real_triggers_still_are():
    assert bad("You can get the checklist and the new one is open now.\nWant it?") == []      # get, new, open, now were dropped from the list
    assert bad("This is a guaranteed result.\nWant it?")                                       # a real trigger still fails
    assert not bad("Georgia Pacific runs four sites.\nWorth a look at how Eaton Steel did it?")


def test_the_fill_queue_takes_contacts_at_companies_our_sourcing_found():
    import inspect
    from app import wave1_fill
    src = inspect.getsource(wave1_fill.fill)
    assert "FROM staged_companies s WHERE s.domain = c.company_domain" in src


def test_sweep_does_not_demote_leads_when_the_listing_is_incomplete():
    import inspect
    from app import wave1_fill
    src = inspect.getsource(wave1_fill.sweep)
    assert "lead listing incomplete" in src and "0.98" in src


def _rows(a=(1000, 700, 3, 0), b=(1000, 700, 1, 0), c=(1000, 700, 0, 0)):
    mk = lambda t: {"leads": t[0], "sent": t[1], "replied": 10, "interested": t[2], "booked": t[3], "positive": t[2] + t[3]}
    return {"A5": mk(a), "A7": mk(b), "A10": mk(c)}


def _cfg():
    from app import testgrid
    return {"rules": {"freeze_days": 7, "decision_day": 10, "booked_weight": 3, "min_sends_per_angle": 700}, "angles": ["A5", "A7", "A10"]}


def test_nothing_is_decided_until_every_angle_has_sent_the_minimum(monkeypatch):
    from datetime import datetime, timezone
    from app import testgrid, db
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    launched, now = datetime(2026, 10, 16, tzinfo=timezone.utc), datetime(2026, 10, 27, tzinfo=timezone.utc)
    short = _rows(c=(1000, 650, 0, 0))
    v = testgrid.decide(short, launched, _cfg(), now)
    assert v["status"] == "monitoring" and v["waiting_for_volume"] == {"A10": 650}
    v = testgrid.decide(_rows(), launched, _cfg(), now)
    assert v["status"] == "decided" and v["winner"] == "A5" and "per email sent" in v["reason"]


def test_scoring_is_per_email_sent_not_per_lead_loaded(monkeypatch):
    from app import testgrid
    slow = {"leads": 1500, "sent": 700, "replied": 5, "interested": 2, "booked": 0}      # 2 per 700 sent
    fast = {"leads": 1500, "sent": 1400, "replied": 5, "interested": 3, "booked": 0}     # 3 per 1400 sent: lower per send, higher raw
    assert testgrid._score(slow, 3)[0] > testgrid._score(fast, 3)[0]
    assert testgrid._score({"leads": 100, "replied": 0, "interested": 1, "booked": 0}, 3)[0] == 0.01   # no sends known: falls back to per lead


def test_nightly_status_is_plain_and_lists_each_angle():
    from app import nightly
    txt = nightly.status_text({"status": "monitoring", "by_angle": _rows(), "reason": "frozen until 23 Oct"})
    assert "A5: 700 sent, 1,000 loaded" in txt and "frozen until 23 Oct" in txt


def test_lookalike_clone_keeps_the_parents_proofs_and_swaps_only_the_angle():
    from pathlib import Path
    from app import angle_derivation as ad
    base = Path(__file__).resolve().parent.parent / "clients" / "tfp" / "prompts" / "a10-peer-proof"
    prop = {"reasoning": "loss framing", "pitch_one_liner": "A peer already pays for this exposure.", "display_name": "Peer Cost",
            "email1_voice": "Open with the exposure at their named site, then the peer.", "email2_voice": "Offer one product fact."}
    e1 = ad._clone_prompt((base / "prompt_email1.txt").read_text(encoding="utf-8"), "email1", "peer-cost", "A10", prop)
    assert "Open with the exposure at their named site" in e1 and "P1:" in e1 and "NEVER MENTION" in e1 and "DATA YOU RECEIVE" in e1
    assert "single_site:" not in e1 and 'angle "Peer Cost"' in e1 and "VARIANT: peer-cost" in e1.split("\n")[1]
    e2 = ad._clone_prompt((base / "prompt_email2.txt").read_text(encoding="utf-8"), "email2", "peer-cost", "A10", prop)
    assert 'angle is "Peer Cost"' in e2 and "Offer one product fact." in e2 and "PROOF LIBRARY" in e2
    r = ad._clone_prompt((base / "prompt_research.txt").read_text(encoding="utf-8"), "research", "peer-cost", "A10", prop)
    assert "company_short" in r and "VARIANT: peer-cost" in r


def test_angle_learning_is_on_for_tfp_in_clone_mode():
    from app import angle_derivation as ad
    assert ad._enabled() and ad._clone_mode() and ad._campaign_prefix() == "TFP_W1_"
    assert "Truck Fall Prevention" in ad._brainstorm_system() and "ProductEVO" not in ad._brainstorm_system()


def test_no_winner_is_named_and_nobody_is_paused_when_nobody_has_a_positive_reply(monkeypatch):
    from datetime import datetime, timezone
    from app import testgrid, db
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    v = testgrid.decide(_rows(a=(1000, 800, 0, 0), b=(1000, 800, 0, 0), c=(1000, 800, 0, 0)), datetime(2026, 10, 16, tzinfo=timezone.utc), _cfg(),
                        datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert v["status"] == "monitoring" and "nobody is paused on a tie" in v["reason"]


def test_sends_and_replies_come_from_emailbison_counters(monkeypatch):
    from app import testgrid, db
    from app.clients import emailbison
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return [{"angle": "A5", "campaign_id": 1}]
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    monkeypatch.setattr(emailbison, "get_campaign", lambda cid: {"emails_sent": 665, "unique_replies": 8, "interested": 2})
    rows = {"A5": {"leads": 10, "replied": 0, "interested": 0, "booked": 1, "positive": 1}}
    testgrid._attach_sends(rows, {})
    assert rows["A5"] == {"leads": 10, "replied": 8, "interested": 2, "booked": 1, "positive": 3, "sent": 665}
