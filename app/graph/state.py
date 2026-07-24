"""The state that flows through the LangGraph build graph.

One BuildState object is threaded through every node (Orchestrator, the four
builders, the Judge). Nodes read what they need and write back only their
piece — low coupling: a node never mutates another node's fields.

Kept deliberately flat and typed. The graph's *durability* (resume-after-
failure, interrupt-for-human) comes from the Postgres checkpointer, not from
anything clever in this structure.
"""
from __future__ import annotations

from typing import Literal, Optional, TypedDict

Mode = Literal["create", "follow_up", "update"]
ArtifactType = Literal["wireframe", "plan", "gantt", "budget"]


class BuildState(TypedDict, total=False):
    # --- Set by the caller / Agent 1, read by the Orchestrator ---
    project_id: str
    intake_id: str  # agent.meeting_intake row that kicked off this run
    meeting_class: Literal["onboarding", "follow_up", "update", "final_qa"]
    sub_type: Optional[ArtifactType]  # which artifact a follow_up targets
    language: Literal["es", "en"]  # drives Budget currency
    notes: str  # the raw transcript, source of truth for every builder

    # --- Set by the Orchestrator, read by the builders ---
    entry_agent: ArtifactType  # where the chain starts (2/3/4/5 -> its artifact type)
    mode: Mode
    progress_summary: Optional[str]  # update mode only: real build state vs. plan

    # --- The re-trigger loop (§7): set when a human edit kicks off a resume ---
    edited_artifact_type: Optional[ArtifactType]

    # --- Judge loop bookkeeping, per artifact currently in flight ---
    current_artifact_type: Optional[ArtifactType]
    draft: Optional[dict]  # the builder's current draft, awaiting or post Judge
    judge_round: int  # 0, 1, or 2
    judge_verdict: Optional[Literal["approve", "reject"]]
    judge_feedback: Optional[str]

    # --- Terminal signalling ---
    needs_human_review: bool  # set true when the Judge never approved within the cap
