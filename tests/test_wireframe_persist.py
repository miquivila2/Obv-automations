"""persist_wireframe / load_latest_wireframe against a fake Supabase: version
increments per project, and an explicit `id` (the Judge's draft_ref_id) is
honored so a feedback row and its persisted draft share the same id.
"""
import pytest

from app.services.wireframe_persist import load_latest_wireframe, persist_wireframe
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


def test_first_version_is_1(fake):
    row = persist_wireframe(project_id="p1", payload={"screens": []}, model_id="m", intake_id=None)
    assert row["version"] == 1
    assert row["source"] == "agent"
    assert row["status"] == "draft"


def test_second_call_increments_version(fake):
    persist_wireframe(project_id="p1", payload={}, model_id="m", intake_id=None)
    second = persist_wireframe(project_id="p1", payload={}, model_id="m", intake_id=None)
    assert second["version"] == 2


def test_versions_are_scoped_per_project(fake):
    persist_wireframe(project_id="p1", payload={}, model_id="m", intake_id=None)
    other_project_row = persist_wireframe(project_id="p2", payload={}, model_id="m", intake_id=None)
    assert other_project_row["version"] == 1


def test_load_latest_returns_the_highest_version(fake):
    persist_wireframe(project_id="p1", payload={"v": 1}, model_id="m", intake_id=None)
    persist_wireframe(project_id="p1", payload={"v": 2}, model_id="m", intake_id=None)
    assert load_latest_wireframe("p1")["payload"] == {"v": 2}


def test_load_latest_returns_none_when_no_draft_exists(fake):
    assert load_latest_wireframe("no-such-project") is None


def test_explicit_id_is_honored(fake):
    row = persist_wireframe(project_id="p1", payload={}, model_id="m", intake_id=None, id="fixed-id")
    assert row["id"] == "fixed-id"


def test_no_explicit_id_gets_a_generated_one(fake):
    row = persist_wireframe(project_id="p1", payload={}, model_id="m", intake_id=None)
    assert row["id"]
