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
    assert bad("You can get the checklist.\nWant it?")   # spam-trigger word get
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
