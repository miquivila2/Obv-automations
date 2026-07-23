"""Agent 3 — Planner.

Reads the notes + the wireframe, produces the needs list (software / hardware /
cloud) and a build plan ordered toward the scope, grouped into logical
milestones. Uses DeepSeek V3.2 — one of the two agents deliberately given a
stronger model, because a bad plan cascades into a bad Gantt and a bad budget.

Inputs: meeting notes + latest wireframe.
Modes: create | follow-up (load latest plan first).
"""
from __future__ import annotations

from app.graph.state import BuildState
from app.services.artifacts import load_latest
from app.services.bedrock import chat_model_for


async def build_plan(state: BuildState) -> BuildState:
    model = chat_model_for("planner")

    wireframe = load_latest(state["project_id"], "wireframe")
    wireframe_ctx = f"\n\nWireframe:\n{wireframe['content']}" if wireframe else ""

    prior = ""
    if state.get("mode") == "follow_up":
        latest = load_latest(state["project_id"], "plan")
        if latest:
            prior = f"\n\nCurrent plan to revise (change ONLY what the notes ask):\n{latest['content']}"

    system = (
        "You are Oblivion's planning agent. From the notes and wireframe, produce:\n"
        "1) a needs list split into software, hardware, and cloud;\n"
        "2) a build plan ordered by how to construct toward the scope (dependencies first), "
        "grouped into logical sections/milestones.\n"
        "Return structured JSON with 'needs' and 'milestones' keys."
    )
    human = f"Meeting notes:\n{state['notes']}{wireframe_ctx}{prior}"

    response = await model.ainvoke([("system", system), ("human", human)])
    return {**state, "current_artifact_type": "plan", "draft": {"raw": response.content}}
