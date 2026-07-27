"""Plaud integration via the official Plaud MCP server (`@plaud-ai/mcp`), not
the Developer Platform REST API originally scoped in docs §9.7. Plaud ships an
MCP server (docs.plaud.ai/plaud-mcp-cli/mcp) that any Plaud user can install —
no separate developer approval process — which is a faster path than waiting
on PLAUD_CLIENT_ID/PLAUD_API_KEY access.

HOW THIS TALKS TO PLAUD: this repo runs the MCP server as a local subprocess
(stdio transport — the only one it documents) via `npx -y @plaud-ai/mcp@latest`,
and calls its tools directly with the official `mcp` Python client SDK. No LLM
is in this loop — it's a plain, deterministic tool call (list_files /
get_transcript), same spirit as calling any other API client.

ASSUMPTION TO VERIFY — the one genuine unknown here: the server's auth is an
interactive browser OAuth flow, caching a token at ~/.plaud/tokens-mcp.json.
We assume ONE interactive login, done once by hand on the machine that runs
this backend (`npx -y @plaud-ai/mcp@latest install`), leaves a token that
every later, non-interactive subprocess call can reuse. Plaud's docs neither
confirm nor deny that reuse. If it's wrong (token expired, server re-prompts
for a browser), the symptom would be this call hanging waiting for a browser
that never opens in a headless deployment — _CALL_TIMEOUT_SECONDS turns that
into a clear timeout instead of a silent hang (fail loud, docs §10).

THE MATCHING PROBLEM (new — not anticipated by the original spec): a Plaud
recording carries no reference to a CRM `public.events` row; nothing links
"this calendar event" to "this Plaud recording" directly. find_recording_id
resolves it by TIME WINDOW OVERLAP against list_files' date filters, the only
correlation Plaud's tool surface exposes. Same rule as classification's
deterministic project match (docs §4.1): exactly one candidate in the window
-> use it; zero or more than one is ambiguous, and this never guesses — it
raises rather than silently attaching the wrong transcript to the wrong
meeting.

Requires Node.js >= 20 and the ability to run `npx` on whatever machine ends
up hosting this backend (still undecided — docs §3.5).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

_NPX_COMMAND = "npx"
_NPX_ARGS = ["-y", "@plaud-ai/mcp@latest"]
# Generous for local subprocess/npx startup, but must not hang forever if the
# OAuth-reuse assumption above turns out wrong.
_CALL_TIMEOUT_SECONDS = 20.0
# Recordings rarely start exactly on the calendar event's start time (Plaud is
# started/stopped by hand) — pad the search window on both sides.
_MATCH_PADDING_MINUTES = 15


async def _call_tool(tool_name: str, arguments: dict) -> list:
    """Spawn the Plaud MCP server and call one tool. Assumes it is already
    authenticated (see module docstring) — this code never drives the OAuth
    flow itself."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=_NPX_COMMAND, args=_NPX_ARGS)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=_CALL_TIMEOUT_SECONDS)
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments), timeout=_CALL_TIMEOUT_SECONDS
            )
            if result.isError:
                raise RuntimeError(f"Plaud MCP tool {tool_name!r} returned an error: {result.content}")
            return result.content


def _select_unambiguous_match(recordings: list[dict], event_id: str) -> str:
    """Pure: the deterministic half of find_recording_id (see its docstring for
    the matching rule). Exactly one candidate -> its id; zero or many -> raise,
    never guess."""
    if len(recordings) == 1:
        return recordings[0]["id"]
    if not recordings:
        raise ValueError(
            f"No Plaud recording found in the event window for event {event_id!r} — "
            "nothing to match. Check the recording was made and has synced to Plaud."
        )
    ids = [r.get("id") for r in recordings]
    raise ValueError(
        f"{len(recordings)} Plaud recordings overlap event {event_id!r}'s window "
        f"({ids}) — ambiguous, refusing to guess which one is the right transcript."
    )


async def find_recording_id(*, event_id: str, start_at: datetime, end_at: datetime) -> str:
    """Resolve the one Plaud recording that overlaps [start_at, end_at]
    (padded — see _MATCH_PADDING_MINUTES). Raises if zero or more than one
    candidate matches (see _select_unambiguous_match)."""
    padding = timedelta(minutes=_MATCH_PADDING_MINUTES)
    date_from = (start_at - padding).date().isoformat()
    date_to = (end_at + padding).date().isoformat()

    content = await _call_tool("list_files", {"date_from": date_from, "date_to": date_to})
    import json

    files = json.loads(content[0].text) if content else []
    recordings = files.get("source_list", files) if isinstance(files, dict) else files
    return _select_unambiguous_match(recordings, event_id)


async def fetch_transcript(*, event_id: str, plaud_note_id: str | None) -> str:
    """Fetch the meeting transcript for one calendar event from Plaud.

    `plaud_note_id` (a Plaud recording id) must already be resolved — either
    supplied directly (the manual webhook path, app/main.py's
    CalendarTimerPayload) or found via find_recording_id first (the automatic
    calendar-timer path, app/services/calendar_timer.py:process_due_event).
    This function does not do that resolution itself: it fetches one specific
    recording, by id, full stop.
    """
    if plaud_note_id is None:
        raise ValueError(
            f"fetch_transcript: no plaud_note_id for event {event_id!r} — "
            "resolve one via find_recording_id first."
        )

    content = await _call_tool("get_transcript", {"id": plaud_note_id})
    # get_transcript returns one text content block holding the transcript
    # (timestamps + speaker labels) — see docs.plaud.ai/plaud-mcp-cli/mcp.
    return content[0].text if content else ""
