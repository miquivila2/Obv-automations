"""Factory for LangChain chat models backed by AWS Bedrock.

Every node builds its model through `chat_model_for(agent)` instead of
constructing ChatBedrockConverse directly — that's what keeps app/config.py's
MODEL_REGISTRY the single place a model id lives.

Open verification item (see docs/ARCHITECTURE.md "Open questions"): the seven
model ids in the registry were added to Bedrock via Project Mantle in
Feb 2026. Confirm `langchain-aws` speaks to them correctly through the
Converse API, and that all seven are enabled in AWS_REGION, before relying on
this in anything but a dev environment:

    aws bedrock list-foundation-models --region $AWS_REGION \
        --query "modelSummaries[].modelId"
"""
from __future__ import annotations

from functools import lru_cache

from langchain_aws import ChatBedrockConverse

from app.config import get_settings, model_id_for


@lru_cache
def chat_model_for(agent: str, *, temperature: float = 0.2) -> ChatBedrockConverse:
    """Return a cached chat model instance for the given agent key
    (must match a key in app.config.MODEL_REGISTRY)."""
    settings = get_settings()
    return ChatBedrockConverse(
        model=model_id_for(agent),
        region_name=settings.aws_region,
        temperature=temperature,
    )
