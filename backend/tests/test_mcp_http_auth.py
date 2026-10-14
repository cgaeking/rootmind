"""MCP over HTTP: bearer auth (fail closed, constant time, never logged), Origin check, JSON-RPC."""

from __future__ import annotations

import logging
import os
import stat

import pytest
from fastapi.testclient import TestClient
from helpers import TEST_TOKEN

from rootmind import mcp_http
from rootmind.mcp_http import TokenError, create_app, resolve_token

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def mcp(app_config, indexed_repo, caplog):
    """An MCP HTTP client over the indexed tiny corpus; checks the token never reaches the logs."""
    caplog.set_level(logging.DEBUG)
    app = create_app(TEST_TOKEN, app_config, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        yield client
    assert TEST_TOKEN not in caplog.text


def _call(client, name, arguments=None, request_id=2, **extra_params):
    params = {"name": name, **extra_params}
    if arguments is not None:
        params["arguments"] = arguments
    body = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
    return client.post("/mcp", json=body, headers=AUTH).json()


# --- authentication ---------------------------------------------------------------------------


def test_missing_header_401(mcp):
    response = mcp.post("/mcp", json=LIST)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token",
        f"Bearer {TEST_TOKEN}x",
        f"Basic {TEST_TOKEN}",
        TEST_TOKEN,
        "Bearer ",
    ],
)
def test_wrong_credentials_401(mcp, header):
    response = mcp.post("/mcp", json=LIST, headers={"Authorization": header})
    assert response.status_code == 401


def test_right_token_200(mcp):
    response = mcp.post("/mcp", json=LIST, headers=AUTH)
    assert response.status_code == 200
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert len(names) == 5
    assert "read_document" in names


def test_token_compared_in_constant_time(mcp, monkeypatch):
    calls = []
    real = mcp_http.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(mcp_http.secrets, "compare_digest", spy)
    mcp.post("/mcp", json=LIST, headers={"Authorization": "Bearer nope"})
    mcp.post("/mcp", json=LIST, headers=AUTH)

    assert calls == [(b"nope", TEST_TOKEN.encode()), (TEST_TOKEN.encode(), TEST_TOKEN.encode())]


def test_health_needs_no_auth(mcp):
    response = mcp.get("/health")
    assert response.status_code == 200
    assert response.json()["server"]["name"] == "rootmind"


def test_sse_endpoints_require_auth(mcp):
    assert mcp.get("/mcp/sse").status_code == 401
    assert mcp.post("/mcp/message?sessionId=x", json=LIST).status_code == 401
    assert mcp.post("/mcp/message?sessionId=unknown", json=LIST, headers=AUTH).status_code == 404


# --- origin --------------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1.evil.example"])
def test_bad_origin_403(mcp, origin):
    response = mcp.post("/mcp", json=LIST, headers={**AUTH, "Origin": origin})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin", ["http://localhost:5173", "http://127.0.0.1:3000", "http://[::1]:8080", "null"]
)
def test_loopback_origins_allowed(mcp, origin):
    assert mcp.post("/mcp", json=LIST, headers={**AUTH, "Origin": origin}).status_code == 200


def test_bad_origin_is_rejected_before_auth(mcp):
    response = mcp.post("/mcp", json=LIST, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_extra_allowed_origins(app_config, indexed_repo, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", " https://n8n.example.com , ")
    extra = mcp_http._env_allowed_origins()
    assert extra == ["https://n8n.example.com"]

    app = create_app(TEST_TOKEN, app_config, allowed_origins=extra, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        headers = {**AUTH, "Origin": "https://n8n.example.com"}
        assert client.post("/mcp", json=LIST, headers=headers).status_code == 200
        headers["Origin"] = "https://other.example.com"
        assert client.post("/mcp", json=LIST, headers=headers).status_code == 403


# --- token resolution ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_empty_env_token_fails_closed(monkeypatch, value):
    monkeypatch.setenv("MCP_API_TOKEN", value)
    with pytest.raises(TokenError, match="refusing to start"):
        resolve_token()
    with pytest.raises(TokenError):
        create_app(value)


def test_env_token_wins(monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", f"  {TEST_TOKEN}  ")
    token, source = resolve_token()
    assert token == TEST_TOKEN
    assert source == "environment variable MCP_API_TOKEN"
    assert not mcp_http.token_file_path().exists()


def test_generated_token_file(monkeypatch):
    from rootmind.config import app_home

    token, source = resolve_token()

    path = app_home() / "data" / "api_token.txt"
    assert path == mcp_http.token_file_path()
    assert path.read_text(encoding="utf-8") == token
    assert len(token) >= 40
    assert token not in source and str(path) in source
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Reused on the next start.
    assert resolve_token() == (token, f"file {path}")


def test_empty_token_file_fails_closed():
    path = mcp_http.token_file_path()
    path.parent.mkdir(parents=True)
    path.write_text("  \n", encoding="utf-8")
    with pytest.raises(TokenError, match="is empty"):
        resolve_token()


def test_main_prints_a_fingerprint_never_the_token(monkeypatch, capsys, caplog):
    import uvicorn

    caplog.set_level(logging.DEBUG)
    started = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, host, port: started.update(host=host, port=port)
    )
    monkeypatch.setenv("MCP_API_TOKEN", TEST_TOKEN)

    mcp_http.main(["--port", "3999"])

    out = capsys.readouterr()
    assert started == {"host": "127.0.0.1", "port": 3999}
    assert f"sha256:{mcp_http.token_fingerprint(TEST_TOKEN)}" in out.out
    assert TEST_TOKEN not in out.out + out.err
    assert TEST_TOKEN not in caplog.text


def test_main_refuses_an_empty_token(monkeypatch, capsys):
    monkeypatch.setenv("MCP_API_TOKEN", " ")
    with pytest.raises(SystemExit) as exc:
        mcp_http.main([])
    assert exc.value.code == 2
    assert "refusing to start" in capsys.readouterr().err


def test_fingerprint_is_short_and_stable():
    fingerprint = mcp_http.token_fingerprint(TEST_TOKEN)
    assert len(fingerprint) == 8
    assert fingerprint == mcp_http.token_fingerprint(TEST_TOKEN)
    assert TEST_TOKEN not in fingerprint


# --- JSON-RPC -------------------------------------------------------------------------------


def test_notification_202(mcp):
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = mcp.post("/mcp", json=note, headers=AUTH)
    assert response.status_code == 202
    assert response.content == b""


def test_client_response_message_202(mcp):
    response = mcp.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "result": {}}, headers=AUTH)
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("requested", "answered"),
    [("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"), ("1999-01-01", "2025-11-25")],
)
def test_initialize_negotiates_the_protocol_version(mcp, requested, answered):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": requested},
    }
    result = mcp.post("/mcp", json=body, headers=AUTH).json()["result"]
    assert result["protocolVersion"] == answered
    assert result["serverInfo"]["name"] == "rootmind"


def test_tool_call(mcp):
    result = _call(mcp, "search_documents", {"query": "lantern"})["result"]
    assert result["isError"] is False
    assert "camp_notes.md" in result["content"][0]["text"]


def test_tool_error_sets_is_error(mcp):
    result = _call(mcp, "read_document", {"doc_id": "nope"})["result"]
    assert result["isError"] is True
    assert "Error: document not found" in result["content"][0]["text"]
    assert _call(mcp, "format_disk", {})["result"]["isError"] is True


def test_flat_arguments_compatibility_shim(mcp):
    """Some clients (n8n) send tool arguments flat in params; client metadata is dropped."""
    result = _call(mcp, "search_documents", query="lantern", sessionId="abc", chatInput="hi")
    assert "camp_notes.md" in result["result"]["content"][0]["text"]


def test_serves_only_by_default(app_config, indexed_repo):
    """Regression: MCP HTTP indexed by default and pruned folders added via the REST API."""
    app = create_app(TEST_TOKEN, app_config)
    with TestClient(app, base_url="http://127.0.0.1:3000"):
        assert app.state.mcp.sync_manager is None


def test_index_flag_enables_the_scheduler(monkeypatch):
    import uvicorn

    monkeypatch.setenv("MCP_API_TOKEN", TEST_TOKEN)
    seen = {}
    real_create_app = mcp_http.create_app

    def spy(*args, **kwargs):
        seen["enable_scheduler"] = kwargs.get("enable_scheduler")
        return real_create_app(*args, **kwargs)

    monkeypatch.setattr(mcp_http, "create_app", spy)
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: None)

    mcp_http.main([])
    assert seen == {"enable_scheduler": False}
    mcp_http.main(["--index"])
    assert seen == {"enable_scheduler": True}


def test_tools_only_see_configured_connectors(app_config, indexed_repo):
    app_config.connectors = []
    app = create_app(TEST_TOKEN, app_config, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        text = _call(client, "search_documents", {"query": "lantern"})["result"]["content"][0]
    assert text["text"] == "No results for 'lantern'."


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, -32601),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]}, -32602),
        ({"jsonrpc": "2.0", "id": 1}, -32600),
        ("not an object", -32600),
    ],
)
def test_json_rpc_errors(mcp, body, code):
    response = mcp.post("/mcp", json=body, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["error"]["code"] == code


def test_parse_error_and_batches_are_400(mcp):
    bad = mcp.post(
        "/mcp", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == -32700
    batch = mcp.post("/mcp", json=[LIST, LIST], headers=AUTH)
    assert batch.status_code == 400


def test_initialize_sends_instructions(mcp):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    result = mcp.post("/mcp", json=body, headers=AUTH).json()["result"]
    assert "user's own files on this computer" in result["instructions"]
    assert "search_documents" in result["instructions"]
