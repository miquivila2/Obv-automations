"""Stub chat model — lets the whole system run with no AWS account.

When `settings.model_provider == "stub"`, `chat_model_for` returns a
`StubChatModel` instead of a real Bedrock client. It mimics the slice of the
LangChain chat-model interface our nodes actually use:

  * `.with_structured_output(Schema).ainvoke(messages)` -> a canned `Schema`
    instance. The canned value lives ON the schema as a `stub()` classmethod,
    so each structured-output type owns its own placeholder (high cohesion) and
    this stub stays generic.
  * plain `.ainvoke(messages)` / `.invoke(messages)` -> an AIMessage with a
    clearly-marked placeholder string.

Design intent: swapping `MODEL_PROVIDER=stub` -> `bedrock` is the ONLY change
needed to go from canned to real outputs. Nodes never know which they got.
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage


class _StubStructured:
    """What `.with_structured_output(schema)` returns in stub mode."""

    def __init__(self, agent: str, schema: type) -> None:
        self._agent = agent
        self._schema = schema

    def _make(self) -> Any:
        stub_factory = getattr(self._schema, "stub", None)
        if stub_factory is None:
            raise NotImplementedError(
                f"Stub mode: {self._schema.__name__} needs a `stub()` classmethod returning a "
                f"canned instance (agent={self._agent!r}). Add one, or set MODEL_PROVIDER=bedrock."
            )
        return stub_factory()

    async def ainvoke(self, _messages: Any) -> Any:
        return self._make()

    def invoke(self, _messages: Any) -> Any:
        return self._make()


class StubChatModel:
    """Minimal stand-in for a LangChain chat model, used when there's no AWS."""

    def __init__(self, agent: str) -> None:
        self._agent = agent

    def with_structured_output(self, schema: type, **_kwargs: Any) -> _StubStructured:
        return _StubStructured(self._agent, schema)

    async def ainvoke(self, _messages: Any) -> AIMessage:
        return AIMessage(content=f"[stub:{self._agent}] placeholder response")

    def invoke(self, _messages: Any) -> AIMessage:
        return AIMessage(content=f"[stub:{self._agent}] placeholder response")
