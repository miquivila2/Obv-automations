"""Rate and currency resolution for the Budget agent.

Decisions locked with the team (docs/ARCHITECTURE.md §5):
  * Currency follows the meeting language: English -> USD, Spanish -> MXN.
    A project's `preferred_currency` (if set in the CRM) overrides that.
  * Rates are configurable one-or-two-tier. Today the CRM stores a single
    `projects.hourly_rate`, so we expose it as one 'standard' tier. The return
    shape is a tier->rate dict, so adding a second tier later is additive and
    doesn't change callers.

Reads the CRM's `public.projects` (read-only). Never writes.
"""
from __future__ import annotations

# Fallback used only when a project has no hourly_rate set yet (e.g. fresh local
# test data). Keeps a dev run from crashing; real projects carry their own rate.
_DEFAULT_HOURLY_RATE = 60.0
_DEFAULT_TIER = "standard"


def resolve_currency(language: str, project_preferred: str | None = None) -> str:
    """USD for English meetings, MXN for Spanish — unless the project pins a
    preferred currency in the CRM."""
    if project_preferred:
        return project_preferred
    return "USD" if language == "en" else "MXN"


def resolve_rates(project_id: str) -> tuple[dict[str, float], str | None]:
    """Return (rate_by_tier, project_preferred_currency) for a project.

    rate_by_tier is a {tier_name: hourly_rate} dict — one 'standard' tier today.
    """
    from app.db.client import get_supabase

    rows = (
        get_supabase()
        .table("projects")
        .select("hourly_rate,preferred_currency,currency")
        .eq("id", project_id)
        .limit(1)
        .execute()
        .data
    )
    project = rows[0] if rows else {}
    rate = project.get("hourly_rate") or _DEFAULT_HOURLY_RATE
    preferred = project.get("preferred_currency") or project.get("currency")
    return {_DEFAULT_TIER: float(rate)}, preferred
