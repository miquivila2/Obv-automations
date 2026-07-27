-- 0005_qa_findings.sql
-- agent.qa_findings (Agent 8 — Final QA scope-switch detector, docs §9.2)
--
-- Same production-safety contract as 0001 (see its header): purely additive,
-- lives entirely in `agent`, no FK into `public`. Agent 8 never writes to the
-- CRM and never changes any status anywhere — this table is its ONLY output,
-- a notification log for a human to review. A row here means "a human should
-- look at this scope switch", nothing more; no downstream agent reads or acts
-- on it, and no other part of the system is triggered by a row landing here.

create table if not exists agent.qa_findings (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,                          -- logical ref -> public.projects(id)
  intake_id uuid references agent.meeting_intake(id),  -- the Final QA meeting that triggered this check
  has_scope_switch boolean not null,
  summary text not null,
  requested_scope text,                              -- the raw Final QA meeting notes, for human context
  model_id text,
  created_at timestamptz not null default now()
);

create index if not exists idx_qa_findings_project on agent.qa_findings (project_id, created_at desc);
