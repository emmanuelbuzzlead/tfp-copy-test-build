"""TFP build: the spec's prompts load through the engine, the copy rules catch the spec's bans, the client profile is what Emmanuel set."""
import pytest

from app import client, copy_rules, prompts

ANGLES = ["a4-safety-page", "a5-load-type", "a7-carrier-liability", "a10-peer-proof"]


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
        assert "P1:" in pr.system and "INEOS" not in pr.system and "SKIP RULE" in pr.system
        assert "Do NOT start with a greeting" in pr.system or "Do not start with a greeting" in pr.system
    assert "{research_report}" in e1.user_template and "{email_1_output}" in e2.user_template


def bad(text):
    return copy_rules.check(text)


def test_spec_bans_are_enforced():
    assert bad("You can get the checklist.\nWant it?")   # spam-trigger word get
    assert bad("INEOS Composites uses it.\nWant the story?")
    assert bad("A fall at an Eaton plant cost $1.4M.\nWant the story?")                 # never name Eaton next to the fall
    assert bad("First line!\nSecond line!\nWant it?")                                   # one exclamation at most
    assert bad("We built it and we love it.\nOur panels fit.\nWant it?")                # we/our/us at most once
    assert bad("Open to a quick call?")                                                 # meeting ask, spam word, quick call
    assert bad("Nucor runs 4 sites: all differ.\nWant it?")                             # colon in the opening line


def test_a_clean_email_passes_the_tfp_rules():
    ok = ("Four service centers means four yards with four different dock layouts.\n\n"
          "Steel Dynamics ran into that across its flat rolled plants and ended up with one approach sized to each site.\n\n"
          "Worth sending how they rolled it out?")
    assert bad(ok) == []
