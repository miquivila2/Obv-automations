"""Supabase client — the CRM data connection.

Every agent node reads/writes business data (meetings, artifacts, milestones,
etc.) through this client, authenticated with the service role key. This is
server-to-server access, not a browser session, so RLS is bypassed here on
purpose — RLS stays enabled on the tables themselves as defense in depth for
any future component that connects with the anon key.

This is a *separate* connection from the LangGraph checkpointer (see
checkpointer.py): this one talks to Supabase's PostgREST API for business
data, the checkpointer talks directly to the underlying Postgres for the
graph's execution state. Same database, two different concerns — don't merge
them.
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:  # avoid importing the heavy driver at module load
    from supabase import Client


@lru_cache
def get_supabase() -> "Client":
    # Imported lazily so modules that merely reference get_supabase (e.g. graph
    # nodes) can be imported and unit-tested without the supabase driver installed.
    from supabase import create_client

    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
