import json

from fastapi.testclient import TestClient

from api import app
from utils import etl_tools
from utils.anonymous_sessions import AnonymousSessionRegistry
from utils.session_context import session_id as current_session_id


def test_expired_session_deletes_owned_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    registry = AnonymousSessionRegistry(ttl_seconds=0)
    owner, _ = registry.create()
    token = current_session_id.set(owner)
    try:
        result = etl_tools._save(__import__("pandas").DataFrame({"value": [1]}), "source", "csv", "test", "test")
    finally:
        current_session_id.reset(token)

    artifact_id = result["artifact"]["id"]
    assert (tmp_path / f"{artifact_id}.meta").is_file()
    assert not registry.is_active(owner)
    assert not list(tmp_path.iterdir())


def test_artifact_download_requires_the_owning_session(tmp_path, monkeypatch):
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    registry = AnonymousSessionRegistry()
    monkeypatch.setattr("api._sessions", registry)
    owner, _ = registry.create()
    token = current_session_id.set(owner)
    try:
        result = etl_tools._save(__import__("pandas").DataFrame({"value": [1]}), "source", "csv", "test", "test")
    finally:
        current_session_id.reset(token)

    artifact_id = result["artifact"]["id"]
    first = TestClient(app)
    first.cookies.set("data_agent_session", owner)
    assert first.get(f"/api/artifacts/{artifact_id}").status_code == 200

    second, _ = registry.create()
    other = TestClient(app)
    other.cookies.set("data_agent_session", second)
    assert other.get(f"/api/artifacts/{artifact_id}").status_code == 404


def test_session_endpoint_starts_a_fixed_ttl(monkeypatch):
    registry = AnonymousSessionRegistry(ttl_seconds=900)
    monkeypatch.setattr("api._sessions", registry)
    client = TestClient(app)
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json()["expires_in"] <= 900
    assert "data_agent_session=" in response.headers["set-cookie"]

    same = client.get("/api/session")
    assert same.json()["session_id"] == response.json()["session_id"]
