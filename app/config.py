"""Central configuration: environment settings and the model registry.

This module is the single source of truth for "which model runs which agent".
Every node in app/graph/nodes/ must pull its model id from MODEL_REGISTRY —
never hardcode a Bedrock model id inline. That's what keeps this file the one
place you touch when a model gets swapped, deprecated, or re-priced.

See docs/ARCHITECTURE.md ("Model registry") for the reasoning behind each pick.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Supabase ---
    supabase_url: str
    supabase_service_role_key: str
    supabase_db_uri: str  # direct Postgres connection string, used only by the LangGraph checkpointer

    # --- AWS Bedrock ---
    aws_region: str = "us-east-1"  # verify all 7 model ids below are actually enabled in this region
                                     # before relying on it — see docs/ARCHITECTURE.md "Open questions"

    # --- Classification thresholds ---
    classification_confidence_threshold: float = 0.70  # below this, a meeting goes to pending_review

    # --- Judge loop ---
    judge_max_rounds: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()


@dataclass(frozen=True)
class AgentModel:
    model_id: str
    role: str  # one-line reminder of *why* this model, so this file doubles as living documentation


# The full model registry agreed on during architecture review.
# All models are open-weight, served on-demand (pay-per-token, no idle GPU cost)
# via AWS Bedrock's Project Mantle inference layer.
MODEL_REGISTRY: dict[str, AgentModel] = {
    "meeting_notes": AgentModel(
        model_id="zai.glm-4.7-flash",
        role="Agent 1 — cheapest viable model for a 4-class classification task; "
             "low-confidence results fall through to the review queue rather than "
             "justifying a bigger model here.",
    ),
    "orchestrator_update_summary": AgentModel(
        model_id="minimax.minimax-m2.1",
        role="Agent 7, update mode only — synthesizes code_progress vs. plan/Gantt. "
             "The class->agent routing itself is deterministic code, NOT an LLM call "
             "(see app/graph/nodes/orchestrator.py) — this entry is for the one part "
             "of Agent 7 that genuinely needs reasoning.",
    ),
    "wireframe": AgentModel(
        model_id="moonshotai.kimi-k2.5",
        role="Agent 2 — needs native vision (interprets meeting whiteboard photos), "
             "structured JSON output, and reliable tool-calling to persist to Supabase. "
             "Cheaper AND better than the vision alternative (Qwen3-VL-235B) evaluated "
             "alongside it.",
    ),
    "planner": AgentModel(
        model_id="deepseek.v3.2",
        role="Agent 3 — genuine multi-step reasoning (needs list + ordered build plan "
             "across SW/HW/cloud, grouped into milestones). One of the two agents "
             "deliberately given a stronger model, since its errors cascade into "
             "Gantt and Budget.",
    ),
    "gantt": AgentModel(
        model_id="qwen.qwen3-next-80b-a3b-instruct",
        role="Agent 4 — structured transformation of an already-reasoned plan into "
             "milestones/tasks. Does not need Planner-grade reasoning, so it gets "
             "the cheap specialist instead.",
    ),
    "budget": AgentModel(
        model_id="qwen.qwen3-next-80b-a3b-instruct",
        role="Agent 5 — long-context few-shot grounding against past FLOWSIGHT/Axo "
             "budgets. IMPORTANT: hours x rate arithmetic is computed in code "
             "(see app/graph/nodes/budget.py), never trusted to the LLM.",
    ),
    "judge": AgentModel(
        model_id="moonshotai.kimi-k2-thinking",
        role="Agent 6 — shared reviewer for artifacts 2-5. Deliberately a different "
             "lab/lineage than most builders (Qwen/DeepSeek/MiniMax/GLM) so it doesn't "
             "share their blind spots. Known overlap: also Moonshot-family with Agent 2 "
             "(Wireframe) — accepted as a tradeoff because the Judge only evaluates "
             "wireframe *structure* (JSON vs. notes), never the visual render, so the "
             "shared vision lineage isn't actually exercised in that review. "
             "See docs/ARCHITECTURE.md 'Judge/Wireframe family overlap'.",
    ),
}


def model_id_for(agent: str) -> str:
    """Look up the Bedrock model id for an agent. Raises KeyError on typos —
    fail loudly rather than silently falling back to a default model."""
    return MODEL_REGISTRY[agent].model_id
