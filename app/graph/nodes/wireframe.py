"""Agent 2 — Wireframe.

Builds an editable wireframe (structured JSON, rendered by the CRM itself)
from the meeting notes. Uses Kimi K2.5 for its native vision — it can read a
whiteboard photo from the meeting — plus reliable structured output.

Inputs: meeting notes (+ project), a library of past wireframes as few-shot
examples, and an optional whiteboard image.

Modes: create (fresh) | follow-up (load latest, change only what the notes ask).

This node only produces a draft into state['draft']; the Judge loop and
persistence are wired in the graph (build.py). Keeping the node free of the
loop mechanics is what lets all four builders share the same edges.

Open item: the exact wireframe JSON schema (screens/components/nav) is not yet
fixed — see docs §9.1. `_WIREFRAME_SCHEMA_HINT` is the placeholder contract.
"""
from __future__ import annotations

from app.graph.state import BuildState
from app.services.artifacts import load_latest
from app.services.bedrock import chat_model_for

_WIREFRAME_SCHEMA_HINT = (
    "Return JSON: {\"screens\": [{\"name\": str, \"purpose\": str, "
    "\"components\": [str], \"visible_to_roles\": [str], \"navigates_to\": [str]}]}"
)


async def build_wireframe(state: BuildState) -> BuildState:
    model = chat_model_for("wireframe")

    prior = ""
    if state.get("mode") == "follow_up":
        latest = load_latest(state["project_id"], "wireframe")
        if latest:
            prior = f"\n\nCurrent wireframe to revise (change ONLY what the notes ask):\n{latest['content']}"

    system = (
        "You are Oblivion's wireframe agent. From the meeting notes, produce an editable, "
        "low-fidelity wireframe as structured JSON: the screens/windows implied by the scope, "
        "the components on each, which user roles see each screen, and navigation between them. "
        "If the notes lack enough detail to infer a coherent screen, do NOT invent it — omit it "
        "and note the gap.\n\n" + _WIREFRAME_SCHEMA_HINT
    )
    human = f"Meeting notes:\n{state['notes']}{prior}"

    # If a whiteboard image is available it would be attached to the human message here
    # (Kimi K2.5 vision). Left out until the ingestion path carries images.
    response = await model.ainvoke([("system", system), ("human", human)])

    return {**state, "current_artifact_type": "wireframe", "draft": {"raw": response.content}}
