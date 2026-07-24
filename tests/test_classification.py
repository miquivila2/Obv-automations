"""The deterministic project-matching step runs before any LLM call, so it's
testable in isolation. We test the matching logic directly.

Projects carry name + client name/company (read from the CRM); aliases and known
emails come from a separate `agent.project_matchers` list.
"""
from app.services.classification import ClassificationResult, _deterministic_match

_PROJECTS = [
    {"id": "p-dasp", "name": "DASP", "client_name": "Farmacia DASP", "client_company": "DASP SA"},
    {"id": "p-equilibrio", "name": "Equilibrio", "client_name": "Equilibrio", "client_company": None},
]

_MATCHERS = [
    {"project_id": "p-dasp", "kind": "alias", "value": "la farmacia"},
    {"project_id": "p-dasp", "kind": "email", "value": "ops@dasp.mx"},
    {"project_id": "p-equilibrio", "kind": "email", "value": "it@equilibrio.mx"},
]


def test_match_by_attendee_email():
    matches = _deterministic_match(["ops@dasp.mx"], "meeting notes", _PROJECTS, _MATCHERS)
    assert len(matches) == 1 and matches[0]["id"] == "p-dasp"


def test_match_by_client_name_in_notes():
    # The client name "Farmacia DASP" appears in the notes (no matcher needed).
    matches = _deterministic_match([], "Today we spoke with Farmacia DASP about the order", _PROJECTS, _MATCHERS)
    assert len(matches) == 1 and matches[0]["id"] == "p-dasp"


def test_match_by_alias_matcher():
    # The custom alias "la farmacia" resolves to DASP.
    matches = _deterministic_match([], "call with la farmacia about pricing", _PROJECTS, _MATCHERS)
    assert len(matches) == 1 and matches[0]["id"] == "p-dasp"


def test_no_match_returns_empty():
    matches = _deterministic_match(["unknown@somewhere.com"], "new client with no alias", _PROJECTS, _MATCHERS)
    assert matches == []


def test_ambiguous_match_returns_multiple():
    # Both projects referenced -> ambiguous -> falls through to the LLM (len != 1).
    matches = _deterministic_match([], "we compared Equilibrio and DASP", _PROJECTS, _MATCHERS)
    assert len(matches) == 2


def test_stub_classification_goes_to_review():
    # The stub output must be low-confidence so stubbed runs never auto-assign.
    result = ClassificationResult.stub()
    assert result.confidence == 0.0 and result.project_id is None
