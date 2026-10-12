"""The service host's runner (``service run``) without the SCM: real apps on port 0.

The SCM glue itself (pywin32 ServiceFramework) runs only under the Service
Control Manager; the service e2e test in CI covers it.
"""

from __future__ import annotations

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


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows message")
def test_run_service_outside_windows(capsys):
    assert host.run_service("backend", 18000, "S-1-5-21-1") == 2
    assert "Windows only" in capsys.readouterr().err
