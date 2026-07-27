"""persist_plan / load_latest_plan against a fake Supabase — mirrors
test_wireframe_persist.py, for the CRM's public.project_plan_drafts table.
"""
import pytest

from app.services.plan_persist import load_latest_plan, persist_plan
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


def test_persist_plan_writes_draft_status(fake):
    row = persist_plan(project_id="p1", brief="b", payload={"phases": []}, model_id="m", intake_id=None)
    assert row["status"] == "draft"
    assert row["brief"] == "b"


def test_load_latest_returns_the_most_recently_created(fake):
    persist_plan(project_id="p1", brief="first", payload={}, model_id="m", intake_id=None)
    persist_plan(project_id="p1", brief="second", payload={}, model_id="m", intake_id=None)
    assert load_latest_plan("p1")["brief"] == "second"


def test_load_latest_returns_none_when_no_plan_exists(fake):
    assert load_latest_plan("no-such-project") is None


def test_explicit_id_is_honored(fake):
    row = persist_plan(project_id="p1", brief="b", payload={}, model_id="m", intake_id=None, id="fixed-id")
    assert row["id"] == "fixed-id"
