-- 0001_agent_layer.sql
-- Oblivion Multi-Agent Build Automation — agent layer.
--
-- ============================================================================
-- PRODUCTION-SAFETY CONTRACT (read this before running anything)
-- ============================================================================
-- The Supabase database is owned by the live Lovable CRM (oblivionlabs.lovable.app),
-- in the `public` schema. This migration MUST NOT put the CRM at any risk.
--
-- Guarantees enforced by this file:
--   1. NOTHING is added to `public`. Every object we create lives in a dedicated
--      `agent` schema (and the `langgraph` schema for the checkpointer). The CRM's
--      `public` schema is left byte-for-byte untouched.
--   2. NO foreign keys point INTO CRM tables. Cross-references to public.events /
--      public.projects / public.team_members are stored as plain uuid columns
--      ("logical" references, enforced in app code). This is deliberate: a real FK
--      into a CRM table could BLOCK a delete/update on the CRM side, which would
--      affect production. We never want that. FKs exist ONLY among agent.* tables.
--   3. No ALTER, no DROP, no DELETE against any CRM object. Purely additive,
--      idempotent (`if not exists`).
--
-- The CRM's own tables are REUSED by reading/writing them from application code
-- (see docs/ARCHITECTURE.md §3 "CRM integration"), NOT redefined here.
--
-- Agent -> where it persists:
--   Agent 1 Meeting Notes  reads public.events  -> agent.meeting_intake
--   Agent 7 Orchestrator   -> agent.code_progress (update mode)
--   Agent 2 Wireframe      -> agent.wireframe_drafts
--   Agent 3 Planner        -> public.project_plan_drafts   (EXISTING CRM table, via app code)
--   Agent 4 Gantt          -> public.gantt_tasks           (EXISTING CRM table, via app code)
--   Agent 5 Budget         -> public.budget_line_items     (EXISTING CRM table, via app code)
--   Agent 6 Judge          -> agent.artifact_feedback
--   Rates                  -> public.projects / public.team_members (read-only, via app code)
-- ============================================================================

create extension if not exists pgcrypto;
create schema if not exists agent;      -- everything we own lives here, isolated from the CRM
create schema if not exists langgraph;  -- LangGraph checkpointer tables (created by the library)

-- ----------------------------------------------------------------------------
-- agent.meeting_intake  (Agent 1)
-- Transcript + classification for a calendar meeting.
-- `event_id` is a LOGICAL reference to public.events(id) — no FK by design.
-- ----------------------------------------------------------------------------
create table if not exists agent.meeting_intake (
  id uuid primary key default gen_random_uuid(),
  event_id uuid unique,                             -- logical ref -> public.events(id); unique = idempotency
  project_id uuid,                                  -- logical ref -> public.projects(id); null while pending_review
  plaud_note_id text,                               -- external Plaud ref; dedupes re-imports
  transcript text,
  language text check (language in ('es','en')),    -- drives Agent 5 currency (USD/EN, MXN/ES)
  class text check (class in ('onboarding','follow_up','update','final_qa')),
  sub_type text check (sub_type in ('wireframe','plan','gantt','budget')),  -- only for follow_up
  classification_confidence numeric,
  classification_method text check (classification_method in ('deterministic','llm')),
  status text not null default 'pending_review'
    check (status in ('pending_review','classified','processed','error')),
  created_at timestamptz not null default now()
);

create index if not exists idx_intake_project on agent.meeting_intake (project_id);
create index if not exists idx_intake_status on agent.meeting_intake (status);

-- ----------------------------------------------------------------------------
-- agent.project_matchers  (Agent 1, deterministic match)
-- Aliases / known client emails WE control, to resolve a meeting to a project
-- without an LLM call. Lives in OUR schema — we never add columns to the CRM's
-- projects table. Optional: matching also falls back to reading public.projects
-- name and public.clients name/company from app code.
-- `project_id` is a logical ref -> public.projects(id), no FK.
-- ----------------------------------------------------------------------------
create table if not exists agent.project_matchers (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,                         -- logical ref -> public.projects(id)
  kind text not null check (kind in ('alias','email')),
  value text not null,
  created_at timestamptz not null default now(),
  unique (kind, value)
);

-- ----------------------------------------------------------------------------
-- agent.wireframe_drafts  (Agent 2)
-- Mirrors the CRM's existing `public.project_plan_drafts` shape (payload jsonb +
-- approval fields) so a future CRM view can render it. The one artifact type the
-- CRM has no table for. Kept in `agent` schema to honor the no-touch-`public`
-- contract; if the CRM later needs to show wireframes, expose it via a view.
-- ----------------------------------------------------------------------------
create table if not exists agent.wireframe_drafts (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,                         -- logical ref -> public.projects(id)
  version int not null,
  status text not null default 'draft'
    check (status in ('draft','in_review','approved','needs_human_review','superseded')),
  payload jsonb,                                    -- wireframe JSON (screens/components)
  warnings jsonb not null default '[]',
  pipeline_meta jsonb not null default '{}',        -- model_id, tokens, rounds
  source text not null default 'agent' check (source in ('agent','human')),
  source_intake_id uuid references agent.meeting_intake(id),  -- FK to OUR table: safe
  approved_by uuid,                                 -- logical ref -> public.team_members(id)
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (project_id, version)
);

create index if not exists idx_wireframe_project
  on agent.wireframe_drafts (project_id, version desc);

-- ----------------------------------------------------------------------------
-- agent.artifact_feedback  (Agent 6, Judge)
-- Judge loop record across all artifact types. Polymorphic reference because the
-- four artifacts live in four different tables (two of them in the CRM).
-- ----------------------------------------------------------------------------
create table if not exists agent.artifact_feedback (
  id uuid primary key default gen_random_uuid(),
  artifact_type text not null check (artifact_type in ('wireframe','plan','gantt','budget')),
  artifact_ref uuid not null,                       -- id of the draft/row reviewed (see artifact_type)
  round int not null check (round between 1 and 2), -- Judge cap; revisit if the cap changes
  verdict text not null check (verdict in ('approve','reject')),
  feedback_text text,
  judge_model_id text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_feedback_artifact
  on agent.artifact_feedback (artifact_type, artifact_ref);

-- ----------------------------------------------------------------------------
-- agent.code_progress  (Agent 7, update mode)
-- ----------------------------------------------------------------------------
create table if not exists agent.code_progress (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,                         -- logical ref -> public.projects(id)
  snapshot_at timestamptz not null default now(),
  summary text not null,
  source_ref text,                                  -- commit sha / PR number / issue range
  created_at timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- agent.runs  (observability, all agents)
-- One row per agent execution + token usage for cost tracking (ARCHITECTURE §11).
-- ----------------------------------------------------------------------------
create table if not exists agent.runs (
  id uuid primary key default gen_random_uuid(),
  project_id uuid,                                  -- logical ref -> public.projects(id)
  intake_id uuid references agent.meeting_intake(id),  -- FK to OUR table: safe
  agent_name text not null
    check (agent_name in ('meeting_notes','orchestrator','wireframe','planner','gantt','budget','judge')),
  model_id text,
  trigger_source text,                              -- 'webhook' | 'manual' | 'resume'
  status text not null default 'running'
    check (status in ('running','success','failed','timeout')),
  error text,
  input_tokens int,
  output_tokens int,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create index if not exists idx_runs_project on agent.runs (project_id, started_at desc);
