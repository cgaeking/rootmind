"""service install / uninstall / status and the `service` command line, with fake adapters.

The fake machine (:class:`FakeEnv`) stands in for the Service Control Manager,
the process token, the program folder and the HTTP checks. Config and token
files are real files in a temporary "ProgramData" folder. Runs on every OS.
"""

from __future__ import annotations

import io
import json
import ntpath
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from rootmind.service import (
    BACKEND_SERVICE,
    MCP_SERVICE,
    ExitCode,
    cli,
    imagepath,
    install,
    layout,
    scm,
)
from rootmind.winaccess import AccessContextError

USER_SID = "S-1-5-21-1-2-3-1001"
OTHER_SID = "S-1-5-21-1-2-3-1002"
PF = r"C:\Program Files"
USER_EXE = r"C:\Users\me\AppData\Local\RootMind\backend\rootmind-backend.exe"
VERSION = "1.2.0"
MCP_ACCOUNT = "NT SERVICE\\RootMind-MCP-HTTP"


def program_exe(folder: str) -> str:
    return rf"{PF}\RootMind Service\{folder}\rootmind-backend.exe"


class FakeScm:
    """The Service Control Manager, in memory."""

    def __init__(self, env: FakeEnv) -> None:
        self.env = env
        self.services: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_start: set[str] = set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def add(self, name, image_path, account="LocalSystem", state=scm.RUNNING, pid=1234):
        self.services[name] = {
            "image_path": image_path,
            "account": account,
            "state": state,
            "pid": pid,
            "spec": None,
        }

    def query(self, name):
        record = self.services.get(name)
        if record is None:
            return None
        return scm.ServiceStatus(
            name=name,
            image_path=record["image_path"],
            account=record["account"],
            start_type=2,
            state=record["state"],
            pid=record["pid"] if record["state"] == scm.RUNNING else 0,
            win32_exit_code=1066 if record["state"] == scm.STOPPED else 0,
            service_exit_code=2 if record["state"] == scm.STOPPED else 0,
        )

    def create(self, spec):
        self.calls.append(("create", spec.name))
        assert spec.name not in self.services
        self.services[spec.name] = {
            "image_path": spec.image_path,
            "account": spec.account,
            "state": scm.STOPPED,
            "pid": 0,
            "spec": spec,
        }

    def update(self, spec):
        self.calls.append(("update", spec.name))
        record = self.services[spec.name]
        record.update(image_path=spec.image_path, account=spec.account, spec=spec)

    def start(self, name):
        self.calls.append(("start", name))
        if name in self.fail_start:
            raise scm.ScmError(None, "StartService: failed", None, 1053)
        self.services[name]["state"] = scm.RUNNING
        self.services[name]["pid"] = 4321

    def stop(self, name):
        self.calls.append(("stop", name))
        self.services[name]["state"] = scm.STOPPED

    def delete(self, name):
        self.calls.append(("delete", name))
        del self.services[name]


class FakeEnv:
    """The machine as seen by the install logic."""

    def __init__(self, tmp_path: Path) -> None:
        self.version = VERSION
        self.executable = USER_EXE
        self.windows = True
        self.release = True
        self.elevated = True
        self.sid = USER_SID
        self.access_error: Exception | None = None
        self.root = tmp_path / "ProgramData" / "RootMind"
        self.busy: set[int] = set()
        self.data_refusal: layout.LayoutError | None = None
        self.data_problem_list: list[str] = []
        self.manager = FakeScm(self)
        self.events: list[tuple] = []
        self.http_log: list[tuple] = []
        self.unhealthy: set[str] = set()
        self.open_status: set[str] = set()  # services that answer /status without a token
        self.app_present = True
        self.folders = 0
        self.clock = 0.0
        self.squatters: dict[int, int] = {}  # port -> PID of another program listening there
        self.owner_error: OSError | None = None
        self.source_problems: list[str] = []

    # process
    def is_windows(self):
        return self.windows

    def is_release_build(self):
        return self.release

    def is_elevated(self):
        return self.elevated

    def process_user_sid(self):
        return self.sid

    def make_administrators_default_owner(self):
        self.events.append(("default_owner",))
        if self.owner_error is not None:
            raise self.owner_error

    def check_access(self, user_sid):
        self.events.append(("check_access", user_sid))
        if self.access_error is not None:
            raise self.access_error

    def program_source_problems(self, folder, approver_sid):
        self.events.append(("check_source", folder, approver_sid))
        return list(self.source_problems)

    def app_installed(self):
        return self.app_present

    # folders
    def program_files(self):
        return PF

    def data_root(self):
        return self.root

    def prepare_data(self, root, user_sid, mcp_enabled):
        self.events.append(("prepare_data", user_sid, mcp_enabled))
        if self.data_refusal is not None:
            raise self.data_refusal
        created = not root.exists()
        for sub in ("", *layout.DATA_SUBFOLDERS, layout.TMP_FOLDER):
            (root / sub).mkdir(parents=True, exist_ok=True)
        return created

    def normalize_data(self, root, user_sid, mcp_enabled):
        self.events.append(("normalize_data", user_sid, mcp_enabled))

    def data_problems(self, root):
        return list(self.data_problem_list)

    def remove_data(self, root):
        self.events.append(("remove_data",))
        import shutil

        shutil.rmtree(root)

    def install_program(self, source_dir, version):
        self.folders += 1
        folder = rf"{PF}\RootMind Service\{version}-{self.folders:08x}"
        self.events.append(("install_program", source_dir, folder))
        return folder

    def remove_old_programs(self, keep):
        self.events.append(("remove_old_programs", keep))
        return []

    def remove_all_programs(self):
        self.events.append(("remove_all_programs",))
        return ["pending-file"] if self.executable.startswith(PF) else []

    # services, ports, http
    def scm(self, *, write):
        return self.manager

    def wait_process_exit(self, pid, timeout):
        self.events.append(("wait_process_exit", pid))
        return True

    def can_bind(self, port):
        return port not in self.busy

    def port_owned_by(self, port, pid):
        self.events.append(("port_owned_by", port, pid))
        if port in self.squatters:
            return self.squatters[port] == pid
        name = self._service_on(port)
        return name is not None and self.manager.services[name]["pid"] == pid

    def _service_on(self, port):
        for name, record in self.manager.services.items():
            image = imagepath.parse(record["image_path"])
            if image is not None and image.port == port and record["state"] == scm.RUNNING:
                return name
        return None

    def http(self, method, url, *, token=None, body=None):
        parts = urlsplit(url)
        self.http_log.append((method, parts.path, token is not None))
        name = self._service_on(parts.port)
        if name is None or name in self.unhealthy:
            return 0, None
        if parts.path == "/health":
            return 200, {"status": "healthy"}
        if name == BACKEND_SERVICE:
            expected = (self.root / "data" / "rest_token.txt").read_text().strip()
            if parts.path == "/":
                return 200, {"name": "RootMind API", "version": self.version, "auth": "bearer"}
            if name in self.open_status:
                return 200, {}
            return (200, {}) if token == expected else (401, {"detail": "Missing bearer token"})
        expected = (self.root / "data" / "api_token.txt").read_text().strip()
        assert body["method"] == "initialize"
        return (200, {"result": {}}) if token == expected else (401, {"detail": "Invalid token"})

    def sleep(self, seconds):
        self.clock += seconds

    def monotonic(self):
        return self.clock

    @staticmethod
    def same_path(a, b):
        return ntpath.normcase(a) == ntpath.normcase(b)


@pytest.fixture
def env(tmp_path):
    return FakeEnv(tmp_path)


def run_install(env, **options):
    out, err = [], []
    code = install.install(env, install.InstallOptions(**options), out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def run_uninstall(env, purge=False):
    out, err = [], []
    code = install.uninstall(env, purge=purge, out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def install_existing(env, *, mcp_port=None, port=8000, sid=USER_SID, folder="1.2.0-11111111"):
    """Services as an earlier install left them (running)."""
    env.manager.add(
        BACKEND_SERVICE, imagepath.build(program_exe(folder), "backend", port, sid), pid=100
    )
    if mcp_port is not None:
        env.manager.add(
            MCP_SERVICE,
            imagepath.build(program_exe(folder), "mcp-http", mcp_port),
            account=MCP_ACCOUNT,
            pid=101,
        )
    data = env.root / "data"
    data.mkdir(parents=True)
    (env.root / "logs").mkdir()
    (data / "rest_token.txt").write_text("rest-token-from-before")
    if mcp_port is not None:
        (data / "api_token.txt").write_text("mcp-token-from-before")
    (env.root / "config.yaml").write_text("database:\n  path: ./data/db/index.db\n")


# --- fresh install ------------------------------------------------------------------------


def test_fresh_install_registers_the_backend(env):
    code, out, err = run_install(env, mcp=False)
    assert code == 0, err
    record = env.manager.services[BACKEND_SERVICE]
    assert record["image_path"] == (
        rf'"{PF}\RootMind Service\1.2.0-00000001\rootmind-backend.exe" '
        f"service run backend --port 8000 --user-sid {USER_SID}"
    )
    spec = record["spec"]
    assert spec.account == "LocalSystem"
    assert spec.sid_type == scm.SERVICE_SID_TYPE_NONE
    assert spec.required_privileges is None
    assert spec.failure_delays_ms == (5_000, 30_000, 60_000)
    assert MCP_SERVICE not in env.manager.services
    assert record["state"] == scm.RUNNING
    assert (
        "install_program",
        ntpath.dirname(USER_EXE),
        rf"{PF}\RootMind Service\1.2.0-00000001",
    ) in (env.events)
    assert ("prepare_data", USER_SID, False) in env.events
    assert ("remove_old_programs", rf"{PF}\RootMind Service\1.2.0-00000001") in env.events
    assert "installed (version 1.2.0)" in out
    assert "http://127.0.0.1:8000" in out


def test_fresh_install_writes_config_tokens_and_log(env):
    assert run_install(env, mcp=False)[0] == 0
    config = yaml.safe_load((env.root / "config.yaml").read_text())
    assert config == {"database": {"path": "./data/db/index.db"}}
    token = (env.root / "data" / "rest_token.txt").read_text()
    assert len(token) >= 40
    assert not (env.root / "data" / "api_token.txt").exists()
    log = (env.root / "logs" / "install.log").read_text()
    assert "install: exit 0" in log
    assert token not in log


def test_fresh_install_checks_health_and_authentication(env):
    assert run_install(env)[0] == 0
    assert ("GET", "/health", False) in env.http_log
    assert ("GET", "/status", False) in env.http_log
    assert ("GET", "/status", True) in env.http_log


def test_mcp_over_http_is_on_by_default(env):
    code, out, err = run_install(env)
    assert code == 0, err
    backend = imagepath.parse(env.manager.services[BACKEND_SERVICE]["image_path"])
    mcp = imagepath.parse(env.manager.services[MCP_SERVICE]["image_path"])
    assert (backend.port, mcp.port) == (8000, 3000)
    assert env.manager.services[MCP_SERVICE]["state"] == scm.RUNNING
    assert (env.root / "data" / "api_token.txt").read_text()
    assert ("prepare_data", USER_SID, True) in env.events
    assert "http://127.0.0.1:3000/mcp" in out


def test_no_mcp_http_on_a_fresh_install(env):
    code, _out, err = run_install(env, mcp=False)
    assert code == 0, err
    assert MCP_SERVICE not in env.manager.services
    assert not (env.root / "data" / "api_token.txt").exists()
    assert ("prepare_data", USER_SID, False) in env.events


def test_install_with_mcp(env):
    code, out, err = run_install(env, mcp=True, port="auto", mcp_port="auto")
    assert code == 0, err
    mcp = env.manager.services[MCP_SERVICE]
    assert mcp["image_path"] == (
        rf'"{PF}\RootMind Service\1.2.0-00000001\rootmind-backend.exe" service run mcp-http --port 3000'
    )
    spec = mcp["spec"]
    assert spec.account == MCP_ACCOUNT
    assert spec.sid_type == scm.SERVICE_SID_TYPE_UNRESTRICTED
    assert spec.required_privileges == ("SeChangeNotifyPrivilege",)
    assert mcp["state"] == scm.RUNNING
    rest = (env.root / "data" / "rest_token.txt").read_text()
    mcp_token = (env.root / "data" / "api_token.txt").read_text()
    assert rest and mcp_token and rest != mcp_token
    assert ("prepare_data", USER_SID, True) in env.events
    assert ("normalize_data", USER_SID, True) in env.events
    assert ("POST", "/mcp", False) in env.http_log and ("POST", "/mcp", True) in env.http_log
    assert "http://127.0.0.1:3000/mcp" in out


def test_tokens_never_reach_the_image_path_or_output(env):
    code, out, err = run_install(env, mcp=True)
    assert code == 0
    tokens = [
        (env.root / "data" / name).read_text() for name in ("rest_token.txt", "api_token.txt")
    ]
    for record in env.manager.services.values():
        for token in tokens:
            assert token not in record["image_path"]
    for token in tokens:
        assert token not in out and token not in err


def test_no_start_registers_without_starting(env):
    code, out, _err = run_install(env, start=False)
    assert code == 0
    assert env.manager.services[BACKEND_SERVICE]["state"] == scm.STOPPED
    assert not env.http_log
    assert "not started" in out


# --- ports ------------------------------------------------------------------------------------


def test_default_port_is_strict(env):
    env.busy = {8000}
    code, _out, err = run_install(env)
    assert code == ExitCode.PORT_BUSY
    assert "Port 8000" in err
    assert not env.manager.services
    assert not env.root.exists()


def test_auto_ports_fall_back(env):
    env.busy = {8000, 18300, 3000}
    code, _out, err = run_install(env, port="auto", mcp=True, mcp_port="auto")
    assert code == 0, err
    backend = imagepath.parse(env.manager.services[BACKEND_SERVICE]["image_path"])
    mcp = imagepath.parse(env.manager.services[MCP_SERVICE]["image_path"])
    assert (backend.port, mcp.port) == (18301, 18400)


def test_explicit_ports(env):
    code, _out, err = run_install(env, port=18123, mcp=True, mcp_port=18124)
    assert code == 0, err
    assert imagepath.parse(env.manager.services[MCP_SERVICE]["image_path"]).port == 18124


def test_same_port_twice_is_busy(env):
    code, _out, _err = run_install(env, port=18123, mcp=True, mcp_port=18123)
    assert code == ExitCode.PORT_BUSY


# --- refusals before any change ------------------------------------------------------------------


def nothing_changed(env):
    return not env.manager.calls and not env.root.exists() and env.folders == 0


def test_not_windows(env):
    env.windows = False
    assert run_install(env)[0] == ExitCode.USAGE
    assert nothing_changed(env)


def test_not_the_release_build_names_the_download(env):
    env.release = False
    code, _out, err = run_install(env)
    assert code == ExitCode.USAGE
    assert (
        "https://github.com/cgaeking/rootmind/releases/download/v1.2.0/"
        "rootmind-backend-windows-x64.zip"
    ) in err
    assert nothing_changed(env)


@pytest.mark.parametrize(
    ("version", "url"),
    [
        ("1.2.0", "releases/download/v1.2.0/rootmind-backend-windows-x64.zip"),
        ("1.2.0rc1", "releases/download/v1.2.0-rc.1/rootmind-backend-windows-x64.zip"),
        ("1.3.0b2", "releases/download/v1.3.0-beta.2/rootmind-backend-windows-x64.zip"),
        ("1.3.0a3", "releases/download/v1.3.0-alpha.3/rootmind-backend-windows-x64.zip"),
        ("1.3.0.dev4", "releases"),
        ("1.3.0+local", "releases"),
    ],
)
def test_release_download_address(version, url):
    from rootmind.service import release_zip_url

    assert release_zip_url(version) == f"https://github.com/cgaeking/rootmind/{url}"


def test_not_elevated(env):
    env.elevated = False
    assert run_install(env)[0] == ExitCode.NOT_ELEVATED
    assert nothing_changed(env)


def test_user_sid_must_be_the_approving_account(env):
    code, _out, err = run_install(env, user_sid="S-1-5-32-545")
    assert code == ExitCode.ACCOUNT_MISMATCH
    assert nothing_changed(env)


def test_user_sid_matching_the_approving_account(env):
    assert run_install(env, user_sid=USER_SID.lower().replace("s-", "S-"))[0] == 0


def test_access_context_failure(env):
    env.access_error = AccessContextError("no context")
    code, _out, err = run_install(env)
    assert code == ExitCode.ACCESS_CONTEXT
    assert "no context" in err
    assert nothing_changed(env)


FOREIGN_IMAGES = [
    r"C:\tools\other-service.exe",
    r'"C:\Windows\System32\cmd.exe" /c exit 0',
    # our command line, but a program outside the service's program folder
    rf'"{USER_EXE}" service run backend --port 8000 --user-sid {USER_SID}',
]


@pytest.mark.parametrize(
    ("name", "image"),
    [(name, image) for name in (BACKEND_SERVICE, MCP_SERVICE) for image in FOREIGN_IMAGES]
    + [
        # our layout, but the other role for this name
        (BACKEND_SERVICE, rf'"{program_exe("1.2.0-1")}" service run mcp-http --port 3000'),
        (
            MCP_SERVICE,
            rf'"{program_exe("1.2.0-1")}" service run backend --port 8000 --user-sid {USER_SID}',
        ),
    ],
)
def test_foreign_service_is_refused_and_untouched(env, name, image):
    env.manager.add(name, image)
    code, _out, err = run_install(env)
    assert code == ExitCode.FOREIGN_SERVICE
    assert f"sc.exe stop {name}" in err and f"sc.exe delete {name}" in err
    assert env.manager.services[name]["image_path"] == image
    assert not env.manager.calls
    assert not env.root.exists()


def test_data_folder_refused(env):
    env.data_refusal = layout.LayoutError(
        layout.NOT_PROTECTED, ["data: S-1-5-32-545 may change it"]
    )
    code, _out, err = run_install(env)
    assert code == ExitCode.DATA_FOLDER_REFUSED
    assert "not protected" in err and "S-1-5-32-545" in err
    assert not env.manager.services


def test_mcp_token_from_needs_mcp(env, tmp_path):
    source = tmp_path / "token.txt"
    source.write_text("abc")
    assert run_install(env, mcp=False, mcp_token_from=source)[0] == ExitCode.USAGE
    # MCP is on by default for a new install, so the file is used then.
    assert run_install(env, mcp_token_from=source)[0] == 0
    assert (env.root / "data" / "api_token.txt").read_text() == "abc"


@pytest.mark.parametrize("content", [b"", b"   \n", b"a b", b"x" * 5000, b"\xff\xfe\x00"])
def test_mcp_token_from_refuses_bad_files(env, tmp_path, content):
    source = tmp_path / "token.txt"
    source.write_bytes(content)
    assert run_install(env, mcp=True, mcp_token_from=source)[0] == ExitCode.USAGE
    assert nothing_changed(env)


def test_mcp_token_from_refuses_folders_and_missing_files(env, tmp_path):
    assert run_install(env, mcp=True, mcp_token_from=tmp_path)[0] == ExitCode.USAGE
    assert run_install(env, mcp=True, mcp_token_from=tmp_path / "missing")[0] == ExitCode.USAGE


# --- options that carry data -------------------------------------------------------------------


def test_mcp_token_from_file(env, tmp_path):
    source = tmp_path / "api_token.txt"
    source.write_text("\ufeffexisting-mcp-token-value\n", encoding="utf-8")
    code, _out, err = run_install(env, mcp=True, mcp_token_from=source)
    assert code == 0, err
    assert (env.root / "data" / "api_token.txt").read_text() == "existing-mcp-token-value"


# --- update ------------------------------------------------------------------------------------


def test_update_keeps_ports_tokens_and_mcp(env):
    install_existing(env, port=18300, mcp_port=18401)
    code, out, err = run_install(env, port="auto")
    assert code == 0, err
    backend = imagepath.parse(env.manager.services[BACKEND_SERVICE]["image_path"])
    mcp = imagepath.parse(env.manager.services[MCP_SERVICE]["image_path"])
    assert (backend.port, mcp.port) == (18300, 18401)
    assert backend.exe == program_exe("1.2.0-00000001") == mcp.exe
    assert backend.user_sid == USER_SID
    assert (env.root / "data" / "rest_token.txt").read_text() == "rest-token-from-before"
    assert (env.root / "data" / "api_token.txt").read_text() == "mcp-token-from-before"
    assert env.manager.calls[:4] == [
        ("stop", BACKEND_SERVICE),
        ("stop", MCP_SERVICE),
        ("update", BACKEND_SERVICE),
        ("update", MCP_SERVICE),
    ]
    new_folder = rf"{PF}\RootMind Service\1.2.0-00000001"
    assert ("remove_old_programs", new_folder) in env.events
    # The old processes have ended before the old folder is deleted.
    assert env.events.index(("wait_process_exit", 100)) < env.events.index(
        ("remove_old_programs", new_folder)
    )
    assert ("wait_process_exit", 101) in env.events
    assert "updated (version 1.2.0)" in out


def test_update_keeps_mcp_off(env):
    """MCP over HTTP is on by default only for a new install; an update keeps the choice."""
    install_existing(env)
    code, _out, err = run_install(env, port="auto", mcp_port="auto")
    assert code == 0, err
    assert MCP_SERVICE not in env.manager.services
    assert not (env.root / "data" / "api_token.txt").exists()


def test_a_failed_update_restarts_the_old_services(env):
    install_existing(env, mcp_port=3000)
    before = {name: dict(record) for name, record in env.manager.services.items()}

    def broken(*args):
        raise OSError("the disk is full")

    env.install_program = broken
    code, _out, err = run_install(env)
    assert code == ExitCode.UNEXPECTED
    assert "the disk is full" in err
    for name, record in env.manager.services.items():
        assert record["state"] == scm.RUNNING
        assert record["image_path"] == before[name]["image_path"]
    assert ("start", BACKEND_SERVICE) in env.manager.calls


def test_update_stops_the_services_before_checking_the_data_folder(env):
    install_existing(env, mcp_port=3000)
    states = []
    original = env.prepare_data

    def prepare(root, user_sid, mcp_enabled):
        states.append({name: r["state"] for name, r in env.manager.services.items()})
        return original(root, user_sid, mcp_enabled)

    env.prepare_data = prepare
    assert run_install(env)[0] == 0
    assert states == [{BACKEND_SERVICE: scm.STOPPED, MCP_SERVICE: scm.STOPPED}]


def test_a_refused_data_folder_in_an_update_restarts_the_old_services(env):
    install_existing(env, mcp_port=3000)
    env.data_refusal = layout.LayoutError(layout.NOT_PROTECTED, ["data: S-1-1-0 may change it"])
    code, _out, err = run_install(env)
    assert code == ExitCode.DATA_FOLDER_REFUSED
    assert "S-1-1-0" in err
    assert all(r["state"] == scm.RUNNING for r in env.manager.services.values())
    assert ("update", BACKEND_SERVICE) not in env.manager.calls


@pytest.mark.parametrize("database", [None, "./data/index.db", "C:/elsewhere/index.db"])
def test_update_moves_the_index_into_the_db_folder(env, database):
    install_existing(env)
    config = {"connectors": [{"name": "docs", "type": "local", "path": "D:/Docs"}]}
    if database is not None:
        config["database"] = {"path": database}
    (env.root / "config.yaml").write_text(yaml.safe_dump(config))
    code, out, err = run_install(env, mcp=True)
    assert code == 0, err
    saved = yaml.safe_load((env.root / "config.yaml").read_text())
    assert saved["database"] == {"path": "./data/db/index.db"}
    assert saved["connectors"][0]["path"] == "D:/Docs"
    assert (env.root / "config.yaml.bak").exists()
    assert "The index moves to" in out


def test_update_keeps_a_config_whose_index_is_in_place(env):
    install_existing(env)
    before = (env.root / "config.yaml").read_text()
    code, out, _err = run_install(env)
    assert code == 0
    assert (env.root / "config.yaml").read_text() == before
    assert not (env.root / "config.yaml.bak").exists()
    assert "The index moves" not in out


def test_update_does_not_test_the_kept_ports(env):
    install_existing(env, port=8000)
    env.busy = {8000}  # our own (stopped) service held it a moment ago
    assert run_install(env, port="auto")[0] == 0


def test_update_warns_about_an_explicit_other_port(env):
    install_existing(env, port=8000)
    code, _out, err = run_install(env, port=9000)
    assert code == 0
    assert "keeps port 8000" in err


def test_update_by_another_account_is_refused(env):
    install_existing(env, sid=OTHER_SID)
    code, _out, err = run_install(env)
    assert code == ExitCode.ACCOUNT_MISMATCH
    assert "another account" in err
    assert not env.manager.calls


def test_turning_mcp_on_from_the_program_files_exe(env):
    install_existing(env, folder="1.2.0-0a1b2c3d")
    env.executable = program_exe("1.2.0-0a1b2c3d")
    code, _out, err = run_install(env, mcp=True, mcp_port="auto")
    assert code == 0, err
    assert env.folders == 0  # no copy: this exe is the registered one
    mcp = imagepath.parse(env.manager.services[MCP_SERVICE]["image_path"])
    assert mcp.exe == env.executable and mcp.port == 3000
    assert ("create", MCP_SERVICE) in env.manager.calls
    assert not any(event[0] == "remove_old_programs" for event in env.events)


def test_turning_mcp_off_removes_the_service_and_its_entries(env):
    install_existing(env, mcp_port=3000, folder="1.2.0-0a1b2c3d")
    env.executable = program_exe("1.2.0-0a1b2c3d")
    code, _out, err = run_install(env, mcp=False)
    assert code == 0, err
    assert MCP_SERVICE not in env.manager.services
    assert ("delete", MCP_SERVICE) in env.manager.calls
    assert ("prepare_data", USER_SID, False) in env.events
    assert ("normalize_data", USER_SID, False) in env.events


def test_update_with_only_the_mcp_service_left(env):
    env.manager.add(
        MCP_SERVICE,
        imagepath.build(program_exe("1.2.0-1"), "mcp-http", 18400),
        account=MCP_ACCOUNT,
    )
    code, _out, err = run_install(env, port="auto")
    assert code == 0, err
    assert ("create", BACKEND_SERVICE) in env.manager.calls
    assert imagepath.parse(env.manager.services[MCP_SERVICE]["image_path"]).port == 18400


# --- start and health ----------------------------------------------------------------------


def test_health_check_failure_keeps_the_services(env):
    env.unhealthy = {BACKEND_SERVICE}
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "did not answer" in err
    assert BACKEND_SERVICE in env.manager.services
    assert "install: exit 7" in (env.root / "logs" / "install.log").read_text()


def test_a_service_that_stops_fails_fast(env):
    env.unhealthy = {BACKEND_SERVICE}

    original_start = env.manager.start

    def start_then_stop(name):
        original_start(name)
        env.manager.services[name]["state"] = scm.STOPPED

    env.manager.start = start_then_stop
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "stopped (exit code 1066/2)" in err
    assert env.clock < install.HEALTH_TIMEOUT_SECONDS


def test_an_open_api_fails_the_check(env):
    env.open_status = {BACKEND_SERVICE}
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "without the token answered 200" in err


def test_start_failure(env):
    env.manager.fail_start = {BACKEND_SERVICE}
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "did not start" in err


def test_tokens_go_only_to_the_service_process(env):
    code, _out, err = run_install(env, mcp=True)
    assert code == 0, err
    assert ("port_owned_by", 8000, 4321) in env.events
    assert ("port_owned_by", 3000, 4321) in env.events


def test_no_token_reaches_another_program_on_the_rest_port(env):
    env.squatters = {8000: 666}
    code, _out, err = run_install(env, mcp=True)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "Another program listens on 127.0.0.1:8000" in err
    assert "No token was sent" in err
    assert not any(with_token for _method, _path, with_token in env.http_log)
    assert BACKEND_SERVICE in env.manager.services  # kept for its log


def test_no_token_reaches_another_program_on_the_mcp_port(env):
    install_existing(env, mcp_port=3000)
    env.squatters = {3000: 666}
    code, _out, err = run_install(env)
    assert code == ExitCode.HEALTH_CHECK_FAILED
    assert "127.0.0.1:3000" in err
    assert ("POST", "/mcp", True) not in env.http_log
    assert ("POST", "/mcp", False) not in env.http_log


def test_tokens_wait_until_the_service_reports_running(env):
    original_start = env.manager.start
    original_query = env.manager.query
    polls = []

    def start_pending(name):
        original_start(name)
        env.manager.services[name]["state"] = scm.START_PENDING

    def query(name):
        polls.append(name)
        record = env.manager.services.get(name)
        if len(polls) > 3 and record is not None and record["state"] == scm.START_PENDING:
            record["state"] = scm.RUNNING
        return original_query(name)

    env.manager.start = start_pending
    env.manager.query = query
    code, _out, err = run_install(env)
    assert code == 0, err
    # Health is asked only once the service runs (and has bound its port).
    assert env.http_log[0] == ("GET", "/health", False)
    assert env.clock > 0


def test_administrators_own_new_files(env):
    assert run_install(env)[0] == 0
    assert env.events[0] == ("default_owner",)
    assert run_uninstall(env)[0] == 0
    assert env.events.count(("default_owner",)) == 2


def test_a_failed_owner_change_only_warns(env):
    env.owner_error = OSError(None, "not allowed", None, 1307)
    code, _out, err = run_install(env)
    assert code == 0
    assert "cannot make Administrators the owner" in err


def test_not_elevated_changes_no_owner(env):
    env.elevated = False
    run_install(env)
    assert ("default_owner",) not in env.events


def test_fresh_install_checks_the_source_folder(env):
    assert run_install(env)[0] == 0
    assert ("check_source", ntpath.dirname(USER_EXE), USER_SID) in env.events


def test_an_unsafe_source_folder_is_refused_before_any_change(env):
    env.source_problems = ["rootmind-backend.exe: S-1-5-11 may change it"]
    code, _out, err = run_install(env)
    assert code == ExitCode.UNSAFE_PROGRAM_FOLDER == 10
    assert "can be changed by accounts other than administrators" in err
    assert "S-1-5-11 may change it" in err
    assert not env.manager.services
    assert not env.root.exists()
    assert env.folders == 0


def test_an_update_from_an_unsafe_source_leaves_the_services_running(env):
    install_existing(env, mcp_port=3000)
    env.source_problems = [r"C:\Downloads\rootmind-backend: S-1-5-32-545 may change it"]
    code, _out, _err = run_install(env)
    assert code == ExitCode.UNSAFE_PROGRAM_FOLDER
    assert not any(call[0] in ("stop", "update", "delete") for call in env.manager.calls)
    assert env.manager.services[BACKEND_SERVICE]["state"] == scm.RUNNING


def test_the_registered_copy_is_not_checked_again(env):
    install_existing(env, folder="1.2.0-0a1b2c3d")
    env.executable = program_exe("1.2.0-0a1b2c3d")
    env.source_problems = ["would refuse"]
    code, _out, err = run_install(env, port="auto", mcp_port="auto")
    assert code == 0, err
    assert not any(event[0] == "check_source" for event in env.events)


def test_an_unprotected_program_folder_is_refused(env):
    def refuse(*args):
        raise layout.LayoutError("RootMind Service exists but is not protected", ["x: S-1-1-0"])

    env.install_program = refuse
    code, _out, err = run_install(env)
    assert code == ExitCode.UNEXPECTED
    assert "not protected" in err and "S-1-1-0" in err
    assert "Traceback" not in (env.root / "logs" / "install.log").read_text()
    assert BACKEND_SERVICE not in env.manager.services


def test_unexpected_errors_are_logged(env):
    def broken(*args):
        raise RuntimeError("disk on fire")

    env.install_program = broken
    code, _out, err = run_install(env)
    assert code == ExitCode.UNEXPECTED
    assert "disk on fire" in err
    log = (env.root / "logs" / "install.log").read_text()
    assert "Traceback" in log and "install: exit 1" in log


# --- uninstall ------------------------------------------------------------------------------


def test_uninstall_not_elevated(env):
    env.elevated = False
    assert run_uninstall(env)[0] == ExitCode.NOT_ELEVATED


def test_uninstall_nothing_installed(env):
    code, out, _err = run_uninstall(env)
    assert code == 0
    assert "nothing to do" in out


def test_uninstall_refuses_a_foreign_service(env):
    env.manager.add(BACKEND_SERVICE, r"C:\tools\other-service.exe")
    code, _out, _err = run_uninstall(env)
    assert code == ExitCode.FOREIGN_SERVICE
    assert env.manager.services[BACKEND_SERVICE]["image_path"] == r"C:\tools\other-service.exe"
    assert not env.manager.calls


def test_uninstall_removes_services_and_the_rest_token_only(env):
    install_existing(env, mcp_port=3000)
    code, out, err = run_uninstall(env)
    assert code == 0, err
    assert not env.manager.services
    assert ("wait_process_exit", 100) in env.events and ("wait_process_exit", 101) in env.events
    assert not (env.root / "data" / "rest_token.txt").exists()
    assert (env.root / "data" / "api_token.txt").read_text() == "mcp-token-from-before"
    assert (env.root / "config.yaml").exists()
    assert ("remove_all_programs",) in env.events
    assert "The MCP token was kept in" in out
    assert "uninstall: exit 0" in (env.root / "logs" / "install.log").read_text()


def test_uninstall_does_not_log_into_an_unchecked_folder(env):
    install_existing(env)
    env.data_problem_list = ["logs: is a link or reparse point"]
    assert run_uninstall(env)[0] == 0
    assert not (env.root / "logs" / "install.log").exists()


@pytest.mark.parametrize("purge", [False, True])
def test_uninstall_leaves_an_unprotected_data_folder_alone(env, purge):
    """A folder that other accounts may change is not touched, not even the token in it."""
    install_existing(env)
    env.data_problem_list = ["data: S-1-5-32-545 may change it"]
    code, _out, err = run_uninstall(env, purge=purge)
    assert code == 0
    assert not env.manager.services  # the services are removed all the same
    assert ("remove_all_programs",) in env.events
    assert (env.root / "data" / "rest_token.txt").exists()
    assert ("remove_data",) not in env.events
    assert "left unchanged" in err


def test_uninstall_twice(env):
    install_existing(env)
    assert run_uninstall(env)[0] == 0
    code, out, _err = run_uninstall(env)
    assert code == 0
    assert "nothing to do" in out


def test_uninstall_purge_removes_the_data(env):
    install_existing(env, mcp_port=3000)
    code, out, err = run_uninstall(env, purge=True)
    assert code == 0, err
    assert not env.root.exists()
    assert "The MCP token was kept" not in out


def test_uninstall_purge_refuses_links(env):
    install_existing(env)

    def refuse(root):
        raise layout.LayoutError("contains links")

    env.remove_data = refuse
    assert run_uninstall(env, purge=True)[0] == ExitCode.DATA_FOLDER_REFUSED


def test_uninstall_from_the_program_files_exe_reports_pending_files(env):
    install_existing(env, folder="1.2.0-0a1b2c3d")
    env.executable = program_exe("1.2.0-0a1b2c3d")
    code, out, _err = run_uninstall(env)
    assert code == 0
    assert "deleted at the next restart" in out


# --- status ---------------------------------------------------------------------------------


STATUS_KEYS = {
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
}


def test_status_not_installed(env):
    info = install.status(env)
    assert set(info) == STATUS_KEYS
    assert info["installed"] is False and info["ours"] is False
    assert info["mcp_http"] == {
        "installed": False,
        "state": None,
        "port": None,
        "token_file": None,
        "token_readable": False,
    }
    assert info["warnings"] == []


def test_status_installed_and_running(env):
    install_existing(env, mcp_port=3000, folder="1.2.0-0a1b2c3d")
    info = install.status(env)
    assert info == {
        "installed": True,
        "ours": True,
        "state": "running",
        "port": 8000,
        "version": VERSION,
        "binary": program_exe("1.2.0-0a1b2c3d"),
        "user_sid": USER_SID,
        "token_file": str(env.root / "data" / "rest_token.txt"),
        "token_readable": True,
        "mcp_http": {
            "installed": True,
            "state": "running",
            "port": 3000,
            "token_file": str(env.root / "data" / "api_token.txt"),
            "token_readable": True,
        },
        "warnings": [],
    }
    json.dumps(info)


def test_status_warnings(env):
    install_existing(env)
    env.manager.services[BACKEND_SERVICE]["state"] = scm.STOPPED
    (env.root / "data" / "rest_token.txt").unlink()
    env.app_present = False
    info = install.status(env)
    assert info["version"] is None
    assert "service is stopped" in info["warnings"]
    assert "token not readable for this account" in info["warnings"]
    hint = [w for w in info["warnings"] if w.startswith("RootMind is not installed")]
    assert hint and f'"{program_exe("1.2.0-11111111")}" service uninstall' in hint[0]


def test_status_uninstall_hint_only_for_the_stored_account(env):
    install_existing(env, sid=OTHER_SID)
    env.app_present = False
    assert not any(w.startswith("RootMind is not") for w in install.status(env)["warnings"])


def test_status_foreign_service(env):
    env.manager.add(BACKEND_SERVICE, r"C:\tools\other-service.exe")
    info = install.status(env)
    assert info["installed"] is True and info["ours"] is False
    assert info["port"] is None and info["token_file"] is None
    assert "service is not ours" in info["warnings"]


def test_format_status(env):
    install_existing(env, mcp_port=3000)
    text = install.format_status(install.status(env))
    assert "running" in text and "http://127.0.0.1:8000" in text and "/mcp" in text


# --- command line --------------------------------------------------------------------------


def run_cli(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv, env=env)
    return code, out.getvalue(), err.getvalue()


def test_cli_status_json(env):
    code, out, _err = run_cli(["status", "--json"], env)
    assert code == 0
    assert set(json.loads(out)) == STATUS_KEYS


def test_cli_status_text(env):
    code, out, _err = run_cli(["status"], env)
    assert code == 0 and "not installed" in out


def test_cli_status_reports_scm_errors_without_a_traceback(env):
    def broken(*, write):
        raise scm.ScmError(None, "OpenSCManager: access denied", None, 5)

    env.scm = broken
    code, _out, err = run_cli(["status"], env)
    assert code == ExitCode.UNEXPECTED
    assert "cannot read the state of the background service" in err
    assert "Traceback" not in err


def test_cli_windows_only(env):
    env.windows = False
    for argv in (["status"], ["install"], ["uninstall"]):
        code, _out, err = run_cli(argv, env)
        assert code == ExitCode.USAGE
        assert "Windows only" in err


@pytest.mark.parametrize(
    "argv",
    [
        # The exact command lines the desktop app builds (desktop/src-tauri/src/service.rs).
        ["install", "--port", "auto", "--no-mcp-http", "--user-sid", USER_SID],
        ["install", "--port", "auto", "--mcp-http", "--mcp-port", "auto", "--user-sid", USER_SID],
        ["install", "--port", "auto", "--user-sid", USER_SID],
        ["install", "--mcp-http", "--mcp-port", "auto"],
        ["install", "--no-mcp-http"],
        ["uninstall"],
        ["uninstall", "--purge"],
        ["status", "--json"],
        # Every file option with fixed ports, without starting (command line).
        [
            "install",
            "--port",
            "8000",
            "--mcp-http",
            "--mcp-port",
            "3000",
            "--mcp-token-from",
            r"C:\data\api_token.txt",
            "--no-start",
        ],
    ],
)
def test_cli_accepts_the_app_command_lines(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.command == argv[0]


def test_cli_install_options_reach_the_installer(env, monkeypatch):
    seen = {}

    def fake_install(env_, options, out, err):
        seen["options"] = options
        return 0

    monkeypatch.setattr(install, "install", fake_install)
    code, _out, _err = run_cli(
        ["install", "--port", "auto", "--mcp-http", "--mcp-port", "18401", "--user-sid", USER_SID],
        env,
    )
    assert code == 0
    options = seen["options"]
    assert (options.port, options.mcp, options.mcp_port, options.user_sid, options.start) == (
        "auto",
        True,
        18401,
        USER_SID,
        True,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["install", "--port", "0"],
        ["install", "--port", "x"],
        ["install", "--mcp-http", "--no-mcp-http"],
        ["install", "--user-sid", "me"],
        ["run", "backend"],
        ["run", "indexer", "--port", "8000"],
        ["run", "backend", "--port", "0", "--user-sid", USER_SID],
        [],
    ],
)
def test_cli_usage_errors(argv):
    with pytest.raises(SystemExit) as raised, redirect_stderr(io.StringIO()):
        cli.build_parser().parse_args(argv)
    assert raised.value.code == 2


def test_cli_run_backend_needs_the_account(env):
    code, _out, err = run_cli(["run", "backend", "--port", "18000"], env)
    assert code == ExitCode.USAGE
    assert "--user-sid" in err


def test_cli_run_is_not_listed():
    assert "run" not in cli.build_parser().format_help().replace("Run RootMind", "")


def test_rootmind_service_dispatch_outside_windows(monkeypatch):
    """`rootmind service ...` reaches this package; elsewhere than Windows it exits 2."""
    from rootmind import main as main_module

    if sys.platform == "win32":
        pytest.skip("checks the non-Windows message")
    with pytest.raises(SystemExit) as raised, redirect_stderr(io.StringIO()) as err:
        main_module.main(["service", "status"])
    assert raised.value.code == 2
    assert "Windows only" in err.getvalue()
