"""File-backed stand-in for the Supabase client.

Implements exactly the slice of the supabase-py / postgrest-py interface this
codebase actually uses — `.table()`, `.schema()`, `.storage.from_()`, and the
query builder chain (select/insert/update/upsert/delete + eq/in_/gte/lte/
order/limit/execute). Everything persists to ONE JSON file, so state survives
a restart and can be inspected or hand-edited between runs.

WHY THIS SHAPE, and not a new repository interface: `app.db.client.get_supabase()`
is already the single seam every data access in this codebase goes through.
Matching its interface here means the pipeline needs zero changes to run against
mock data — which is the whole point (swap the file for the real CRM later by
flipping one env var, not by touching 29 call sites).

DELIBERATE FIDELITY CHOICES — this fake is meant to SURFACE bugs, not hide them:
  * A missing table raises, exactly like PostgREST does, instead of silently
    behaving like an empty one. A typo'd table name must fail loudly.
  * `.insert()` on a table with a UNIQUE constraint we know about (see
    _UNIQUE_CONSTRAINTS) raises on conflict, so idempotency logic is really
    exercised rather than quietly passing.
  * Rows are deep-copied in and out, so a caller mutating a returned dict can't
    accidentally corrupt the store — a class of bug that would be invisible
    in-process but very real against a database.

NOT modelled (out of scope, and documented so nobody mistakes silence for
support): RLS, foreign-key enforcement, check constraints, transactions,
column-type coercion, and PostgREST's embedded-resource syntax beyond the one
nested select classification uses.
"""
from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Tables the pipeline reads/writes, per schema. A table absent here does not
# exist as far as this store is concerned — and querying it raises, mirroring
# PostgREST's behaviour for an unknown relation.
_KNOWN_TABLES: dict[str, list[str]] = {
    "public": [
        "projects",
        "clients",
        "events",
        "team_members",
        "project_plan_drafts",
        "gantt_tasks",
        "budget_line_items",
        "tasks",
    ],
    "agent": [
        "meeting_intake",
        "project_matchers",
        "wireframe_drafts",
        "artifact_feedback",
        "code_progress",
        "runs",
        "gantt_task_ownership",
        "gantt_task_details",
        "project_repos",
        "qa_findings",
        "artifact_examples",
    ],
}

# UNIQUE constraints worth enforcing, because real code depends on them for
# correctness rather than just tidiness. agent.meeting_intake.event_id is THE
# idempotency guarantee for Agent 1 (docs: a timer firing twice for one event
# must not double-process) — a fake that ignored it would let a real
# double-processing bug through untested.
_UNIQUE_CONSTRAINTS: dict[tuple[str, str], list[str]] = {
    ("agent", "meeting_intake"): ["event_id"],
    ("agent", "project_matchers"): ["kind", "value"],
    ("agent", "project_repos"): ["project_id"],
    ("agent", "wireframe_drafts"): ["project_id", "version"],
}


class MockDatabaseError(Exception):
    """Raised for conditions a real database would reject. Deliberately not a
    subclass of anything the pipeline catches broadly, so failures surface."""


class _Result:
    def __init__(self, data: list[dict]) -> None:
        self.data = data


class _Query:
    def __init__(self, store: "MockStore", schema: str, table: str) -> None:
        self._store = store
        self._schema = schema
        self._table = table
        self._op: str | None = None
        self._payload: list[dict] | None = None
        self._on_conflict: str | None = None
        self._filters: list[tuple] = []
        self._order: tuple | None = None
        self._limit: int | None = None
        self._selected_columns: str | None = None

    # --- builder -----------------------------------------------------------
    def insert(self, payload):
        self._op = "insert"
        self._payload = payload if isinstance(payload, list) else [payload]
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload if isinstance(payload, list) else [payload]
        self._on_conflict = on_conflict
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def select(self, columns: str = "*", **_kwargs):
        self._op = self._op or "select"
        self._selected_columns = columns
        return self

    def eq(self, key, value):
        self._filters.append(("eq", key, value))
        return self

    def in_(self, key, values):
        self._filters.append(("in", key, list(values)))
        return self

    def gte(self, key, value):
        self._filters.append(("gte", key, value))
        return self

    def lte(self, key, value):
        self._filters.append(("lte", key, value))
        return self

    def order(self, key, desc=False):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    # --- execution ---------------------------------------------------------
    def _rows(self) -> list[dict]:
        return self._store._rows(self._schema, self._table)

    def _matches(self, row: dict) -> bool:
        for kind, key, value in self._filters:
            actual = row.get(key)
            if kind == "eq" and actual != value:
                return False
            if kind == "in" and actual not in value:
                return False
            # A None on either side of an ordered comparison is not an error in
            # Postgres (it's just never true), so mirror that instead of raising.
            if kind == "gte" and (actual is None or value is None or actual < value):
                return False
            if kind == "lte" and (actual is None or value is None or actual > value):
                return False
        return True

    def _violates_unique(self, candidate: dict, ignore_id: str | None = None) -> str | None:
        keys = _UNIQUE_CONSTRAINTS.get((self._schema, self._table))
        if not keys:
            return None
        if any(candidate.get(k) is None for k in keys):
            return None  # NULLs don't collide in Postgres UNIQUE semantics
        for existing in self._rows():
            if ignore_id is not None and existing.get("id") == ignore_id:
                continue
            if all(existing.get(k) == candidate.get(k) for k in keys):
                return ", ".join(keys)
        return None

    def execute(self) -> _Result:
        with self._store._lock:
            result = self._execute_locked()
            self._store._flush()
        return result

    def _execute_locked(self) -> _Result:
        rows = self._rows()

        if self._op == "insert":
            created = []
            for item in self._payload:
                row = copy.deepcopy(item)
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault("created_at", _now_iso())
                conflict = self._violates_unique(row)
                if conflict is not None:
                    raise MockDatabaseError(
                        f"duplicate key value violates unique constraint on "
                        f"{self._schema}.{self._table} ({conflict}) — this is what the real "
                        f"database would do; the caller's idempotency handling is wrong."
                    )
                rows.append(row)
                created.append(copy.deepcopy(row))
            return _Result(created)

        if self._op == "upsert":
            written = []
            keys = (
                [k.strip() for k in self._on_conflict.split(",")]
                if self._on_conflict
                else _UNIQUE_CONSTRAINTS.get((self._schema, self._table), ["id"])
            )
            for item in self._payload:
                existing = next(
                    (r for r in rows if all(r.get(k) == item.get(k) for k in keys)), None
                )
                if existing is not None:
                    existing.update(copy.deepcopy(item))
                    written.append(copy.deepcopy(existing))
                else:
                    row = copy.deepcopy(item)
                    row.setdefault("id", str(uuid.uuid4()))
                    row.setdefault("created_at", _now_iso())
                    rows.append(row)
                    written.append(copy.deepcopy(row))
            return _Result(written)

        if self._op == "update":
            matched = [r for r in rows if self._matches(r)]
            for r in matched:
                r.update(copy.deepcopy(self._payload))
            return _Result([copy.deepcopy(r) for r in matched])

        if self._op == "delete":
            matched = [r for r in rows if self._matches(r)]
            for r in matched:
                rows.remove(r)
            return _Result([copy.deepcopy(r) for r in matched])

        # select
        selected = [copy.deepcopy(r) for r in rows if self._matches(r)]
        if self._order:
            key, desc = self._order
            selected.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse=desc)
        if self._limit is not None:
            selected = selected[: self._limit]
        selected = [self._embed(r) for r in selected]
        return _Result(selected)

    def _embed(self, row: dict) -> dict:
        """PostgREST embedded resources — only the one shape this codebase uses:
        `projects.select("id,clients(name,company)")` in classification."""
        columns = self._selected_columns or "*"
        if "clients(" not in columns or self._table != "projects":
            return row
        client_id = row.get("client_id")
        client = next(
            (c for c in self._store._rows("public", "clients") if c.get("id") == client_id), None
        )
        row["clients"] = {"name": client.get("name"), "company": client.get("company")} if client else None
        return row


class _SchemaClient:
    def __init__(self, store: "MockStore", schema: str) -> None:
        self._store = store
        self._schema = schema

    def table(self, name: str) -> _Query:
        self._store._assert_table(self._schema, name)
        return _Query(self._store, self._schema, name)


class _Bucket:
    def __init__(self, store: "MockStore", bucket: str) -> None:
        self._store = store
        self._bucket = bucket

    def upload(self, path: str, content: bytes, options: dict | None = None) -> dict:
        """Writes the real bytes to disk under mock_data/storage/<bucket>/, so a
        generated .docx can actually be opened and inspected after a run."""
        target = self._store.storage_dir / self._bucket / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else bytes(content))
        self._store.log(f"storage: wrote {len(content)} bytes to {self._bucket}/{path}")
        return {"path": path}

    def list(self, path: str = "") -> list[dict]:
        base = self._store.storage_dir / self._bucket / path
        if not base.exists():
            return []
        return [{"name": p.name} for p in base.rglob("*") if p.is_file()]


class _Storage:
    def __init__(self, store: "MockStore") -> None:
        self._store = store

    def from_(self, bucket: str) -> _Bucket:
        return _Bucket(self._store, bucket)


class MockStore:
    """The file-backed database. One JSON file holds every table."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.storage_dir = self.path.parent / "storage"
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, list[dict]]] = {}
        self.storage = _Storage(self)
        self._load()

    # --- persistence -------------------------------------------------------
    def _load(self) -> None:
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self._data = {}
        for schema, tables in _KNOWN_TABLES.items():
            self._data.setdefault(schema, {})
            for table in tables:
                self._data[schema].setdefault(table, [])

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")

    def _assert_table(self, schema: str, table: str) -> None:
        if table not in _KNOWN_TABLES.get(schema, []):
            raise MockDatabaseError(
                f'relation "{schema}.{table}" does not exist — the real database would '
                f"reject this too. Either the name is wrong, or _KNOWN_TABLES in "
                f"app/mock/store.py needs updating to match a new migration."
            )

    def _rows(self, schema: str, table: str) -> list[dict]:
        return self._data.setdefault(schema, {}).setdefault(table, [])

    # --- supabase-client-compatible surface --------------------------------
    def table(self, name: str) -> _Query:
        self._assert_table("public", name)
        return _Query(self, "public", name)

    def schema(self, name: str) -> _SchemaClient:
        if name not in _KNOWN_TABLES:
            raise MockDatabaseError(f"schema {name!r} is not exposed in the mock store")
        return _SchemaClient(self, name)

    # --- test-harness helpers (not part of the supabase interface) ---------
    def log(self, message: str) -> None:
        import logging

        logging.getLogger("mock.store").debug(message)

    def rows(self, schema: str, table: str) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._rows(schema, table))

    def replace_table(self, schema: str, table: str, rows: list[dict]) -> None:
        with self._lock:
            self._data.setdefault(schema, {})[table] = copy.deepcopy(rows)
            self._flush()

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                f"{schema}.{table}": len(rows)
                for schema, tables in self._data.items()
                for table, rows in tables.items()
                if rows
            }

    def reset(self) -> None:
        with self._lock:
            self._data = {}
            self._load()
            self._flush()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_STORE: MockStore | None = None


def get_mock_store() -> MockStore:
    """Process-wide singleton, mirroring get_supabase()'s lru_cache behaviour."""
    global _STORE
    if _STORE is None:
        from app.config import get_settings

        _STORE = MockStore(Path(get_settings().mock_data_path))
        if not _STORE.counts():
            from app.mock.seed import seed_store

            seed_store(_STORE)
    return _STORE
