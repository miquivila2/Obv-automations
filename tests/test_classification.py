"""The deterministic project-matching step runs before any LLM call, so it's
testable in isolation. We test the matching logic directly."""
from app.services.classification import _deterministic_match

_PROJECTS = [
    {
        "id": "p-dasp",
        "name": "DASP",
        "aliases": ["dasp", "farmacia dasp"],
        "attendee_emails": ["ops@dasp.mx"],
    },
    {
        "id": "p-equilibrio",
        "name": "Equilibrio",
        "aliases": ["equilibrio"],
        "attendee_emails": ["it@equilibrio.mx"],
    },
]


def test_match_by_attendee_email():
    matches = _deterministic_match(["ops@dasp.mx"], "notas de la reunión", _PROJECTS)
    assert len(matches) == 1 and matches[0]["id"] == "p-dasp"


def test_match_by_alias_in_notes():
    matches = _deterministic_match([], "Hoy hablamos con Farmacia DASP sobre el pedido", _PROJECTS)
    assert len(matches) == 1 and matches[0]["id"] == "p-dasp"


def test_no_match_returns_empty():
    matches = _deterministic_match(["unknown@somewhere.com"], "cliente nuevo sin alias", _PROJECTS)
    assert matches == []


def test_ambiguous_match_returns_multiple():
    # Both projects referenced -> ambiguous -> falls through to the LLM (len != 1).
    matches = _deterministic_match([], "comparamos Equilibrio y DASP", _PROJECTS)
    assert len(matches) == 2
