-- 0006_artifact_examples.sql
-- agent.artifact_examples (few-shot library for Agents 2/5 and the Judge, docs §9.4)
--
-- Same production-safety contract as 0001 (see its header): purely additive,
-- lives entirely in `agent`, no FK into `public`.
--
-- WHY jsonb AND NOT A FILE IN STORAGE
-- -----------------------------------
-- The useful few-shot example is the shape the MODEL EMITS, not the rendered
-- deliverable. Agent 5 outputs budget LINE ITEMS (category/description/hours/
-- tier/month/justification) and the arithmetic happens in code (docs §5.2) —
-- so showing it a finished .docx grounds it in the wrong artifact. Same for
-- Agent 2: it emits a screens JSON, not a picture. Storage still holds the
-- rendered .docx budgets (that's Agent 5's output), but this table is the
-- library the prompts actually read.
--
-- CONFIDENTIALITY: rows here hold REAL CLIENT CONTENT (the FLOWSIGHT / Axo
-- Capital budgets are the gold standard). That is exactly why this lives in
-- Supabase and not in the repo — see .gitignore, which blocks examples/ and
-- *.docx. This migration creates the table EMPTY; seeding it is a human task
-- performed against the database, never a commit.

create table if not exists agent.artifact_examples (
  id uuid primary key default gen_random_uuid(),
  artifact_type text not null check (artifact_type in ('wireframe','plan','gantt','budget')),
  label text not null,                               -- human name, e.g. 'Axo Capital — Feb 2026'
  payload jsonb not null,                            -- same shape the builder emits (see WHY jsonb above)
  -- Human-curated quality flag. DELIBERATELY NOT derived from Judge approvals:
  -- that would be circular — the Judge grading against examples the Judge itself
  -- promoted lets the system's own average output become the standard and drift
  -- downward. A row is exemplary because a HUMAN said so.
  is_gold boolean not null default false,
  notes text,                                        -- why this one is a good reference
  created_at timestamptz not null default now()
);

create index if not exists idx_artifact_examples_lookup
  on agent.artifact_examples (artifact_type, is_gold desc, created_at desc);
