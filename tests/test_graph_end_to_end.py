"""The whole build chain, start to END, through the real compiled graph.

Every other test in this suite exercises one piece in isolation. This one runs
what actually ships: orchestrator -> wireframe -> judge -> persist -> plan ->
judge -> persist -> gantt -> ... -> budget -> END, with the real edge
mechanics, the real Judge loop wiring and the real persistence dispatch.

Nothing external is involved: MODEL_PROVIDER=stub (conftest) makes every model
return its schema's canned `stub()` output, a MemorySaver replaces the Postgres
checkpointer, and FakeSupabase replaces the database and its storage bucket.

Worth having because the isolated tests structurally cannot catch chain-level
regressions. The judge_round leak — round never reset between artifacts, so
Budget reached round 4 and would have violated artifact_feedback's
`check (round between 1 and 2)` — was invisible to every per-node test and is
exactly what `test_each_artifact_is_judged_in_its_own_round` now pins down.
"""
import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.graph.build import build_graph
from tests.fakes import FakeSupabase

_ARTIFACT_ORDER = ["wireframe", "plan", "gantt", "budget"]


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    # A real project row, so the Budget agent prices against a configured rate
    # and the .docx carries a project name instead of falling back to the uuid.
    fake_client.seed("public", "projects", [{"id": "p1", "name": "Acme", "hourly_rate": 50.0}])
    return fake_client


async def _run_chain(state: dict) -> dict:
    graph = build_graph(MemorySaver())
    return await graph.ainvoke(state, config={"configurable": {"thread_id": state["project_id"]}})


def _onboarding_state() -> dict:
    return {
        "project_id": "p1",
        "intake_id": "i1",
        "meeting_class": "onboarding",
        "sub_type": None,
        "language": "en",
        "notes": "Client wants a dashboard and a detail screen.",
        "judge_round": 0,
        "trigger_source": "manual",
    }


# ---------------------------------------------------------------------------
# Happy path: onboarding runs the full chain and writes all four artifacts
# ---------------------------------------------------------------------------
async def test_onboarding_runs_the_whole_chain_to_the_end(fake):
    result = await _run_chain(_onboarding_state())

    # Budget is terminal, so finishing the chain means it was the last artifact.
    assert result["current_artifact_type"] == "budget"
    assert result.get("needs_human_review", False) is False


async def test_all_four_artifacts_are_persisted_to_their_own_tables(fake):
    await _run_chain(_onboarding_state())

    # The wireframe is the only artifact the CRM has no table for (docs 3.3).
    assert len(fake.rows("agent", "wireframe_drafts")) == 1
    # The other three go into the CRM's own existing tables.
    assert len(fake.rows("public", "project_plan_drafts")) == 1
    assert len(fake.rows("public", "gantt_tasks")) >= 1
    assert len(fake.rows("public", "budget_line_items")) >= 1


async def test_each_artifact_is_judged_in_its_own_round(fake):
    # Regression guard for the judge_round leak: four artifacts, each approved
    # on its first round -> four feedback rows, every one of them round 1.
    # Before persist() reset judge_round, these read 1, 2, 3, 4 and the fourth
    # would have violated the table's `check (round between 1 and 2)`.
    await _run_chain(_onboarding_state())

    feedback = fake.rows("agent", "artifact_feedback")
    assert [r["artifact_type"] for r in feedback] == _ARTIFACT_ORDER
    assert [r["round"] for r in feedback] == [1, 1, 1, 1]
    assert all(r["verdict"] == "approve" for r in feedback)


async def test_wireframe_and_plan_share_their_id_with_their_feedback_row(fake):
    # For the two artifacts that insert one row per version, the Judge's
    # draft_ref_id becomes that row's id, so feedback and draft line up.
    await _run_chain(_onboarding_state())

    feedback_by_type = {r["artifact_type"]: r["artifact_ref"] for r in fake.rows("agent", "artifact_feedback")}
    assert fake.rows("agent", "wireframe_drafts")[0]["id"] == feedback_by_type["wireframe"]
    assert fake.rows("public", "project_plan_drafts")[0]["id"] == feedback_by_type["plan"]


async def test_every_node_execution_is_recorded_as_a_successful_run(fake):
    await _run_chain(_onboarding_state())

    runs = fake.rows("agent", "runs")
    assert all(r["status"] == "success" for r in runs)
    assert all(r["project_id"] == "p1" and r["trigger_source"] == "manual" for r in runs)

    ran = [r["agent_name"] for r in runs]
    assert ran.count("judge") == 4  # one per artifact
    for agent_name in ("orchestrator", "wireframe", "planner", "gantt", "budget"):
        assert agent_name in ran


async def test_gantt_side_tables_are_written_alongside_the_crm_rows(fake):
    # public.gantt_tasks has no source or description column, so ownership and
    # descriptions live in our schema and must stay in step with it (docs 9.9).
    await _run_chain(_onboarding_state())

    task_ids = {r["id"] for r in fake.rows("public", "gantt_tasks")}
    assert {r["gantt_task_id"] for r in fake.rows("agent", "gantt_task_ownership")} == task_ids
    assert {r["gantt_task_id"] for r in fake.rows("agent", "gantt_task_details")} == task_ids


async def test_budget_pdf_is_uploaded_to_storage(fake):
    # Was .docx; switched to PDF (docs §5.2/§9.12 reopened) — see budget_pdf.py.
    await _run_chain(_onboarding_state())

    assert len(fake.uploads) == 1
    upload = fake.uploads[0]
    assert upload["bucket"] == "budgets"
    assert upload["path"].startswith("p1/budget_")
    assert upload["path"].endswith(".pdf")
    assert upload["content"][:4] == b"%PDF"


# ---------------------------------------------------------------------------
# Follow-up mode enters at the artifact named by sub_type, not at the top
# ---------------------------------------------------------------------------
async def test_follow_up_enters_mid_chain_and_skips_upstream_artifacts(fake):
    await _run_chain({**_onboarding_state(), "meeting_class": "follow_up", "sub_type": "gantt"})

    # Entering at the Gantt means wireframe and plan are never built...
    assert fake.rows("agent", "wireframe_drafts") == []
    assert fake.rows("public", "project_plan_drafts") == []
    # ...but the chain still flows downstream from there into the budget.
    assert len(fake.rows("public", "gantt_tasks")) >= 1
    assert len(fake.rows("public", "budget_line_items")) >= 1
    assert [r["artifact_type"] for r in fake.rows("agent", "artifact_feedback")] == ["gantt", "budget"]


# ---------------------------------------------------------------------------
# The ADR that matters: never silently accept an artifact the Judge rejected
# ---------------------------------------------------------------------------
class _RejectingModel:
    """Stands in for the Judge's model, rejecting every draft it is shown."""

    def with_structured_output(self, schema, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        from app.graph.nodes.judge import JudgeVerdict

        return JudgeVerdict(verdict="reject", feedback="Missing the reporting screen.")


@pytest.fixture
def rejecting_judge(monkeypatch):
    """Only the Judge's model rejects; the builders keep their stub output."""
    import app.graph.nodes.judge as judge_module

    real_chat_model_for = judge_module.chat_model_for
    monkeypatch.setattr(
        judge_module,
        "chat_model_for",
        lambda agent, **kw: _RejectingModel() if agent == "judge" else real_chat_model_for(agent, **kw),
    )


async def test_an_artifact_rejected_twice_is_never_persisted(fake, rejecting_judge):
    await _run_chain(_onboarding_state())

    # Two rounds, both rejected -> the graph stops at human_review (docs 8 ADR:
    # flag it, do not write the "best version" anyway).
    feedback = fake.rows("agent", "artifact_feedback")
    assert [r["round"] for r in feedback] == [1, 2]
    assert all(r["artifact_type"] == "wireframe" and r["verdict"] == "reject" for r in feedback)

    assert fake.rows("agent", "wireframe_drafts") == []  # nothing written
    assert fake.rows("public", "project_plan_drafts") == []  # chain never advanced


async def test_both_judge_rounds_group_under_one_artifact_ref(fake, rejecting_judge):
    # A revise round reuses the ref, so the two rounds read as one artifact's
    # review history rather than two unrelated reviews.
    await _run_chain(_onboarding_state())

    refs = {r["artifact_ref"] for r in fake.rows("agent", "artifact_feedback")}
    assert len(refs) == 1
