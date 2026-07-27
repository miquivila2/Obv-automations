"""The few-shot example library (docs §9.4). The behaviour that matters here is
the SELECTION POLICY: builders may fall back to merely-recent examples, the
Judge must only ever see curated gold ones.
"""
import pytest

from app.services.example_library import (
    format_examples_block,
    load_examples,
    load_gold_examples,
)
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


def _seed(fake, rows):
    fake.seed("agent", "artifact_examples", rows)


def _example(label, *, artifact_type="budget", is_gold=False, created_at=0, payload=None):
    return {
        "label": label,
        "artifact_type": artifact_type,
        "is_gold": is_gold,
        "created_at": created_at,
        "payload": payload if payload is not None else {"lines": []},
    }


def test_unseeded_library_returns_nothing_and_renders_nothing(fake):
    assert load_examples("budget") == []
    assert format_examples_block([], heading="Past budgets:") == ""


def test_builder_policy_puts_gold_first_then_recent(fake):
    _seed(
        fake,
        [
            _example("recent-but-ordinary", created_at=99),
            _example("old-gold", is_gold=True, created_at=1),
        ],
    )

    labels = [e["label"] for e in load_examples("budget")]

    # Gold outranks recency — that's the whole point of the flag.
    assert labels == ["old-gold", "recent-but-ordinary"]


def test_judge_policy_never_sees_a_non_gold_example(fake):
    _seed(
        fake,
        [
            _example("ordinary", created_at=99),
            _example("gold", is_gold=True, created_at=1),
        ],
    )

    assert [e["label"] for e in load_gold_examples("budget")] == ["gold"]


def test_examples_are_scoped_to_their_artifact_type(fake):
    _seed(
        fake,
        [
            _example("a-budget", artifact_type="budget"),
            _example("a-wireframe", artifact_type="wireframe"),
        ],
    )

    assert [e["label"] for e in load_examples("wireframe")] == ["a-wireframe"]


def test_limit_caps_the_context_spent_on_examples(fake):
    _seed(fake, [_example(f"e{i}", created_at=i) for i in range(10)])

    assert len(load_examples("budget", limit=2)) == 2


def test_block_renders_label_notes_and_payload():
    example = {"label": "Axo", "notes": "two-tier rates", "payload": {"lines": [{"hours": 4}]}}

    block = format_examples_block([example], heading="Reference:")

    assert "Reference:" in block
    assert "### Axo — two-tier rates" in block
    assert '"hours": 4' in block


def test_block_survives_an_example_with_no_notes():
    block = format_examples_block([{"label": "Bare", "payload": {}}], heading="H:")

    assert "### Bare" in block
    assert "—" not in block  # no dangling separator when notes is absent


# --- Wiring: the policy has to survive the trip into the actual prompts -------

class _CapturingModel:
    """Records the messages it's asked to invoke, then returns a canned verdict."""

    def __init__(self, result):
        self._result = result
        self.messages = None

    def with_structured_output(self, _schema):
        return self

    async def ainvoke(self, messages):
        self.messages = messages
        return self._result


async def test_judge_prompt_carries_gold_but_not_ordinary_examples(fake, monkeypatch):
    from app.graph.nodes.judge import JudgeVerdict, judge

    _seed(
        fake,
        [
            _example("ORDINARY-EXAMPLE", is_gold=False, created_at=99),
            _example("GOLD-EXAMPLE", is_gold=True, created_at=1),
        ],
    )
    capturing = _CapturingModel(JudgeVerdict(verdict="approve", feedback=""))
    monkeypatch.setattr("app.graph.nodes.judge.chat_model_for", lambda *a, **kw: capturing)

    await judge(
        {"current_artifact_type": "budget", "notes": "n", "draft": {}, "judge_round": 0}
    )

    human_message = capturing.messages[-1][1]
    assert "GOLD-EXAMPLE" in human_message
    # The whole point of the Judge's gold-only policy: yesterday's average output
    # must never quietly become the bar it judges against.
    assert "ORDINARY-EXAMPLE" not in human_message


async def test_builder_prompt_carries_examples(fake, monkeypatch):
    from app.graph.nodes.budget import BudgetDraft, build_budget

    _seed(fake, [_example("ORDINARY-EXAMPLE", is_gold=False, created_at=99)])
    capturing = _CapturingModel(BudgetDraft.stub())
    monkeypatch.setattr("app.services.bedrock.chat_model_for", lambda *a, **kw: capturing)
    monkeypatch.setattr("app.services.rates.resolve_rates", lambda _p: ({"standard": 100}, None))

    await build_budget({"project_id": "p1", "language": "en", "notes": "n"})

    # Builders, unlike the Judge, may fall back to a merely-recent example.
    assert "ORDINARY-EXAMPLE" in capturing.messages[-1][1]
