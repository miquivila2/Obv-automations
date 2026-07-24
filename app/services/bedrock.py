"""Model-provider entry point: `chat_model_for(agent)`.

Every node and service builds its model through `chat_model_for(agent)` instead
of constructing a client directly — that's what keeps app/config.py's
MODEL_REGISTRY the single place a model id lives, and lets us swap the whole
provider with one env var.

Dispatch (app.config.Settings.model_provider):
  * "stub"    -> app.services.stub_models.StubChatModel (no deps, canned outputs)
  * "ollama"  -> langchain_ollama.ChatOllama (real models, running locally, free)
  * "bedrock" -> langchain_aws.ChatBedrockConverse (real, pay-per-token)

Open verification item (see docs/ARCHITECTURE.md "Open questions"): the seven
model ids in the registry were added to Bedrock via Project Mantle in Feb 2026.
Before flipping MODEL_PROVIDER=bedrock in anything but dev, confirm all seven are
enabled in AWS_REGION and that langchain-aws speaks to them via the Converse API:

    aws bedrock list-foundation-models --region $AWS_REGION \
        --query "modelSummaries[].modelId"
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import get_settings, model_id_for


@lru_cache
def chat_model_for(agent: str, *, temperature: float = 0.2) -> Any:
    """Return a cached chat model for the given agent key (must match a key in
    app.config.MODEL_REGISTRY). Type is intentionally `Any`: the stub and the
    real Bedrock client share only the slice of interface our nodes use."""
    settings = get_settings()

    if settings.model_provider == "stub":
        from app.services.stub_models import StubChatModel

        return StubChatModel(agent)

    if settings.model_provider == "ollama":
        # One local model for every agent (settings.ollama_model). The production
        # model_id is intentionally ignored here: the frontier models in
        # MODEL_REGISTRY don't fit on a local machine — local dev uses a smaller
        # proxy to exercise the pipeline with real calls.
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=temperature,
        )

    # provider == "bedrock"
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(
        model=model_id_for(agent),
        region_name=settings.aws_region,
        temperature=temperature,
    )
