"""In-memory stand-in for supabase.Client, scoped per test.

Mimics only the slice of the postgrest-py / storage3 interface this codebase
actually uses (insert/select/update/upsert/delete/eq/in_/gte/lte/order/limit/
execute, schema(), storage.from_().upload()). No network, no real Postgres —
good enough to test our persistence code's read/write logic without pulling in
a real Supabase project.
"""
from __future__ import annotations

import uuid


class _Result:
    def __init__(self, data: list[dict]) -> None:
        self.data = data


class _Query:
    def __init__(self, store: list[dict]) -> None:
        self._store = store
        self._op: str | None = None
        self._payload: list[dict] | None = None
        self._on_conflict: str | None = None
        self._filters: list[tuple] = []
        self._order: tuple | None = None
        self._limit: int | None = None

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

    def select(self, *_args, **_kwargs):
        self._op = self._op or "select"
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

    def _matches(self, row) -> bool:
        for kind, key, value in self._filters:
            if kind == "eq" and row.get(key) != value:
                return False
            if kind == "in" and row.get(key) not in value:
                return False
            if kind == "gte" and row.get(key) < value:
                return False
            if kind == "lte" and row.get(key) > value:
                return False
        return True

    def execute(self) -> _Result:
        if self._op == "insert":
            rows = []
            for item in self._payload:
                row = dict(item)
                row.setdefault("id", str(uuid.uuid4()))
                # Real Postgres rows get a monotonic `created_at` default; mimic
                # that with insertion order so `.order("created_at", desc=...)`
                # is meaningful even though nothing here sets a real timestamp.
                row.setdefault("created_at", len(self._store))
                self._store.append(row)
                rows.append(row)
            return _Result(rows)

        if self._op == "upsert":
            rows = []
            for item in self._payload:
                key = self._on_conflict
                existing = next((r for r in self._store if key and r.get(key) == item.get(key)), None)
                if existing is not None:
                    existing.update(item)
                    rows.append(existing)
                else:
                    row = dict(item)
                    row.setdefault("id", str(uuid.uuid4()))
                    self._store.append(row)
                    rows.append(row)
            return _Result(rows)

        if self._op == "update":
            matched = [r for r in self._store if self._matches(r)]
            for r in matched:
                r.update(self._payload)
            return _Result(matched)

        if self._op == "delete":
            matched = [r for r in self._store if self._matches(r)]
            for r in matched:
                self._store.remove(r)
            return _Result(matched)

        # select
        rows = [r for r in self._store if self._matches(r)]
        if self._order:
            key, desc = self._order
            rows = sorted(rows, key=lambda r: r.get(key), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows)


class _SchemaClient:
    def __init__(self, fake: "FakeSupabase", schema: str) -> None:
        self._fake = fake
        self._schema = schema

    def table(self, name: str) -> _Query:
        return _Query(self._fake._table(self._schema, name))


class _Bucket:
    def __init__(self, fake: "FakeSupabase", bucket: str) -> None:
        self._fake = fake
        self._bucket = bucket

    def upload(self, path, content, options=None):
        self._fake.uploads.append({"bucket": self._bucket, "path": path, "content": content, "options": options})
        return {"path": path}


class _Storage:
    def __init__(self, fake: "FakeSupabase") -> None:
        self._fake = fake

    def from_(self, bucket: str) -> _Bucket:
        return _Bucket(self._fake, bucket)


class FakeSupabase:
    """Seed tables with `fake.seed(schema, table, rows)` before exercising code
    that reads them; inspect writes afterwards with `fake.rows(schema, table)`."""

    def __init__(self) -> None:
        self._tables: dict[tuple[str, str], list[dict]] = {}
        self.uploads: list[dict] = []
        self.storage = _Storage(self)

    def _table(self, schema: str, name: str) -> list[dict]:
        return self._tables.setdefault((schema, name), [])

    def seed(self, schema: str, name: str, rows: list[dict]) -> None:
        self._tables[(schema, name)] = [dict(r) for r in rows]

    def rows(self, schema: str, name: str) -> list[dict]:
        return self._tables.get((schema, name), [])

    def table(self, name: str) -> _Query:
        return _Query(self._table("public", name))

    def schema(self, name: str) -> _SchemaClient:
        return _SchemaClient(self, name)
