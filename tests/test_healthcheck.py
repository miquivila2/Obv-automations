"""The healthcheck is the only thing standing between "our tests pass against a
fake" and "the real CRM actually has these columns" (see app/healthcheck.py's
module docstring). So its detection logic gets tested too — a healthcheck that
reports PASS when the schema is wrong is worse than none at all.

Uses a purpose-built schema-aware fake rather than tests/fakes.py: what matters
here is precisely the failure modes FakeSupabase doesn't model — an unexposed
schema, a missing column, a missing bucket.
"""
import pytest

from app.healthcheck import (
    _describe,
    check_agent_schema_exposed,
    check_crm_contract,
    check_storage_bucket,
    report_assumptions,
)


class _APIError(Exception):
    """Stands in for postgrest's APIError; the healthcheck matches on its text."""


def _top_level_columns(columns: str) -> list[str]:
    """Split a PostgREST select list on commas that are NOT inside parentheses,
    so an embedded resource ('clients(name,company)') reads as the single
    top-level column 'clients' rather than two broken fragments."""
    names, depth, current = [], 0, ""
    for char in columns:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            names.append(current)
            current = ""
            continue
        current += char
    names.append(current)
    return [n.split("(")[0].strip() for n in names]


class _Query:
    def __init__(self, fake, schema, table):
        self._fake, self._schema, self._table = fake, schema, table

    def select(self, columns="*", **_kw):
        known = self._fake.columns.get((self._schema, self._table))
        if known is None:
            raise _APIError(f'relation "{self._schema}.{self._table}" does not exist')
        for name in _top_level_columns(columns):
            if name not in ("*", "") and name not in known:
                raise _APIError(f'column {self._table}.{name} does not exist')
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return type("R", (), {"data": self._fake.rows.get((self._schema, self._table), [])})()


class _SchemaClient:
    def __init__(self, fake, schema):
        self._fake, self._schema = fake, schema

    def table(self, name):
        return _Query(self._fake, self._schema, name)


class _Bucket:
    def __init__(self, fake, bucket):
        self._fake, self._bucket = fake, bucket

    def list(self):
        if self._bucket not in self._fake.buckets:
            raise _APIError("Bucket not found")
        return []


class _Storage:
    def __init__(self, fake):
        self._fake = fake

    def from_(self, bucket):
        return _Bucket(self._fake, bucket)


class SchemaAwareFake:
    """A Supabase stand-in that enforces schema exposure, table and column
    existence — the three things the healthcheck exists to detect."""

    def __init__(self, columns=None, exposed=("public", "agent"), buckets=("budgets",)):
        self.columns = columns or {}
        self.exposed = set(exposed)
        self.buckets = set(buckets)
        self.rows = {}
        self.storage = _Storage(self)

    def table(self, name):
        return _Query(self, "public", name)

    def schema(self, name):
        if name not in self.exposed:
            raise _APIError(f'PGRST106: The schema must be one of the following: {", ".join(self.exposed)}')
        return _SchemaClient(self, name)


def _install(monkeypatch, fake):
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# The most likely first-run failure: `agent` not added to Exposed schemas
# ---------------------------------------------------------------------------
def test_unexposed_agent_schema_is_detected(monkeypatch, capsys):
    _install(monkeypatch, SchemaAwareFake(exposed=("public",)))

    assert check_agent_schema_exposed() is False
    out = capsys.readouterr().out
    assert "NOT exposed" in out
    assert "Exposed schemas" in out  # the actionable fix, not just a stack trace


def test_exposed_agent_schema_passes(monkeypatch):
    _install(monkeypatch, SchemaAwareFake(columns={("agent", "meeting_intake"): {"id"}}))
    assert check_agent_schema_exposed() is True


# ---------------------------------------------------------------------------
# The integration contract: a CRM column we write that doesn't actually exist
# ---------------------------------------------------------------------------
def _full_crm_schema() -> dict:
    from app.healthcheck import _CRM_TABLES

    schema = {("public", table): set(cols) for table, cols in _CRM_TABLES.items()}
    schema[("public", "projects")] |= {"clients"}  # the nested client relationship
    return schema


def test_matching_crm_schema_passes(monkeypatch):
    _install(monkeypatch, SchemaAwareFake(columns=_full_crm_schema()))
    assert check_crm_contract() is True


def test_a_missing_column_is_reported_by_name(monkeypatch, capsys):
    schema = _full_crm_schema()
    schema[("public", "gantt_tasks")].discard("anchor_date")
    _install(monkeypatch, SchemaAwareFake(columns=schema))

    assert check_crm_contract() is False
    out = capsys.readouterr().out
    # Naming the exact offender is the point: a bare "select failed" would leave
    # you diffing 12 columns by hand.
    assert "anchor_date" in out
    assert "gantt_tasks" in out


def test_a_missing_crm_table_is_reported(monkeypatch, capsys):
    schema = _full_crm_schema()
    del schema[("public", "budget_line_items")]
    _install(monkeypatch, SchemaAwareFake(columns=schema))

    assert check_crm_contract() is False
    assert "budget_line_items" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Storage bucket for the generated budget .docx
# ---------------------------------------------------------------------------
def test_missing_storage_bucket_is_detected(monkeypatch, capsys):
    _install(monkeypatch, SchemaAwareFake(buckets=()))
    assert check_storage_bucket() is False
    assert "Storage" in capsys.readouterr().out


def test_present_storage_bucket_passes(monkeypatch):
    _install(monkeypatch, SchemaAwareFake())
    assert check_storage_bucket() is True


# ---------------------------------------------------------------------------
# Assumption reporting must describe SHAPE, never client data
# ---------------------------------------------------------------------------
def test_describe_identifies_emails_without_echoing_them():
    described = _describe(["ops@dasp.mx", "cto@dasp.mx"])
    assert "email" in described
    assert "2" in described
    assert "ops@dasp.mx" not in described  # privacy: shape only, never values


def test_describe_identifies_uuids():
    assert "uuid" in _describe(["3f2504e0-4f89-41d3-9a0c-0305e82c3301"])


def test_describe_handles_null_and_empty():
    assert _describe(None) == "null"
    assert _describe([]) == "empty list"


def test_report_assumptions_never_prints_attendee_values(monkeypatch, capsys):
    fake = _install(monkeypatch, SchemaAwareFake(columns=_full_crm_schema()))
    fake.rows[("public", "events")] = [{"attendee_ids": ["ops@dasp.mx"]}]
    fake.rows[("public", "project_plan_drafts")] = [{"status": "draft"}]

    report_assumptions()

    out = capsys.readouterr().out
    assert "ops@dasp.mx" not in out  # the whole point of _describe
    assert "email" in out
    assert "draft" in out


def test_report_assumptions_warns_when_our_status_is_unused(monkeypatch, capsys):
    fake = _install(monkeypatch, SchemaAwareFake(columns=_full_crm_schema()))
    fake.rows[("public", "events")] = []
    fake.rows[("public", "project_plan_drafts")] = [{"status": "pending"}, {"status": "approved"}]

    report_assumptions()

    out = capsys.readouterr().out
    # We write 'draft' (docs 9.8, an unverified assumption). If the CRM has
    # never used that value, say so loudly rather than discovering it in prod.
    assert "'draft' is NOT among them" in out


def test_report_assumptions_survives_an_empty_database(monkeypatch, capsys):
    _install(monkeypatch, SchemaAwareFake(columns=_full_crm_schema()))
    report_assumptions()  # must not raise on a fresh project with no rows
    out = capsys.readouterr().out
    assert "cannot confirm" in out.lower()


# ---------------------------------------------------------------------------
# budget_line_items.source — the only assumption whose blast radius is DELETE
# ---------------------------------------------------------------------------
def test_preexisting_agent_rows_are_flagged_as_deletable(monkeypatch, capsys):
    fake = _install(monkeypatch, SchemaAwareFake(columns=_full_crm_schema()))
    fake.rows[("public", "budget_line_items")] = [
        {"source": "agent"}, {"source": "agent"}, {"source": "human"},
    ]

    report_assumptions()

    out = capsys.readouterr().out
    # persist_budget DELETEs rows matching source='agent'. If the CRM already
    # has some this system didn't write, that's data loss waiting to happen.
    assert "2 row(s) already have source='agent'" in out
    assert "WILL delete them" in out


def test_no_agent_rows_means_no_delete_warning(monkeypatch, capsys):
    fake = _install(monkeypatch, SchemaAwareFake(columns=_full_crm_schema()))
    fake.rows[("public", "budget_line_items")] = [{"source": "human"}]

    report_assumptions()

    out = capsys.readouterr().out
    assert "human" in out
    assert "WILL delete them" not in out


@pytest.mark.parametrize("check", [check_agent_schema_exposed, check_storage_bucket])
def test_checks_never_raise_on_a_dead_connection(monkeypatch, check):
    class _Dead:
        def __getattr__(self, _name):
            raise _APIError("connection refused")

    monkeypatch.setattr("app.db.client.get_supabase", lambda: _Dead())
    assert check() is False  # reports, never explodes mid-deploy
