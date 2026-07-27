"""Agent 6 — Judge (shared reviewer).

One reviewer for all four artifact types, built once and called by every
builder (Lean: the review loop is not re-implemented in each agent). The
Judge compares an artifact against the source notes and returns either
APPROVE or structured, actionable feedback.

Two design points from the architecture review baked in here:

  * PER-ARTIFACT RUBRICS. "Evaluate a wireframe" and "evaluate a budget" are
    different jobs. Same runner, different criteria — see _RUBRICS. A generic
    "compare and approve" prompt would blur domains that shouldn't be blurred.

  * DON'T OVER-FEEDBACK. If the draft is already good, approve it cleanly in
    one round instead of inventing changes. This is stated explicitly in the
    system prompt because it's the failure mode a critical model falls into.

Also reads the gold-standard example library (docs §9.4) for the artifact type
under review — GOLD ONLY, never merely-recent examples, so the bar can't drift
downward as the system's own output accumulates. See app/services/
example_library.py.

The loop cap (2 rounds) and the "never approved -> needs_human_review" outcome
live in the graph edges (app/graph/build.py), not here. This node just
produces one verdict for one draft.
"""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.graph.state import BuildState
from app.services.bedrock import chat_model_for

# Per-artifact review criteria. Same Judge model, different lens.
_RUBRICS: dict[str, str] = {
    "wireframe": (
        "Evaluate STRUCTURE ONLY, not visual aesthetics (the render is low-fidelity by design). "
        "Does the wireframe JSON contain every screen the notes imply? Are the conditional flows "
        "(different user roles seeing different screens) complete? Are navigation links coherent?"
    ),
    "plan": (
        "Does the plan cover the full scope in the notes and wireframe? Are software/hardware/cloud "
        "needs all listed? Is the build order logical (dependencies before dependents)? Are milestones "
        "coherent groupings, not arbitrary splits?"
    ),
    "gantt": (
        "Do the milestones and tasks faithfully reflect the plan? Is the monthly breakdown realistic? "
        "Does every plan item map to at least one task? No invented scope beyond the plan."
    ),
    "budget": (
        "Do the line items match the Gantt tasks? Is each hours estimate justified? Is the currency "
        "correct for the meeting language (USD/EN, MXN/ES)? Do NOT recompute totals — arithmetic is "
        "validated in code; judge the justification and completeness of the lines."
    ),
}


class JudgeVerdict(BaseModel):
    verdict: Literal["approve", "reject"]
    feedback: str = Field(
        ...,
        description="Empty or a brief 'looks good' when approving; specific, actionable items when rejecting.",
    )

    @classmethod
    def stub(cls) -> "JudgeVerdict":
        """Canned output for MODEL_PROVIDER=stub: approve, so a stubbed run flows
        cleanly through the whole chain end-to-end. The reject/revise/
        needs_human_review path is exercised by unit tests that construct verdicts
        directly (tests/test_judge_loop.py), not by the stub."""
        return cls(verdict="approve", feedback="[stub] auto-approved")


async def judge(state: BuildState) -> BuildState:
    """Review the current draft against the notes using the artifact's rubric."""
    from app.config import model_id_for
    from app.db.client import get_supabase
    from app.services.example_library import format_examples_block, load_gold_examples

    artifact_type = state["current_artifact_type"]
    rubric = _RUBRICS[artifact_type]
    incoming_round = state.get("judge_round", 0)

    # A fresh artifact review (incoming_round == 0, reset by persist() when the
    # chain advances to the next artifact type) gets a new ref id; a revise round
    # (reject, round < cap) reuses the one already in state so all rounds for the
    # same artifact group under one id.
    draft_ref_id = state.get("draft_ref_id") if incoming_round > 0 else str(uuid.uuid4())

    model = chat_model_for("judge").with_structured_output(JudgeVerdict)
    system = (
        "You are Oblivion's quality reviewer for auto-generated project artifacts. "
        "Review the draft against the source notes using the criteria below, and return "
        "either 'approve' or 'reject' with feedback.\n\n"
        "RULE — do not over-feedback: if the draft is already good, approve it cleanly. "
        "Do not invent changes to look thorough. Only reject for real, actionable problems.\n\n"
        f"Criteria for a {artifact_type}:\n{rubric}"
    )
    # GOLD ONLY here (see example_library's module docstring): the Judge measures
    # a draft against the standard, so a merely-recent example must never become
    # the bar it judges by.
    standard = format_examples_block(
        load_gold_examples(artifact_type),
        heading="Oblivion's gold-standard reference for this artifact type — judge against this bar:",
    )
    human = f"Source notes:\n{state['notes']}\n\nDraft ({artifact_type}):\n{state['draft']}{standard}"

    result: JudgeVerdict = await model.ainvoke([("system", system), ("human", human)])
    new_round = incoming_round + 1

    get_supabase().schema("agent").table("artifact_feedback").insert(
        {
            "artifact_type": artifact_type,
            "artifact_ref": draft_ref_id,
            "round": new_round,
            "verdict": result.verdict,
            "feedback_text": result.feedback,
            "judge_model_id": model_id_for("judge"),
        }
    ).execute()

    return {
        **state,
        "judge_round": new_round,
        "judge_verdict": result.verdict,
        "judge_feedback": result.feedback,
        "draft_ref_id": draft_ref_id,
    }
