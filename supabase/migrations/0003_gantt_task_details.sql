-- 0003_gantt_task_details.sql
-- Additive to the `agent` schema only — does NOT touch `public` (the CRM).
--
-- WHY THIS EXISTS (audit gap #3, docs/ARCHITECTURE.md §9): the original spec asks
-- for Gantt "tasks + descriptions", but public.gantt_tasks has no column to hold
-- one — and we don't alter CRM tables. This is the side table that carries what
-- the CRM's schema can't: one row per gantt_task, holding its description.
--
-- Kept SEPARATE from agent.gantt_task_ownership (0002) on purpose: ownership
-- answers "is this row ours" (needed for the update/delete safety boundary),
-- this table answers "what's the extra content for it" — two different concerns,
-- even though today they happen to share the same lifecycle (both written/deleted
-- alongside the same gantt_tasks row).

create table if not exists agent.gantt_task_details (
  gantt_task_id uuid primary key,  -- logical ref -> public.gantt_tasks(id); no FK (see 0001 contract)
  description text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
