"""MCP HTTP re-reads the folder list when config.yaml changes; the token leaves the environment."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from helpers import TEST_TOKEN, index_config

from rootmind import config as config_module
from rootmind import mcp_http
from rootmind.config import get_config, init_config

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _write(path: Path, data) -> None:
    """Write the config and move its mtime forward, so every write counts as a change."""
    before = path.stat().st_mtime_ns if path.exists() else 0
    text = data if isinstance(data, str) else yaml.safe_dump(data)
    path.write_text(text, encoding="utf-8")
    stamp = max(path.stat().st_mtime_ns, before + 1_000_000_000)
    os.utime(path, ns=(stamp, stamp))


def _search(client: TestClient, query: str = "lantern") -> str:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_documents", "arguments": {"query": query}},
    }
    response = client.post("/mcp", json=body, headers=AUTH)
    assert response.status_code == 200
    return response.json()["result"]["content"][0]["text"]


@pytest.fixture
def config_path(rlm_home) -> Path:
    """config.yaml in RLM_HOME with the tiny corpus as ``docs``, indexed."""
    init_config()
    repo = index_config(get_config())
    repo.engine.dispose()
    return rlm_home / "config.yaml"


@pytest.fixture
def mcp(config_path, caplog):
    caplog.set_level(logging.INFO)
    app = mcp_http.create_app(TEST_TOKEN, get_config())
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        yield client


def _with_connectors(path: Path, connectors: list[dict]) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["connectors"] = connectors
    return data


def test_folder_changes_apply_without_restart(mcp, config_path):
    docs = yaml.safe_load(config_path.read_text(encoding="utf-8"))["connectors"]
    assert "camp_notes.md" in _search(mcp)

    _write(config_path, _with_connectors(config_path, []))
    assert _search(mcp) == "No results for 'lantern'."

    _write(config_path, _with_connectors(config_path, docs))
    assert "camp_notes.md" in _search(mcp)


def test_reads_the_file_only_after_a_change(mcp, config_path, monkeypatch):
    calls = []
    real_load = mcp_http.load_config

    def spy(path=None):
        calls.append(path)
        return real_load(path)

    monkeypatch.setattr(mcp_http, "load_config", spy)
    for _ in range(3):
        _search(mcp)
    assert calls == []

    _write(config_path, _with_connectors(config_path, []))
    _search(mcp)
    _search(mcp)
    assert calls == [config_path]


def test_unreadable_config_keeps_the_folder_list(mcp, config_path, caplog):
    _write(config_path, "connectors: [unclosed\n")
    assert "camp_notes.md" in _search(mcp)
    assert "keeping the folder list" in caplog.text

    # A later valid change is picked up.
    _write(config_path, {"connectors": []})
    assert _search(mcp) == "No results for 'lantern'."


def test_a_change_before_the_server_starts_is_used(config_path):
    """The config handed to create_app was read earlier; the file's folder list wins."""
    config = get_config()
    _write(config_path, _with_connectors(config_path, []))
    app = mcp_http.create_app(TEST_TOKEN, config)
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        assert _search(client) == "No results for 'lantern'."


def test_deleted_config_keeps_the_folder_list(mcp, config_path):
    config_path.unlink()
    assert "camp_notes.md" in _search(mcp)


def test_without_a_config_file_the_given_config_applies(app_config, indexed_repo):
    """No config.yaml (as in the other MCP tests): the config passed to create_app stays."""
    assert not config_module.config_file_path().exists()
    app = mcp_http.create_app(TEST_TOKEN, app_config)
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        assert "camp_notes.md" in _search(client)
        assert app.state.mcp.config_path == config_module.config_file_path()


def test_main_watches_the_config_file_it_was_given(rlm_home, monkeypatch):
    import uvicorn

    other = rlm_home / "other.yaml"
    other.write_text((rlm_home / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: started.update(app=app))
    monkeypatch.setenv("MCP_API_TOKEN", TEST_TOKEN)

    mcp_http.main(["--config", "other.yaml"])

    with TestClient(started["app"], base_url="http://127.0.0.1:3000"):
        assert started["app"].state.mcp.config_path == other


def test_main_removes_the_token_from_the_environment(monkeypatch):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: None)
    monkeypatch.setenv("MCP_API_TOKEN", TEST_TOKEN)

    mcp_http.main([])

    assert "MCP_API_TOKEN" not in os.environ


def test_main_removes_an_empty_token_too(monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", " ")
    with pytest.raises(SystemExit):
        mcp_http.main([])
    assert "MCP_API_TOKEN" not in os.environ
