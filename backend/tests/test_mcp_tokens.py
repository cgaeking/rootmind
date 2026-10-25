"""Named MCP tokens: folder scope, revocation, the REST routes, the network listener and its
settings (bind, TLS, Host allowlist), and per-folder re-scans.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
import ssl
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from helpers import TEST_TOKEN, index_config, rest_client
from test_service_host import Running, home, restore_process_state  # noqa: F401 (fixtures)

from rootmind import config as config_module
from rootmind import mcp_http
from rootmind.auth import token_fingerprint
from rootmind.config import (
    AppConfig,
    ConnectorConfig,
    McpHttpConfig,
    McpNetworkConfig,
    McpTokenConfig,
    load_config,
    mcp_network_problems,
    save_mcp_tokens,
)
from rootmind.mcp_http import NetworkListener, create_app, host_without_port, token_sha256
from rootmind.service import host

ALICE = "alice-token-for-tests-0001"  # pragma: allowlist secret
BOB = "bob-token-for-tests-0002"  # pragma: allowlist secret


def _body(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _tool(client: TestClient, token: str, name: str, arguments: dict) -> httpx.Response:
    return client.post(
        "/mcp",
        json=_body("tools/call", {"name": name, "arguments": arguments}),
        headers={"Authorization": f"Bearer {token}"},
    )


def _text(response) -> str:
    assert response.status_code == 200, response.text
    return response.json()["result"]["content"][0]["text"]


def _bump(path: Path) -> None:
    """Move the file's mtime forward so the server sees a change even on coarse clocks."""
    stamp = path.stat().st_mtime_ns + 2_000_000_000
    os.utime(path, ns=(stamp, stamp))


@pytest.fixture
def two_folders(tmp_path, app_config):
    """The tiny corpus as ``docs`` plus a folder ``other`` with one note, both indexed."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "other_notes.txt").write_text("The silver lantern is in the attic.", encoding="utf-8")
    app_config.connectors.append(ConnectorConfig(name="other", type="local", path=str(other)))
    repo = index_config(app_config)
    repo.engine.dispose()
    app_config.mcp_http = McpHttpConfig(
        tokens=[
            McpTokenConfig(name="alice", sha256=token_sha256(ALICE), folders=["other"]),
            McpTokenConfig(name="bob", sha256=token_sha256(BOB)),
        ]
    )
    return app_config


@pytest.fixture
def mcp(two_folders, caplog):
    caplog.set_level(logging.DEBUG)
    app = create_app(TEST_TOKEN, two_folders)
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        yield client
    for secret in (TEST_TOKEN, ALICE, BOB):
        assert secret not in caplog.text


# --- scope ---------------------------------------------------------------------------


def test_a_scoped_token_sees_only_its_folders(mcp):
    found = _text(_tool(mcp, ALICE, "search_documents", {"query": "lantern"}))
    assert "other_notes.txt" in found
    assert "camp_notes.md" not in found
    listed = _text(_tool(mcp, ALICE, "list_documents", {}))
    assert "other_notes.txt" in listed and "handbook.txt" not in listed
    fuzzy = _text(_tool(mcp, ALICE, "search_fuzzy", {"query": "lantren"}))
    assert "camp_notes.md" not in fuzzy


def test_a_scoped_token_cannot_read_another_folders_document(mcp, two_folders):
    from rootmind.database.repository import DocumentRepository

    repo = DocumentRepository(two_folders.database.path)
    hidden = next(d["doc_id"] for d in repo.list_documents(connector_name="docs"))
    repo.engine.dispose()
    response = _tool(mcp, ALICE, "read_document", {"doc_id": hidden})
    assert response.json()["result"]["isError"] is True
    assert "Staff handbook" not in response.text
    assert "lantern stays" not in response.text


def test_statistics_follow_the_scope(mcp):
    scoped = _text(_tool(mcp, ALICE, "get_statistics", {}))
    everything = _text(_tool(mcp, TEST_TOKEN, "get_statistics", {}))
    assert "Indexed: 1\n" in scoped
    assert "Indexed: 8\n" in everything


def test_unscoped_tokens_see_every_folder(mcp):
    for token in (TEST_TOKEN, BOB):
        found = _text(_tool(mcp, token, "search_documents", {"query": "lantern"}))
        assert "other_notes.txt" in found and "camp_notes.md" in found


def test_instructions_name_only_the_scoped_folders(mcp, two_folders):
    def instructions(token):
        response = mcp.post(
            "/mcp",
            json=_body("initialize", {"protocolVersion": "2025-06-18"}),
            headers={"Authorization": f"Bearer {token}"},
        )
        return response.json()["result"]["instructions"]

    docs_label = Path(two_folders.connectors[0].path).name
    assert "other" in instructions(ALICE) and docs_label not in instructions(ALICE)
    assert docs_label in instructions(TEST_TOKEN)


def test_a_wrong_token_is_refused(mcp):
    response = _tool(mcp, "not-a-token", "get_statistics", {})
    assert response.status_code == 401


def test_tool_calls_are_logged_with_the_token_name(mcp, caplog):
    _tool(mcp, ALICE, "get_statistics", {})
    assert "Tool call: get_statistics (token alice)" in caplog.text


def test_an_sse_session_belongs_to_its_token(two_folders):
    app = create_app(TEST_TOKEN, two_folders)
    state_holder = {}
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        state_holder["state"] = app.state.mcp
        # Open a session as alice without streaming: register it the way /mcp/sse does.
        import asyncio

        identity = mcp_http.TokenIdentity("alice", frozenset({"other"}))
        app.state.mcp.sse_sessions["s1"] = (asyncio.Queue(), identity)
        as_bob = client.post(
            "/mcp/message?sessionId=s1",
            json=_body("tools/list"),
            headers={"Authorization": f"Bearer {BOB}"},
        )
        as_alice = client.post(
            "/mcp/message?sessionId=s1",
            json=_body("tools/list"),
            headers={"Authorization": f"Bearer {ALICE}"},
        )
    assert as_bob.status_code == 404
    assert as_alice.status_code == 202


# --- revocation through config.yaml ---------------------------------------------------


@pytest.fixture
def served(rlm_home):
    """The MCP app over RLM_HOME's config.yaml (indexed), with alice and bob as named tokens."""
    config_module.init_config()
    repo = index_config(config_module.get_config())
    repo.engine.dispose()
    path = rlm_home / "config.yaml"
    save_mcp_tokens(
        [
            {"name": "alice", "sha256": token_sha256(ALICE), "folders": ["docs"]},
            {"name": "bob", "sha256": token_sha256(BOB)},
        ],
        path,
    )
    app = create_app(TEST_TOKEN, config_module.get_config())
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        yield client, path


def test_a_revoked_token_is_refused_at_once_and_others_keep_working(served):
    client, path = served
    assert _tool(client, ALICE, "get_statistics", {}).status_code == 200
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    save_mcp_tokens([t for t in data["mcp_http"]["tokens"] if t["name"] != "alice"], path)
    _bump(path)
    assert _tool(client, ALICE, "get_statistics", {}).status_code == 401
    assert _tool(client, BOB, "get_statistics", {}).status_code == 200
    assert _tool(client, TEST_TOKEN, "get_statistics", {}).status_code == 200


def test_a_new_token_works_without_a_restart(served):
    client, path = served
    carol = "carol-token-for-tests-0003"  # pragma: allowlist secret
    assert _tool(client, carol, "get_statistics", {}).status_code == 401
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    save_mcp_tokens(
        [*data["mcp_http"]["tokens"], {"name": "carol", "sha256": token_sha256(carol)}], path
    )
    _bump(path)
    assert _tool(client, carol, "get_statistics", {}).status_code == 200


def test_tokens_are_stored_as_hashes_only(served):
    _client, path = served
    text = path.read_text(encoding="utf-8")
    assert ALICE not in text and BOB not in text
    assert token_sha256(ALICE) in text


# --- the REST routes -----------------------------------------------------------------


@pytest.fixture
def rest(rlm_home):
    from rootmind.api.app import create_app as create_rest_app

    (rlm_home / "data").mkdir(exist_ok=True)
    (rlm_home / "data" / "api_token.txt").write_text(TEST_TOKEN, encoding="utf-8")
    with rest_client(create_rest_app()) as client:
        yield client


def test_rest_create_list_revoke(rest, rlm_home, caplog):
    caplog.set_level(logging.INFO)
    created = rest.post("/mcp/tokens", json={"name": "alice", "folders": ["docs"]})
    assert created.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    token = created.json()["token"]
    assert created.json()["fingerprint"] == token_fingerprint(token)
    assert token not in caplog.text
    saved = load_config(rlm_home / "config.yaml").mcp_http.tokens
    assert [(t.name, t.folders, t.sha256) for t in saved] == [
        ("alice", ["docs"], token_sha256(token))
    ]
    assert token not in (rlm_home / "config.yaml").read_text(encoding="utf-8")

    listed = rest.get("/mcp/tokens").json()
    assert [(t["name"], t["kind"], t["folders"]) for t in listed["tokens"]] == [
        ("default", "file", None),
        ("alice", "named", ["docs"]),
    ]
    assert listed["tokens"][0]["fingerprint"] == token_fingerprint(TEST_TOKEN)
    assert listed["network"]["enabled"] is False
    assert token not in str(listed)

    assert rest.delete("/mcp/tokens/alice").json() == {"revoked": "alice"}
    assert load_config(rlm_home / "config.yaml").mcp_http.tokens == []
    assert rest.delete("/mcp/tokens/alice").status_code == 404


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"name": "default"}, 400, "not 'default'"),
        ({"name": "has space"}, 400, "letters, digits"),
        ({"name": "x", "folders": ["nope"]}, 400, "No folder named nope"),
        ({"name": "x", "folders": []}, 400, "at least one folder"),
    ],
)
def test_rest_create_refusals(rest, body, status, detail):
    response = rest.post("/mcp/tokens", json=body)
    assert response.status_code == status
    assert detail in response.json()["detail"]


def test_rest_create_refuses_a_duplicate_name(rest):
    assert rest.post("/mcp/tokens", json={"name": "alice"}).status_code == 200
    assert rest.post("/mcp/tokens", json={"name": "ALICE"}).status_code == 409


def test_rest_cannot_revoke_the_default_token(rest):
    response = rest.delete("/mcp/tokens/default")
    assert response.status_code == 400
    assert "service install" in response.json()["detail"]


def test_saving_folders_keeps_the_tokens(rest, rlm_home):
    rest.post("/mcp/tokens", json={"name": "alice"})
    body = rest.get("/config").json()
    assert rest.post("/config", json=body).status_code == 200
    assert [t.name for t in load_config(rlm_home / "config.yaml").mcp_http.tokens] == ["alice"]


def test_rest_token_works_on_mcp_until_revoked(rest, rlm_home):
    """End to end in one home: the backend writes, the MCP server reads config.yaml."""
    config_module.init_config()
    repo = index_config(config_module.get_config())
    repo.engine.dispose()
    token = rest.post("/mcp/tokens", json={"name": "alice", "folders": ["docs"]}).json()["token"]
    app = create_app(TEST_TOKEN, config_module.get_config())
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        assert _tool(client, token, "get_statistics", {}).status_code == 200
        rest.delete("/mcp/tokens/alice")
        _bump(rlm_home / "config.yaml")
        assert _tool(client, token, "get_statistics", {}).status_code == 401


# --- settings ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        {"name": "default", "sha256": "0" * 64},
        {"name": "bad name", "sha256": "0" * 64},
        {"name": "ok", "sha256": "XYZ"},
        {"name": "ok", "sha256": "A" * 64},
    ],
)
def test_invalid_tokens_are_refused(token):
    with pytest.raises(ValueError):
        McpTokenConfig(**token)


def test_duplicate_tokens_are_refused():
    with pytest.raises(ValueError, match="unique"):
        McpHttpConfig(tokens=[{"name": "a", "sha256": "0" * 64}, {"name": "A", "sha256": "1" * 64}])
    with pytest.raises(ValueError, match="same hash"):
        McpHttpConfig(tokens=[{"name": "a", "sha256": "0" * 64}, {"name": "b", "sha256": "0" * 64}])


def test_the_network_listener_is_off_by_default():
    settings = AppConfig().mcp_http
    assert settings.network.bind is None
    assert settings.network.allow_default_token is False
    assert mcp_network_problems(settings) == []


@pytest.mark.parametrize(
    ("network", "hosts", "problem"),
    [
        ({"bind": "0.0.0.0"}, ["srv"], "or insecure_http: true"),  # noqa: S104
        ({"bind": "0.0.0.0", "tls_cert": "c.pem"}, ["srv"], "both tls_cert and tls_key"),  # noqa: S104
        ({"bind": "0.0.0.0", "insecure_http": True}, [], "allowed_hosts"),  # noqa: S104
        ({"bind": "127.0.0.1"}, [], "allowed_hosts"),
    ],
)
def test_network_settings_are_checked(network, hosts, problem):
    settings = McpHttpConfig(network=McpNetworkConfig(**network), allowed_hosts=hosts)
    assert any(problem in p for p in mcp_network_problems(settings))


@pytest.mark.parametrize(
    "network",
    [
        {"bind": "0.0.0.0", "tls_cert": "c.pem", "tls_key": "k.pem"},  # noqa: S104
        {"bind": "192.168.1.5", "insecure_http": True},
        {"bind": "127.0.0.1", "port": 3443},  # a reverse proxy on this machine
        {"bind": "::1"},
    ],
)
def test_complete_network_settings_pass(network):
    settings = McpHttpConfig(network=McpNetworkConfig(**network), allowed_hosts=["srv"])
    assert mcp_network_problems(settings) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("srv.example:3443", "srv.example"),
        ("SRV", "srv"),
        ("[::1]:3443", "::1"),
        ("[fe80::1]", "fe80::1"),
        ("10.0.0.5", "10.0.0.5"),
        ("", None),
        (None, None),
    ],
)
def test_host_without_port(value, expected):
    assert host_without_port(value) == expected


# --- the standalone mcp-http command ---------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--host", "0.0.0.0"], "--insecure-http"),  # noqa: S104
        (["--host", "0.0.0.0", "--tls-cert", "c.pem"], "go together"),  # noqa: S104
        (["--tls-cert", "missing.pem", "--tls-key", "missing.key"], "no such file"),
    ],
)
def test_standalone_refuses_unsafe_listening(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        mcp_http.main(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_standalone_plain_http_on_the_network_needs_the_flag(monkeypatch, tmp_path):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    monkeypatch.setenv("MCP_API_TOKEN", TEST_TOKEN)
    mcp_http.main(["--host", "0.0.0.0", "--insecure-http"])  # noqa: S104
    assert seen == {"host": "0.0.0.0", "port": 3000}  # noqa: S104


def test_standalone_tls(monkeypatch, tmp_path):
    import uvicorn

    cert, key = _self_signed(tmp_path, "127.0.0.2")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    monkeypatch.setenv("MCP_API_TOKEN", TEST_TOKEN)
    mcp_http.main(["--host", "0.0.0.0", "--tls-cert", str(cert), "--tls-key", str(key)])  # noqa: S104
    assert seen["ssl_certfile"] == str(cert) and seen["ssl_keyfile"] == str(key)


# --- the network listener --------------------------------------------------------------


@pytest.fixture
def network_client(two_folders):
    app = create_app(TEST_TOKEN, two_folders)
    wrapped = NetworkListener(app, lambda: ["rootmind.example", "10.0.0.5"])
    with TestClient(wrapped, base_url="https://rootmind.example:3443") as client:
        yield client


def test_network_listener_refuses_other_hosts(network_client):
    response = network_client.post(
        "/mcp",
        json=_body("tools/list"),
        headers={"Authorization": f"Bearer {BOB}", "Host": "evil.example"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Host not allowed"}


def test_network_listener_takes_named_tokens_only(network_client):
    assert _tool(network_client, BOB, "get_statistics", {}).status_code == 200
    assert _tool(network_client, TEST_TOKEN, "get_statistics", {}).status_code == 401


def test_network_listener_can_allow_the_default_token(two_folders):
    two_folders.mcp_http.network = McpNetworkConfig(
        bind="0.0.0.0",  # noqa: S104
        insecure_http=True,
        allow_default_token=True,
    )
    app = create_app(TEST_TOKEN, two_folders)
    wrapped = NetworkListener(app, lambda: ["rootmind.example"])
    with TestClient(wrapped, base_url="https://rootmind.example:3443") as client:
        assert _tool(client, TEST_TOKEN, "get_statistics", {}).status_code == 200


def test_the_loopback_listener_is_unchanged(mcp):
    """Without NetworkListener the default token works and no Host is checked."""
    response = mcp.post(
        "/mcp",
        json=_body("tools/list"),
        headers={"Authorization": f"Bearer {TEST_TOKEN}", "Host": "abc.ngrok.app"},
    )
    assert response.status_code == 200


# --- the service host: both listeners --------------------------------------------------


def _self_signed(folder: Path, address: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rootmind-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _free_port(address: str) -> int:
    with socket.socket() as sock:
        sock.bind((address, 0))
        return sock.getsockname()[1]


def _network_home(home: Path, network: dict, hosts: list[str]) -> None:  # noqa: F811
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["mcp_http"] = {
        "network": network,
        "allowed_hosts": hosts,
        "tokens": [{"name": "bob", "sha256": token_sha256(BOB)}],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (home / "data" / "api_token.txt").write_text(TEST_TOKEN, encoding="utf-8")


def test_the_service_refuses_incomplete_network_settings(home):  # noqa: F811
    _network_home(home, {"bind": "0.0.0.0"}, ["srv"])  # noqa: S104
    runner = host.ServiceRunner("mcp-http", 0, home=home)
    with pytest.raises(host.ServiceConfigError, match="insecure_http"):
        runner.prepare()
    assert runner.sock is None  # nothing was bound


def test_the_service_refuses_the_loopback_mcp_port_twice(home):  # noqa: F811
    _network_home(home, {"bind": "127.0.0.1", "port": 18431}, ["rm.example"])
    with pytest.raises(host.ServiceConfigError, match="loopback MCP port"):
        host.ServiceRunner("mcp-http", 18431, home=home).prepare()


def test_a_loopback_network_listener_for_a_reverse_proxy(home):  # noqa: F811
    port = _free_port("127.0.0.1")
    _network_home(home, {"bind": "127.0.0.1", "port": port}, ["rm.example"])
    running = Running(host.ServiceRunner("mcp-http", 0, home=home))
    try:
        body = _body("tools/list")
        proxied = {
            "Authorization": f"Bearer {BOB}",
            "Host": "rm.example",
            "X-Forwarded-Proto": "https",
        }
        url = f"http://127.0.0.1:{port}/mcp"
        assert httpx.post(url, json=body, headers=proxied).status_code == 200
        other_host = {**proxied, "Host": "127.0.0.1"}
        assert httpx.post(url, json=body, headers=other_host).status_code == 400
        default = {**proxied, "Authorization": f"Bearer {TEST_TOKEN}"}
        assert httpx.post(url, json=body, headers=default).status_code == 401
    finally:
        assert running.stop() is True


def test_the_service_refuses_an_unreadable_certificate(home, tmp_path):  # noqa: F811
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate", encoding="utf-8")
    _network_home(
        home,
        {"bind": "127.0.0.2", "port": 18443, "tls_cert": str(bad), "tls_key": str(bad)},
        ["127.0.0.2"],
    )
    with pytest.raises(host.ServiceConfigError, match="cannot load"):
        host.ServiceRunner("mcp-http", 0, home=home).prepare()


def test_the_service_serves_tls_to_the_network_and_plain_loopback(home, tmp_path):  # noqa: F811
    cert, key = _self_signed(tmp_path, "127.0.0.2")
    port = _free_port("127.0.0.2")
    _network_home(
        home,
        {"bind": "127.0.0.2", "port": port, "tls_cert": str(cert), "tls_key": str(key)},
        ["127.0.0.2"],
    )
    running = Running(host.ServiceRunner("mcp-http", 0, home=home))
    try:
        body = _body("tools/list")
        network = f"https://127.0.0.2:{port}/mcp"
        with httpx.Client(
            verify=ssl.create_default_context(cafile=str(cert)), timeout=10
        ) as client:
            ok = client.post(network, json=body, headers={"Authorization": f"Bearer {BOB}"})
            default = client.post(
                network, json=body, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
            )
        assert ok.status_code == 200
        assert default.status_code == 401
        # The loopback listener (app, Claude Desktop bundle, remote access) is unchanged.
        loopback = httpx.post(
            f"{running.url}/mcp", json=body, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        assert loopback.status_code == 200
        with pytest.raises(httpx.HTTPError):
            httpx.post(f"http://127.0.0.2:{port}/mcp", json=body, timeout=5)
    finally:
        assert running.stop() is True
    assert running.runner.network_sock.fileno() == -1  # closed


# --- re-scans of network shares ----------------------------------------------------------


def test_folders_with_rescan_minutes_get_their_own_job(tmp_path):
    import asyncio

    from rootmind.database.repository import DocumentRepository
    from rootmind.indexer import SyncManager

    repo = DocumentRepository(tmp_path / "db" / "s.db")
    config = AppConfig(
        connectors=[
            ConnectorConfig(name="share", type="local", path=str(tmp_path), rescan_minutes=5),
            ConnectorConfig(name="local", type="local", path=str(tmp_path)),
        ]
    )
    config.indexer.sync_interval_hours = 0
    config.indexer.sync_schedule = None
    manager = SyncManager(repo, {"share": object(), "local": object()}, config)

    async def scenario():
        manager.setup_scheduler()
        jobs = {job.id: str(job.trigger) for job in manager.scheduler.get_jobs()}
        manager.shutdown_scheduler()
        return jobs

    try:
        jobs = asyncio.run(scenario())
    finally:
        repo.engine.dispose()
    assert jobs == {"rescan:share": "interval[0:05:00]"}


def test_a_rescan_syncs_only_its_folder(tmp_path, monkeypatch):
    import asyncio

    from rootmind.database.repository import DocumentRepository
    from rootmind.indexer import SyncManager

    repo = DocumentRepository(tmp_path / "db" / "s.db")
    manager = SyncManager(repo, {}, AppConfig())
    calls = []
    monkeypatch.setattr(manager, "start_background", lambda **kwargs: calls.append(kwargs) or True)
    asyncio.run(manager._rescan("share"))
    repo.engine.dispose()
    assert calls == [{"connector_name": "share"}]


def test_rescan_minutes_survive_a_save_from_the_app(rest, rlm_home):
    body = rest.get("/config").json()
    assert body["connectors"][0]["rescan_minutes"] is None
    body["connectors"][0]["rescan_minutes"] = 10
    assert rest.post("/config", json=body).status_code == 200
    body = rest.get("/config").json()
    assert body["connectors"][0]["rescan_minutes"] == 10
    # The app sends the folders without the field: the setting stays.
    for connector in body["connectors"]:
        connector.pop("rescan_minutes")
    rest.post("/config", json=body)
    assert load_config(rlm_home / "config.yaml").connectors[0].rescan_minutes == 10
    # 0 removes it.
    body["connectors"][0]["rescan_minutes"] = 0
    rest.post("/config", json=body)
    assert load_config(rlm_home / "config.yaml").connectors[0].rescan_minutes is None
