-- 0007_enable_rls.sql
-- Enable Row Level Security on every agent.* table.
--
-- This closes a real gap between intent and code: app/db/client.py's own
-- docstring already says "RLS stays enabled on the tables themselves as
-- defense in depth for any future component that connects with the anon
-- key" -- but migrations 0001-0006 never actually enabled it.
--
-- This app always connects with the SERVICE_ROLE key, which bypasses RLS by
-- Supabase design -- so this migration changes NOTHING about how this
-- backend behaves today. What it changes: with RLS on and zero policies
-- defined, the `anon` and `authenticated` Postgres roles get ZERO access to
-- these tables, even after the `agent` schema is exposed to PostgREST.
-- Deny-by-default, not permit-by-accident.
--
-- No policies are added on purpose: nothing in this system's design calls
-- for a browser or anon/authenticated client to read or write these tables
-- directly (docs §3.4 -- agents write to the CRM's own tables for that; the
-- CRM's frontend never talks to `agent.*`). If that ever becomes a real
-- requirement, add narrow, specific policies then -- don't pre-guess them now.

alter table agent.meeting_intake enable row level security;
alter table agent.project_matchers enable row level security;
alter table agent.wireframe_drafts enable row level security;
alter table agent.artifact_feedback enable row level security;
alter table agent.code_progress enable row level security;
alter table agent.runs enable row level security;
alter table agent.gantt_task_ownership enable row level security;
alter table agent.gantt_task_details enable row level security;
alter table agent.project_repos enable row level security;
alter table agent.qa_findings enable row level security;
alter table agent.artifact_examples enable row level security;
