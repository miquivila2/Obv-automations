-- 0002_gantt_task_ownership.sql
-- Additive to the `agent` schema only — does NOT touch `public` (the CRM).
--
-- WHY THIS EXISTS: public.gantt_tasks has no `source` (agent/human) column, unlike
-- budget_line_items which already has one. Without SOME way to know which rows the
-- agent itself created, a Gantt regeneration (follow-up mode) can't tell its own
-- prior rows apart from anything a human added/edited — and would either duplicate
-- rows or risk touching a human's row. This table is that missing authorship record,
-- kept entirely in OUR schema instead of altering the CRM's table.
--
-- Decided with the team (docs/ARCHITECTURE.md §5, gap #1/#2 resolution): on
-- regeneration, the agent may UPDATE or DELETE rows it owns (per this table),
-- matched by position, id reused across regenerations so depends_on chains stay
-- valid. It must NEVER touch a public.gantt_tasks row that has no ownership record
-- here — that's how a human-created row stays untouched.

create table if not exists agent.gantt_task_ownership (
  gantt_task_id uuid primary key,   -- logical ref -> public.gantt_tasks(id); no FK (see 0001 contract)
  project_id uuid not null,         -- logical ref -> public.projects(id)
  position int not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_gantt_ownership_project
  on agent.gantt_task_ownership (project_id, position);
