"""The ``rootmind`` command line: bind resolution, dispatch and the API process."""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import httpx
import pytest
import yaml
from helpers import capture_serving

import rootmind
from rootmind import config as config_module
from rootmind import main as main_module
from rootmind.config import AppConfig
from rootmind.main import main, resolve_bind

CHILD_TOKEN = "child-test-token"  # pragma: allowlist secret


def _write_config(data: dict) -> Path:
    path = config_module.app_home() / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --- version and help --------------------------------------------------------------------


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    line = capsys.readouterr().out.strip()
    assert line == f"rootmind {rootmind.__version__}"
    assert line.split()[-1] == rootmind.__version__


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for command in (
        "api",
        "index",
        "rebuild-trigrams",
        "mcp",
        "mcp-http",
        "service",
        "status",
        "folders",
        "search",
        "ask",
        "config",
        "remote-access",
    ):
        assert command in out


def test_api_help_lists_the_options(capsys):
    with pytest.raises(SystemExit):
        main(["api", "--help"])
    out = capsys.readouterr().out
    for option in ("--host", "--port", "--require-token"):
        assert option in out
    # The desktop app's built-in backend is gone, and with it these options.
    for option in ("--exit-with-parent", "--import-config"):
        assert option not in out


@pytest.mark.parametrize("option", [["--exit-with-parent"], ["--import-config", "x.yaml"]])
def test_removed_api_options_are_refused(monkeypatch, option, capsys):
    seen = capture_serving(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["api", *option])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert "bind" not in seen


# --- bind resolution (bug a) ---------------------------------------------------------------


def test_config_port_is_used(monkeypatch):
    _write_config({"api": {"port": 18123}})
    seen = capture_serving(monkeypatch)

    main(["api"])

    assert seen["bind"] == ("127.0.0.1", 18123)


def test_config_host_is_used(monkeypatch):
    _write_config({"api": {"host": "localhost", "port": 18123}})
    seen = capture_serving(monkeypatch)
    main(["api"])
    assert seen["bind"] == ("localhost", 18123)


def test_bare_command_uses_the_same_resolver(monkeypatch):
    _write_config({"api": {"port": 18123}})
    seen = capture_serving(monkeypatch)

    main([])

    assert seen["bind"] == ("127.0.0.1", 18123)


@pytest.mark.parametrize(
    ("argv", "env", "expected"),
    [
        (["api"], {}, ("127.0.0.1", 18123)),
        (["api"], {"RLM_PORT": "18124"}, ("127.0.0.1", 18124)),
        (["api", "--port", "18125"], {"RLM_PORT": "18124"}, ("127.0.0.1", 18125)),
        (["api", "--port", "0"], {"RLM_PORT": "18124"}, ("127.0.0.1", 0)),
        (["api"], {"RLM_PORT": "0"}, ("127.0.0.1", 0)),
        (["api"], {"RLM_HOST": "localhost"}, ("localhost", 18123)),
        (["api", "--host", "127.0.0.1"], {"RLM_HOST": "localhost"}, ("127.0.0.1", 18123)),
        (["api"], {"RLM_HOST": " ", "RLM_PORT": " "}, ("127.0.0.1", 18123)),
    ],
)
def test_flag_beats_env_beats_config(monkeypatch, argv, env, expected):
    _write_config({"api": {"port": 18123}})
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    seen = capture_serving(monkeypatch)

    main(argv)

    assert seen["bind"] == expected


def test_defaults_without_config_or_env():
    assert resolve_bind(None, None, AppConfig()) == ("127.0.0.1", 8000)
    assert resolve_bind("::1", 0, AppConfig()) == ("::1", 0)


@pytest.mark.parametrize("value", ["abc", "70000", "-1", "80.5"])
def test_invalid_env_port_exits_2(monkeypatch, value, caplog):
    monkeypatch.setenv("RLM_PORT", value)
    seen = capture_serving(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["api"])
    assert exc.value.code == 2
    assert "RLM_PORT" in caplog.text
    assert "bind" not in seen


@pytest.mark.parametrize("value", ["abc", "70000", "-1"])
def test_invalid_port_flag_exits_2(monkeypatch, value, capsys):
    capture_serving(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["api", "--port", value])
    assert exc.value.code == 2
    assert "--port" in capsys.readouterr().err


def test_busy_port_exits_1(monkeypatch, caplog):
    def busy(host, port):
        raise OSError("address in use")

    monkeypatch.setattr(main_module, "bind_socket", busy)
    with pytest.raises(SystemExit) as exc:
        main(["api", "--port", "18123"])
    assert exc.value.code == 1
    assert "Cannot listen on 127.0.0.1:18123" in caplog.text


# --- socket ------------------------------------------------------------------------------


def test_bind_socket_port_0_is_exclusive():
    sock = main_module.bind_socket("127.0.0.1", 0)
    try:
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1"
        assert 0 < port < 65536
        if sys.platform == "win32":
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
            sock.listen()
            with pytest.raises(OSError):
                main_module.bind_socket("127.0.0.1", port)
    finally:
        sock.close()


# --- dispatch of mcp, mcp-http and service ------------------------------------------------


@pytest.fixture
def no_setup(monkeypatch):
    """Fail if logging or the config is set up (forwarded commands must not do either)."""

    def refuse(*args, **kwargs):
        raise AssertionError("called before dispatch")

    monkeypatch.setattr(main_module, "setup_logging", refuse)
    monkeypatch.setattr(main_module, "init_config", refuse)


@pytest.fixture
def forwarded(monkeypatch, no_setup):
    """Record the argv that the mcp and mcp-http entry points receive."""
    from rootmind import mcp_http, mcp_stdio

    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(mcp_stdio, "main", lambda argv: calls.append(("mcp", argv)))
    monkeypatch.setattr(mcp_http, "main", lambda argv: calls.append(("mcp-http", argv)))
    return calls


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["mcp"], ("mcp", [])),
        (["mcp", "--config", "x.yaml"], ("mcp", ["--config", "x.yaml"])),
        (["mcp-http", "--port", "3999", "--index"], ("mcp-http", ["--port", "3999", "--index"])),
        (["mcp-http", "--help"], ("mcp-http", ["--help"])),
        (["--config", "x.yaml", "mcp"], ("mcp", ["--config", "x.yaml"])),
        (
            ["-c", "x.yaml", "-d", "mcp-http", "--port", "1"],
            ("mcp-http", ["--config", "x.yaml", "--port", "1"]),
        ),
        (["--config=x.yaml", "mcp"], ("mcp", ["--config", "x.yaml"])),
        (["-cx.yaml", "mcp"], ("mcp", ["--config", "x.yaml"])),
        (["--debug", "mcp"], ("mcp", [])),
    ],
)
def test_mcp_commands_are_forwarded(forwarded, argv, expected):
    main(argv)
    assert forwarded == [expected]


@pytest.fixture
def fake_service(monkeypatch):
    """A stand-in for ``rootmind.service.cli`` (provided by the service package)."""
    module = types.ModuleType("rootmind.service.cli")
    module.calls = []
    module.result = 0

    def fake_main(argv):
        module.calls.append(argv)
        return module.result

    module.main = fake_main
    monkeypatch.setitem(sys.modules, "rootmind.service.cli", module)
    return module


def test_service_is_forwarded(fake_service, no_setup):
    main(["service", "status", "--json"])
    assert fake_service.calls == [["status", "--json"]]


def test_service_exit_code_is_passed_on(fake_service, no_setup):
    fake_service.result = 5
    with pytest.raises(SystemExit) as exc:
        main(["service", "install"])
    assert exc.value.code == 5


def test_service_refuses_a_config_option(fake_service, no_setup, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--config", "x.yaml", "service", "status"])
    assert exc.value.code == 2
    assert fake_service.calls == []
    assert "--config" in capsys.readouterr().err


def test_service_missing_from_the_installation(monkeypatch, no_setup, capsys):
    monkeypatch.setitem(sys.modules, "rootmind.service.cli", None)
    with pytest.raises(SystemExit) as exc:
        main(["service", "status"])
    assert exc.value.code == 2
    assert "not part of this installation" in capsys.readouterr().err


# --- the real process ----------------------------------------------------------------------


def _drain(stream, lines: queue.Queue) -> None:
    for raw in iter(stream.readline, b""):
        lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
    lines.put(None)


class Child:
    """``python -m rootmind.main ...`` with piped stdio, read by background threads."""

    def __init__(self, args: list[str], home: Path, env: dict[str, str] | None = None):
        child_env = dict(os.environ)
        child_env.update(RLM_HOME=str(home), RLM_DISABLE_SCHEDULER="1", PYTHONIOENCODING="utf-8")
        child_env.update(env or {})
        self.process = subprocess.Popen(  # noqa: S603 (fixed arguments, this interpreter)
            [sys.executable, "-m", "rootmind.main", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=home.parent,
            env=child_env,
        )
        self.stdout: queue.Queue = queue.Queue()
        self.stderr: queue.Queue = queue.Queue()
        self.out_lines: list[str] = []
        for stream, target in (
            (self.process.stdout, self.stdout),
            (self.process.stderr, self.stderr),
        ):
            threading.Thread(target=_drain, args=(stream, target), daemon=True).start()

    def next_line(self, timeout: float) -> str | None:
        line = self.stdout.get(timeout=timeout)
        if line is not None:
            self.out_lines.append(line)
        return line

    def wait_for(self, predicate, timeout: float = 90) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out; stdout so far: {self.out_lines}")
            try:
                line = self.next_line(remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(
                    f"process ended early ({self.process.wait()}); stdout: {self.out_lines}; "
                    f"stderr: {self.stderr_text()}"
                )
            if predicate(line):
                return line

    def rest_of_stdout(self) -> list[str]:
        while True:
            try:
                line = self.next_line(timeout=10)
            except queue.Empty:
                break
            if line is None:
                break
        return self.out_lines

    def stderr_text(self) -> str:
        parts = []
        while not self.stderr.empty():
            part = self.stderr.get_nowait()
            if part is not None:
                parts.append(part)
        return "\n".join(parts)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture
def child_home(tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


_LISTENING = re.compile(r"RootMind API listening on http://127\.0\.0\.1:(\d+)$")


def _listening_port(child: Child) -> int:
    """The port from the API's log line (``--port 0`` lets the system choose it)."""
    line = child.wait_for(lambda text: _LISTENING.search(text) is not None)
    port = int(_LISTENING.search(line)[1])
    assert 0 < port < 65536
    return port


def _wait_healthy(base: str, client: httpx.Client, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if client.get(f"{base}/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        assert time.monotonic() < deadline, f"{base} did not become healthy"
        time.sleep(0.1)


def test_api_on_a_free_port_with_a_token(child_home):
    child = Child(["api", "--port", "0"], child_home, env={"RLM_API_TOKEN": CHILD_TOKEN})
    try:
        port = _listening_port(child)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=10) as client:
            _wait_healthy(base, client)
            assert client.get(f"{base}/health").json() == {"status": "healthy"}
            assert client.get(f"{base}/").json()["auth"] == "bearer"
            assert client.get(f"{base}/status").status_code == 401
            authorized = {"Authorization": f"Bearer {CHILD_TOKEN}"}
            assert client.get(f"{base}/status", headers=authorized).status_code == 200
        child.close()
        stdout = child.rest_of_stdout()
        # There is no ready line any more (it was for the desktop app's built-in backend).
        assert not any("ROOTMIND_LISTENING" in line for line in stdout)
        assert CHILD_TOKEN not in "\n".join(stdout) + child.stderr_text()
    finally:
        child.close()


def test_mcp_dispatch_keeps_stdout_json_only(child_home):
    child = Child(["mcp"], child_home)

    def send(message: dict) -> None:
        child.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        child.process.stdin.flush()

    def response(request_id: int) -> dict:
        line = child.wait_for(lambda text: True, timeout=90)
        data = json.loads(line)
        assert data.get("id") == request_id, data
        return data

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "rootmind-tests", "version": "0"},
                },
            }
        )
        assert response(1)["result"]["serverInfo"]["name"] == "rootmind"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(response(2)["result"]["tools"]) == 5

        child.process.stdin.close()
        child.process.wait(timeout=30)
        for line in child.rest_of_stdout():
            json.loads(line)
    finally:
        child.close()


def test_no_token_in_logs_for_require_token(child_home):
    """The generated REST token is written to its file, never to stdout or stderr."""
    child = Child(["api", "--port", "0", "--require-token"], child_home)
    try:
        port = _listening_port(child)
        token = (child_home / "data" / "rest_token.txt").read_text(encoding="utf-8")
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=10) as client:
            _wait_healthy(base, client)
            response = client.get(f"{base}/status", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
        child.close()
        output = "\n".join(child.rest_of_stdout()) + child.stderr_text()
        assert token not in output
        assert "fingerprint sha256:" in output
    finally:
        child.close()
