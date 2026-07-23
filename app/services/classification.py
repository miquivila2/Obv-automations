"""Agent 1's core job: figure out which project a meeting belongs to, and
what kind of meeting it is.

Two-stage resolution, cheapest-first:

  1. DETERMINISTIC MATCH — no LLM call. Check the calendar invite's attendee
     emails against `projects.attendee_emails`, and the meeting title/notes
     against `projects.aliases`. If exactly one project matches, we're done:
     confidence 1.0, zero cost, zero non-determinism.

  2. LLM FALLBACK — only if step 1 finds zero or more-than-one candidate.
     GLM-4.7-Flash gets the list of active projects (name, client, aliases)
     plus the meeting title/notes excerpt, and returns a structured
     classification: which project (or "new"), the meeting class, and the
     follow-up sub-type if applicable.

A result is only auto-applied if confidence >= settings.classification_
confidence_threshold. Below that, the meeting is written with
status='pending_review' and a human resolves it — this is the review queue
the original whiteboard doc already assumed but never specified the
mechanics of.

We deliberately never auto-create a new `projects` row from this flow. A
misheard/misspelled client name creating a duplicate project is a worse
failure mode than one extra item in the human review queue.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# bedrock/supabase/settings are imported lazily inside the functions that call
# them, so the pure `_deterministic_match` can be imported and tested without the
# LLM/DB stack. Low coupling, fast tests.

MeetingClass = Literal["onboarding", "follow_up", "update", "final_qa"]
SubType = Literal["wireframe", "plan", "gantt", "budget"]


class ClassificationResult(BaseModel):
    project_id: Optional[str] = Field(
        None, description="UUID of the matched project, or null if none matched / new project."
    )
    new_project_suggested_name: Optional[str] = Field(
        None, description="If no existing project matched, a suggested name for a new one."
    )
    meeting_class: MeetingClass
    sub_type: Optional[SubType] = Field(
        None, description="Required when meeting_class is 'follow_up', otherwise null."
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., description="One-sentence justification, for the review queue UI.")


def _deterministic_match(
    attendee_emails: list[str], title_and_notes: str, active_projects: list[dict]
) -> list[dict]:
    """Return the subset of active_projects that match by attendee email or
    alias substring. Cheap, exact, and run before any LLM call."""
    text = title_and_notes.lower()
    matches = []
    for project in active_projects:
        email_hit = bool(set(attendee_emails) & set(project.get("attendee_emails", [])))
        alias_hit = any(alias.lower() in text for alias in project.get("aliases", []) + [project["name"]])
        if email_hit or alias_hit:
            matches.append(project)
    return matches


async def classify_meeting(
    *, attendee_emails: list[str], title_and_notes: str
) -> ClassificationResult:
    from app.db.client import get_supabase

    supabase = get_supabase()
    active_projects = (
        supabase.table("projects").select("id,name,client_name,aliases,attendee_emails").eq("status", "active").execute().data
    )

    candidates = _deterministic_match(attendee_emails, title_and_notes, active_projects)

    if len(candidates) == 1:
        project_result = ClassificationResult(
            project_id=candidates[0]["id"],
            meeting_class="onboarding",  # placeholder — class still needs the LLM even on a deterministic project match
            confidence=1.0,
            reasoning="Deterministic match on attendee email or project alias.",
        )
        # The project is resolved deterministically, but *meeting class* (onboarding vs.
        # follow-up vs. update vs. final_qa) still requires reading the notes — that part
        # always goes through the LLM. Re-run with the project pre-selected so the model
        # only has to decide class/sub_type, not re-derive the project.
        return await _llm_classify(title_and_notes, active_projects, forced_project=candidates[0])

    return await _llm_classify(title_and_notes, active_projects, forced_project=None)


async def _llm_classify(
    title_and_notes: str, active_projects: list[dict], forced_project: dict | None
) -> ClassificationResult:
    from app.services.bedrock import chat_model_for

    model = chat_model_for("meeting_notes").with_structured_output(ClassificationResult)

    project_list = "\n".join(
        f"- id={p['id']} name={p['name']!r} client={p['client_name']!r} aliases={p.get('aliases', [])}"
        for p in active_projects
    )
    forced_hint = (
        f"\nThe project has ALREADY been determined deterministically: id={forced_project['id']}. "
        f"Set project_id to exactly that value and focus only on meeting_class/sub_type."
        if forced_project
        else ""
    )

    system = (
        "You classify a meeting transcript for Oblivion, a custom software company. "
        "Given the list of active projects below and the meeting content, decide:\n"
        "1) which project this meeting belongs to (or that it's a brand new project),\n"
        "2) the meeting class: 'onboarding' (new project, run the full build chain), "
        "'follow_up' (revising one existing artifact — set sub_type to wireframe/plan/gantt/budget), "
        "'update' (progress check on a live build), or 'final_qa' (acceptance stage).\n"
        "If nothing matches confidently, set project_id to null and fill "
        "new_project_suggested_name instead. Never invent a project_id.\n\n"
        f"Active projects:\n{project_list}{forced_hint}"
    )

    result = await model.ainvoke([("system", system), ("human", title_and_notes)])
    return result


async def apply_classification(meeting_id: str, result: ClassificationResult) -> None:
    """Persist the classification result, routing low-confidence results to
    the human review queue instead of auto-assigning them."""
    from app.config import get_settings
    from app.db.client import get_supabase

    settings = get_settings()
    supabase = get_supabase()

    status = "classified" if result.confidence >= settings.classification_confidence_threshold else "pending_review"

    supabase.table("meetings").update(
        {
            "project_id": result.project_id,
            "class": result.meeting_class,
            "sub_type": result.sub_type,
            "classification_confidence": result.confidence,
            "classification_method": "llm",
            "status": status,
        }
    ).eq("id", meeting_id).execute()
