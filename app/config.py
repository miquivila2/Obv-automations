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
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Data source ---
    # "supabase" -> the real database (production CRM + our agent schema).
    # "mock"     -> a local JSON file, driven by the test UI in app/mock/.
    #               Lets the whole pipeline be stress-tested without touching
    #               the live CRM. The swap happens inside app/db/client.py, so
    #               no agent or persistence service knows which one it got.
    data_source: Literal["supabase", "mock"] = "supabase"
    mock_data_path: str = "mock_data/crm.json"

    # --- Supabase ---
    # Required only when data_source == "supabase"; the mock path never reads
    # them, so a mock-mode run works with these left at their defaults.
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_db_uri: str = ""  # direct Postgres connection string, used only by the LangGraph checkpointer

    # --- Model provider ---
    # "stub"    -> no deps; chat_model_for returns canned outputs for fast unit tests.
    # "ollama"  -> real open-weight models running LOCALLY (no AWS, no cost, fully private).
    #              Uses smaller local proxies of the production models — see ollama_model.
    # "bedrock" -> real AWS Bedrock (requires AWS creds + model access enabled in aws_region).
    # The switch is one env var; nodes never know which provider they got.
    model_provider: Literal["stub", "ollama", "bedrock"] = "stub"

    # --- Ollama (only used when model_provider == "ollama") ---
    ollama_base_url: str = "http://localhost:11434"
    # One local model for every agent by default: local dev exercises the pipeline
    # with REAL model calls, it doesn't need to match production model quality.
    # Override per project/machine. Must be pulled first: `ollama pull <name>`.
    ollama_model: str = "qwen3:8b"

    # --- AWS Bedrock (only used when model_provider == "bedrock") ---
    aws_region: str = "us-east-1"  # verify all 7 model ids below are actually enabled in this region
                                     # before relying on it — see docs/ARCHITECTURE.md "Open questions"

    # --- Inbound webhook auth ---
    # Shared secret required in the X-Webhook-Secret header on every trigger
    # endpoint (see app/main.py). Set it to the same value in the Supabase
    # Database Webhook's headers and in the calendar-timer scheduler's curl.
    # None disables the check — acceptable ONLY for local dev; the app refuses
    # to start without it when the host is reachable from anywhere else.
    webhook_secret: str | None = None

    # --- GitHub (Agent 7 update mode, §9.3) ---
    # Read-only PAT (repo:read / public_repo scope is enough) for
    # app/services/github_progress.py. Optional: unauthenticated requests work
    # for public repos, just at a much lower rate limit.
    github_token: str | None = None

    # --- Classification thresholds ---
    classification_confidence_threshold: float = 0.70  # below this, a meeting goes to pending_review

    # Comma-separated email domains that belong to US, not to a client. Attendees
    # at these domains are never learned as project matchers: our own people
    # attend every client's meetings, so their addresses carry no signal about
    # which project a meeting belongs to — and learning one silently breaks
    # deterministic matching for every project afterwards (see
    # classification._learn_email_matchers).
    internal_email_domains: str = "oblivionlabs.com,eada.net"

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
    "qa": AgentModel(
        model_id="deepseek.v3.2",
        role="Agent 8 — Final QA scope-switch detector. Same model as Planner "
             "(deliberately, not the cheap-and-mechanical tier): missing a real "
             "scope switch here means it ships unnoticed, and a false positive "
             "just costs a human a look at a flagged finding — asymmetric risk "
             "that justifies the stronger model.",
    ),
}


def model_id_for(agent: str) -> str:
    """Look up the Bedrock model id for an agent. Raises KeyError on typos —
    fail loudly rather than silently falling back to a default model."""
    return MODEL_REGISTRY[agent].model_id
