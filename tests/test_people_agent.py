from app.clients import people_agent as pa


def test_parse_people_tolerates_fence_and_extra_words():
    txt = 'Here you go:\n```json\n[{"Full Name": "Jane Smith", "First Name": "Jane", "Last Name": "Smith", "Job Title": "CEO", "Domain": "x.com"}]\n```'
    out = pa.parse_people(txt)
    assert out and out[0]["first_name"] == "Jane" and out[0]["title"] == "CEO"


def test_parse_people_empty_and_garbage():
    assert pa.parse_people("[]") == []
    assert pa.parse_people("no one found") == []


def test_title_ok_matches_targets_as_whole_words():
    assert pa.title_ok("Owner - President")
    assert pa.title_ok("Co-Owner")
    assert pa.title_ok("Founder & CEO")
    assert not pa.title_ok("Instructor")
    assert not pa.title_ok("Ownership Coordinator")


def test_email_candidates_first_name_only_first():
    c = pa.email_candidates("Katie", "Lukach", "x.edu")
    assert c[0] == "katie@x.edu" and "katie.lukach@x.edu" in c and len(c) == len(set(c))


def test_resolve_email_stops_on_catch_all(monkeypatch):
    monkeypatch.setattr(pa, "_mv", lambda e: "catch_all")
    assert pa.resolve_email("A", "B", "x.com")[0] is None
