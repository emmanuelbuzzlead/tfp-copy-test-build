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
    assert len(p.verticals) == 16 and "Construction and Infrastructure" in p.verticals and "Defense and Aerospace Manufacturing" in p.verticals


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
    assert "239 of 4,500 leads loaded (5%)" in html and "What I recommend" in html and "Contacts ready for copy" in html


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


def test_materials_standards_match_the_locked_field_rules():
    from app import materials as m
    assert m.check_materials("steel coils and structural beams") == []
    assert "6 words (max 5)" in m.check_materials("steel coils and structural beams today")[0] or m.check_materials("steel coils and structural beams today")
    assert any("lowercase" in x for x in m.check_materials("Steel coils and bars"))
    assert any("both sides vague" in x for x in m.check_materials("freight and goods"))
    assert any("dangling modifier" in x for x in m.check_materials("liquid chemicals and dry"))
    assert any("repeated" in x for x in m.check_materials("pallets and pallets"))
    assert any("generic fallback" in x for x in m.check_materials("palletized freight and general goods"))
    assert m.check_materials("lumber") and m.check_materials("")
    assert m.check_company("Oklahoma Tank Lines United Petroleum Transports") and m.check_company("Acme Inc")
    assert m.check_company("Steel Dynamics") == []


def test_company_name_is_cleaned_with_the_latest_cleaner_and_passes_the_standard():
    from app import materials as m
    from app.company_name_cleaner import clean_company_name
    for raw in ["ALG Worldwide Logistics Inc", "Oklahoma Tank Lines United Petroleum Transports", "KWS Manufacturing Company, LLC",
                "Schnellecke Logistics AG & Co. KG", "OnlineMetals.com", "General Stamping & Metalworks, Inc."]:
        assert m.check_company(clean_company_name(raw)) == [], (raw, clean_company_name(raw))


def test_materials_reply_is_cleaned_and_unclear_means_held(monkeypatch):
    from app import materials as m
    from app.clients import llm
    replies = iter(['"Steel Coils and Structural Beams."', "unclear"])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: next(replies))
    assert m.infer("Acme Steel", "makes steel coils", "Steel") == "steel coils and structural beams"
    assert m.infer("Mystery Co", "", "") is None


def test_infer_retries_once_when_the_first_answer_breaks_the_standard(monkeypatch):
    from app import materials as m
    from app.clients import llm
    replies = iter(["freight and goods", "roofing shingles and underlayment"])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: next(replies))
    assert m.infer("Roof Co", "roofing", "Building Materials") == "roofing shingles and underlayment"


def test_lead_payload_carries_materials_hauled_for_the_og_copy_campaign():
    from app.clients import emailbison
    lead = emailbison._lead_payload({"email": "a@x.com", "first_name": "A", "company_name": "ALG Worldwide Logistics Inc", "company_domain": "x.com",
                                     "materials_hauled": "steel coils and bars"})
    assert lead["custom_fields"]["MATERIALS_HAULED"] == "steel coils and bars" and lead["custom_fields"]["COMPANY"]
    plain = emailbison._lead_payload({"email": "b@x.com", "first_name": "B", "company_name": "Acme", "company_domain": "x.com"})
    assert "MATERIALS_HAULED" not in plain["custom_fields"]


def test_angle_fill_can_be_held_and_the_og_campaign_mode_is_off_by_default(monkeypatch):
    from app import materials_fill, wave1_fill
    monkeypatch.setenv("HOLD_ANGLE_FILL", "true")
    assert wave1_fill.needed() is False and wave1_fill.fill() == {"status": "held"}
    monkeypatch.delenv("MATERIALS_CAMPAIGN_ID", raising=False)
    assert materials_fill.campaign_id() is None and materials_fill.run_pass() == {"status": "not_enabled"}
    monkeypatch.setenv("MATERIALS_CAMPAIGN_ID", "2157")
    assert materials_fill.campaign_id() == 2157 and materials_fill.target() == 3500


def test_runway_page_shows_the_og_campaign_as_one_more_row_against_its_goal(monkeypatch):
    from app import runway_page
    n = dict(goal=4500, target=1500, loaded=4500, loaded_by={"A5": 1500, "A7": 1500, "A10": 1500}, per_hour=0, bank=917, held=0, sourced=30581, searched=13138,
             left=17443, with_contacts=3437, contact_rows=5000, fit_failed=3311, cps=0.4, potential=11444.0,
             og=[{"campaign": 2157, "target": 3500, "loaded": 900, "per_hour": 300, "held_unclear": 12, "held_standard": 3, "name": "Single Site | 21st Oct (OG copy)", "sent": 0, "interested": 0}])
    monkeypatch.setattr(runway_page, "numbers", lambda: n)
    monkeypatch.setattr(runway_page, "_targets", lambda: {"target": 1500, "angles": {"A5": 2103, "A7": 2106, "A10": 2107}})
    monkeypatch.setattr(runway_page, "held_reasons", lambda: [])
    monkeypatch.setattr(runway_page, "_vertical_rows", lambda: [])
    html = runway_page.page_html()
    assert "900 of 3,500" in html and "campaign 2157" in html and html.count("Leads by campaign") == 1
    assert "900 of 3,500 leads loaded across 1 fill campaign (26%)" in html


def test_company_only_mode_and_the_campaign_total_counts_leads_loaded_by_others(monkeypatch):
    from app import materials_fill
    from app.clients import emailbison
    monkeypatch.setenv("MATERIALS_CAMPAIGN_ID", "2174")
    monkeypatch.setenv("MATERIALS_NEEDED", "false")
    assert materials_fill.needs_materials() is False
    monkeypatch.setattr(emailbison, "get_campaign", lambda cid: {"total_leads": 602})
    assert materials_fill.loaded() == 602                        # 602 came from another source and still count toward the 2,000
    monkeypatch.setenv("MATERIALS_NEEDED", "true")
    assert materials_fill.needs_materials() is True


def test_leads_already_in_the_campaign_are_never_pushed_again(monkeypatch):
    from app import materials_fill
    from app.clients import emailbison
    materials_fill._existing_cache.clear()
    monkeypatch.setattr(emailbison, "_paged", lambda path, params=None, cap=400: [{"email": "A@X.com"}, {"email": "b@y.com"}])
    assert materials_fill.existing_emails(2174) == {"a@x.com", "b@y.com"}


def test_several_campaigns_can_be_filled_and_a_raised_goal_wins(monkeypatch):
    from app import materials_fill, db
    monkeypatch.setenv("MATERIALS_CAMPAIGNS", "2174:2100:company, 2190:1500:materials")
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return [{"campaign_id": 2174, "target": 5000}]
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    sp = materials_fill.specs()
    assert [(x["campaign"], x["target"], x["materials"]) for x in sp] == [(2174, 5000, False), (2190, 1500, True)]
    assert materials_fill.target() == 6500 and materials_fill.target(2190) == 1500 and materials_fill.label() == "2174, 2190"


def test_scale_review_bar_needs_volume_and_real_interest():
    from app import scale_review
    g = dict(scale_review.DEFAULTS)
    ok, why = scale_review.judge({"sent": 900, "interested": 5}, g)
    assert not ok and "1,500" in why
    ok, why = scale_review.judge({"sent": 2000, "interested": 2}, g)
    assert not ok and "bar is 3" in why
    ok, why = scale_review.judge({"sent": 4000, "interested": 3}, g)
    assert not ok and "0.75" in why
    ok, why = scale_review.judge({"sent": 2500, "interested": 4}, g)
    assert ok and "1.60 per 1,000" in why


def test_tfp_winner_scales_to_3500_first_and_asks_before_going_further():
    from app import scale_review, testgrid
    cfg = testgrid.load_config()
    gate = cfg["scale_gate"]
    assert gate["stage1_target"] == 3500 and gate["review_after_days"] == 5 and gate["next_targets"] == [5000, 10000]
    assert scale_review.gate()["min_sends"] == gate["min_sends"]
    import inspect
    src = inspect.getsource(testgrid._evaluate_and_record_locked)
    assert "stage1" in src and "scale_review.schedule" in src


def test_scale_buttons_are_routed_to_the_review_approval():
    from app import web
    kind, command, user = web.interaction_to_command({"type": "block_actions", "user": {"username": "e"},
                                                       "actions": [{"action_id": "scale_review_5000", "value": "angle:A5|5000"}]})
    assert kind == "scale_review" and command == "angle:A5|5000"


def test_backfill_keeps_valid_values_fixes_filler_and_lists_what_it_cannot_describe(monkeypatch):
    from app import db, materials_backfill as b, materials_fill
    from app.clients import emailbison
    leads = [
        {"id": 1, "email": "a@steelco.com", "first_name": "A", "last_name": "B", "title": "Safety Manager", "company": "Steelco",
         "custom_variables": [{"name": "company", "value": "Steelco"}, {"name": "materials_hauled", "value": "steel coils and bars"}]},
        {"id": 2, "email": "b@roofco.com", "first_name": "B", "last_name": "C", "title": "Plant Manager", "company": "Roofco",
         "custom_variables": [{"name": "company", "value": "Roofco"}, {"name": "materials_hauled", "value": "general freight and palletized goods"}]},
        {"id": 3, "email": "c@mystery.com", "first_name": "C", "last_name": "D", "title": "EHS Manager", "company": "Mystery Corp",
         "custom_variables": [{"name": "company", "value": "Mystery Corp"}]},
        {"id": 5, "email": "e@acme.com", "first_name": "E", "last_name": "F", "title": "Manager", "company": "ACME Industries Inc.",
         "custom_variables": [{"name": "company", "value": "ACME Industries Inc."}, {"name": "materials_hauled", "value": "pallets and crates"}]},
        {"id": 4, "email": "d@gmail.com", "first_name": "D", "last_name": "E", "title": "Manager", "company": "Dee Inc", "custom_variables": []},
    ]
    sent = []
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def executemany(self, *a, **k): pass
        def fetchone(self): return None
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    monkeypatch.setattr(materials_fill, "ensure_tables", lambda: None)
    monkeypatch.setattr(b, "_ensure_unresolved", lambda: None)
    monkeypatch.setattr(emailbison, "_paged", lambda path, params=None, cap=400: leads)
    monkeypatch.setattr(emailbison, "ensure_custom_variables", lambda names: None)
    monkeypatch.setattr(emailbison, "_send_retry", lambda method, path, body=None: sent.append(body))
    monkeypatch.setattr(materials_fill, "phrases", lambda rows: ({"roofco.com": "roofing shingles and underlayment", "mystery.com": None}, {}))
    dry = b.run(2174, apply=False)
    assert (dry["leads"], dry["already_valid"], dry["to_fix"], dry["company_recleaned"], dry["resolved"], dry["unresolved"], dry["applied"]) == (5, 2, 3, 1, 2, 2, False) and not sent
    done = b.run(2174, apply=True)
    assert done["applied"] and len(sent) == 1 and sent[0]["existing_lead_behavior"] == "patch"
    by = {l["email"]: l for l in sent[0]["leads"]}
    assert {"name": "materials_hauled", "value": "roofing shingles and underlayment"} in by["b@roofco.com"]["custom_variables"]
    assert by["e@acme.com"]["company"] != "ACME Industries Inc." and {"name": "materials_hauled", "value": "pallets and crates"} in by["e@acme.com"]["custom_variables"]



def test_parallel_chunks_never_search_the_same_company_twice(monkeypatch):
    from app import scaling, testrun
    class Prof: verticals = ("V",)
    pool = [{"domain": f"c{i}.com", "name": f"C{i}"} for i in range(10)]
    monkeypatch.setattr(scaling, "pick_companies", lambda verts, n: pool[:n])
    testrun._claimed.clear()
    first = testrun._claim(Prof, 4)
    second = testrun._claim(Prof, 4)
    assert [c["domain"] for c in first] == ["c0.com", "c1.com", "c2.com", "c3.com"]
    assert [c["domain"] for c in second] == ["c4.com", "c5.com", "c6.com", "c7.com"]
    assert testrun._claim_peek(Prof) is True
    testrun._claim(Prof, 10)
    assert testrun._claim_peek(Prof) is False
    testrun._claimed.clear()


def test_a_sourcing_round_ends_with_a_forced_tier_pass_and_the_page_counts_searchable_companies():
    import inspect
    from app import runway, testrun
    src = inspect.getsource(testrun._source_round)
    assert "funnel.refresh(force=True)" in src and src.index("funnel.refresh(force=True)") > src.index("source_round=rnd")
    rsrc = inspect.getsource(runway.numbers)
    assert "f.tier = 1" in rsrc and "untiered" in rsrc          # the pool is companies the search can pick, not stored minus searched


def test_sourcing_only_mode_stops_when_the_bank_is_full_and_never_loads_anything(monkeypatch):
    from app import testrun
    monkeypatch.setenv("SOURCING_ONLY", "true")
    monkeypatch.setenv("SOURCING_TARGET", "50")
    assert testrun.sourcing_only() is True and testrun.sourcing_target() == 50
    monkeypatch.setattr(testrun, "_bank", lambda: 60)
    monkeypatch.setattr(testrun, "_load", lambda: {})
    assert testrun.ensure_running() == "done"                       # 60 ready contacts already meet a goal of 50
    import inspect
    src = inspect.getsource(testrun.run)
    assert src.index("if only:") < src.index("materials_fill.run_pass()")     # the consumer branch is skipped in this mode


def test_scorecard_report_is_plain_and_flags_unmeasured_cost(monkeypatch):
    from app import sourcing_health as sh
    monkeypatch.setattr(sh, "scorecard", lambda: dict(searched=1000, with_contacts=260, yield_pct=26.0, fit_failed=250, fit_failed_pct=25.0, contacts=420,
                        contacts_per_company_with_contacts=1.6, contacts_per_company_searched=0.42, searched_last_hour=900, searched_last_24h=5000,
                        gateway_skips_24h=120, bank=300, held=[("behind a blocked mail gateway", 12)], best_verticals=[{"vertical": "Steel", "yield_pct": 33.0}],
                        weakest_verticals=[{"vertical": "Rail", "yield_pct": 12.0}], sent=2000, bounced=80, bounce_pct=4.0))
    monkeypatch.setattr(sh, "providers", lambda: dict(keys={"email verification": ["MILLIONVERIFIER_API_KEY: set", "TRYKITT_API_KEY: MISSING"]},
                        openrouter_balance=71.5, vendor_error_lines_last_hour=3, latest_vendor_error="stage4: 429 rate limit"))
    out = sh.report()
    assert "Gave at least one contact: 260 (26.0%)" in out and "Cost per contact: not measured" in out
    assert "email verification: TRYKITT_API_KEY: MISSING" in out and "Bounce rate of what was sent: 4.0%" in out and "$71.5" in out


def test_yield_breaker_puts_the_chunk_back_and_pauses_instead_of_burning_companies(monkeypatch):
    from app import company_fit, db, scaling, testrun
    class Cur:
        calls = []
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): Cur.calls.append(sql)
        def fetchone(self): return {"n": 0}
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    companies = [{"domain": f"c{i}.com", "name": f"C{i}", "vertical": "V"} for i in range(250)]
    monkeypatch.setattr(testrun, "_claim", lambda prof, n: companies)
    monkeypatch.setattr(scaling, "source_contacts", lambda comps, run_id: [{"company_domain": "c1.com"}])      # 1 of 250 gave a contact
    monkeypatch.setattr(company_fit, "check", lambda *a, **k: {})
    said = []
    monkeypatch.setattr(testrun, "_say", lambda text: said.append(text))
    from app import sourcing_health
    monkeypatch.setattr(sourcing_health, "probe", lambda force=False: "AI Ark: KEY REJECTED")
    testrun._trips.update(count=0, until=0.0)
    class Prof: verticals = ("V",)
    assert testrun._one_chunk(Prof, {}, "run", 250) == -1
    assert testrun._paused() is True and any("found_at = NULL" in c for c in Cur.calls)
    assert said and "yield collapsed" in said[0] and "AI Ark: KEY REJECTED" in said[0]
    testrun._trips.update(count=0, until=0.0)


def test_a_single_interested_reply_never_decides_the_test_or_pauses_anyone(monkeypatch):
    from datetime import datetime, timezone
    from app import testgrid, db
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    cfg = _cfg()
    cfg["rules"]["min_positive_to_decide"] = 3
    launched, now = datetime(2026, 10, 16, tzinfo=timezone.utc), datetime(2026, 10, 27, tzinfo=timezone.utc)
    tie = {"A5": {"leads": 1500, "sent": 3500, "replied": 20, "interested": 0, "booked": 0, "positive": 0},
           "A7": {"leads": 1500, "sent": 3500, "replied": 30, "interested": 1, "booked": 0, "positive": 1},
           "A10": {"leads": 2300, "sent": 2400, "replied": 44, "interested": 1, "booked": 0, "positive": 1}}
    v = testgrid.decide(tie, launched, cfg, now)
    assert v["status"] == "monitoring" and "only 1 positive reply" in v["reason"] and "nobody is paused" in v["reason"]
    tie["A10"].update(interested=3, positive=3)
    v = testgrid.decide(tie, launched, cfg, now)
    assert v["status"] == "decided" and v["winner"] == "A10"


def test_add_leads_raises_the_goal_by_the_amount_pressed_and_is_capped(monkeypatch):
    from app import materials_fill as mf, runway, testrun
    raised = {}
    monkeypatch.setattr(mf, "specs", lambda: [{"campaign": 2174, "target": 2100, "materials": False}])
    monkeypatch.setattr(mf, "overview", lambda: [])
    monkeypatch.setattr(mf, "loaded", lambda cid=None: 2103)
    monkeypatch.setattr(mf, "raise_target", lambda cid, t: raised.update({cid: t}))
    monkeypatch.setattr(mf, "_nudge", lambda: None)
    monkeypatch.setattr(mf.db, "log", lambda *a, **k: None)
    monkeypatch.setattr(testrun, "_bank", lambda: 40)
    monkeypatch.setattr(runway, "numbers", lambda: {"left": 8000})
    r = mf.add_leads(2174, 500, "emmanuel")
    assert r["ok"] and r["target"] == 2603 and raised == {2174: 2603} and "8,000 companies" in r["note"]
    assert not mf.add_leads(2174, 5000)["ok"] and not mf.add_leads(2174, 0)["ok"]       # above the 3,000 cap
    assert "not one of this client" in mf.add_leads(9999, 100)["reason"]


def test_slack_add_leads_button_and_commands_are_wired():
    from app import materials_fill as mf, web
    from app.stages import s6_approval
    kind, command, user = web.interaction_to_command({"type": "block_actions", "user": {"username": "e"}, "actions": [{"action_id": "add_leads_500", "value": "2174|500"}]})
    assert kind == "add_leads" and command == "2174|500"
    import inspect
    src = inspect.getsource(s6_approval.handle_command)
    assert 'action in ("campaigns", "controls")' in src and 'action == "add"' in src and 'action == "register"' in src
    assert "confirm" in inspect.getsource(mf.controls_blocks)             # every button asks "are you sure" first


def test_page_has_one_dropdown_with_simple_labels_and_the_endpoint_needs_the_password(monkeypatch):
    from app import materials_fill, runway_page, web
    rows = [
        {"campaign": 2103, "kind": "angle", "angle": "A5", "name": "A5 load type", "loaded": 1547, "target": 1500, "sent": 3561, "interested": 0, "waiting": 2, "days_left": 0.0,
         "recommendation": "No interested yet", "queue": "out of leads"},
        {"campaign": 2107, "kind": "angle", "angle": "A10", "name": "A10 peer proof", "loaded": 2373, "target": 2973, "sent": 2441, "interested": 1, "waiting": 1063, "days_left": 8.1,
         "recommendation": "Recommended", "queue": "8 days of leads left"},
        {"campaign": 2106, "kind": "angle", "angle": "A7", "name": "A7 carrier", "loaded": 1507, "target": 1500, "sent": 3538, "interested": 1, "waiting": 2, "days_left": 0.0,
         "recommendation": "Recommended", "queue": "out of leads"}]
    monkeypatch.setattr(materials_fill, "overview", lambda: rows)
    assert materials_fill.suggested(rows) == 2106                      # both have an interested reply; A7 runs out of leads first
    html = runway_page._controls_html()
    assert html.count('<select id="camp">') == 1 and 'value="2106" selected' in html and "1 interested, out of leads, Recommended" in html
    assert "0 interested, out of leads, No interested yet" in html and 'value="500"' in html and 'value="3000"' in html and 'onclick="addLeads()"' in html
    none = [dict(r, interested=0, recommendation="No interested yet") for r in rows]
    monkeypatch.setattr(materials_fill, "overview", lambda: none)
    assert materials_fill.suggested(none) is None and "none is recommended" in runway_page._controls_html()
    monkeypatch.setattr(materials_fill, "overview", lambda: rows)
    assert "/api/add-leads" in runway_page.ADD_JS and "Authorization" in runway_page.ADD_JS
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    c = TestClient(web.app)
    assert c.post("/api/add-leads", json={"campaign": 2174, "leads": 500}).status_code == 401
    assert c.post("/api/add-leads", json={"campaign": 2174, "leads": 500}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/api/add-leads", json={"campaign": 2174, "leads": 100}, headers={"Authorization": "Bearer s3cret"}).status_code == 400
    assert c.post("/api/add-leads", json={"campaign": 2174, "leads": 5000}, headers={"Authorization": "Bearer s3cret"}).status_code == 400
    monkeypatch.setattr(materials_fill, "add_leads", lambda cid, n, user=None: {"ok": True, "campaign": cid, "was": 2103, "target": 2603, "added": n, "note": "ok"})
    r = c.post("/api/add-leads", json={"campaign": 2174, "leads": 500}, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json()["ok"] and "2,603" in r.json()["message"]
    monkeypatch.delenv("ADMIN_TOKEN")
    assert c.post("/api/add-leads", json={"campaign": 2174, "leads": 500}, headers={"Authorization": "Bearer "}).status_code == 401       # no token set: closed


def test_angle_campaigns_use_the_scale_path_when_leads_are_added(monkeypatch):
    from app import materials_fill as mf, testgrid
    monkeypatch.setattr(mf, "overview", lambda: [{"campaign": 2107, "kind": "angle", "angle": "A10", "name": "A10", "loaded": 2373, "target": 2973}])
    monkeypatch.setattr(mf.db, "log", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(testgrid, "auto_expand_champion", lambda angle, run_id=None, target_leads=None, requires_approval=False: seen.update(angle=angle, target=target_leads) or {"target_set": True})
    r = mf.add_leads(2107, 1000, "web page")
    assert r["ok"] and seen == {"angle": "A10", "target": 3973} and "copy-test angle" in r["note"]       # 2,973 goal not yet reached, plus 1,000


def test_a_close_race_keeps_both_angles_running_and_day_21_asks_once(monkeypatch):
    from datetime import datetime, timezone
    from app import db, nightly, testgrid
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []
    monkeypatch.setattr(db, "cursor", lambda: Cur())
    cfg = _cfg()
    cfg["rules"].update(min_positive_to_decide=3, min_positive_margin=2)
    launched, now = datetime(2026, 10, 16, tzinfo=timezone.utc), datetime(2026, 11, 20, tzinfo=timezone.utc)
    rows = {"A5": {"leads": 1500, "sent": 3500, "replied": 20, "interested": 0, "booked": 0, "positive": 0},
            "A7": {"leads": 1500, "sent": 3500, "replied": 30, "interested": 3, "booked": 0, "positive": 3},
            "A10": {"leads": 2300, "sent": 2400, "replied": 44, "interested": 4, "booked": 0, "positive": 4}}
    v = testgrid.decide(rows, launched, cfg, now)
    assert v["status"] == "monitoring" and "a lead of 2" in v["reason"] and "nobody is paused" in v["reason"]
    rows["A10"].update(interested=5, positive=5)
    assert testgrid.decide(rows, launched, cfg, now)["status"] == "decided"                # 5 against 3 is a lead of 2
    posted = []
    monkeypatch.setattr(nightly.slack, "post", lambda text, **k: posted.append(text))
    monkeypatch.setattr(testgrid, "load_config", lambda path=None: dict(cfg, rules=dict(cfg["rules"], reminder_day=21)))
    class Cur2(Cur):
        def fetchone(self):
            return {"first": datetime(2026, 9, 1, tzinfo=timezone.utc)}        # long enough ago that day 21 has passed on any real clock
    monkeypatch.setattr(db, "cursor", lambda: Cur2())
    monkeypatch.setattr(nightly, "_once_ever", lambda name: True)
    assert nightly.day_reminder({"status": "monitoring", "by_angle": rows, "reason": "close"}) is True and "still no winner" in posted[0]
    monkeypatch.setattr(nightly, "_once_ever", lambda name: False)
    assert nightly.day_reminder({"status": "monitoring", "by_angle": rows}) is False
    assert nightly.day_reminder({"status": "decided"}) is False


def test_contact_search_waits_while_verification_is_down_and_resumes_when_it_is_back(monkeypatch):
    from app import sourcing_health, testrun
    said = []
    monkeypatch.setattr(testrun, "_say", lambda t: said.append(t))
    testrun._verifier_state["alerted"] = False
    state = {"ok": False}
    monkeypatch.setattr(sourcing_health, "verifier_ok", lambda max_age=300.0: (state["ok"], "MillionVerifier answers error (credits 0)"))
    assert testrun._verifier_blocked() is True and testrun._verifier_blocked() is True
    assert len(said) == 1 and "verification is down" in said[0] and "resumes by itself" in said[0]       # one alert, not one per check
    state["ok"] = True
    assert testrun._verifier_blocked() is False and "working again" in said[1]
    assert testrun._verifier_blocked() is False and len(said) == 2


def test_verifier_check_reads_the_millionverifier_error_answer(monkeypatch):
    import httpx
    from app import sourcing_health as sh
    class R:
        def __init__(self, d): self.d = d
        def json(self): return self.d
    monkeypatch.setenv("MILLIONVERIFIER_API_KEY", "k")
    sh._verifier_cache.update(at=0.0, ok=True, text="")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R({"result": "error", "resultcode": 4, "credits": 0}))
    assert sh.verifier_ok(0)[0] is False
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R({"result": "invalid", "credits": 9000}))
    assert sh.verifier_ok(0) == (True, "MillionVerifier ok (credits 9000)")
    monkeypatch.delenv("MILLIONVERIFIER_API_KEY")
    assert sh.verifier_ok(0)[0] is False


def test_verifier_check_tests_the_buzzlead_gateway_when_the_build_verifies_through_it(monkeypatch):
    from app import sourcing_health as sh
    from app.clients import gateway_verify
    monkeypatch.setattr(gateway_verify, "enabled", lambda: True)
    sh._verifier_cache.update(at=0.0, ok=True, text="")
    monkeypatch.setattr(gateway_verify, "verify_many", lambda emails, providers=None: {"info@microsoft.com": {"status": "unknown", "steps": ["verify_mv_quality: info@microsoft.com: quality=bad"]}})
    ok, text = sh.verifier_ok(0)
    assert ok and "BuzzLead gateway" in text
    sh._verifier_cache.update(at=0.0, ok=True, text="")
    monkeypatch.setattr(gateway_verify, "verify_many", lambda emails, providers=None: {"info@microsoft.com": {"status": "unknown", "steps": ["verify_mv_quality: info@microsoft.com: quality="]}})
    ok, text = sh.verifier_ok(0)
    assert not ok and "empty quality" in text


def test_the_job_has_work_while_a_fill_campaign_or_an_angle_is_short_of_its_goal(monkeypatch):
    from app import materials_fill, testrun, wave1_fill
    monkeypatch.delenv("SOURCING_ONLY", raising=False)
    monkeypatch.setattr(materials_fill, "enabled", lambda: True)
    state = {"all_done": True, "angle_left": 0}
    monkeypatch.setattr(materials_fill, "all_done", lambda: state["all_done"])
    monkeypatch.setattr(testrun, "_angle_left", lambda: state["angle_left"])
    assert testrun.work_left() is False                         # 2174 is at its goal and the angles are at theirs
    state["angle_left"] = 1600
    assert testrun.work_left() is True                          # A7 or A10 was topped up
    state.update(angle_left=0, all_done=False)
    assert testrun.work_left() is True                          # a fill campaign was topped up


def test_angle_left_reads_the_scale_targets_and_respects_the_hold(monkeypatch):
    from app import testrun, wave1_fill
    monkeypatch.setattr(wave1_fill, "_held", lambda: False)
    monkeypatch.setattr(wave1_fill, "_setup", lambda: ({}, {"A7": "a7", "A10": "a10", "A5": "a5"}, {"A7": 1, "A10": 2, "A5": 3}, {"A7": 3500, "A10": 3500, "A5": 1500}))
    monkeypatch.setattr(wave1_fill, "counts", lambda campaign_of: {"A7": 1914, "A10": 2373, "A5": 1547})
    assert testrun._angle_left() == (3500 - 1914) + (3500 - 2373)         # A5 is past its goal and adds nothing
    monkeypatch.setattr(wave1_fill, "_held", lambda: True)
    assert testrun._angle_left() == 0


def test_the_slow_scale_tick_can_be_switched_off(monkeypatch):
    import inspect
    from app import runner
    assert "DISABLE_SCALE_TICK" in inspect.getsource(runner._tick_copy_test_scaling)


def test_maritime_is_out_and_manufacturing_and_construction_are_priorities():
    from app import client
    p = client.profile()
    assert "Maritime and Rail" not in p.verticals and len(p.verticals) == 16
    assert set(p.priority_verticals) <= set(p.verticals) and "Construction and Infrastructure" in p.priority_verticals
    assert "General Manufacturing and Industrial Products" in p.priority_verticals and "Maritime and Rail" not in p.priority_verticals


def test_priority_verticals_are_searched_first_and_sourced_in_volume(monkeypatch):
    import inspect
    from app import scaling, testrun
    assert "priority" in inspect.getsource(scaling.pick_companies) and "SCALE_PRIORITY_BOOST" in inspect.getsource(testrun._source_round)
    calls = []
    from app import modes
    monkeypatch.setattr(modes, "run_source", lambda prof, v, n, companies_only=False: calls.append((v, n)))
    monkeypatch.setattr(testrun, "_staged", lambda: 0)
    monkeypatch.setattr(testrun, "_say", lambda t: None)
    monkeypatch.setattr(testrun, "_save", lambda st: None)
    from app import funnel
    monkeypatch.setattr(funnel, "refresh", lambda *a, **k: {})
    class Prof:
        priority_verticals = ("Construction and Infrastructure", "Machinery and Equipment Manufacturing")
        verticals = ("Construction and Infrastructure", "Machinery and Equipment Manufacturing", "Trucking and Freight Carriers")
    monkeypatch.setenv("SCALE_PRIORITY_BOOST", "4")
    testrun._source_round(Prof, {}, 150)
    assert calls == [("Construction and Infrastructure", 600), ("Machinery and Equipment Manufacturing", 600)]    # trucking is not sourced further


def test_contacts_are_only_assigned_to_angles_that_still_need_leads():
    from collections import Counter
    from app import wave1_fill
    doms = [f"company{i}.com" for i in range(3000)]
    both = Counter(wave1_fill._assign(d, ["A7", "A10"]) for d in doms)
    assert set(both) == {"A7", "A10"} and 1300 < both["A7"] < 1700           # a finished A5 takes no share: the two open angles split the stream
    only = Counter(wave1_fill._assign(d, ["A7"]) for d in doms)
    assert only == {"A7": 3000}                                              # the last open angle gets all of it, not a third
    assert wave1_fill._assign("steelco.com", ["A7", "A10"]) == wave1_fill._assign("steelco.com", ["A7", "A10"])     # stable: one company never splits
    import inspect
    src = inspect.getsource(wave1_fill.fill)
    assert "open_list = [a for a in angles if have_now[a] < targets[a]]" in src and "_assign(dom, open_list)" in src


def test_gateway_verify_uses_small_batches_and_re_asks_people_the_gateway_skipped(monkeypatch):
    from app.clients import gateway_verify as gv
    asked = []

    def fake(people):
        asked.append(len(people))
        # the first call of any size silently skips every other person; the re-ask answers all
        if len(asked) == 1:
            return [{"valid_email": p["email"]} if i % 2 == 0 else {} for i, p in enumerate(people)]
        return [{"valid_email": p["email"]} for p in people]

    monkeypatch.setattr(gv, "_find_chunk", fake)
    people = [{"first_name": "a", "last_name": str(i), "domain": "x.com", "email": f"a{i}@x.com"} for i in range(40)]
    out = gv.find_and_verify(people)
    assert gv.BATCH == 16 and max(asked[:3]) <= 16
    assert all(r for r in out) and [r["valid_email"] for r in out] == [p["email"] for p in people]


def test_company_cleaner_cap_is_three_by_default_and_two_for_the_quick_run_campaigns():
    from app import materials
    from app.company_name_cleaner import clean_company_name
    assert clean_company_name("Oklahoma Tank Lines United Petroleum Transports") == "Oklahoma Tank Lines"
    assert len(clean_company_name("Oklahoma Tank Lines United Petroleum Transports", 2).split()) == 2
    assert len(clean_company_name("Acme Building Supply Company, LLC", 2).split()) <= 2
    assert clean_company_name("Steel Dynamics, Inc.", 2) == "Steel Dynamics"
    assert materials.check_company("Oklahoma Tank Lines") == []
    assert materials.check_company("Oklahoma Tank Lines", 2) == ["3 words (max 2)"]


def test_list_enrich_has_no_fit_gate_only_checks_the_merge_fields_render():
    from app import list_enrich as L
    ok, _, merge = L.judge("HG", dict(city="Tulsa, OK", shipped_product="Steel Joists", plant_count="4"))
    assert ok and merge == {"CITY": "Tulsa", "SHIPPED_PRODUCT": "steel joists", "PLANT_COUNT": "4"}
    assert L.judge("HG", dict(city="Tulsa", shipped_product="steel joists", plant_count=1))[2]["PLANT_COUNT"] == "a couple of"   # variant 3: "with a couple of plants"
    assert L.judge("HG", dict(city="Tulsa", shipped_product="steel joists"))[2]["PLANT_COUNT"] == "a couple of"
    assert not L.judge("HG", dict(city=None, shipped_product="steel joists", plant_count=3))[0]
    assert L.judge("BM", dict(city="Boise", product_mix="lumber and trusses"))[0]
    assert not L.judge("BM", dict(city="Boise", product_mix="lumber"))[0]
    assert L.judge("CN", dict(city="Dallas"))[0] and L.judge("BR", dict(city="Reno"))[0]
    assert not L.judge("CN", {})[0]


def test_lead_payload_carries_list_specific_merge_fields():
    from app.clients import emailbison
    lead = emailbison._lead_for_upsert({"email": "a@b.com", "first_name": "A", "company_name": "Acme", "extra_fields": {"CITY": "Tulsa"}, "company_max_words": 2})
    assert {"name": "city", "value": "Tulsa"} in lead["custom_variables"]


def test_gateway_verify_a_failed_batch_costs_only_that_batch(monkeypatch):
    from app.clients import gateway_verify as gv
    from app.clients.http import VendorError
    monkeypatch.setattr(gv, "RETRY_PAUSE", 0)
    calls = {"n": 0}

    def fake(people):
        calls["n"] += 1
        if len(people) > gv.RETRY_BATCH:        # the big batch times out every time, the halves work
            raise VendorError("buzzlead-verify", 504, "upstream request timeout")
        return [{"valid_email": p["email"]} for p in people]

    monkeypatch.setattr(gv, "_find_chunk", fake)
    people = [{"first_name": "a", "last_name": str(i), "domain": "x.com", "email": f"a{i}@x.com"} for i in range(40)]
    out = gv.find_and_verify(people)
    assert len(out) == 40 and all(r for r in out)


def test_two_word_company_names_do_not_collapse_to_initials():
    from app.company_name_cleaner import clean_company_name as c
    assert c("B & B Molded Products Inc", 2) == "B&B Molded"
    assert c("A H Beck Foundation Co Inc", 2) == "AH Beck"
    assert c("J & L WOOD PRODUCTS, INC.", 2) == "J&L Wood"
    assert c("J. Gibson McIlvain Company", 2) == "Gibson McIlvain"
    assert c("Nickolas M. Savko & Sons, Inc.", 2) == "Nickolas Savko"
    assert c("Z Contractors Inc.", 2) == "Z Contractors"
    assert c("21st Century Plastics Inc", 2) == "21st Century"
    assert c("Steel Dynamics, Inc.") == "Steel Dynamics"             # the 3-word path is not changed by the 2-word rule


def test_gateway_verify_a_504_during_the_re_ask_does_not_lose_the_call(monkeypatch):
    from app.clients import gateway_verify as gv
    from app.clients.http import VendorError
    monkeypatch.setattr(gv, "RETRY_PAUSE", 0)
    n = {"calls": 0}

    def fake(people):
        n["calls"] += 1
        if n["calls"] == 1:
            return [{} for _ in people]                       # the gateway skips everyone in the first pass
        raise VendorError("buzzlead-verify", 504, "upstream request timeout")   # and times out on the re-ask

    monkeypatch.setattr(gv, "_find_chunk", fake)
    people = [{"first_name": "a", "last_name": str(i), "domain": "x.com", "email": f"a{i}@x.com"} for i in range(10)]
    out = gv.find_and_verify(people)
    assert len(out) == 10 and not any(out)


def test_quick_run_lists_job_stops_workers_when_the_held_pile_projects_past_the_goal(monkeypatch):
    import threading
    from app import list_scale as ls
    monkeypatch.setattr(ls, "n_loaded", lambda code: 900)
    monkeypatch.setattr(ls, "n_held", lambda code: 150)
    assert ls._source_worker("BM", 1000, threading.Event()) == "enough"          # 900 + 150 * 0.85 is past 1,000
    monkeypatch.setattr(ls, "n_held", lambda code: 0)
    monkeypatch.setattr(ls, "_verifier_wait", lambda: None)
    monkeypatch.setattr(ls, "companies_left", lambda code: 0)
    monkeypatch.setattr(ls, "_top_up", lambda code: False)
    assert ls._source_worker("BM", 1000, threading.Event()) == "exhausted"


def test_quick_run_lists_work_left_ignores_stopped_and_finished_lists(monkeypatch):
    from app import list_scale as ls
    monkeypatch.setattr(ls, "targets", lambda: {"BM": {"target": 1000, "stuck_reason": None}, "HG": {"target": 1000, "stuck_reason": "out of companies"}})
    monkeypatch.setattr(ls, "n_loaded", lambda code: {"BM": 1000, "HG": 300}[code])
    assert ls.work_left() is False                                               # BM is at its goal, HG is stopped
    monkeypatch.setattr(ls, "n_loaded", lambda code: 500)
    assert ls.work_left() is True


def test_gateway_calls_share_one_concurrency_cap():
    from app.clients import gateway_verify as gv
    assert gv._GATE._value >= 1


def test_broad_angle_takes_other_verticals_only_after_warehousing_runs_out(monkeypatch):
    from app import list_scale as ls
    own = ["Logistics, Warehousing and 3PL", "Trucking and Freight Carriers", "Plastics Manufacturing"]
    monkeypatch.setattr(ls, "_left_in", lambda verticals: 500)
    assert ls.verticals_for("BR") == own
    monkeypatch.setattr(ls, "_left_in", lambda verticals: 0)
    br = ls.verticals_for("BR")
    assert br[:3] == own and "Wholesale and Distribution" in br
    assert not {"Machinery and Equipment Manufacturing", "Building Materials and Products", "Construction and Infrastructure"} & set(br)
    assert ls.verticals_for("CN") == ["Construction and Infrastructure"]          # Heavy goods and Construction never fall back
    bm = ls.verticals_for("BM")
    assert bm[0] == "Building Materials and Products" and set(bm[1:]) == {"Wholesale and Distribution", "Other Industrial Shippers"}


def test_quick_run_lists_job_starts_a_list_whose_stop_note_is_cleared_while_it_runs(monkeypatch):
    import threading
    from app import list_scale as ls
    state = {"calls": 0, "started": [], "done": set()}
    br_started = threading.Event()
    monkeypatch.setattr(ls, "ensure_tables", lambda: None)
    monkeypatch.setattr(ls, "_lock", lambda h: True)
    monkeypatch.setattr(ls, "_beat", lambda h: None)
    monkeypatch.setattr(ls, "_release", lambda h: None)
    monkeypatch.setattr(ls, "_say", lambda t: None)
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)
    monkeypatch.setattr(ls, "n_loaded", lambda c: 0)

    def fake_targets():
        state["calls"] += 1
        out = {}
        for c in ls.LISTS:
            stuck = "finished" if c in state["done"] else None
            if c == "BR" and state["calls"] < 3 and c not in state["done"]:
                stuck = "out of companies"                       # BR is stopped at first, its note is cleared on the third check
            if c in ("BM", "CN"):
                stuck = "finished"
            out[c] = {"target": 1000, "stuck_reason": stuck}
        return out

    def fake_loop(c, tg, h):
        state["started"].append(c)
        if c == "HG":
            br_started.wait(5)                                   # HG keeps the job alive until BR has been started
        else:
            br_started.set()
        state["done"].add(c)

    monkeypatch.setattr(ls, "targets", fake_targets)
    monkeypatch.setattr(ls, "_list_loop", fake_loop)
    assert ls.run()["status"] == "finished"
    assert sorted(state["started"]) == ["BR", "HG"]


def test_a_yield_collapse_pauses_only_the_list_that_tripped():
    import time
    from app import list_scale as ls
    assert set(ls._yield_pause) == set(ls.LISTS)
    ls._yield_pause["BM"]["t"] = time.time() + 600
    assert ls._yield_pause["CN"]["t"] < time.time()                # Construction is not held back by Building materials
    ls._yield_pause["BM"]["t"] = 0.0


def test_quickenrich_company_search_follows_the_cursor_past_10000_and_counts_only_new(monkeypatch):
    from app.clients import quickenrich as q
    calls = []

    def fake(vendor, method, url, headers=None, json_body=None, **kw):
        calls.append(json_body)
        n = len(calls)
        if n > 3:
            return {"data": [], "meta": {"has_more": False}}
        assert ("page" in json_body) == (n == 1) and ("cursor" in json_body) == (n > 1)     # page 1, then the cursor, never an offset page
        rows = [{"url": f"c{n}-{i}.com", "company_name": "x"} for i in range(100)]
        return {"data": rows, "meta": {"next_cursor": f"cur{n}", "has_more": True}}

    monkeypatch.setattr(q, "request", fake)
    monkeypatch.setattr(q, "map_industries", lambda inds: ["Construction"])
    monkeypatch.setattr(q, "_headers", lambda: {})
    monkeypatch.setattr(q, "map_employee_range", lambda a, b: ["100 - 249"])
    stored = {f"c1-{i}.com" for i in range(100)} | {f"c2-{i}.com" for i in range(50)}
    out = q.find_companies(industries=["Construction"], employee_min=100, limit=120, exclude=stored)
    assert len(out) == 120 and not {c["domain"] for c in out} & stored


def test_connections_of_finished_threads_are_closed(monkeypatch):
    import threading
    from app import db

    class FakeConn:
        closed = False

        def close(self):
            self.closed = True

    made = []

    def fake_connect(*a, **k):
        c = FakeConn(); made.append(c); return c

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    import types
    monkeypatch.setattr(db, "settings", types.SimpleNamespace(database_url="postgres://x"))
    t = threading.Thread(target=db.connect); t.start(); t.join()
    assert made and not made[0].closed
    db._local.conn = None
    db.connect()                                   # opening a new one sweeps the finished thread's connection
    assert made[0].closed


def test_pool_threads_hand_their_connection_back_after_each_use(monkeypatch):
    import types
    from concurrent.futures import ThreadPoolExecutor
    from app import db

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeConn:
        closed = False
        def cursor(self): return FakeCur()
        def close(self): self.closed = True

    made = []
    monkeypatch.setattr(db.psycopg, "connect", lambda *a, **k: made.append(FakeConn()) or made[-1])
    monkeypatch.setattr(db, "settings", types.SimpleNamespace(database_url="postgres://x"))

    def use():
        with db.cursor():
            pass
        return made[-1].closed

    with ThreadPoolExecutor(2) as ex:
        assert all(ex.map(lambda _: use(), range(4)))            # every pool use ends with its connection closed


def test_nested_cursor_on_a_pool_thread_keeps_the_outer_connection_open(monkeypatch):
    # list_scale.tag_bank calls verticals_for inside its own cursor; the inner cursor closed the shared connection and BM and BR
    # crashed silently every 30 seconds (2026-10-28)
    import types
    from concurrent.futures import ThreadPoolExecutor
    from app import db

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeConn:
        closed = False
        def cursor(self): return FakeCur()
        def close(self): self.closed = True

    made = []
    monkeypatch.setattr(db.psycopg, "connect", lambda *a, **k: made.append(FakeConn()) or made[-1])
    monkeypatch.setattr(db, "settings", types.SimpleNamespace(database_url="postgres://x"))

    def use():
        with db.cursor():
            with db.cursor():
                pass
            outer_open = not made[-1].closed
        return outer_open, made[-1].closed

    with ThreadPoolExecutor(1) as ex:
        outer_open, closed_after = ex.submit(use).result()
    assert outer_open and closed_after


def test_broad_angle_sources_logistics_trucking_and_plastics_in_turn(monkeypatch):
    # Emmanuel 2026-10-28: "trucking, logistics, plastics". New companies for each of the three, once each, while its own verticals are low
    import types
    from app import list_scale as ls, client, funnel, modes
    asked = []
    monkeypatch.setattr(ls, "_topped", {c: 0 for c in ls.LISTS})
    monkeypatch.setattr(ls, "_left_in", lambda verticals: 10)
    monkeypatch.setattr(ls, "_say", lambda text: None)
    monkeypatch.setattr(client, "profile", lambda: None)
    monkeypatch.setattr(modes, "run_source", lambda profile, v, ask, companies_only: asked.append(v))
    monkeypatch.setattr(funnel, "refresh", lambda force=False: None)

    class Cur:
        def execute(self, *a): pass
        def fetchone(self): return {"n": 0}

    class Ctx:
        def __enter__(self): return Cur()
        def __exit__(self, *a): return False
    monkeypatch.setattr(ls.db, "cursor", lambda: Ctx())
    for _ in range(5):
        ls._top_up("BR")
    assert asked == ["Logistics, Warehousing and 3PL", "Trucking and Freight Carriers", "Plastics Manufacturing"]


def test_plastics_is_declared_for_sourcing_but_not_a_main_vertical():
    import tomllib, pathlib
    cfg = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / "clients" / "tfp" / "client.toml").read_text(encoding="utf-8"))
    defs = {d["name"]: d for d in cfg["vertical_defs"]}
    assert defs["Plastics Manufacturing"]["industries"] == ["Plastics Manufacturing"]
    assert "Plastics Manufacturing" not in cfg["verticals"]
