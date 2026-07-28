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
                "Ops (Northwind): Necesitamos un portal de inventario para tres almacenes.\n"
                "Miquel (Oblivion): Perfecto. ¿Cómo se organizan los roles hoy en el almacén?\n"
                "Ops (Northwind): Login por roles: almacenista, supervisor y administrador. "
                "El almacenista solo registra entradas y salidas.\n"
                "Miquel (Oblivion): ¿Y el supervisor?\n"
                "Ops (Northwind): Aprueba ajustes de stock y ve reportes. El administrador "
                "gestiona usuarios y catálogos.\n"
                "Miquel (Oblivion): ¿Alguna alerta o reporte específico?\n"
                "Ops (Northwind): Alertas cuando un artículo baja del mínimo, y un reporte "
                "mensual exportable a Excel. Ya tenemos lectores de código de barras USB.\n"
                "Miquel (Oblivion): ¿Dónde se va a alojar?\n"
                "Ops (Northwind): En la nube, accesible desde tablets."
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
                "Ops (Northwind): Queríamos hablar del presupuesto del portal.\n"
                "Miquel (Oblivion): Claro, ¿qué te preocupa?\n"
                "Ops (Northwind): Las horas de QA están altas. Queremos reducir el alcance de "
                "las pruebas automatizadas a solo los flujos críticos.\n"
                "Miquel (Oblivion): Entendido. ¿Algo más?\n"
                "Ops (Northwind): Sí, separar el módulo de reportes como una fase posterior "
                "para bajar el costo inicial."
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
                "Miquel (Oblivion): How's the sprint going?\n"
                "CTO (Halcyon): The ingestion service is done and deployed.\n"
                "Miquel (Oblivion): And the charting layer?\n"
                "CTO (Halcyon): About half finished — we're blocked on aggregation query "
                "performance.\n"
                "Miquel (Oblivion): What do you need from us?\n"
                "CTO (Halcyon): Re-plan the timeline for two extra weeks on the data layer, "
                "and push back the reporting milestone."
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
                "Miquel (Oblivion): Let's do the final walkthrough of the dashboards.\n"
                "CTO (Halcyon): They look good, we sign off on the agreed scope.\n"
                "Miquel (Oblivion): Great, anything else before launch?\n"
                "CTO (Halcyon): Actually yes — we now also want a native mobile app for iOS "
                "and Android with offline sync, plus a public API for our partners.\n"
                "Miquel (Oblivion): That wasn't part of the original engagement.\n"
                "CTO (Halcyon): We know, but we'd like it included before launch."
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
                "Miquel (Oblivion): Tell me a bit about what you're looking for.\n"
                "Prospect: We're not sure yet — some kind of scheduling tool, maybe.\n"
                "Miquel (Oblivion): Any specifics on users or workflow?\n"
                "Prospect: Not really, nothing is decided on our end yet."
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
