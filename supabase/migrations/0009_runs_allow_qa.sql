-- 0009_runs_allow_qa.sql
-- Adds Agent 8 ('qa') to agent.runs' agent_name check constraint.
--
-- Real gap found via the architecture review: Agent 8 (Final QA) has run
-- entirely outside the LangGraph build chain since it was introduced
-- (migration 0005) — which is correct, it's not part of the Judge-reviewed
-- build chain (docs §9.2). But that also meant it never got the observability
-- every graph node gets for free via app/graph/build.py's `_tracked()`
-- wrapper: zero rows in agent.runs for Agent 8, ever. Worse, even wiring it
-- up would have failed outright — 'qa' was never in this constraint.
--
-- Same production-safety contract as 0001: purely additive to our own
-- `agent` schema, no FK into `public`, no CRM impact whatsoever.
--
-- Postgres has no ALTER CHECK — drop and recreate under the same
-- (default-generated) name so this is idempotent and matches what 0001
-- would have produced if 'qa' had been included from the start.

alter table agent.runs drop constraint if exists runs_agent_name_check;

alter table agent.runs add constraint runs_agent_name_check
  check (agent_name in
    ('meeting_notes','orchestrator','wireframe','planner','gantt','budget','judge','qa'));
