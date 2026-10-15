"""This build serves Lumion only and refuses to start without CLIENT, so tests
declare one before anything imports the app."""
import os

os.environ.setdefault("CLIENT", "tfp")


# tests/test_pipeline.py was inherited from the IDPR/ProductEVO build. The tests listed in
# inherited_client_specific.txt exercise THOSE clients' data (their titles, prompts, scale
# bars, Slack channels) or the generic title ladder that a client with declared titles no
# longer uses. This build deliberately carries none of that data, so they are skipped, not
# deleted: the list is the to-do for re-writing the review, pooling and model-campaign ones
# against Lumion.
import pathlib

import pytest

_NA = set((pathlib.Path(__file__).parent / "inherited_client_specific.txt").read_text().split())


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="inherited IDPR/ProductEVO test, depends on their client data")
    for item in items:
        if item.name in _NA:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _generic_validator_tests_ignore_proq_minimums(request, monkeypatch):
    """The inherited validator tests use short sample emails. Lumion's 380/600 character minimums are tested
    in test_lumion.py, which does not apply this."""
    if request.module.__name__.rsplit(".", 1)[-1].startswith("test_lumion"):
        return
    from app import copy_rules, validator
    # The inherited generic tests write IDPR-style sample emails; Lumion's own bans are tested in test_lumion.py.
    monkeypatch.setattr(copy_rules, "current", lambda: copy_rules.Rules())
    monkeypatch.setattr(validator, "_min_chars", lambda which: None)
    monkeypatch.setattr(validator, "required_paragraphs", lambda: None)   # the two-paragraph rule is tested in test_lumion.py
    # The inherited intake and top-up tests exercise the generic engine, so they run with the automatic intake ON.
    monkeypatch.setenv("AUTO_INTAKE", "true")
