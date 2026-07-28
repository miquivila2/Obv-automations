-- 0008_budget_documents.sql
-- agent.budget_documents — the Axo Capital-format budget's document-level
-- metadata that public.budget_line_items has no columns for.
--
-- Same production-safety contract as 0001: purely additive, lives entirely in
-- `agent`, no FK into `public`.
--
-- REVERSES two previously "locked" decisions (docs/ARCHITECTURE.md §5.2 and
-- §9.12), on explicit request after the user shared the real Axo Capital
-- reference document: IVA and per-month discounts are back (§5.2), and
-- contingency + market comparison + milestone payments are being built
-- (§9.12) — see the updated ARCHITECTURE.md for the reasoning.
--
-- WHAT STAYS COMPUTED IN CODE vs. WHAT IS HUMAN-ENTERED (critical distinction,
-- confirmed explicitly by the user — do not blur this in future work):
--   * Line items, subtotal, IVA amount: code, from budget_math.py. Never the LLM.
--   * discount_pct_by_month, contingency_pct, milestones' part_pct: ALWAYS
--     start at zero/null. Nothing in this system invents a discount, a
--     contingency percentage, or a payment split — "esto lo hacemos nosotros"
--     / "debe estar al 0%". A human fills these in later (today: via the mock
--     console's edit form; in production: wherever this table ends up being
--     exposed). Agent 5 must never write a non-zero value into these fields.
--   * market_comparison: computed in code from a static, USD-only config of
--     published rate bands (app/services/budget_market_comparison.py),
--     scaled by the project's real total hours — not fabricated per project,
--     and never converted to another currency (docs' locked no-conversion rule).

create table if not exists agent.budget_documents (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null unique,           -- logical ref -> public.projects(id); one doc per project, upserted
  currency text not null,
  iva_rate numeric not null default 0.16,    -- Axo's real document uses 16% (Mexican IVA)
  discount_pct_by_month jsonb not null default '{}',  -- {"June": 0, ...} — human-edited, agent writes only 0
  contingency_pct numeric,                   -- null until a human sets it; never agent-computed
  milestones jsonb not null default '[]',    -- [{"when","description","part_pct","amount"}] part_pct always 0 from the agent
  market_comparison jsonb,                   -- snapshot at generation time; null when currency != USD (no conversion)
  model_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table agent.budget_documents enable row level security;
