"""LangGraph's Postgres checkpointer, pointed at the same Supabase database.

This is what makes the graph resumable: if Agent 4 fails after Agents 2 and 3
already wrote successfully, a resumed run picks up from Agent 4's node instead
of re-running the whole chain. It's also what backs the interrupt() call in
the Judge node when round 2 doesn't approve (see graph/nodes/judge.py) —
the graph state is durably persisted, not held in process memory.

Uses its own `langgraph` Postgres schema (created in the 0001 migration) so
its tables never collide with the business tables Supabase's REST client
manages in `client.py`.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from app.config import get_settings


@asynccontextmanager
async def get_checkpointer():
    settings = get_settings()

    if settings.data_source == "mock":
        # Mock mode has no Postgres to point at. An in-memory saver keeps the
        # graph's cyclic Judge loop and interrupt() working exactly as they do
        # in production — what's lost is only DURABILITY across process restarts,
        # which a single test run doesn't need. Stated plainly so nobody reads a
        # clean mock run as proof that resume-after-crash works.
        from langgraph.checkpoint.memory import MemorySaver

        yield MemorySaver()
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(settings.supabase_db_uri) as saver:
        # Idempotent: only creates tables on first run.
        await saver.setup()
        yield saver
