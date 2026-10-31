"""REST API bearer token: middleware, open routes, docs, check order, CORS, token resolution."""

from __future__ import annotations

import logging
import os
import re

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from helpers import REST_BASE_URL, capture_serving, rest_client
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from rootmind import auth
from rootmind.api.app import OPEN_PATHS, BearerAuthMiddleware, create_app
from rootmind.auth import TokenError
from rootmind.main import UsageError, main, resolve_api_token

TOKEN = "rest-test-token"  # pragma: allowlist secret
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TAURI_ORIGIN = "http://tauri.localhost"


def _app_routes() -> list:
    """Every route of the REST app with auth on (``app.routes``, included routers expanded)."""
    app = create_app(api_token=TOKEN)
    return [context.original_route for context in iter_route_contexts(app.routes)]


def _route_cases() -> list[tuple[str, str]]:
    """``(method, path)`` for every route, with path parameters filled in.

    A route without HTTP methods (a websocket) appears as ``WEBSOCKET`` and
    fails the tests below.
    """
    cases = []
    for route in _app_routes():
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        methods = getattr(route, "methods", None) or ["WEBSOCKET"]
        cases.extend((method, path) for method in sorted(methods))
    return cases


ROUTES = _route_cases()
PROTECTED = [(m, p) for m, p in ROUTES if not (m in ("GET", "HEAD") and p in OPEN_PATHS)]


@pytest.fixture
def secured(rlm_home):
    """A client for the REST app with auth on; requests carry no token unless a test adds one."""
    with rest_client(create_app(api_token=TOKEN)) as client:
        yield client


# --- every route ------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_every_route_needs_the_token(secured, method, path):
    response = secured.request(method, path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Missing bearer token"}


def test_every_route_is_an_http_route():
    """Fails when a websocket or mounted route appears, and when the docs are served."""
    routes = _app_routes()
    for route in routes:
        assert isinstance(route, APIRoute), f"unexpected route {route!r}"
    assert not {"/docs", "/redoc", "/openapi.json"} & {route.path for route in routes}
    # Only GET and HEAD on / and /health are open.
    assert set(ROUTES) - set(PROTECTED) == {
        (method, path) for method in ("GET", "HEAD") for path in OPEN_PATHS
    }
    assert len(PROTECTED) > 15


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", sorted(OPEN_PATHS))
def test_open_routes_answer_without_a_token(secured, method, path):
    assert secured.request(method, path).status_code == 200


def test_post_to_an_open_path_needs_the_token(secured):
    assert secured.post("/health").status_code == 401
    assert secured.post("/").status_code == 401


def test_root_reports_whether_auth_is_on(secured):
    body = secured.get("/").json()
    assert body["auth"] == "bearer"
    assert body["name"] == "RootMind API"
    assert secured.get("/health").json() == {"status": "healthy"}


def test_root_reports_no_auth_without_a_token(api):
    assert api.get("/").json()["auth"] == "none"
    assert api.get("/status").status_code == 200


def test_auth_state_follows_the_latest_app(rlm_home):
    create_app(api_token=TOKEN)
    with rest_client(create_app()) as client:
        assert client.get("/").json()["auth"] == "none"
        assert client.get("/status").status_code == 200


# --- credentials ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "detail"),
    [
        ("Bearer wrong-token", "Invalid token"),
        (f"Bearer {TOKEN}x", "Invalid token"),
        (f"Bearer {TOKEN[:-1]}", "Invalid token"),
        (f"Basic {TOKEN}", "Missing bearer token"),
        (TOKEN, "Missing bearer token"),
        ("Bearer ", "Missing bearer token"),
        ("Bearer", "Missing bearer token"),
        ("", "Missing bearer token"),
    ],
)
def test_wrong_credentials_401(secured, header, detail):
    response = secured.get("/status", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == {"detail": detail}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_right_token_200(secured, scheme):
    response = secured.get("/status", headers={"Authorization": f"{scheme} {TOKEN}"})
    assert response.status_code == 200
    assert "total_documents" in response.json()


def test_config_round_trip_with_the_token(rlm_home):
    with rest_client(create_app(api_token=TOKEN), token=TOKEN) as client:
        body = client.get("/config").json()
        assert client.post("/config", json=body).status_code == 200


def test_token_is_read_only_from_the_header(secured):
    assert secured.get(f"/status?token={TOKEN}").status_code == 401
    assert secured.get(f"/status?access_token={TOKEN}").status_code == 401
    secured.cookies.set("token", TOKEN)
    assert secured.get("/status").status_code == 401


def test_token_compared_in_constant_time(secured, monkeypatch):
    calls = []
    real = auth.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.secrets, "compare_digest", spy)
    secured.get("/status", headers={"Authorization": "Bearer nope"})
    secured.get("/status", headers=AUTH)

    assert calls == [(b"nope", TOKEN.encode()), (TOKEN.encode(), TOKEN.encode())]


@pytest.mark.parametrize("token", ["", "   "])
def test_empty_token_refused(token):
    with pytest.raises(TokenError, match="refusing to start"):
        create_app(api_token=token)


# --- docs --------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_are_off_with_auth(secured, path):
    assert secured.get(path, headers=AUTH).status_code == 404
    assert secured.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_work_without_auth(api, path):
    assert api.get(path).status_code == 200


# --- order of the checks -----------------------------------------------------------------


def test_non_loopback_peer_403_before_401(rlm_home):
    app = create_app(api_token=TOKEN)
    with TestClient(app, base_url=REST_BASE_URL, client=("192.168.1.20", 40000)) as client:
        assert client.get("/status").status_code == 403
        assert client.get("/status", headers=AUTH).status_code == 403


def test_foreign_host_400_before_401(secured):
    assert secured.get("/status", headers={"Host": "evil.example"}).status_code == 400
    assert secured.get("/status", headers={**AUTH, "Host": "evil.example"}).status_code == 400


def test_cors_preflight_needs_no_token(secured):
    response = secured.options(
        "/config",
        headers={
            "Origin": TAURI_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TAURI_ORIGIN
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_401_carries_cors_headers(secured):
    response = secured.get("/status", headers={"Origin": TAURI_ORIGIN})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == TAURI_ORIGIN


# --- websocket -------------------------------------------------------------------------


@pytest.fixture
def ws_client():
    async def echo(websocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", echo)])
    return TestClient(BearerAuthMiddleware(app, token=TOKEN))


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_websocket_without_the_token_is_closed_1008(ws_client, headers):
    with pytest.raises(WebSocketDisconnect) as closed:
        with ws_client.websocket_connect("/ws", headers=headers) as ws:
            ws.receive_text()
    assert closed.value.code == 1008


def test_websocket_with_the_token_is_accepted(ws_client):
    with ws_client.websocket_connect("/ws", headers=AUTH) as ws:
        assert ws.receive_text() == "hello"


# --- token resolution (rootmind api) --------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_env_token_exits_2(monkeypatch, value):
    seen = capture_serving(monkeypatch)
    monkeypatch.setenv("RLM_API_TOKEN", value)
    with pytest.raises(SystemExit) as exc:
        main(["api"])
    assert exc.value.code == 2
    assert "bind" not in seen
    assert "RLM_API_TOKEN" not in os.environ


def test_require_token_creates_the_file_and_reuses_it():
    token = resolve_api_token(require_token=True)

    path = auth.rest_token_path()
    assert path == auth.app_home() / "data" / "rest_token.txt"
    assert path.read_text(encoding="utf-8") == token
    assert len(token) >= 40
    assert resolve_api_token(require_token=True) == token


def test_env_token_beats_the_file(monkeypatch):
    path = auth.rest_token_path()
    path.parent.mkdir(parents=True)
    path.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv("RLM_API_TOKEN", f" {TOKEN} ")

    assert resolve_api_token(require_token=True) == TOKEN
    assert resolve_api_token(require_token=True) == "file-token"


def test_empty_token_file_fails():
    path = auth.rest_token_path()
    path.parent.mkdir(parents=True)
    path.write_text(" \n", encoding="utf-8")
    with pytest.raises(UsageError, match="is empty"):
        resolve_api_token(require_token=True)


def test_a_token_file_that_cannot_be_created_is_a_usage_error(monkeypatch):
    def refuse(path, text):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(auth, "write_private_file", refuse)
    with pytest.raises(UsageError, match="Cannot create .*rest_token.txt: Access is denied"):
        resolve_api_token(require_token=True)


def test_a_token_file_created_at_the_same_moment_is_used(monkeypatch):
    """Two processes generate at once: the second one reads the first one's token."""

    def lose_the_race(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("the-other-process", encoding="utf-8")
        raise FileExistsError(17, "File exists", str(path))

    monkeypatch.setattr(auth, "write_private_file", lose_the_race)
    assert resolve_api_token(require_token=True) == "the-other-process"


def test_no_token_means_no_auth_and_a_warning(caplog):
    caplog.set_level(logging.WARNING)
    assert resolve_api_token(require_token=False) is None
    assert "authentication is off" in caplog.text
    assert not auth.rest_token_path().exists()


def test_api_with_env_token_serves_with_auth_and_never_logs_it(
    rlm_home, monkeypatch, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    seen = capture_serving(monkeypatch)
    monkeypatch.setenv("RLM_API_TOKEN", TOKEN)

    main(["api", "--port", "0"])

    assert "RLM_API_TOKEN" not in os.environ
    out = capsys.readouterr()
    assert TOKEN not in caplog.text + out.out + out.err
    assert f"sha256:{auth.token_fingerprint(TOKEN)}" in caplog.text
    with rest_client(seen["app"]) as client:
        assert client.get("/status").status_code == 401
        assert client.get("/status", headers=AUTH).status_code == 200


def test_api_require_token_serves_with_the_file_token(rlm_home, monkeypatch):
    seen = capture_serving(monkeypatch)

    main(["api", "--require-token"])

    token = auth.rest_token_path().read_text(encoding="utf-8")
    with rest_client(seen["app"]) as client:
        assert client.get("/status").status_code == 401
        assert (
            client.get("/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        )


def test_api_without_token_serves_without_auth(rlm_home, monkeypatch):
    seen = capture_serving(monkeypatch)
    main(["api"])
    with rest_client(seen["app"]) as client:
        assert client.get("/status").status_code == 200
        assert client.get("/").json()["auth"] == "none"


# --- GET /proof -------------------------------------------------------------------------


NONCE = "ab" * 32


def test_proof_answers_without_a_token_and_proves_it(secured):
    response = secured.get("/proof", params={"nonce": NONCE})
    assert response.status_code == 200
    assert auth.proof_matches(TOKEN, NONCE, response.json())
    assert not auth.proof_matches("another-token", NONCE, response.json())
    assert TOKEN not in response.text


@pytest.mark.parametrize("nonce", [None, "", "ab", "AB" * 32, "zz" * 32, "a" * 33, "ab" * 65])
def test_proof_needs_a_hex_nonce(secured, nonce):
    params = {} if nonce is None else {"nonce": nonce}
    response = secured.get("/proof", params=params)
    assert response.status_code == 400


def test_other_methods_on_proof_need_the_token(secured):
    assert secured.post("/proof", params={"nonce": NONCE}).status_code == 401


def test_proves_token_tells_a_proof_a_wrong_one_and_an_older_server():
    def server(status, body):
        return lambda method, url: (status, body)

    def proving(method, url):
        nonce = url.split("nonce=", 1)[1]
        return 200, {"proof": auth.token_proof(TOKEN.encode(), nonce)}

    assert auth.proves_token(proving, "http://127.0.0.1:8000", TOKEN) is True
    assert auth.proves_token(proving, "http://127.0.0.1:8000", "other") is False
    assert auth.proves_token(server(200, {"proof": "0" * 64}), "u", TOKEN) is False
    assert auth.proves_token(server(200, None), "u", TOKEN) is False
    assert auth.proves_token(server(0, None), "u", TOKEN) is False  # nothing answers
    assert auth.proves_token(server(404, {}), "u", TOKEN) is None  # MCP before /proof
    assert auth.proves_token(server(401, {}), "u", TOKEN) is None  # REST before /proof


def test_every_proof_request_has_a_new_nonce():
    seen = []
    auth.proves_token(lambda m, url: seen.append(url) or (0, None), "u", TOKEN)
    auth.proves_token(lambda m, url: seen.append(url) or (0, None), "u", TOKEN)
    assert seen[0] != seen[1]
    assert all(auth.valid_nonce(url.split("nonce=", 1)[1]) for url in seen)
