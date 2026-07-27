"""Update-mode repo inspection (Agent 7, docs §9.3 — decided: GitHub commits/PRs
via the REST API, over an issue-list convention or a hand-maintained status
file, since it needs zero process discipline from the team and Oblivion's
client repos are already on GitHub).

Reads recent commit and pull-request activity for a project's linked repo and
turns it into a short text summary for the Orchestrator's update-mode LLM call
to compare against the plan (see app/graph/nodes/orchestrator.py:
summarize_progress).

Repo mapping: `agent.project_repos` (project_id -> owner/repo, migration 0004).
We never add a github_repo column to `public.projects` — the no-touch-`public`
contract (docs §3.4) means that link lives in our own schema instead.

Each snapshot is also persisted to `agent.code_progress` (source_ref = the
head commit sha at fetch time), so update mode has a queryable history instead
of a string that only ever lived inside one LLM call.
"""
from __future__ import annotations

_GITHUB_API = "https://api.github.com"


def _load_repo(project_id: str) -> dict | None:
    from app.db.client import get_supabase

    rows = (
        get_supabase()
        .schema("agent")
        .table("project_repos")
        .select("owner,repo")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _format_commits(commits: list[dict]) -> str:
    lines = [
        f"- {c['sha'][:7]} ({c['commit']['author']['date']}, {c['commit']['author']['name']}): "
        f"{c['commit']['message'].splitlines()[0]}"
        for c in commits
    ]
    return "\n".join(lines) if lines else "(no commits found)"


def _format_pulls(pulls: list[dict]) -> str:
    lines = [f"- #{p['number']} [{'merged' if p.get('merged_at') else p['state']}] {p['title']}" for p in pulls]
    return "\n".join(lines) if lines else "(no pull requests found)"


async def fetch_code_progress_snapshot(project_id: str) -> str:
    """Fetch recent commit + PR activity for the project's linked GitHub repo,
    persist it to agent.code_progress, and return the text summary.

    Raises ValueError if no repo is linked yet (agent.project_repos has no row
    for this project) — loud, not a silently empty/misleading summary that
    would flow into the Gantt re-sync (docs §10 "fail loud")."""
    import httpx

    from app.config import get_settings
    from app.db.client import get_supabase

    repo = _load_repo(project_id)
    if repo is None:
        raise ValueError(
            f"No GitHub repo configured for project {project_id} — add a row to "
            "agent.project_repos (project_id, owner, repo) before running update mode."
        )

    settings = get_settings()
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    owner, name = repo["owner"], repo["repo"]
    async with httpx.AsyncClient(base_url=_GITHUB_API, headers=headers, timeout=15.0) as client:
        commits_resp = await client.get(f"/repos/{owner}/{name}/commits", params={"per_page": 10})
        commits_resp.raise_for_status()
        pulls_resp = await client.get(
            f"/repos/{owner}/{name}/pulls",
            params={"state": "all", "per_page": 10, "sort": "updated", "direction": "desc"},
        )
        pulls_resp.raise_for_status()

    commits = commits_resp.json()
    pulls = pulls_resp.json()

    summary = (
        f"Recent commits ({owner}/{name}):\n{_format_commits(commits)}\n\n"
        f"Recent pull requests:\n{_format_pulls(pulls)}"
    )

    get_supabase().schema("agent").table("code_progress").insert(
        {
            "project_id": project_id,
            "summary": summary,
            "source_ref": commits[0]["sha"] if commits else None,
        }
    ).execute()

    return summary
