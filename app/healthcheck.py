"""Deployment healthcheck — run on the TARGET machine before first use.

    python -m app.healthcheck

Exit code 0 if everything passes, 1 otherwise — so it can gate a deploy script.

WHY THIS DOES MORE THAN PING THE DATABASE
-----------------------------------------
This repo and the Lovable CRM are two separate applications that never call
each other; they meet only at the shared Supabase database (docs §3.5). That
makes the DATABASE SCHEMA the entire integration contract — and every test in
this repo runs against an in-memory fake, which proves our logic is right but
proves nothing about whether the real CRM's columns match what we write.

So this checks the contract itself, read-only:
  1. Config loads (all required env vars present).
  2. The configured model provider is ready.
  3. Supabase is reachable at all.
  4. The `agent` schema is exposed to PostgREST. Supabase only serves schemas
     listed under Settings → API → Exposed schemas (default: public,
     graphql_public). Every `.schema("agent")` call in this codebase fails
     until `agent` is added there — the single most likely first-run failure.
  5. Every `agent.*` table exists (i.e. all six migrations were applied).
  6. Every `public.*` CRM table we read or write exists AND has the exact
     columns we use. PostgREST rejects a select naming an unknown column, so
     naming them all is a read-only way to verify the write contract without
     inserting a single row.
  7. The `budgets` Storage bucket exists (Agent 5 uploads the .docx there).
  8. Reports the real shape of the two documented ASSUMPTIONS (docs §9.8,
     §9.10) so they can be confirmed or corrected from evidence.

NOTHING HERE WRITES. It is safe to point at production.

Privacy note: step 8 reports the SHAPE of live data (types, lengths, whether a
string looks like an email), never the values, so the output can be pasted
into a chat or ticket without leaking client data.
"""
from __future__ import annotations

import json
import sys
import urllib.request

# --- The integration contract, as exercised by app/services/*.py -------------
# Columns this codebase actually reads or writes. If a name here is absent from
# the real CRM, the agent that uses it breaks at runtime - which is exactly what
# this file exists to catch before that happens.
_CRM_TABLES: dict[str, list[str]] = {
    # Read-only (classification, rates, budget .docx title).
    "projects": ["id", "name", "status", "hourly_rate", "preferred_currency", "currency"],
    # Read-only (calendar timer). start_at is an ASSUMPTION (docs §9.7) —
    # mirrors end_at's naming, unverified against the real schema.
    "events": ["id", "start_at", "end_at", "project_id", "attendee_ids"],
    # Written by Agent 3 (app/services/plan_persist.py).
    "project_plan_drafts": [
        "id", "project_id", "status", "brief", "payload", "warnings", "pipeline_meta", "created_at",
    ],
    # Written by Agent 4 (app/services/gantt_persist.py).
    "gantt_tasks": [
        "id", "project_id", "phase", "name", "duration_days", "depends_on", "assignees",
        "assignee_ids", "progress", "anchor_date", "position", "source_draft_id",
    ],
    # Written by Agent 5 (app/services/budget_persist.py).
    "budget_line_items": [
        "id", "project_id", "category", "description", "quantity", "unit_rate", "amount",
        "currency", "source", "gantt_task_id", "position", "month", "details",
    ],
}

# Our own tables, one per migration file in supabase/migrations/.
_AGENT_TABLES = [
    "meeting_intake", "project_matchers", "wireframe_drafts", "artifact_feedback",
    "code_progress", "runs",                    # 0001_agent_layer.sql
    "gantt_task_ownership",                     # 0002
    "gantt_task_details",                       # 0003
    "project_repos",                            # 0004
    "qa_findings",                              # 0005
    "artifact_examples",                        # 0006
]

_STORAGE_BUCKET = "budgets"


def _ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _info(msg: str) -> None:
    print(f"         {msg}")


def check_config() -> tuple[bool, object]:
    try:
        from app.config import get_settings

        settings = get_settings()
        _ok(f"config loaded (model_provider={settings.model_provider})")
        return True, settings
    except Exception as e:  # noqa: BLE001 - healthcheck reports, doesn't raise
        _fail(f"config failed to load: {e}")
        return False, None


def check_provider(settings) -> bool:
    provider = settings.model_provider
    if provider == "stub":
        _ok("stub provider - always ready")
        return True

    if provider == "ollama":
        try:
            url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - local, trusted URL
                tags = json.loads(resp.read())
            names = {m.get("name", "") for m in tags.get("models", [])}
            # Ollama tags are like "qwen3:8b"; accept an exact or name-prefix match.
            wanted = settings.ollama_model
            if wanted in names or any(n.split(":")[0] == wanted.split(":")[0] for n in names):
                _ok(f"Ollama up at {settings.ollama_base_url}; model '{wanted}' available")
                return True
            _fail(f"Ollama up but model '{wanted}' not pulled. Run: ollama pull {wanted}")
            return False
        except Exception as e:  # noqa: BLE001
            _fail(f"Ollama not reachable at {settings.ollama_base_url}: {e}. Is `ollama serve` running?")
            return False

    if provider == "bedrock":
        if settings.aws_region:
            _ok(f"bedrock provider - region={settings.aws_region} (verify model access separately)")
            return True
        _fail("bedrock provider but AWS_REGION is empty")
        return False

    _fail(f"unknown model_provider: {provider}")
    return False


def check_supabase() -> bool:
    try:
        from app.db.client import get_supabase

        # Read-only: never writes. Confirms creds + connectivity to the CRM DB.
        get_supabase().table("projects").select("id").limit(1).execute()
        _ok("Supabase reachable (read on public.projects)")
        return True
    except Exception as e:  # noqa: BLE001
        _fail(f"Supabase not reachable: {e}")
        return False


def check_agent_schema_exposed() -> bool:
    """The `agent` schema must be added under Settings -> API -> Exposed schemas.
    Supabase does not expose it by default, and without it EVERY agent-schema
    read and write in this codebase fails."""
    from app.db.client import get_supabase

    try:
        get_supabase().schema("agent").table("meeting_intake").select("id").limit(1).execute()
        _ok("`agent` schema is exposed to PostgREST")
        return True
    except Exception as e:  # noqa: BLE001
        message = str(e)
        if "PGRST106" in message or "schema must be one of" in message:
            _fail("`agent` schema is NOT exposed to PostgREST - every agent.* read/write will fail.")
            _info("Fix: Supabase -> Settings -> API -> Exposed schemas -> add `agent`, then save.")
        else:
            _fail(f"could not query agent.meeting_intake: {message}")
            _info("If this says the table is missing, apply supabase/migrations/ in order.")
        return False


def check_agent_tables() -> bool:
    """Every agent.* table exists - i.e. all six migrations were applied."""
    from app.db.client import get_supabase

    supabase = get_supabase()
    missing = []
    for table in _AGENT_TABLES:
        try:
            supabase.schema("agent").table(table).select("*").limit(1).execute()
        except Exception:  # noqa: BLE001
            missing.append(table)

    if missing:
        _fail(f"missing agent.* tables: {', '.join(missing)}")
        _info("Apply every file in supabase/migrations/ in order (0001 -> 0006).")
        return False
    _ok(f"all {len(_AGENT_TABLES)} agent.* tables present (migrations 0001-0004 applied)")
    return True


def check_crm_contract() -> bool:
    """The heart of this healthcheck: verify the CRM tables have the exact
    columns this codebase reads and writes. PostgREST errors on a select that
    names an unknown column, so this validates the write contract read-only."""
    from app.db.client import get_supabase

    supabase = get_supabase()
    all_ok = True

    for table, columns in _CRM_TABLES.items():
        try:
            supabase.table(table).select(",".join(columns)).limit(1).execute()
            _ok(f"public.{table} - all {len(columns)} columns we use are present")
        except Exception as e:  # noqa: BLE001
            all_ok = False
            message = str(e)
            _fail(f"public.{table}: {message}")
            # Narrow it down: re-check one column at a time so the report names
            # the exact offenders rather than just the first failure.
            bad = []
            for column in columns:
                try:
                    supabase.table(table).select(column).limit(1).execute()
                except Exception:  # noqa: BLE001
                    bad.append(column)
            if bad:
                _info(f"columns not found on public.{table}: {', '.join(bad)}")

    # The nested client read used by classification (projects -> clients).
    try:
        supabase.table("projects").select("id,clients(name,company)").limit(1).execute()
        _ok("public.projects -> clients(name, company) relationship resolves")
    except Exception as e:  # noqa: BLE001
        all_ok = False
        _fail(f"projects->clients nested select failed: {e}")
        _info("Agent 1's deterministic project matching reads client name/company through this.")

    return all_ok


def check_storage_bucket() -> bool:
    """Agent 5 uploads the generated budget .docx to this bucket."""
    from app.db.client import get_supabase

    try:
        get_supabase().storage.from_(_STORAGE_BUCKET).list()
        _ok(f"Storage bucket '{_STORAGE_BUCKET}' exists")
        return True
    except Exception as e:  # noqa: BLE001
        _fail(f"Storage bucket '{_STORAGE_BUCKET}' not usable: {e}")
        _info("Create it: Supabase -> Storage -> New bucket -> name it 'budgets'.")
        return False


def check_plaud_mcp_cli() -> bool:
    """Plaud transcript fetching (app/services/plaud_client.py) spawns
    `npx -y @plaud-ai/mcp@latest` as a subprocess — this just confirms `npx`
    (Node.js >= 20) is on PATH. It does NOT confirm the one-time interactive
    OAuth login has been done (~/.plaud/tokens-mcp.json) — that can only be
    discovered by an actual calendar-timer run, since this check is read-only
    and must not spawn Plaud's server or touch a browser."""
    import shutil
    import subprocess

    npx_path = shutil.which("npx")
    if not npx_path:
        _fail("`npx` not found on PATH - required to run Plaud's MCP server.")
        _info("Install Node.js >= 20 (https://nodejs.org), which bundles npx.")
        return False

    try:
        result = subprocess.run([npx_path, "--version"], capture_output=True, text=True, timeout=10)
        _ok(f"npx available ({npx_path}, v{result.stdout.strip()})")
        _info("One-time setup still required if not done yet: `npx -y @plaud-ai/mcp@latest install`")
        _info("(opens a browser for Plaud login; caches a token this backend then reuses headlessly)")
        return True
    except Exception as e:  # noqa: BLE001
        _fail(f"npx found but failed to run: {e}")
        return False


def _describe(value) -> str:
    """Describe a value's SHAPE without printing client data (see module docstring)."""
    if value is None:
        return "null"
    if isinstance(value, list):
        if not value:
            return "empty list"
        first = value[0]
        kind = type(first).__name__
        looks_like_email = isinstance(first, str) and "@" in first
        hint = " (looks like an email)" if looks_like_email else ""
        if isinstance(first, str) and not looks_like_email and len(first) == 36 and first.count("-") == 4:
            hint = " (looks like a uuid)"
        return f"list of {len(value)} x {kind}{hint}"
    return f"{type(value).__name__}"


def report_assumptions() -> None:
    """Two documented assumptions (docs §9.8, §9.10) that can only be settled
    against real data. Reports evidence; never fails the run."""
    from app.db.client import get_supabase

    supabase = get_supabase()

    # §9.10 - events.attendee_ids: email strings, or team_member uuids?
    try:
        rows = supabase.table("events").select("attendee_ids").limit(5).execute().data
        shapes = {_describe(r.get("attendee_ids")) for r in rows}
        if not rows:
            _warn("events: no rows yet - cannot confirm attendee_ids shape (docs §9.10)")
        else:
            _info(f"events.attendee_ids observed shape(s): {', '.join(sorted(shapes))}")
            _info("  -> we assume EMAIL STRINGS (app/services/calendar_timer.py).")
            _info("  -> if these are uuids, change _resolve_attendee_emails to an id->email lookup.")
    except Exception as e:  # noqa: BLE001
        _warn(f"could not sample events.attendee_ids: {e}")

    # §9.8 - project_plan_drafts.status: which values does the CRM actually use?
    try:
        rows = supabase.table("project_plan_drafts").select("status").limit(50).execute().data
        values = sorted({r.get("status") for r in rows if r.get("status") is not None})
        if not values:
            _warn("project_plan_drafts: no rows yet - cannot confirm the status vocabulary (docs §9.8)")
        else:
            _info(f"project_plan_drafts.status values in use: {', '.join(map(str, values))}")
            _info("  -> we write 'draft' (app/services/plan_persist.py).")
            if "draft" not in values:
                _warn("  -> 'draft' is NOT among them. Confirm before writing to production.")
    except Exception as e:  # noqa: BLE001
        _warn(f"could not sample project_plan_drafts.status: {e}")


def main() -> int:
    print("Oblivion agent layer - deployment healthcheck\n")

    print("Environment")
    ok_config, settings = check_config()
    if not ok_config:
        return 1
    provider_ok = check_provider(settings)

    print("\nSupabase connectivity")
    if not check_supabase():
        print("\nCannot reach Supabase - skipping the schema checks below.")
        return 1

    print("\nAgent schema (ours)")
    schema_ok = check_agent_schema_exposed()
    tables_ok = check_agent_tables() if schema_ok else False

    print("\nCRM integration contract (the columns we read and write)")
    crm_ok = check_crm_contract()

    print("\nStorage")
    storage_ok = check_storage_bucket()

    print("\nPlaud (transcript fetching)")
    plaud_ok = check_plaud_mcp_cli()

    print("\nDocumented assumptions (evidence only - never fails the run)")
    report_assumptions()

    results = [provider_ok, schema_ok, tables_ok, crm_ok, storage_ok, plaud_ok]
    print()
    if all(results):
        print("All checks passed - the agent layer and the CRM agree on the schema.")
        return 0
    print("Some checks failed - see [FAIL] lines above. Do not run the chain until they pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
