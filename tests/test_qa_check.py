"""Agent 8 — run_final_qa_check (docs §9.2). Read-only against the CRM: it may
only ever insert into agent.qa_findings, and only when a scope switch is
actually found. MODEL_PROVIDER=stub (conftest) means the LLM call returns a
canned "no switch" verdict unless a test overrides it.
"""
import pytest

from app.services.plan_persist import persist_plan
from app.services.qa_check import ScopeCheck, run_final_qa_check
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


class _FakeStructuredModel:
    def __init__(self, result):
        self._result = result

    async def ainvoke(self, _messages):
        return self._result


class _FakeChatModel:
    def __init__(self, result):
        self._result = result

    def with_structured_output(self, _schema):
        return _FakeStructuredModel(self._result)


async def test_no_plan_on_file_skips_the_model_entirely(fake):
    out = await run_final_qa_check(project_id="no-such-project", intake_id="i1", notes="n")
    assert out == {"has_scope_switch": False, "reason": "no plan on file to compare against"}
    assert fake.rows("agent", "qa_findings") == []


async def test_stub_verdict_is_no_switch_and_writes_nothing(fake):
    persist_plan(project_id="p1", brief="b", payload={"phases": []}, model_id="m", intake_id=None)

    out = await run_final_qa_check(project_id="p1", intake_id="i1", notes="client is happy")

    assert out["has_scope_switch"] is False
    assert out["summary"].startswith("[stub]")
    assert fake.rows("agent", "qa_findings") == []


async def test_a_real_switch_is_recorded_as_a_finding_not_a_crm_write(fake, monkeypatch):
    persist_plan(project_id="p1", brief="b", payload={"phases": []}, model_id="m", intake_id=None)
    verdict = ScopeCheck(has_scope_switch=True, summary="Client asked for a mobile app, never scoped.")
    monkeypatch.setattr(
        "app.services.bedrock.chat_model_for", lambda agent, **kw: _FakeChatModel(verdict)
    )

    out = await run_final_qa_check(project_id="p1", intake_id="i1", notes="we also need a mobile app")

    assert out["has_scope_switch"] is True
    assert out["summary"] == verdict.summary
    assert "finding_id" in out

    findings = fake.rows("agent", "qa_findings")
    assert len(findings) == 1
    assert findings[0]["project_id"] == "p1"
    assert findings[0]["has_scope_switch"] is True
    assert findings[0]["requested_scope"] == "we also need a mobile app"

    # The only side effect anywhere is that one agent.* insert — the plan in
    # public.* is read, never touched (still just the one row from persist_plan).
    assert len(fake.rows("public", "project_plan_drafts")) == 1
