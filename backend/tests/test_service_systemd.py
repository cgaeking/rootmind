"""The systemd user units: the unit file contract, ``systemctl show`` and the adapter."""

from __future__ import annotations

import subprocess

import pytest

from rootmind.service import systemd

EXE = "/home/me/.local/share/rootmind/runtime/2.3.0-0a1b2c3d/rootmind-backend"

GOLDEN_BACKEND = f"""{systemd.MARKER}
[Unit]
Description=RootMind backend (REST API and document indexing for the RootMind app)
Documentation=https://github.com/cgaeking/rootmind#linux

[Service]
Type=notify
ExecStart="{EXE}" service run backend --port 8000
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=2
TimeoutStartSec=90
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""


def test_the_backend_unit_is_exactly_the_contract():
    assert systemd.build_unit(EXE, "backend", 8000) == GOLDEN_BACKEND


def test_units_round_trip():
    for role, port in (("backend", 8000), ("mcp-http", 18400)):
        unit = systemd.parse_unit(systemd.build_unit(EXE, role, port))
        assert unit == systemd.Unit(role=role, exe=EXE, port=port)


@pytest.mark.parametrize(
    "exe",
    [
        "/home/José Müller/.local/share/rootmind/runtime/v-1/rootmind-backend",
        '/home/a"b/rootmind-backend',
        "/home/a\\b/rootmind-backend",
        "/home/100%/rootmind-backend",
        "/home/$USER/rootmind-backend",
    ],
)
def test_special_characters_are_escaped_and_read_back(exe):
    line = systemd.exec_start(exe, "backend", 8000)
    assert "%%" in line or "%" not in exe
    assert "$$" in line or "$" not in exe
    assert systemd.parse_exec_start(line) == systemd.Unit("backend", exe, 8000)


@pytest.mark.parametrize("exe", ["", "/home/a\nb", "/home/a\tb", "/x\x7f"])
def test_control_characters_are_refused(exe):
    with pytest.raises(ValueError):
        systemd.exec_start(exe, "backend", 8000)


def test_invalid_arguments_are_refused():
    with pytest.raises(ValueError):
        systemd.exec_start(EXE, "indexer", 8000)
    with pytest.raises(ValueError):
        systemd.exec_start(EXE, "backend", 0)


@pytest.mark.parametrize(
    "line",
    [
        f"{EXE} service run backend --port 8000",  # not quoted
        f'"{EXE}"service run backend --port 8000',
        f'"{EXE}" service run backend --port 8000 --extra x',
        f'"{EXE}" service run indexer --port 8000',
        f'"{EXE}" service run backend --port 0',
        f'"{EXE}" service run backend --port 70000',
        f'"{EXE}" service run backend --port 80a',
        f'"{EXE}" service start backend --port 8000',
        f'"{EXE}" service run backend',
        '"/a%b" service run backend --port 8000',  # a lone specifier
        '"/a$b" service run backend --port 8000',
        '"/a\\x" service run backend --port 8000',
        f'"{EXE}',
    ],
)
def test_exec_start_outside_the_contract_is_not_ours(line):
    assert systemd.parse_exec_start(line) is None


def test_a_unit_without_the_marker_or_with_two_exec_starts_is_not_ours():
    assert systemd.parse_unit(GOLDEN_BACKEND.replace(systemd.MARKER, "# mine")) is None
    doubled = GOLDEN_BACKEND.replace(
        "Restart=on-failure",
        f'ExecStart="{EXE}" service run backend --port 9000\nRestart=on-failure',
    )
    assert systemd.parse_unit(doubled) is None
    # ExecStart outside [Service] does not count.
    moved = GOLDEN_BACKEND.replace("[Service]", "[X-Other]")
    assert systemd.parse_unit(moved) is None
    assert systemd.parse_unit("") is None


SHOW = """LoadState=loaded
ActiveState=active
SubState=running
MainPID=4242
ExecMainStatus=0
Result=success
FragmentPath=/home/me/.config/systemd/user/rootmind-backend.service
"""


def test_parse_show():
    state = systemd.parse_show("rootmind-backend.service", SHOW)
    assert state.loaded and state.main_pid == 4242
    assert state.state == systemd.RUNNING
    assert state.fragment_path.endswith("rootmind-backend.service")
    missing = systemd.parse_show(
        "x.service", "LoadState=not-found\nActiveState=inactive\nMainPID=\n"
    )
    assert not missing.loaded and missing.main_pid == 0 and missing.state == systemd.STOPPED


@pytest.mark.parametrize(
    ("active", "state"),
    [
        ("active", "running"),
        ("reloading", "running"),
        ("activating", "start_pending"),
        ("deactivating", "stop_pending"),
        ("inactive", "stopped"),
        ("failed", "stopped"),
        ("maintenance", "other"),
    ],
)
def test_states_use_the_windows_names(active, state):
    assert systemd.UnitState("u", "loaded", active, "x").state == state


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.result = (returncode, stdout, stderr)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        code, out, err = self.result
        return subprocess.CompletedProcess(argv, code, out, err)


def test_the_adapter_runs_systemctl_user_without_a_shell():
    run = FakeRun(stdout=SHOW)
    manager = systemd.Systemctl("/usr/bin/systemctl", run=run, env={"HOME": "/home/me"})
    state = manager.show("rootmind-backend.service")
    assert state.main_pid == 4242
    argv, kwargs = run.calls[0]
    assert argv[:3] == ["/usr/bin/systemctl", "--user", "--no-ask-password"]
    assert argv[3:5] == ["show", "rootmind-backend.service"]
    assert argv[5].startswith("--property=LoadState,")
    assert kwargs["env"]["SYSTEMD_PAGER"] == "" and kwargs["env"]["LC_ALL"] == "C"
    assert "shell" not in kwargs
    manager.enable("a.service", "b.service")
    assert run.calls[-1][0][3:] == ["enable", "a.service", "b.service"]


def test_errors_say_whether_the_user_manager_is_missing():
    manager = systemd.Systemctl(
        "/usr/bin/systemctl",
        run=FakeRun(1, stderr="Failed to connect to bus: No medium found"),
        env={},
    )
    with pytest.raises(systemd.UserManagerUnavailableError):
        manager.check()
    manager = systemd.Systemctl("/usr/bin/systemctl", run=FakeRun(1, stderr="Job failed"), env={})
    with pytest.raises(systemd.SystemctlError) as raised:
        manager.start("x.service")
    assert not isinstance(raised.value, systemd.UserManagerUnavailableError)
    assert "Job failed" in str(raised.value)
    # reset-failed never raises.
    manager.reset_failed("x.service")


def test_no_systemctl():
    manager = systemd.Systemctl("", run=FakeRun(), env={})
    with pytest.raises(systemd.UserManagerUnavailableError):
        manager.check()


def test_runtime_dir_is_filled_in(monkeypatch, tmp_path):
    env = systemd.user_environment({"XDG_RUNTIME_DIR": "/run/user/7"}, uid=7)
    assert env["XDG_RUNTIME_DIR"] == "/run/user/7"
    monkeypatch.setattr(systemd.os.path, "isdir", lambda path: path == "/run/user/1000")
    assert systemd.user_environment({}, uid=1000)["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert "XDG_RUNTIME_DIR" not in systemd.user_environment({}, uid=1001)
