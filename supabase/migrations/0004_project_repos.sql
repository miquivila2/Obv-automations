-- 0004_project_repos.sql
-- agent.project_repos (Agent 7, update mode — docs §9.3, decided: GitHub commits/PRs)
--
-- Same production-safety contract as 0001 (see its header): purely additive,
-- lives entirely in `agent`, no FK into `public`. We never add a github_repo
-- column to public.projects — the CRM's projects table stays untouched; the
-- link lives here instead, one row per project that has a linked repo.

create table if not exists agent.project_repos (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null unique,                  -- logical ref -> public.projects(id)
  owner text not null,                              -- GitHub org/user
  repo text not null,                                -- GitHub repo name
  created_at timestamptz not null default now()
);
