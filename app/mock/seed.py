"""Default fixtures for the mock CRM, so the system is testable the moment it starts.

Deliberately NOT all happy-path. The point of this harness is to surface
parsing/validation/edge-case failures before production, so the seed set
includes meetings that SHOULD be hard:

  * one clean onboarding (the happy path),
  * a follow-up naming a specific artifact,
  * an update meeting on a project with a linked GitHub repo,
  * a final_qa meeting whose notes ask for something beyond the agreed plan
    (Agent 8 should flag a scope switch),
  * an English-language meeting (currency should differ from the Spanish ones),
  * a meeting whose attendees match NO project (classification should fall
    through to pending_review rather than guessing),
  * a meeting with an empty transcript (a real parsing edge case).

All names/companies here are invented. Real client data never lands in this
repo (see .gitignore) — seed the real gold examples into the database directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_PROJECT_ALPHA = "11111111-1111-4111-8111-111111111111"
_PROJECT_BETA = "22222222-2222-4222-8222-222222222222"
_CLIENT_ALPHA = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
_CLIENT_BETA = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def default_projects() -> list[dict]:
    return [
        {
            "id": _PROJECT_ALPHA,
            "name": "Northwind Inventory Portal",
            "status": "active",
            "hourly_rate": 85.0,
            "currency": "MXN",
            "preferred_currency": None,
            "client_id": _CLIENT_ALPHA,
        },
        {
            "id": _PROJECT_BETA,
            "name": "Halcyon Analytics Dashboard",
            "status": "active",
            "hourly_rate": 120.0,
            "currency": "USD",
            "preferred_currency": "USD",
            "client_id": _CLIENT_BETA,
        },
    ]


def default_clients() -> list[dict]:
    return [
        {"id": _CLIENT_ALPHA, "name": "Northwind", "company": "Northwind Supplies SA"},
        {"id": _CLIENT_BETA, "name": "Halcyon", "company": "Halcyon Data Ltd"},
    ]


def default_matchers() -> list[dict]:
    """Deterministic project matching (docs §4.1) — no LLM call needed when these hit."""
    return [
        {"id": "m1", "project_id": _PROJECT_ALPHA, "kind": "email", "value": "ops@northwind.example"},
        {"id": "m2", "project_id": _PROJECT_ALPHA, "kind": "alias", "value": "Northwind"},
        {"id": "m3", "project_id": _PROJECT_BETA, "kind": "email", "value": "cto@halcyon.example"},
        {"id": "m4", "project_id": _PROJECT_BETA, "kind": "alias", "value": "Halcyon"},
    ]


def default_repos() -> list[dict]:
    """Only Beta has a repo — an update meeting on Alpha should therefore fail
    loudly with a clear 'no repo configured' error, which is itself worth testing."""
    return [
        {"id": "r1", "project_id": _PROJECT_BETA, "owner": "halcyon-data", "repo": "analytics-dashboard"},
    ]


def default_events() -> list[dict]:
    """Fake CRM meetings. `end_at` is set in the past so the calendar timer
    treats them as due; `transcript` is our own extra field (the real CRM has
    no such column) that the mock pipeline runner reads instead of calling Plaud."""
    return [
        {
            "id": "e1000000-0000-4000-8000-000000000001",
            "title": "Northwind — kickoff / onboarding",
            "description": "First scoping call for the inventory portal.",
            "organizer": "ops@northwind.example",
            "attendee_ids": ["ops@northwind.example", "mvila@eada.net"],
            "location": "Google Meet",
            "status": "completed",
            "start_at": _hours_ago(3),
            "end_at": _hours_ago(2),
            "project_id": _PROJECT_ALPHA,
            "language": "es",
            "transcript": (
                "Reunión de arranque con Northwind. Necesitan un portal de inventario para "
                "tres almacenes. Requieren login por roles: almacenista, supervisor y "
                "administrador. El almacenista solo registra entradas y salidas; el supervisor "
                "aprueba ajustes de stock y ve reportes; el administrador gestiona usuarios y "
                "catálogos. Piden alertas cuando un artículo baja del mínimo, y un reporte "
                "mensual exportable a Excel. Trabajan con lectores de código de barras USB "
                "existentes. Quieren que esté alojado en la nube y accesible desde tablets."
            ),
        },
        {
            "id": "e1000000-0000-4000-8000-000000000002",
            "title": "Northwind — revisión del presupuesto",
            "description": "Follow-up: el cliente pide ajustar el presupuesto.",
            "organizer": "ops@northwind.example",
            "attendee_ids": ["ops@northwind.example", "mvila@eada.net"],
            "location": "Google Meet",
            "status": "completed",
            "start_at": _hours_ago(2.5),
            "end_at": _hours_ago(1.8),
            "project_id": _PROJECT_ALPHA,
            "language": "es",
            "transcript": (
                "Seguimiento sobre el presupuesto del portal de inventario. El cliente considera "
                "que las horas de QA están altas y quiere reducir el alcance de las pruebas "
                "automatizadas a los flujos críticos únicamente. También pide separar el módulo "
                "de reportes como una fase posterior para bajar el costo inicial."
            ),
        },
        {
            "id": "e1000000-0000-4000-8000-000000000003",
            "title": "Halcyon — sprint progress update",
            "description": "Update meeting on a live build.",
            "organizer": "cto@halcyon.example",
            "attendee_ids": ["cto@halcyon.example", "mvila@eada.net"],
            "location": "Zoom",
            "status": "completed",
            "start_at": _hours_ago(5),
            "end_at": _hours_ago(4),
            "project_id": _PROJECT_BETA,
            "language": "en",
            "transcript": (
                "Progress check on the analytics dashboard. The ingestion service is done and "
                "deployed. The charting layer is about half finished — the team is blocked on "
                "the aggregation query performance. They want the timeline re-planned to account "
                "for two extra weeks on the data layer, and the reporting milestone pushed back."
            ),
        },
        {
            "id": "e1000000-0000-4000-8000-000000000004",
            "title": "Halcyon — final QA / acceptance",
            "description": "Acceptance stage. Contains a scope switch on purpose.",
            "organizer": "cto@halcyon.example",
            "attendee_ids": ["cto@halcyon.example", "mvila@eada.net"],
            "location": "Zoom",
            "status": "completed",
            "start_at": _hours_ago(1.5),
            "end_at": _hours_ago(1),
            "project_id": _PROJECT_BETA,
            "language": "en",
            "transcript": (
                "Acceptance review for the analytics dashboard. The dashboards look good and the "
                "team signs off on the agreed scope. Separately, the client now also wants a "
                "native mobile application for iOS and Android with offline sync, plus a public "
                "API for their partners. Neither was part of the original engagement, but they'd "
                "like it included before launch."
            ),
        },
        {
            "id": "e1000000-0000-4000-8000-000000000005",
            "title": "Unknown prospect — intro call",
            "description": "Edge case: attendees match no known project.",
            "organizer": "hello@unknown-prospect.example",
            "attendee_ids": ["hello@unknown-prospect.example"],
            "location": "Phone",
            "status": "completed",
            "start_at": _hours_ago(6),
            "end_at": _hours_ago(5.5),
            "project_id": None,
            "language": "en",
            "transcript": (
                "Intro call with a prospect we have no project for yet. They described wanting "
                "some kind of scheduling tool but nothing is decided. No scope agreed."
            ),
        },
        {
            "id": "e1000000-0000-4000-8000-000000000006",
            "title": "Northwind — empty recording (edge case)",
            "description": "Edge case: the transcript is empty.",
            "organizer": "ops@northwind.example",
            "attendee_ids": ["ops@northwind.example"],
            "location": "Google Meet",
            "status": "completed",
            "start_at": _hours_ago(8),
            "end_at": _hours_ago(7.5),
            "project_id": _PROJECT_ALPHA,
            "language": "es",
            "transcript": "",
        },
    ]


def seed_store(store) -> None:
    """Populate an empty store with the default fixture set."""
    store.replace_table("public", "projects", default_projects())
    store.replace_table("public", "clients", default_clients())
    store.replace_table("public", "events", default_events())
    store.replace_table("agent", "project_matchers", default_matchers())
    store.replace_table("agent", "project_repos", default_repos())
