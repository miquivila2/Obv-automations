"""Persist an approved plan draft into the CRM's `public.project_plan_drafts`.

That table is the CRM's own draft/approval pattern (payload jsonb + warnings +
approved_by/approved_at) — we reuse it as-is rather than inventing a parallel one.
Only inserts new agent-authored rows; never updates or deletes existing drafts.

ASSUMPTION TO VERIFY (docs §9): `status` has no documented value set for this CRM
table. We use 'draft' as a status is required to run our approve/review workflow;
confirm against real data before relying on it in production, and adjust here if
the CRM already uses different status strings.

NOTE: `approved_by`/`approved_at` are deliberately left unset. Those represent a
HUMAN approving in the CRM UI — our Judge's approval is a separate, internal gate
(recorded in agent.artifact_feedback), not the same thing, and setting them here
would make the Lovable UI show a draft as human-approved when it isn't.
"""
from __future__ import annotations


def load_latest_plan(project_id: str) -> dict | None:
    """Most recent plan draft for a project (no version column on this CRM table —
    ordered by created_at). Used by follow-up mode to load-and-diff."""
    from app.db.client import get_supabase

    rows = (
        get_supabase()
        .table("project_plan_drafts")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def persist_plan(*, project_id: str, brief: str, payload: dict, model_id: str, intake_id: str | None) -> dict:
    """Insert a new agent-authored plan draft. Returns the created row (with id,
    needed by Gantt's source_draft_id)."""
    from app.db.client import get_supabase

    row = (
        get_supabase()
        .table("project_plan_drafts")
        .insert(
            {
                "project_id": project_id,
                "status": "draft",  # see ASSUMPTION TO VERIFY above
                "brief": brief,
                "payload": payload,
                "warnings": [],
                "pipeline_meta": {
                    "agent": "planner",
                    "model_id": model_id,
                    "source_intake_id": intake_id,
                    "source": "agent",
                },
            }
        )
        .execute()
        .data[0]
    )
    return row
