"""The few-shot example library (docs §9.4) — `agent.artifact_examples`.

Answers the question the original spec left open: "where do past wireframes and
budgets live, and how are the BEST ones tagged so the Judge and the builders
pull good references, not just recent ones?"

  * WHERE: `agent.artifact_examples`, one row per curated example, payload in
    the same shape the builder emits (see the migration's header for why that
    beats pointing at a rendered .docx in Storage).
  * BEST: a human-curated `is_gold` flag. Never derived from Judge approvals —
    that loop is circular and lets the system's own average output become its
    own standard.

TWO SELECTION POLICIES, deliberately different:

  * Builders (Agents 2 and 5) — `load_examples`: gold first, then most recent.
    They want something to imitate; a recent real example beats nothing.
  * Judge (Agent 6) — `load_gold_examples`: gold ONLY. The Judge measures a
    draft against the STANDARD. Handing it a merely-recent example would let
    quality ratchet downward one approval at a time.

Both degrade to an empty list, and the callers to an empty prompt block, when
the library has nothing for that artifact type. An unseeded library must not
break a build — it just means no few-shot grounding this run.
"""
from __future__ import annotations

import json

# Few-shot grounding, not a corpus dump: every example is spent context, and a
# long tail of mediocre references dilutes the good ones. Curated libraries at
# this volume (10-25 projects/year, docs §1) are small by nature.
_DEFAULT_LIMIT = 3


def _load(artifact_type: str, *, gold_only: bool, limit: int) -> list[dict]:
    from app.db.client import get_supabase

    query = (
        get_supabase()
        .schema("agent")
        .table("artifact_examples")
        .select("*")
        .eq("artifact_type", artifact_type)
    )
    if gold_only:
        query = query.eq("is_gold", True)
    rows = query.execute().data

    # Ranked in Python rather than via chained PostgREST .order() calls: the
    # ordering is two-key (gold first, then newest) and the result set is tiny
    # by design, so this stays readable and doesn't depend on order-chaining
    # semantics differing between PostgREST and the test fake.
    rows.sort(key=lambda r: (bool(r.get("is_gold")), r.get("created_at") or 0), reverse=True)
    return rows[:limit]


def load_examples(artifact_type: str, *, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Builder policy: gold first, then most recent. May return []."""
    return _load(artifact_type, gold_only=False, limit=limit)


def load_gold_examples(artifact_type: str, *, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Judge policy: curated gold only, never merely-recent. May return []."""
    return _load(artifact_type, gold_only=True, limit=limit)


def format_examples_block(examples: list[dict], *, heading: str) -> str:
    """Render examples as a prompt block. Empty list -> empty string, so callers
    can concatenate unconditionally and an unseeded library adds nothing."""
    if not examples:
        return ""

    rendered = []
    for example in examples:
        label = example.get("label", "(unlabeled)")
        why = f" — {example['notes']}" if example.get("notes") else ""
        rendered.append(f"### {label}{why}\n{json.dumps(example['payload'], ensure_ascii=False)}")

    return f"\n\n{heading}\n" + "\n\n".join(rendered)
