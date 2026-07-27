"""app/services/plaud_client.py talks to Plaud's own MCP server (@plaud-ai/mcp)
as a local subprocess. Nothing here spawns a real subprocess or touches a
browser — mcp.client.stdio.stdio_client and mcp.ClientSession are faked so the
tool-call plumbing and the matching logic are both exercised for real.
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.services.plaud_client import (
    _select_unambiguous_match,
    fetch_transcript,
    find_recording_id,
)


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeCallToolResult:
    def __init__(self, content, isError: bool = False) -> None:
        self.content = content
        self.isError = isError


class _FakeSession:
    """Stands in for mcp.ClientSession. `tool_responses` maps a tool name to
    either a fixed list[_FakeContent] or a callable(arguments) -> that list."""

    def __init__(self, tool_responses: dict) -> None:
        self._tool_responses = tool_responses
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, name: str, arguments: dict) -> _FakeCallToolResult:
        self.calls.append((name, arguments))
        response = self._tool_responses[name]
        content = response(arguments) if callable(response) else response
        return _FakeCallToolResult(content)


def _install_fake_mcp(monkeypatch, tool_responses: dict) -> _FakeSession:
    """Patches the two names plaud_client imports lazily inside _call_tool,
    so no real subprocess is ever spawned."""
    session = _FakeSession(tool_responses)

    @asynccontextmanager
    async def _fake_stdio_client(_params):
        yield (None, None)

    monkeypatch.setattr("mcp.client.stdio.stdio_client", _fake_stdio_client)
    monkeypatch.setattr("mcp.ClientSession", lambda *_a, **_kw: session)
    return session


def _files_response(ids: list[str]):
    payload = {"source_list": [{"id": i} for i in ids]}
    return [_FakeContent(json.dumps(payload))]


# ---------------------------------------------------------------------------
# _select_unambiguous_match — the pure matching rule, same shape as
# classification's deterministic project match: exactly one -> use it, else
# never guess.
# ---------------------------------------------------------------------------
def test_exactly_one_match_is_selected():
    assert _select_unambiguous_match([{"id": "rec-1"}], "e1") == "rec-1"


def test_zero_matches_raises_with_the_event_id():
    with pytest.raises(ValueError, match="No Plaud recording found.*e1"):
        _select_unambiguous_match([], "e1")


def test_multiple_matches_raises_rather_than_guessing():
    with pytest.raises(ValueError, match="ambiguous"):
        _select_unambiguous_match([{"id": "rec-1"}, {"id": "rec-2"}], "e1")


# ---------------------------------------------------------------------------
# find_recording_id — calls list_files and applies the matching rule
# ---------------------------------------------------------------------------
async def test_find_recording_id_resolves_a_single_match(monkeypatch):
    _install_fake_mcp(monkeypatch, {"list_files": _files_response(["rec-1"])})

    result = await find_recording_id(
        event_id="e1",
        start_at=datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
    )
    assert result == "rec-1"


async def test_find_recording_id_pads_the_search_window(monkeypatch):
    session = _install_fake_mcp(monkeypatch, {"list_files": _files_response(["rec-1"])})

    await find_recording_id(
        event_id="e1",
        start_at=datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
    )

    _, args = session.calls[0]
    # Padded by 15 min on both sides, but the padding never crosses the day
    # boundary here — just confirm the query spans the event's own date.
    assert args["date_from"] <= "2026-07-24" <= args["date_to"]


async def test_find_recording_id_raises_on_zero_matches(monkeypatch):
    _install_fake_mcp(monkeypatch, {"list_files": _files_response([])})

    with pytest.raises(ValueError, match="No Plaud recording found"):
        await find_recording_id(
            event_id="e1",
            start_at=datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
        )


async def test_find_recording_id_raises_on_multiple_matches(monkeypatch):
    _install_fake_mcp(monkeypatch, {"list_files": _files_response(["rec-1", "rec-2"])})

    with pytest.raises(ValueError, match="ambiguous"):
        await find_recording_id(
            event_id="e1",
            start_at=datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# fetch_transcript
# ---------------------------------------------------------------------------
async def test_fetch_transcript_requires_a_resolved_recording_id():
    with pytest.raises(ValueError, match="no plaud_note_id"):
        await fetch_transcript(event_id="e1", plaud_note_id=None)


async def test_fetch_transcript_returns_the_transcript_text(monkeypatch):
    session = _install_fake_mcp(
        monkeypatch, {"get_transcript": [_FakeContent("Alice: hello.\nBob: hi there.")]}
    )

    result = await fetch_transcript(event_id="e1", plaud_note_id="rec-1")

    assert result == "Alice: hello.\nBob: hi there."
    assert session.calls == [("get_transcript", {"id": "rec-1"})]


async def test_a_tool_error_result_raises(monkeypatch):
    @asynccontextmanager
    async def _fake_stdio_client(_params):
        yield (None, None)

    class _ErrorSession(_FakeSession):
        async def call_tool(self, name, arguments):
            return _FakeCallToolResult(content=["boom"], isError=True)

    monkeypatch.setattr("mcp.client.stdio.stdio_client", _fake_stdio_client)
    monkeypatch.setattr("mcp.ClientSession", lambda *_a, **_kw: _ErrorSession({}))

    with pytest.raises(RuntimeError, match="returned an error"):
        await fetch_transcript(event_id="e1", plaud_note_id="rec-1")
