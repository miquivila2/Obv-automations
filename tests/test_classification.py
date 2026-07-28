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


def test_stub_with_a_substantive_transcript_is_confident_even_with_no_project_match():
    # Found via the mock harness: a brand-new client's onboarding meeting has
    # no existing project to deterministically match, and previously got
    # stuck at confidence=0.0 no matter how clear and detailed the notes
    # were — blocking the single most important path the system automates.
    # A long, substantive transcript must clear the review-queue threshold.
    notes = "We need a new system. " * 30  # >= 300 chars, no other class keyword
    result = ClassificationResult.stub(messages=[("system", "..."), ("human", notes)])
    assert result.meeting_class == "onboarding"
    assert result.confidence >= 0.70


def test_stub_with_a_short_ambiguous_transcript_stays_low_confidence():
    # The genuine edge case (nothing decided yet) must still route to review.
    notes = "Not sure yet, maybe some kind of tool."
    result = ClassificationResult.stub(messages=[("system", "..."), ("human", notes)])
    assert result.confidence == 0.0


def test_stub_with_an_explicit_class_keyword_is_confident_regardless_of_length():
    notes = "quick follow-up on the budget"
    result = ClassificationResult.stub(messages=[("system", "..."), ("human", notes)])
    assert result.meeting_class == "follow_up"
    assert result.sub_type == "budget"
    assert result.confidence >= 0.70
