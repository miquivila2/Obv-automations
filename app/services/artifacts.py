"""Versioned artifact read/write — shared by all four builder nodes.

The versioning and persistence logic is identical for wireframe/plan/gantt/
budget, so it lives here once instead of being copy-pasted into four nodes
(Lean: single implementation, low coupling). Each builder only differs in how
it *generates* its draft; how the draft is stored is uniform.
"""
from __future__ import annotations

from typing import Optional

from app.db.client import get_supabase
from app.graph.state import ArtifactType


def load_latest(project_id: str, artifact_type: ArtifactType) -> Optional[dict]:
    """Return the highest-version artifact of this type for the project, or None.
    Used by follow-up/update modes to load the current version before diffing."""
    rows = (
        get_supabase()
        .table("artifacts")
        .select("*")
        .eq("project_id", project_id)
        .eq("type", artifact_type)
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _next_version(project_id: str, artifact_type: ArtifactType) -> int:
    latest = load_latest(project_id, artifact_type)
    return (latest["version"] + 1) if latest else 1


def save_artifact(
    *,
    project_id: str,
    artifact_type: ArtifactType,
    content: Optional[dict],
    model_id: str,
    triggering_meeting_id: Optional[str],
    status: str = "approved",
    file_url: Optional[str] = None,
) -> dict:
    """Write a new agent-authored version. `source` is always 'agent' here —
    only human edits (written elsewhere) carry source='human', which is what
    the re-trigger loop keys off (see docs §7)."""
    version = _next_version(project_id, artifact_type)
    row = (
        get_supabase()
        .table("artifacts")
        .insert(
            {
                "project_id": project_id,
                "type": artifact_type,
                "version": version,
                "content": content,
                "file_url": file_url,
                "editable": True,
                "source": "agent",
                "status": status,
                "triggering_meeting_id": triggering_meeting_id,
                "model_id": model_id,
            }
        )
        .execute()
        .data[0]
    )
    return row
