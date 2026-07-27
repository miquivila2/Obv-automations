"""Versioned read/write for `agent.wireframe_drafts` — OUR schema, not the CRM's.

The wireframe is the one artifact the CRM has no table for (docs §3.3), so this
lives entirely in the `agent` schema we own. Same production-safety posture as
everything else here: only inserts new agent-authored versions, never mutates
an existing row.

`approved_by`/`approved_at` are left unset on agent writes for the same reason
as plan_persist: they represent a HUMAN approval, distinct from our Judge's
internal gate.
"""
from __future__ import annotations


def load_latest_wireframe(project_id: str) -> dict | None:
    """Highest-version wireframe draft for a project, or None. Used by follow-up
    mode (and by the Planner, which reads the wireframe as context)."""
    from app.db.client import get_supabase

    rows = (
        get_supabase()
        .schema("agent")
        .table("wireframe_drafts")
        .select("*")
        .eq("project_id", project_id)
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _next_version(project_id: str) -> int:
    latest = load_latest_wireframe(project_id)
    return (latest["version"] + 1) if latest else 1


def persist_wireframe(
    *, project_id: str, payload: dict, model_id: str, intake_id: str | None, id: str | None = None
) -> dict:
    """Insert a new agent-authored wireframe version. Returns the created row.

    `id`, when given, is the Judge's draft_ref_id for this version (see
    app/graph/nodes/judge.py) — passing it through means agent.artifact_feedback
    rows for this draft point at the same id this row ends up with."""
    from app.db.client import get_supabase

    version = _next_version(project_id)
    row_data = {
        "project_id": project_id,
        "version": version,
        "status": "draft",
        "payload": payload,
        "warnings": [],
        "pipeline_meta": {"agent": "wireframe", "model_id": model_id},
        "source": "agent",
        "source_intake_id": intake_id,
    }
    if id is not None:
        row_data["id"] = id
    row = (
        get_supabase()
        .schema("agent")
        .table("wireframe_drafts")
        .insert(row_data)
        .execute()
        .data[0]
    )
    return row
