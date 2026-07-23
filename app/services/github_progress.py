"""Update-mode repo inspection — STUB.

Open question, still unresolved as of docs/ARCHITECTURE.md: whether "real
progress" for the Orchestrator's update mode should be read from GitHub
commits/PRs, the open-issues list (the Axo #54-#82 style the original plan
references), or a status file the team maintains by hand. Don't build the
real implementation until that's decided — this stub exists so
graph/nodes/orchestrator.py has a stable interface to call in the meantime.
"""
from __future__ import annotations


async def fetch_code_progress_snapshot(project_id: str) -> str:
    """Return a text summary of real build progress for the given project,
    for the Orchestrator's update-mode LLM call to compare against the plan.

    TODO: implement once the progress-source decision is made. Options on
    the table: GitHub commits/PRs via a read-only PAT or GitHub App,
    the issue-list convention, or a status file in the repo.
    """
    raise NotImplementedError(
        "github_progress.fetch_code_progress_snapshot: pending the "
        "code-progress-inspection decision — see docs/ARCHITECTURE.md"
    )
