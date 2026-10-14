"""The service host's runner (``service run``) without the SCM: real apps on port 0.

The SCM glue itself (pywin32 ServiceFramework) runs only under the Service
Control Manager; the service e2e test in CI covers it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

from rootmind import config as config_module
from rootmind.access import UNRESTRICTED
from rootmind.service import host
from rootmind.winaccess import AccessContextError


@pytest.fixture
def restore_process_state(monkeypatch):
    """The runner changes the environment, the working folder and logging; undo it."""
    environ = dict(os.environ)
    monkeypatch.chdir(os.getcwd())
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    stdout, stderr = sys.stdout, sys.stderr
    temp = tempfile.tempdir
    raise_exceptions = logging.raiseExceptions
    yield
    os.environ.clear()
    os.environ.update(environ)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in handlers:
            handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    sys.stdout, sys.stderr = stdout, stderr
    tempfile.tempdir = temp
    logging.raiseExceptions = raise_exceptions
    config_module.reset_config()


@pytest.fixture
def home(tmp_path, restore_process_state, tiny_corpus):
    """A data folder as ``service install`` leaves it."""
    root = tmp_path / "ProgramData" / "RootMind"
    for sub in ("data/db", "logs/backend", "logs/mcp-http", "tmp"):
        (root / sub).mkdir(parents=True)
    config = {
        "connectors": [{"name": "docs", "type": "local", "path": str(tiny_corpus)}],
        "database": {"path": "./data/db/index.db"},
        # No background schedule in tests.
        "indexer": {"sync_interval_hours": 0, "sync_schedule": None},
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def access_factory(seen):
    def factory(user_sid):
        seen.append(user_sid)
        return UNRESTRICTED

    return factory


class Running:
    """A runner serving in a thread."""

    def __init__(self, runner: host.ServiceRunner) -> None:
        self.runner = runner
        self.result: list[bool] = []
        runner.prepare()
        self.url = f"http://127.0.0.1:{runner.port_in_use}"
        self.thread = threading.Thread(target=lambda: self.result.append(runner.serve()))
        self.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("the service did not become healthy")

    def stop(self) -> bool:
        self.runner.stop()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive()
        return self.result[0]


def test_backend_serves_with_the_token_file_and_the_access_policy(home, monkeypatch):
    monkeypatch.setenv("RLM_API_TOKEN", "from-the-environment")  # ignored by the service
    monkeypatch.setenv("RLM_DISABLE_SCHEDULER", "1")
    seen: list[str] = []
    runner = host.ServiceRunner(
        "backend", 0, home=home, user_sid="S-1-5-21-1-2-3-1001", access_factory=access_factory(seen)
    )
    running = Running(runner)
    try:
        token = (home / "data" / "rest_token.txt").read_text().strip()
        assert token and token != "from-the-environment"
        assert seen == ["S-1-5-21-1-2-3-1001"]
        assert httpx.get(f"{running.url}/status").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert httpx.get(f"{running.url}/status", headers=headers).status_code == 200
        assert httpx.get(f"{running.url}/docs", headers=headers).status_code == 404
        assert httpx.get(f"{running.url}/").json()["auth"] == "bearer"
        # The environment the service runs in.
        assert os.environ["RLM_HOME"] == str(home)
        assert Path.cwd() == home
        assert os.environ["TEMP"] == os.environ["TMP"] == str(home / "tmp")
        assert "RLM_API_TOKEN" not in os.environ
        assert "RLM_DISABLE_SCHEDULER" not in os.environ  # the backend indexes
    finally:
        assert running.stop() is True


def test_backend_keeps_an_existing_token(home):
    (home / "data" / "rest_token.txt").write_text("kept-token\n")
    running = Running(
        host.ServiceRunner(
            "backend", 0, home=home, user_sid="S-1-5-21-1", access_factory=access_factory([])
        )
    )
    try:
        headers = {"Authorization": "Bearer kept-token"}
        assert httpx.get(f"{running.url}/status", headers=headers).status_code == 200
    finally:
        running.stop()


def test_mcp_serves_with_its_token_file(home, monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", "from-the-environment")
    (home / "data" / "api_token.txt").write_text("mcp-token")
    running = Running(host.ServiceRunner("mcp-http", 0, home=home))
    try:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        }
        assert httpx.post(f"{running.url}/mcp", json=body).status_code == 401
        wrong = {"Authorization": "Bearer from-the-environment"}
        assert httpx.post(f"{running.url}/mcp", json=body, headers=wrong).status_code == 401
        headers = {"Authorization": "Bearer mcp-token"}
        assert httpx.post(f"{running.url}/mcp", json=body, headers=headers).status_code == 200
        assert "TEMP" not in os.environ or os.environ["TEMP"] != str(home / "tmp")
        assert not (home / "data" / "rest_token.txt").exists()
    finally:
        assert running.stop() is True


def test_mcp_runs_remote_access_to_its_own_port(home):
    """The tunnel forwards to the MCP server's port, and closes with the server."""
    from fake_ngrok import FAKE_AUTHTOKEN, FakeNgrok

    from rootmind import remote_access

    (home / "data" / "api_token.txt").write_text("mcp-token")
    (home / "data" / "ngrok_authtoken.txt").write_text(FAKE_AUTHTOKEN)
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["remote_access"] = {"enabled": True, "domain": "rootmind-test.ngrok-free.app"}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    sdk = FakeNgrok()
    built = []

    def factory(root, port):
        manager = remote_access.RemoteAccessManager(
            home=root, port=port, sdk_loader=sdk.loader(), poll_seconds=0.02
        )
        built.append(manager)
        return manager

    running = Running(host.ServiceRunner("mcp-http", 0, home=home, remote_access_factory=factory))
    status_file = home / "data" / "remote_access.json"

    def status():
        try:
            return json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    try:
        deadline = time.monotonic() + 10
        while (status() or {}).get("state") != "connected" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert status()["url"] == "https://rootmind-test.ngrok-free.app"
        [session] = sdk.sessions
        assert session.listener.target == running.url  # the MCP server, nothing else
        assert built[0].port == running.runner.port_in_use
    finally:
        assert running.stop() is True
    assert session.closed and session.listener.closed
    assert status()["state"] == "stopped"


def test_the_backend_writes_remote_access_files_through_the_layout(home):
    from fake_ngrok import FAKE_AUTHTOKEN

    calls = []

    class Protection:
        def write_secret(self, path, text):
            calls.append(("write_secret", path))
            path.write_text(text, encoding="utf-8")

        def share_status(self, path):
            calls.append(("share_status", path))
            path.write_text("{}", encoding="utf-8")

    runner = host.ServiceRunner(
        "backend",
        0,
        home=home,
        user_sid="S-1-5-21-1",
        access_factory=access_factory([]),
        protection_factory=Protection,
    )
    running = Running(runner)
    try:
        token = (home / "data" / "rest_token.txt").read_text().strip()
        response = httpx.post(
            f"{running.url}/remote-access",
            json={"enabled": True, "authtoken": FAKE_AUTHTOKEN},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert FAKE_AUTHTOKEN not in response.text
    finally:
        running.stop()
    assert calls == [
        ("write_secret", home / "data" / "ngrok_authtoken.txt"),
        ("share_status", home / "data" / "remote_access.json"),
    ]
    assert yaml.safe_load((home / "config.yaml").read_text())["remote_access"]["enabled"] is True


def test_default_remote_access_factories(home):
    from rootmind import remote_access
    from rootmind.service import layout

    manager = host._default_remote_access_factory(home, 3456)
    assert isinstance(manager, remote_access.RemoteAccessManager)
    assert manager.target == "http://127.0.0.1:3456"
    assert manager.config_path == home / "config.yaml"
    assert isinstance(host._default_protection_factory(), layout.DataFileProtection)


def test_mcp_without_a_token_file_stops(home):
    runner = host.ServiceRunner("mcp-http", 0, home=home)
    with pytest.raises(host.ServiceConfigError, match="api_token.txt"):
        runner.prepare()
    assert not (home / "data" / "api_token.txt").exists()  # never generated by the service


def test_empty_token_file_stops(home):
    (home / "data" / "rest_token.txt").write_text("  \n")
    runner = host.ServiceRunner("backend", 0, home=home, user_sid="S-1-5-21-1")
    with pytest.raises(host.ServiceConfigError, match="empty"):
        runner.prepare()


@pytest.mark.parametrize(
    "database",
    ["./data/../elsewhere/index.db", "./index.db", "C:/Windows/index.db", "../outside.db"],
)
def test_database_outside_the_data_folder_stops(home, database):
    config = yaml.safe_load((home / "config.yaml").read_text())
    config["database"]["path"] = database
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    for role in ("backend", "mcp-http"):
        runner = host.ServiceRunner(role, 0, home=home, user_sid="S-1-5-21-1")
        with pytest.raises(host.ServiceConfigError, match="database.path"):
            runner.prepare()


def test_mcp_needs_the_database_in_the_db_folder(home):
    """The MCP account may write (WAL files) only in data\\db: a clear error, not a crash loop."""
    config = yaml.safe_load((home / "config.yaml").read_text())
    config["database"]["path"] = "./data/index.db"
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    runner = host.ServiceRunner("mcp-http", 0, home=home)
    with pytest.raises(host.ServiceConfigError, match=r"run service install again"):
        runner.prepare()


def test_unreadable_config_stops(home):
    (home / "config.yaml").write_text("connectors: [")
    with pytest.raises(host.ServiceConfigError, match="config.yaml"):
        host.ServiceRunner("backend", 0, home=home, user_sid="S-1-5-21-1").prepare()


def test_access_context_failure_stops(home):
    def failing(user_sid):
        raise AccessContextError("no context for this account")

    runner = host.ServiceRunner(
        "backend", 0, home=home, user_sid="S-1-5-21-1", access_factory=failing
    )
    with pytest.raises(host.ServiceConfigError, match="no context"):
        runner.prepare()


def test_backend_needs_the_account(home):
    with pytest.raises(host.ServiceConfigError, match="--user-sid"):
        host.ServiceRunner("backend", 0, home=home).prepare()


def test_a_busy_port_is_not_a_config_error(home):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        runner = host.ServiceRunner(
            "backend",
            blocker.getsockname()[1],
            home=home,
            user_sid="S-1-5-21-1",
            access_factory=access_factory([]),
        )
        with pytest.raises(OSError):
            runner.prepare()
    finally:
        blocker.close()


def test_stop_before_serving(home):
    runner = host.ServiceRunner(
        "backend", 0, home=home, user_sid="S-1-5-21-1", access_factory=access_factory([])
    )
    runner.stop()
    runner.prepare()
    assert runner.serve() is True


def test_unknown_role():
    with pytest.raises(ValueError):
        host.ServiceRunner("indexer", 0, home=Path("."))


def test_logging_goes_to_the_role_log(home):
    handler = host.configure_logging(home / "logs" / "backend", "backend")
    assert handler is not None
    logging.getLogger("rootmind.test").info("hello from the service")
    print("printed line")
    print("error line", file=sys.stderr)
    logging.getLogger("uvicorn.error").info("uvicorn line")
    sys.stdout.flush()
    handler.flush()
    text = (home / "logs" / "backend" / "backend.log").read_text(encoding="utf-8")
    assert "hello from the service" in text
    assert "stdout - INFO - printed line" in text
    assert "stderr - ERROR - error line" in text
    assert "uvicorn line" in text
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 10 * 1024 * 1024 and handler.backupCount == 5


def test_logging_never_creates_the_folder(tmp_path, restore_process_state):
    missing = tmp_path / "ProgramData" / "RootMind" / "logs" / "backend"
    assert host.configure_logging(missing, "backend") is None
    print("discarded")
    assert not (tmp_path / "ProgramData").exists()


@pytest.mark.skipif(
    sys.platform == "win32" or sys.platform.startswith("linux"),
    reason="checks the message of other systems",
)
def test_run_service_outside_windows_and_linux(capsys):
    assert host.run_service("backend", 18000, "S-1-5-21-1") == 2
    assert "Windows and Linux only" in capsys.readouterr().err


# --- Linux: per-user runner and the systemd glue ------------------------------------------------


def test_per_user_backend_needs_no_account_and_reads_unrestricted(home, monkeypatch):
    """Linux: the service is the account; no SID, no access policy, owner-only files."""
    monkeypatch.setenv("RLM_DISABLE_SCHEDULER", "1")

    def no_policy(_sid):
        raise AssertionError("the per-user service builds no access policy")

    runner = host.ServiceRunner("backend", 0, home=home, per_user=True, access_factory=no_policy)
    running = Running(runner)
    try:
        token = (home / "data" / "rest_token.txt").read_text().strip()
        headers = {"Authorization": f"Bearer {token}"}
        assert httpx.get(f"{running.url}/status", headers=headers).status_code == 200
        assert os.environ["TMPDIR"] == str(home / "tmp")
        # The ngrok authtoken is written owner-only (0600 on POSIX).
        saved = httpx.post(
            f"{running.url}/remote-access",
            headers=headers,
            json={"authtoken": "a" * 24},
        )
        assert saved.status_code == 200
        authtoken = home / "data" / "ngrok_authtoken.txt"
        assert authtoken.read_text() == "a" * 24
        if sys.platform != "win32":
            assert authtoken.stat().st_mode & 0o777 == 0o600
    finally:
        assert running.stop() is True


def test_per_user_home_check_stops_the_start(home):
    runner = host.ServiceRunner(
        "backend", 0, home=home, per_user=True, home_check=lambda _path: ["data: owned by uid 0"]
    )
    with pytest.raises(host.ServiceConfigError) as raised:
        runner.prepare()
    assert "owned by uid 0" in str(raised.value)
    assert runner.sock is None


class FakeRunner:
    def __init__(self, role, port, *, home, per_user, home_check, fail=None, serve=True):
        self.role, self.port, self.home = role, port, home
        assert per_user is True
        self.home_check = home_check
        self.fail = fail
        self.serve_result = serve
        self.port_in_use = 18555
        self.stopped = False

    def prepare(self):
        if self.fail is not None:
            raise self.fail

    def serve(self):
        return self.serve_result

    def stop(self):
        self.stopped = True


def run_linux(tmp_path, monkeypatch, **runner_options):
    import signal

    monkeypatch.setattr(signal, "signal", lambda *_a: None)
    sent = []
    created = []

    def factory(*args, **kwargs):
        created.append(FakeRunner(*args, **kwargs, **runner_options))
        return created[0]

    code = host.run_linux_service(
        "backend",
        18555,
        home=tmp_path,
        runner_factory=factory,
        notify=lambda message: sent.append(message) or True,
        configure=lambda *_a: None,
    )
    return code, sent, created[0]


@pytest.mark.skipif(sys.platform == "win32", reason="Linux service host")
def test_linux_service_reports_ready_and_stops_cleanly(tmp_path, monkeypatch):
    code, sent, runner = run_linux(tmp_path, monkeypatch)
    assert code == 0
    assert sent[0].startswith("READY=1\nMAINPID=")
    assert "127.0.0.1:18555" in sent[0]
    assert sent[1] == "STOPPING=1"
    # The start check is the data folder check for this account.
    assert runner.home_check(tmp_path / "missing")


@pytest.mark.skipif(sys.platform == "win32", reason="Linux service host")
def test_linux_service_config_error_is_not_restarted(tmp_path, monkeypatch):
    code, sent, _runner = run_linux(tmp_path, monkeypatch, fail=host.ServiceConfigError("bad"))
    assert code == 2 and sent == []
    code, sent, _runner = run_linux(tmp_path, monkeypatch, fail=OSError("port in use"))
    assert code == 1 and sent == []


@pytest.mark.skipif(sys.platform == "win32", reason="Linux service host")
def test_linux_service_ending_on_its_own_is_restarted(tmp_path, monkeypatch):
    code, _sent, _runner = run_linux(tmp_path, monkeypatch, serve=False)
    assert code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX datagram sockets")
def test_sd_notify_sends_to_the_socket(tmp_path):
    import socket

    path = tmp_path / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        assert host.sd_notify("READY=1", {"NOTIFY_SOCKET": str(path)}) is True
        assert server.recv(100) == b"READY=1"
    assert host.sd_notify("READY=1", {}) is False
    assert host.sd_notify("READY=1", {"NOTIFY_SOCKET": str(tmp_path / "gone")}) is False


def test_linux_home_is_next_to_the_program(monkeypatch):
    exe = "/home/me/.local/share/rootmind/runtime/2.3.0-0a1b2c3d/rootmind-backend"
    monkeypatch.setattr(sys, "executable", exe)
    assert host.linux_home().as_posix() == "/home/me/.local/share/rootmind"
