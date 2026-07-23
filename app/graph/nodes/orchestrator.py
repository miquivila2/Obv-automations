"""Agent 7 — Orchestrator (router).

Two distinct responsibilities, deliberately kept separate:

  1. ROUTING (deterministic, no LLM). Map meeting class -> entry agent + mode.
     This is a table, not a judgment call, so it lives in plain code: testable,
     free, reproducible. See ROUTING_TABLE below and tests/test_routing.py.

  2. UPDATE-MODE PROGRESS SUMMARY (LLM). The one part of Agent 7 that genuinely
     needs reasoning: comparing real build progress against the plan. Only runs
     for class='update'. Uses minimax.minimax-m2.1 (see MODEL_REGISTRY).

The original whiteboard doc calls the Orchestrator "the only agent with routing
authority" — that authority is these functions, nothing more.
"""
from __future__ import annotations

from app.graph.state import ArtifactType, BuildState, Mode

# NOTE: bedrock/github imports are done lazily inside summarize_progress (below),
# not at module load — so the deterministic `route` can be imported and unit-tested
# without pulling in the LLM/GitHub stack. Low coupling, fast tests.

# Deterministic routing: (meeting_class) -> (entry_agent, mode).
# For follow_up, the entry agent is the artifact's own type, resolved from sub_type.
# final_qa is intentionally absent — no agent owns it yet (see docs §9).
_CLASS_TO_MODE: dict[str, Mode] = {
    "onboarding": "create",
    "follow_up": "follow_up",
    "update": "update",
}

# In create mode the chain always starts at the wireframe (Agent 2).
_CREATE_ENTRY: ArtifactType = "wireframe"

# In update mode we re-sync tasks/milestones, so the entry point is the Gantt (Agent 4).
_UPDATE_ENTRY: ArtifactType = "gantt"


def route(state: BuildState) -> BuildState:
    """Pure, synchronous routing decision. No side effects, no LLM."""
    meeting_class = state["meeting_class"]

    if meeting_class == "final_qa":
        # No owning agent yet. Surface it rather than silently mis-routing.
        raise NotImplementedError(
            "final_qa has no owning agent — see docs/ARCHITECTURE.md §9. "
            "Handle this class before routing it into the build graph."
        )

    mode = _CLASS_TO_MODE[meeting_class]

    if mode == "create":
        entry = _CREATE_ENTRY
    elif mode == "update":
        entry = _UPDATE_ENTRY
    else:  # follow_up: the artifact named by sub_type is the entry point
        entry = state["sub_type"]
        if entry is None:
            raise ValueError("follow_up meeting has no sub_type — cannot route to an owning agent.")

    return {**state, "entry_agent": entry, "mode": mode, "judge_round": 0}


async def summarize_progress(state: BuildState) -> BuildState:
    """Update mode only: read real build progress and summarize it vs. the plan,
    so the builder updates against reality, not just the notes."""
    from app.services.bedrock import chat_model_for
    from app.services.github_progress import fetch_code_progress_snapshot

    snapshot = await fetch_code_progress_snapshot(state["project_id"])  # stub until progress source decided

    model = chat_model_for("orchestrator_update_summary")
    system = (
        "You compare a software project's real build progress against its plan. "
        "Given the progress snapshot and the meeting notes, produce a concise summary "
        "of what is built vs. what was planned, and what the notes ask to change. "
        "This summary will guide the agent that re-syncs tasks and milestones."
    )
    human = f"Progress snapshot:\n{snapshot}\n\nMeeting notes:\n{state['notes']}"
    response = await model.ainvoke([("system", system), ("human", human)])

    return {**state, "progress_summary": response.content}
