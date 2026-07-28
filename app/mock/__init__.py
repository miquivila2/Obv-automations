"""Mock CRM: a file-backed stand-in for Supabase, plus a tiny web UI to drive it.

Exists so the whole agent pipeline can be stress-tested end to end WITHOUT
touching the production Lovable CRM (docs §3.4 — that database is live).

The seam is `app.db.client.get_supabase()`. Every one of the ~29 call sites in
this codebase goes through it, so swapping the real Supabase client for the
file-backed one here needs NO changes anywhere in the pipeline — the agents,
the persistence services and the graph cannot tell which they got. Flipping
`DATA_SOURCE=mock` -> `supabase` in .env is the entire migration path back to
the real CRM.
"""
