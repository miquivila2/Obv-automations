-- 0001_init_schema.sql
-- Oblivion Multi-Agent Build Automation — initial schema (Build Order Paso 0)
--
-- Design notes (see docs/ARCHITECTURE.md for the full reasoning):
--   * check constraints instead of native Postgres enums: cheap to alter later
--     (ALTER TABLE ... DROP/ADD CONSTRAINT) without the ALTER TYPE migration pain.
--   * `projects` is not in the original whiteboard doc — it's the missing piece
--     that lets Agent 1 answer "which project does this meeting belong to".
--   * `agent_runs` is not in the original doc either — added for observability,
--     since debugging a 7-agent chain without an execution log is unworkable.
--   * LangGraph's own checkpointer tables (`checkpoints`, `checkpoint_writes`,
--     `checkpoint_blobs`) are created by `langgraph-checkpoint-postgres` itself
--     into a separate `langgraph` schema — do not hand-manage them here.

create extension if not exists pgcrypto;
create schema if not exists langgraph;

-- ============ PROJECTS ============
-- The entity Agent 1 must resolve every incoming meeting against.
create table projects (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  client_name text not null,
  aliases text[] not null default '{}',          -- alternate spellings/names for deterministic matching
  attendee_emails text[] not null default '{}',  -- known client emails, for pre-match from calendar invites
  status text not null default 'active'
    check (status in ('active','paused','completed','cancelled')),
  niche text,                                     -- e.g. 'pharma_medical_supply' — optional, informational
  created_at timestamptz not null default now()
);

create index idx_projects_status on projects (status);

-- ============ MEETINGS ============
create table meetings (
  id uuid primary key default gen_random_uuid(),
  project_id uuid references projects(id),       -- null until classified / while in review queue
  calendar_event_id text unique,                  -- idempotency key: the 30-min timer must not double-process
  meeting_datetime timestamptz not null,
  attendees jsonb not null default '[]',          -- [{email, name}] from the calendar invite
  language text check (language in ('es','en')),  -- drives Agent 5's currency rule (USD/EN, MXN/ES)
  class text check (class in ('onboarding','follow_up','update','final_qa')),
  sub_type text check (sub_type in ('wireframe','plan','gantt','budget')),  -- only set when class = follow_up
  classification_confidence numeric,
  classification_method text check (classification_method in ('deterministic','llm')),
  status text not null default 'pending_review'
    check (status in ('pending_review','classified','processed','error')),
  created_at timestamptz not null default now()
);

create index idx_meetings_project on meetings (project_id);
create index idx_meetings_status on meetings (status);

create table raw_notes (
  id uuid primary key default gen_random_uuid(),
  meeting_id uuid not null references meetings(id) on delete cascade,
  plaud_note_id text,          -- external Plaud reference; dedupes re-imports of the same export
  transcript text not null,
  created_at timestamptz not null default now()
);

-- ============ ARTIFACTS (versioned) ============
-- What agents 2-5 produce. `source` is the field the manual-edit re-trigger
-- loop keys off: a write with source='human' is what re-fires the downstream chain,
-- an agent write must never re-trigger itself.
create table artifacts (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id),
  type text not null check (type in ('wireframe','plan','gantt','budget')),
  version int not null,
  content jsonb,                -- structured content (wireframe/plan/gantt JSON)
  file_url text,                 -- Supabase Storage link, for the budget .docx
  editable boolean not null default true,
  source text not null check (source in ('agent','human')),
  status text not null default 'draft'
    check (status in ('draft','in_review','approved','needs_human_review','superseded')),
  triggering_meeting_id uuid references meetings(id),
  model_id text,                 -- which Bedrock model produced this version, for auditability
  created_at timestamptz not null default now(),
  unique (project_id, type, version)
);

create index idx_artifacts_project_type on artifacts (project_id, type, version desc);

create table artifact_feedback (
  id uuid primary key default gen_random_uuid(),
  artifact_id uuid not null references artifacts(id) on delete cascade,
  round int not null check (round between 1 and 2),  -- Judge loop cap; revisit this constraint if the cap changes
  verdict text not null check (verdict in ('approve','reject')),
  feedback_text text,
  judge_model_id text not null,
  created_at timestamptz not null default now()
);

-- ============ CODE PROGRESS (update mode) ============
create table code_progress (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id),
  snapshot_at timestamptz not null default now(),
  summary text not null,
  source_ref text,               -- commit sha / PR number / issue range
  created_at timestamptz not null default now()
);

-- ============ RATES ============
create table rate_config (
  id uuid primary key default gen_random_uuid(),
  currency text not null check (currency in ('USD','MXN')),
  tier text not null,
  hourly_rate numeric not null,
  notes text,
  effective_from date not null default current_date  -- old budgets must not shift if the rate changes later
);

-- ============ MILESTONES & TASKS ============
create table milestones (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id),
  artifact_id uuid references artifacts(id),  -- the Gantt version that defined it
  name text not null,
  target_date date,
  status text not null default 'pending' check (status in ('pending','in_progress','done')),
  created_at timestamptz not null default now()
);

create table tasks (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id),
  milestone_id uuid references milestones(id),
  description text not null,
  status text not null default 'pending' check (status in ('pending','in_progress','done')),
  created_at timestamptz not null default now()
);

-- ============ OBSERVABILITY ============
-- Every agent execution, so "why didn't Agent 4 run" has an answer that
-- doesn't require reconstructing it from Supabase writes after the fact.
create table agent_runs (
  id uuid primary key default gen_random_uuid(),
  project_id uuid references projects(id),
  meeting_id uuid references meetings(id),
  agent_name text not null
    check (agent_name in ('meeting_notes','orchestrator','wireframe','planner','gantt','budget','judge')),
  model_id text,
  trigger_source text,          -- 'webhook' | 'manual' | 'resume'
  status text not null default 'running'
    check (status in ('running','success','failed','timeout')),
  error text,
  input_tokens int,
  output_tokens int,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create index idx_agent_runs_project on agent_runs (project_id, started_at desc);
