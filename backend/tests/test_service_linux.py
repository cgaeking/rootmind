"""service install / uninstall / status on Linux (systemd user units), with fake adapters.

The fake machine (:class:`FakeLinuxEnv`) stands in for ``systemctl --user``,
the account, the program copy and the HTTP checks. Config, token and unit
files are real files in a temporary home, so the logic runs on every OS; the
file modes themselves are tested in ``test_service_linux_layout.py``.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from rootmind.auth import PROOF_PATH, token_proof
from rootmind.service import ExitCode, cli, linux, linux_layout, systemd

VERSION = "2.3.0"
APP_EXE = "/usr/lib/RootMind/backend/rootmind-backend"
BACKEND = "rootmind-backend.service"
MCP = "rootmind-mcp-http.service"


class FakeSystemctl:
    """``systemctl --user`` in memory; a unit is loaded while its file exists."""

    def __init__(self, env: FakeLinuxEnv) -> None:
        self.env = env
        self.active: dict[str, int] = {}  # unit -> main PID
        self.failed: set[str] = set()
        self.enabled: set[str] = set()
        self.calls: list[tuple] = []
        self.unavailable: str | None = None
        self.fail_start: set[str] = set()
        self.foreign_fragments: dict[str, str] = {}
        self.next_pid = 4000

    def check(self):
        if self.unavailable is not None:
            raise systemd.UserManagerUnavailableError(self.unavailable)

    def show(self, unit):
        path = self.env.unit_dir() / unit
        if unit in self.foreign_fragments:
            return systemd.UnitState(
                unit, "loaded", "inactive", "dead", 0, 0, "success", self.foreign_fragments[unit]
            )
        if not path.exists():
            return systemd.UnitState(unit, "not-found", "inactive", "dead")
        if unit in self.active:
            return systemd.UnitState(
                unit, "loaded", "active", "running", self.active[unit], 0, "success", str(path)
            )
        if unit in self.failed:
            return systemd.UnitState(
                unit, "loaded", "failed", "failed", 0, 2, "exit-code", str(path)
            )
        return systemd.UnitState(unit, "loaded", "inactive", "dead", 0, 0, "success", str(path))

    def daemon_reload(self):
        self.calls.append(("daemon-reload",))

    def enable(self, *units):
        self.calls.append(("enable", *units))
        self.enabled.update(units)

    def disable(self, *units):
        self.calls.append(("disable", *units))
        self.enabled.difference_update(units)

    def start(self, unit):
        self.calls.append(("start", unit))
        if unit in self.fail_start:
            self.failed.add(unit)
            raise systemd.SystemctlError(f"systemctl --user start {unit}: Job failed")
        self.next_pid += 1
        self.active[unit] = self.next_pid

    def stop(self, unit):
        self.calls.append(("stop", unit))
        self.active.pop(unit, None)

    def reset_failed(self, unit):
        self.calls.append(("reset-failed", unit))
        self.failed.discard(unit)


class FakeLinuxEnv:
    def __init__(self, tmp_path: Path) -> None:
        self.version = VERSION
        self.executable = APP_EXE
        self.linux = True
        self.release = True
        self.user_id = 1000
        self.home = tmp_path / "home"
        self.root = self.home / ".local" / "share" / "rootmind"
        self.manager = FakeSystemctl(self)
        self.events: list[tuple] = []
        self.http_log: list[tuple] = []
        self.busy: set[int] = set()
        self.data_refusal: linux_layout.LayoutError | None = None
        self.data_problem_list: list[str] = []
        self.source_problems: list[str] = []
        self.squatters: dict[int, int] = {}
        self.unhealthy: set[str] = set()
        self.copies = 0
        self.linger: bool | None = False
        self.clock = 0.0

    def is_linux(self):
        return self.linux

    def is_windows(self):
        return False

    def is_release_build(self):
        return self.release

    def uid(self):
        return self.user_id

    def linger_enabled(self):
        return self.linger

    def data_root(self):
        return self.root

    def unit_dir(self):
        return self.home / ".config" / "systemd" / "user"

    def prepare_data(self, root):
        self.events.append(("prepare_data",))
        if self.data_refusal is not None:
            raise self.data_refusal
        created = not root.exists()
        for sub in ("", *linux_layout.DATA_SUBFOLDERS, "runtime"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        return created

    def normalize_data(self, root):
        self.events.append(("normalize_data",))

    def data_problems(self, root):
        return list(self.data_problem_list)

    def remove_data(self, root):
        self.events.append(("remove_data",))
        import shutil

        shutil.rmtree(root)

    def program_source_problems(self, folder):
        self.events.append(("check_source", folder))
        return list(self.source_problems)

    def runtime_folder(self, n: int) -> str:
        return f"{self.root.as_posix()}/runtime/{VERSION}-{n:08x}"

    def install_program(self, source_dir, version):
        self.copies += 1
        folder = self.runtime_folder(self.copies)
        self.events.append(("install_program", source_dir, folder))
        Path(folder).mkdir(parents=True)
        return folder

    def remove_old_programs(self, keep):
        self.events.append(("remove_old_programs", keep))
        return []

    def remove_all_programs(self, root):
        self.events.append(("remove_all_programs",))
        return []

    def systemctl(self):
        return self.manager

    def wait_process_exit(self, pid, timeout):
        self.events.append(("wait_process_exit", pid))
        return True

    def can_bind(self, port):
        return port not in self.busy

    def _unit_on(self, port):
        for role in linux.ROLES:
            path = systemd.unit_path(self.unit_dir(), role)
            if not path.exists():
                continue
            unit = systemd.parse_unit(path.read_text())
            name = {"backend": BACKEND, "mcp-http": MCP}[role]
            if unit is not None and unit.port == port and name in self.manager.active:
                return name
        return None

    def port_owned_by(self, port, pid):
        self.events.append(("port_owned_by", port, pid))
        if port in self.squatters:
            return self.squatters[port] == pid
        name = self._unit_on(port)
        return name is not None and self.manager.active[name] == pid

    def http(self, method, url, *, token=None, body=None):
        parts = urlsplit(url)
        self.http_log.append((method, parts.path, token is not None))
        if parts.port in self.squatters:
            # Another program answers on the port, but cannot prove the token.
            if parts.path == PROOF_PATH:
                return 200, {"proof": "0" * 64}
            return 200, {"status": "healthy"}
        name = self._unit_on(parts.port)
        if name is None or name in self.unhealthy:
            return 0, None
        if parts.path == "/health":
            return 200, {"status": "healthy"}
        if parts.path == PROOF_PATH:
            file = "rest_token.txt" if name == BACKEND else "api_token.txt"
            known = (self.root / "data" / file).read_text().strip().encode()
            return 200, {"proof": token_proof(known, parse_qs(parts.query)["nonce"][0])}
        if name == BACKEND:
            expected = (self.root / "data" / "rest_token.txt").read_text().strip()
            if parts.path == "/":
                return 200, {"name": "RootMind API", "version": self.version, "auth": "bearer"}
            return (200, {}) if token == expected else (401, {})
        expected = (self.root / "data" / "api_token.txt").read_text().strip()
        assert body["method"] == "initialize"
        return (200, {}) if token == expected else (401, {})

    def sleep(self, seconds):
        self.clock += seconds

    def monotonic(self):
        return self.clock

    @staticmethod
    def same_path(a, b):
        return a == b


@pytest.fixture
def env(tmp_path):
    return FakeLinuxEnv(tmp_path)


def run_install(env, **options):
    out, err = [], []
    code = linux.install(env, linux.InstallOptions(**options), out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def run_uninstall(env, purge=False):
    out, err = [], []
    code = linux.uninstall(env, purge=purge, out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def unit(env, role) -> systemd.Unit | None:
    path = systemd.unit_path(env.unit_dir(), role)
    return systemd.parse_unit(path.read_text()) if path.exists() else None


def install_existing(env, *, mcp_port=3000, port=8000, folder=f"{VERSION}-11111111"):
    """Units as an earlier install left them (running)."""
    exe = f"{env.root.as_posix()}/runtime/{folder}/rootmind-backend"
    Path(exe).parent.mkdir(parents=True)
    env.unit_dir().mkdir(parents=True)
    systemd.unit_path(env.unit_dir(), "backend").write_text(
        systemd.build_unit(exe, "backend", port)
    )
    env.manager.active[BACKEND] = 100
    if mcp_port is not None:
        systemd.unit_path(env.unit_dir(), "mcp-http").write_text(
            systemd.build_unit(exe, "mcp-http", mcp_port)
        )
        env.manager.active[MCP] = 101
    data = env.root / "data"
    data.mkdir(parents=True)
    (env.root / "logs").mkdir()
    (data / "rest_token.txt").write_text("rest-token-from-before")
    if mcp_port is not None:
        (data / "api_token.txt").write_text("mcp-token-from-before")
    (env.root / "config.yaml").write_text("database:\n  path: ./data/db/index.db\n")
    return exe


# --- fresh install ---------------------------------------------------------------------------


def test_fresh_install_writes_and_starts_both_units(env):
    code, out, err = run_install(env)
    assert code == 0, err
    backend, mcp = unit(env, "backend"), unit(env, "mcp-http")
    exe = f"{env.runtime_folder(1)}/rootmind-backend"
    assert (backend.exe, backend.port, mcp.exe, mcp.port) == (exe, 8000, exe, 3000)
    assert env.manager.enabled == {BACKEND, MCP}
    assert set(env.manager.active) == {BACKEND, MCP}
    assert ("daemon-reload",) in env.manager.calls
    assert ("install_program", "/usr/lib/RootMind/backend", env.runtime_folder(1)) in env.events
    assert ("check_source", "/usr/lib/RootMind/backend") in env.events
    assert ("remove_old_programs", env.runtime_folder(1)) in env.events
    assert "installed (version 2.3.0)" in out
    assert "http://127.0.0.1:3000/mcp" in out
    assert "loginctl enable-linger" in out


def test_fresh_install_writes_config_tokens_discovery_and_log(env):
    assert run_install(env)[0] == 0
    assert yaml.safe_load((env.root / "config.yaml").read_text()) == {
        "database": {"path": "./data/db/index.db"}
    }
    rest = (env.root / "data" / "rest_token.txt").read_text()
    mcp = (env.root / "data" / "api_token.txt").read_text()
    assert len(rest) >= 40 and len(mcp) >= 40 and rest != mcp
    assert json.loads((env.root / "data" / "remote_access.json").read_text())["state"] == "off"
    discovery = json.loads((env.root / "service.json").read_text())
    assert discovery == {
        "executable": f"{env.runtime_folder(1)}/rootmind-backend",
        "version": VERSION,
    }
    log = (env.root / "logs" / "install.log").read_text()
    assert "install: exit 0" in log
    for token in (rest, mcp):
        assert token not in log
        assert token not in (env.root / "service.json").read_text()
        for role in linux.ROLES:
            assert token not in systemd.unit_path(env.unit_dir(), role).read_text()


def test_install_checks_health_listener_and_authentication(env):
    assert run_install(env)[0] == 0
    assert ("GET", "/health", False) in env.http_log
    assert ("GET", "/status", False) in env.http_log and ("GET", "/status", True) in env.http_log
    assert ("POST", "/mcp", False) in env.http_log and ("POST", "/mcp", True) in env.http_log
    # Each service proves its token before the token is sent.
    assert [p for _m, p, _t in env.http_log].count(PROOF_PATH) == 2
    assert ("GET", PROOF_PATH, False) in env.http_log


def test_no_mcp_http(env):
    code, out, err = run_install(env, mcp=False)
    assert code == 0, err
    assert unit(env, "mcp-http") is None
    assert env.manager.enabled == {BACKEND}
    assert not (env.root / "data" / "api_token.txt").exists()
    assert "/mcp" not in out


def test_no_start_enables_without_starting(env):
    code, out, _err = run_install(env, start=False)
    assert code == 0
    assert env.manager.enabled == {BACKEND, MCP}
    assert not env.manager.active and not env.http_log
    assert "not started" in out


def test_mcp_token_from_a_file(env, tmp_path):
    source = tmp_path / "token.txt"
    source.write_text("given-token\n")
    assert run_install(env, mcp_token_from=source)[0] == 0
    assert (env.root / "data" / "api_token.txt").read_text() == "given-token"


def test_no_linger_hint_when_it_is_on(env):
    env.linger = True
    assert "enable-linger" not in run_install(env)[1]


# --- ports ------------------------------------------------------------------------------------


def test_default_port_is_strict(env):
    env.busy = {8000}
    code, _out, err = run_install(env)
    assert code == ExitCode.PORT_BUSY
    assert "Port 8000" in err
    assert not env.unit_dir().exists() and not env.root.exists()


def test_auto_ports_fall_back(env):
    env.busy = {8000, 18300, 3000}
    assert run_install(env, port="auto", mcp_port="auto")[0] == 0
    assert (unit(env, "backend").port, unit(env, "mcp-http").port) == (18301, 18400)


# --- refusals before any change ------------------------------------------------------------------


def nothing_changed(env):
    return not env.manager.calls and not env.root.exists() and not env.unit_dir().exists()


@pytest.mark.parametrize(
    ("setup", "code", "message"),
    [
        (lambda e: setattr(e, "linux", False), ExitCode.USAGE, "Windows and Linux only"),
        (
            lambda e: setattr(e, "release", False),
            ExitCode.USAGE,
            "rootmind-backend-linux-x64.tar.gz",
        ),
        (lambda e: setattr(e, "user_id", 0), ExitCode.ACCOUNT_MISMATCH, "not as root"),
        (
            lambda e: setattr(e.manager, "unavailable", "Failed to connect to bus"),
            ExitCode.NO_USER_MANAGER,
            "user manager",
        ),
        (
            lambda e: setattr(e, "source_problems", ["/x: writable by the group or others"]),
            ExitCode.UNSAFE_PROGRAM_FOLDER,
            "writable by the group or others",
        ),
    ],
)
def test_refusals(env, setup, code, message):
    setup(env)
    result, _out, err = run_install(env)
    assert result == code
    assert message in err
    assert nothing_changed(env)


def test_user_sid_is_refused(env):
    code, _out, err = run_install(env, user_sid="S-1-5-21-1-2-3-1001")
    assert code == ExitCode.USAGE and "--user-sid is for Windows" in err
    assert nothing_changed(env)


def test_release_url_names_the_version(env):
    env.release = False
    _code, _out, err = run_install(env)
    assert "/download/v2.3.0/rootmind-backend-linux-x64.tar.gz" in err


def test_a_foreign_unit_file_is_left_alone(env):
    env.unit_dir().mkdir(parents=True)
    path = env.unit_dir() / BACKEND
    path.write_text("[Service]\nExecStart=/usr/bin/other\n")
    code, _out, err = run_install(env)
    assert code == ExitCode.FOREIGN_SERVICE
    assert BACKEND in err
    assert path.read_text() == "[Service]\nExecStart=/usr/bin/other\n"
    assert not env.root.exists()


def test_our_marker_with_a_program_outside_the_data_folder_is_foreign(env):
    env.unit_dir().mkdir(parents=True)
    (env.unit_dir() / BACKEND).write_text(
        systemd.build_unit("/opt/elsewhere/rootmind-backend", "backend", 8000)
    )
    assert run_install(env)[0] == ExitCode.FOREIGN_SERVICE


def test_a_unit_loaded_from_elsewhere_is_foreign(env):
    env.manager.foreign_fragments[MCP] = "/etc/systemd/user/rootmind-mcp-http.service"
    code, _out, err = run_install(env)
    assert code == ExitCode.FOREIGN_SERVICE
    assert "/etc/systemd/user/rootmind-mcp-http.service" in err


def test_data_folder_refused(env):
    env.data_refusal = linux_layout.LayoutError(
        linux_layout.NOT_PROTECTED, ["data: owned by uid 0"]
    )
    code, _out, err = run_install(env)
    assert code == ExitCode.DATA_FOLDER_REFUSED
    assert "owned by uid 0" in err
    assert not env.unit_dir().exists()


# --- updates -----------------------------------------------------------------------------------


def test_update_keeps_ports_tokens_and_mcp_and_switches_the_program(env):
    old = install_existing(env, port=18301, mcp_port=18400)
    code, out, err = run_install(env, port="auto", mcp_port="auto")
    assert code == 0, err
    assert (unit(env, "backend").port, unit(env, "mcp-http").port) == (18301, 18400)
    assert unit(env, "backend").exe == f"{env.runtime_folder(1)}/rootmind-backend" != old
    assert (env.root / "data" / "rest_token.txt").read_text() == "rest-token-from-before"
    assert (env.root / "data" / "api_token.txt").read_text() == "mcp-token-from-before"
    stops = [c for c in env.manager.calls if c[0] == "stop"]
    assert stops == [("stop", BACKEND), ("stop", MCP)]
    assert ("wait_process_exit", 100) in env.events and ("wait_process_exit", 101) in env.events
    assert "updated (version 2.3.0)" in out


def test_update_keeps_mcp_off(env):
    install_existing(env, mcp_port=None)
    assert run_install(env)[0] == 0
    assert unit(env, "mcp-http") is None


def test_update_from_the_registered_copy_copies_nothing(env):
    env.executable = install_existing(env)
    code, _out, err = run_install(env, mcp=False)
    assert code == 0, err
    assert env.copies == 0
    assert not any(e[0] == "check_source" for e in env.events)
    assert unit(env, "mcp-http") is None
    assert ("disable", MCP) in env.manager.calls
    assert MCP not in env.manager.active


def test_turning_mcp_off_turns_remote_access_off(env):
    install_existing(env)
    (env.root / "config.yaml").write_text(
        "database:\n  path: ./data/db/index.db\nremote_access:\n  enabled: true\n"
    )
    code, out, _err = run_install(env, mcp=False)
    assert code == 0
    assert yaml.safe_load((env.root / "config.yaml").read_text())["remote_access"] == {
        "enabled": False
    }
    assert "Remote access was turned off" in out


def test_a_failure_before_the_switch_restarts_the_old_units(env):
    install_existing(env)
    env.data_refusal = linux_layout.LayoutError("refused", [])
    code, _out, _err = run_install(env)
    assert code == ExitCode.DATA_FOLDER_REFUSED
    assert set(env.manager.active) == {BACKEND, MCP}
    assert unit(env, "backend").exe.endswith(f"{VERSION}-11111111/rootmind-backend")


def test_a_database_outside_data_db_is_moved(env):
    install_existing(env)
    (env.root / "config.yaml").write_text("database:\n  path: ./data/index.db\n")
    code, out, err = run_install(env)
    assert code == 0, err
    assert yaml.safe_load((env.root / "config.yaml").read_text())["database"] == {
        "path": "./data/db/index.db"
    }
    assert "rebuilt" in out


# --- start and health ----------------------------------------------------------------------------


def test_a_unit_that_does_not_start_fails_with_7(env):
    env.manager.fail_start.add(BACKEND)
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "journalctl --user -u rootmind-backend.service" in err


def test_no_answer_fails_with_7(env):
    env.unhealthy.add(MCP)
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "did not answer" in err


def test_no_token_goes_to_a_squatter(env):
    env.squatters[8000] = 666
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "127.0.0.1:8000 could not prove that it is" in err
    assert not any(token for _m, _p, token in env.http_log)


# --- uninstall ---------------------------------------------------------------------------------------


def test_uninstall_removes_units_programs_and_the_rest_token(env):
    install_existing(env)
    (env.root / "service.json").write_text("{}")
    code, out, err = run_uninstall(env)
    assert code == 0, err
    assert not (env.unit_dir() / BACKEND).exists() and not (env.unit_dir() / MCP).exists()
    assert ("disable", BACKEND, MCP) in env.manager.calls
    assert not env.manager.active
    assert ("remove_all_programs",) in env.events
    assert not (env.root / "data" / "rest_token.txt").exists()
    assert not (env.root / "service.json").exists()
    assert (env.root / "data" / "api_token.txt").exists()
    assert "Removed rootmind-backend.service, rootmind-mcp-http.service." in out
    assert "MCP token was kept" in out


def test_uninstall_purge_deletes_the_data(env):
    install_existing(env)
    assert run_uninstall(env, purge=True)[0] == 0
    assert not env.root.exists()


def test_uninstall_leaves_an_unprotected_folder_alone(env):
    install_existing(env)
    env.data_problem_list = ["data: writable by the group or others"]
    code, _out, err = run_uninstall(env, purge=True)
    assert code == 0
    assert "left unchanged" in err
    assert (env.root / "data" / "rest_token.txt").exists()


def test_uninstall_when_nothing_is_installed(env):
    code, out, _err = run_uninstall(env)
    assert code == 0
    assert "nothing to do" in out


def test_uninstall_refuses_a_foreign_unit(env):
    env.unit_dir().mkdir(parents=True)
    (env.unit_dir() / MCP).write_text("[Service]\n")
    assert run_uninstall(env)[0] == ExitCode.FOREIGN_SERVICE
    assert (env.unit_dir() / MCP).exists()


# --- status ---------------------------------------------------------------------------------------------


def test_status_of_a_running_service(env):
    exe = install_existing(env)
    info = linux.status(env)
    assert info["installed"] and info["ours"]
    assert (info["state"], info["port"], info["version"], info["binary"]) == (
        "running",
        8000,
        VERSION,
        exe,
    )
    assert info["user_sid"] is None
    assert info["token_readable"]
    assert info["mcp_http"] == {
        "installed": True,
        "state": "running",
        "port": 3000,
        "token_file": str(env.root / "data" / "api_token.txt"),
        "token_readable": True,
    }
    assert info["warnings"] == []
    assert info["unit_dir"] == str(env.unit_dir())


def test_status_has_the_windows_keys(env):
    """The keys of the Windows status, plus unit_dir and data_dir."""
    info = linux.status(env)
    assert set(info) == {
        "installed",
        "ours",
        "state",
        "port",
        "version",
        "binary",
        "user_sid",
        "token_file",
        "token_readable",
        "mcp_http",
        "warnings",
        "unit_dir",
        "data_dir",
    }
    assert set(info["mcp_http"]) == {"installed", "state", "port", "token_file", "token_readable"}


def test_status_without_a_user_manager(env):
    install_existing(env)
    env.manager.unavailable = "Failed to connect to bus"
    info = linux.status(env)
    assert info["installed"] and info["ours"] and info["state"] is None
    assert info["mcp_http"]["state"] is None
    assert any("user manager" in w for w in info["warnings"])


def test_status_of_a_foreign_unit(env):
    env.unit_dir().mkdir(parents=True)
    (env.unit_dir() / BACKEND).write_text("[Service]\n")
    info = linux.status(env)
    assert info["installed"] and not info["ours"]
    assert "rootmind-backend.service" in linux.format_status(info)


def test_status_stopped(env):
    install_existing(env)
    env.manager.active.clear()
    info = linux.status(env)
    assert info["state"] == "stopped" and info["version"] is None
    assert "service is stopped" in info["warnings"]
    assert "MCP service is stopped" in info["warnings"]
    assert "stopped" in linux.format_status(info)


# --- the command line ------------------------------------------------------------------------------


def run_cli(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv, env=env)
    return code, out.getvalue(), err.getvalue()


def test_cli_dispatches_to_linux(env):
    install_existing(env)
    code, out, _err = run_cli(["status", "--json"], env)
    assert code == 0
    assert json.loads(out)["unit_dir"] == str(env.unit_dir())
    code, out, _err = run_cli(["status"], env)
    assert code == 0 and "RootMind background service: running" in out


def test_cli_install_and_uninstall_on_linux(env):
    code, _out, err = run_cli(["install", "--port", "auto", "--mcp-port", "auto"], env)
    assert code == 0, err
    assert run_cli(["uninstall"], env)[0] == 0


def test_cli_status_foreign_on_linux_is_not_an_error(env):
    env.unit_dir().mkdir(parents=True)
    (env.unit_dir() / BACKEND).write_text("x")
    code, out, _err = run_cli(["status", "--json"], env)
    assert code == 0
    assert json.loads(out)["ours"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="the run command needs no SID on Linux")
def test_cli_run_backend_needs_no_sid_on_linux(monkeypatch, env):
    from rootmind.service import host

    seen = {}

    def fake_run(*args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(host, "run_service", fake_run)
    assert run_cli(["run", "backend", "--port", "18000"], env)[0] == 0
    assert seen["args"] == ("backend", 18000, None)


def test_the_linux_commands_import_no_windows_module():
    """pywin32, the access check, the SCM and the ACL code are Windows only."""
    import subprocess

    code = (
        "import sys, json\n"
        "import rootmind.service.cli, rootmind.service.linux, rootmind.service.linux_system\n"
        "import rootmind.service.host, rootmind.service.procnet\n"
        "names = ('rootmind.winaccess', 'rootmind.service.install', 'rootmind.service.layout',\n"
        "         'rootmind.service.scm', 'rootmind.service.system', 'rootmind.service.listeners')\n"
        "bad = [m for m in sys.modules if m in names or m.startswith(('win32', 'pywintypes'))]\n"
        "print(json.dumps(bad))\n"
    )
    result = subprocess.run(  # noqa: S603 (this interpreter, fixed code)
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert json.loads(result.stdout) == []
