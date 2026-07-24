"""Assembles the LangGraph build graph and wires the loop mechanics.

Structure of the graph:

    orchestrator ──(update mode)──▶ progress_summary ──┐
         │                                             │
         └──────────────(other modes)─────────────────┤
                                                       ▼
                          ┌──────────── entry_agent picks the start ──────────┐
                          ▼                                                    │
    wireframe ─▶ judge ─▶ (approve? next : reject&<2? wireframe : human_review)
       plan ──▶ judge ─▶ ...
      gantt ──▶ judge ─▶ ...
     budget ─▶ judge ─▶ (approve? END : ...)

The Judge loop (max 2 rounds, then needs_human_review) lives entirely in the
conditional edges here — the builder and judge nodes stay ignorant of it, so
all four builders reuse the exact same loop wiring. That's the shared-helper
principle from docs §6.1 expressed as graph edges instead of a copied while-loop.

Persistence (writing the approved artifact via app.services.artifacts) and the
budget's in-code pricing happen in the per-artifact persist step, kept thin here.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graph.nodes import budget, gantt, orchestrator, planner, wireframe
from app.graph.nodes.judge import judge
from app.graph.state import ArtifactType, BuildState

# The linear build chain: each artifact's successor. Budget is terminal.
_NEXT_IN_CHAIN: dict[ArtifactType, ArtifactType | None] = {
    "wireframe": "plan",
    "plan": "gantt",
    "gantt": "budget",
    "budget": None,
}

_BUILDER_NODES = {
    "wireframe": wireframe.build_wireframe,
    "plan": planner.build_plan,
    "gantt": gantt.build_gantt,
    "budget": budget.build_budget,
}


def _route_from_orchestrator(state: BuildState) -> str:
    """After routing, update mode goes through progress summary first; then
    everything jumps to the entry agent chosen by the Orchestrator."""
    if state["mode"] == "update":
        return "progress_summary"
    return state["entry_agent"]


def _after_judge(state: BuildState) -> str:
    """The Judge loop decision, in one place:
      - approve            -> persist the approved artifact
      - reject, round < 2  -> back to the same builder to revise
      - reject, round == 2 -> stop, flag for human review
    """
    settings = get_settings()

    if state["judge_verdict"] == "approve":
        return "persist"

    if state["judge_round"] < settings.judge_max_rounds:
        return state["current_artifact_type"]  # revise: re-enter the same builder node

    return "human_review"  # never approved within the cap


async def persist(state: BuildState) -> BuildState:
    """Write the just-approved artifact to its store. Per-artifact persistence
    lives in the services layer; this node just dispatches on the artifact type.
    Only Budget is implemented today — the other builders are still stubs."""
    artifact_type = state["current_artifact_type"]

    if artifact_type == "budget":
        from app.services.budget_persist import persist_budget

        persist_budget(project_id=state["project_id"], draft=state["draft"])
    else:
        raise NotImplementedError(
            f"persistence for {artifact_type!r} is not implemented yet (Session 5)."
        )
    return state


def _after_persist(state: BuildState) -> str:
    """After persisting, continue down the linear chain, or END if terminal."""
    nxt = _NEXT_IN_CHAIN[state["current_artifact_type"]]
    return nxt if nxt is not None else END


def _human_review(state: BuildState) -> BuildState:
    """Terminal marker: the artifact never passed the Judge. Flagged, not
    silently accepted (ADR, docs §8). The graph interrupts here so a human
    resolves it; the checkpointer holds the state durably meanwhile."""
    return {**state, "needs_human_review": True}


def build_graph(checkpointer):
    g = StateGraph(BuildState)

    # Orchestrator + update-mode progress summary
    g.add_node("orchestrator", orchestrator.route)
    g.add_node("progress_summary", orchestrator.summarize_progress)

    # Builders + shared Judge + human-review terminal
    for name, fn in _BUILDER_NODES.items():
        g.add_node(name, fn)
    g.add_node("judge", judge)
    g.add_node("persist", persist)
    g.add_node("human_review", _human_review)

    g.add_edge(START, "orchestrator")

    # From orchestrator: either to progress summary (update) or straight to entry agent.
    g.add_conditional_edges(
        "orchestrator",
        _route_from_orchestrator,
        {"progress_summary": "progress_summary", **{t: t for t in _BUILDER_NODES}},
    )
    # After the progress summary, jump to the entry agent (Gantt in update mode).
    g.add_conditional_edges(
        "progress_summary", lambda s: s["entry_agent"], {t: t for t in _BUILDER_NODES}
    )

    # Every builder hands its draft to the shared Judge.
    for name in _BUILDER_NODES:
        g.add_edge(name, "judge")

    # The Judge routes back to a builder (revise), to persist (approved), or to
    # human review (never approved within the cap).
    g.add_conditional_edges(
        "judge",
        _after_judge,
        {**{t: t for t in _BUILDER_NODES}, "persist": "persist", "human_review": "human_review"},
    )
    # After persisting, continue down the chain to the next builder, or END.
    g.add_conditional_edges(
        "persist", _after_persist, {**{t: t for t in _BUILDER_NODES}, END: END}
    )
    g.add_edge("human_review", END)

    # interrupt_before human_review lets a human step in before the run "ends"
    # on an unapproved artifact.
    return g.compile(checkpointer=checkpointer, interrupt_before=["human_review"])
