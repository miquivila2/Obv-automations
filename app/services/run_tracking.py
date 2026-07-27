"""Observability for every agent execution (docs §11, audit cross-cutting gap):
one row per node run in `agent.runs`, so cost/latency/failure can be queried
later without log-diving. The table existed since 0001_agent_layer.sql but
nothing wrote to it — this is that write path.

Built once, wrapped around every graph node in app/graph/build.py (`_tracked`)
— Lean: don't scatter start/finish calls inside each node function.
"""
from __future__ import annotations

from datetime import datetime, timezone

_AGENT_NAMES = {
    "meeting_notes",
    "orchestrator",
    "wireframe",
    "planner",
    "gantt",
    "budget",
    "judge",
}


def start_run(
    *, project_id: str | None, intake_id: str | None, agent_name: str, trigger_source: str | None
) -> str:
    """Insert a 'running' row. Returns its id, to be passed to finish_run."""
    from app.db.client import get_supabase

    if agent_name not in _AGENT_NAMES:
        raise ValueError(f"unknown agent_name {agent_name!r} — not in agent.runs' check constraint")

    row = (
        get_supabase()
        .schema("agent")
        .table("runs")
        .insert(
            {
                "project_id": project_id,
                "intake_id": intake_id,
                "agent_name": agent_name,
                "trigger_source": trigger_source,
                "status": "running",
            }
        )
        .execute()
        .data[0]
    )
    return row["id"]


def finish_run(run_id: str, *, status: str, model_id: str | None = None, error: str | None = None) -> None:
    """Mark a run 'success' or 'failed' and stamp finished_at."""
    from app.db.client import get_supabase

    get_supabase().schema("agent").table("runs").update(
        {
            "status": status,
            "model_id": model_id,
            "error": error,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", run_id).execute()
