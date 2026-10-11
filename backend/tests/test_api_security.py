"""Localhost hardening of the unauthenticated REST API: peer, Host, CORS, content type, bind."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from helpers import REST_BASE_URL, TEST_TOKEN, rest_client

from rootmind.api.app import create_app


def _preflight(client, origin: str):
    return client.options(
        "/config",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


@pytest.mark.parametrize("peer", ["192.168.1.20", "10.0.0.5", "2001:db8::1", "testclient"])
def test_non_loopback_peers_rejected(rlm_home, peer):
    """Bound to a network interface by mistake, the API still refuses other machines."""
    with TestClient(create_app(), base_url=REST_BASE_URL, client=(peer, 40000)) as client:
        response = client.get("/health", headers={"Host": "127.0.0.1"})
    assert response.status_code == 403


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_loopback_peers_accepted(rlm_home, peer):
    with TestClient(create_app(), base_url=REST_BASE_URL, client=(peer, 40000)) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.nip.io", "192.168.1.20"])
def test_foreign_host_header_rejected(api, host):
    """DNS rebinding: a page on another name must not reach the API."""
    response = api.get("/health", headers={"Host": host})
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "tauri.localhost"])
def test_loopback_host_headers_accepted(api, host):
    assert api.get("/health", headers={"Host": host}).status_code == 200


def test_cors_rejects_foreign_origins(api):
    response = _preflight(api, "https://evil.example")
    assert "access-control-allow-origin" not in response.headers
    simple = api.get("/config", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


@pytest.mark.parametrize(
    "origin", ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"]
)
def test_cors_allows_the_desktop_app(api, origin):
    response = _preflight(api, origin)
    assert response.headers.get("access-control-allow-origin") == origin
    assert "access-control-allow-credentials" not in response.headers


def test_vite_dev_origin_only_with_rlm_dev(rlm_home, monkeypatch):
    dev_origin = "http://localhost:1420"
    with rest_client(create_app()) as client:
        assert "access-control-allow-origin" not in _preflight(client, dev_origin).headers

    monkeypatch.setenv("RLM_DEV", "1")
    with rest_client(create_app()) as client:
        allowed = _preflight(client, dev_origin)
        assert allowed.headers.get("access-control-allow-origin") == dev_origin


def test_config_post_requires_json_content_type(api):
    """A cross-site form or text/plain "simple request" cannot change the config (CSRF)."""
    body = json.dumps(api.get("/config").json())

    as_text = api.post("/config", content=body, headers={"Content-Type": "text/plain"})
    as_form = api.post(
        "/config", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    assert as_text.status_code == 422
    assert as_form.status_code == 422


def test_default_bind_is_loopback():
    from rootmind.config import AppConfig
    from rootmind.main import build_parser

    assert AppConfig().api.host == "127.0.0.1"
    assert build_parser().parse_args(["api"]).host == "127.0.0.1"


def test_mcp_http_binds_to_loopback_without_host(monkeypatch):
    import uvicorn

    from rootmind import mcp_http

    monkeypatch.setenv("MCP_API_TOKEN", TEST_TOKEN)
    monkeypatch.delenv("MCP_HTTP_HOST", raising=False)
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: started.update(host=host))

    mcp_http.main([])

    assert started == {"host": "127.0.0.1"}


def test_mcp_http_has_no_unauthenticated_docs(app_config, indexed_repo):
    from rootmind.mcp_http import create_app as create_mcp_app

    with TestClient(create_mcp_app(TEST_TOKEN, app_config)) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
