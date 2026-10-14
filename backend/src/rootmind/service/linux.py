"""``service install``, ``uninstall`` and ``status`` on Linux (systemd user units).

The same commands as on Windows (:mod:`.install`), for the account that runs
them and without root: two systemd user units in
``$XDG_CONFIG_HOME/systemd/user/`` (``rootmind-backend.service``, and
``rootmind-mcp-http.service`` while MCP over HTTP is on), the data in
``$XDG_DATA_HOME/rootmind`` and the program copy in its ``runtime/``
(:mod:`.linux_layout`). The units run as the account, so the file modes are
the access control; there is no folder access policy and no second account.

Install (and update, when our units exist):

1. Preconditions: Linux and the release build (2), not root (8), the
   account's service manager answers (11). ``--user-sid`` is refused (2).
2. A unit with our name that we did not write: exit 3, untouched. Our
   units: update mode, port and MCP on/off read back from them (sticky).
3. Ports for new units (busy: 4); the program folder to copy from must be
   changeable by nobody but root and this account (10).
4. Update mode: stop the units.
5. The data folder: created 0700, or validated and normalized (refused: 6).
6. Copy the program into ``runtime/<version>-<id>/`` (skipped when this
   executable is already the copy the units run).
7. Config, 8. tokens (0600) and the remote-access status file, as on Windows.
9. Write the unit files, ``daemon-reload``, ``enable``; ``--no-mcp-http``
   stops, disables and deletes the MCP unit.
10. Start them and check health and authentication (7). A token is sent only
    once the unit's own process owns the listening socket.
11. Delete older program copies, write ``service.json`` (where the program
    copy is; no secret) and append to ``logs/install.log``.

The units start when the account logs in and stop when its last session
ends. ``loginctl enable-linger`` keeps them running without a session (for
an agent such as n8n that calls RootMind at any time); install only says so.
"""

from __future__ import annotations

import contextlib
import json
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    API_FALLBACK_PORTS,
    DEFAULT_API_PORT,
    DEFAULT_MCP_PORT,
    LINUX_EXE_NAME,
    MCP_FALLBACK_PORTS,
    ROLE_BACKEND,
    ROLE_MCP,
    SERVICE_DATABASE_PATH,
    UNIT_BY_ROLE,
    ExitCode,
    ServiceCommandError,
    linux_layout,
    ports,
    release_linux_url,
    systemd,
)
from .common import (
    DEFAULT_CONFIG,
    Report,
    check_auth,
    database_in_service_folder,
    ensure_status_file,
    ensure_token,
    mcp_initialize,
    read_token_file,
    run_command,
    token_readable,
    turn_remote_access_off,
    write_atomic,
)

STOP_TIMEOUT_SECONDS = 60
HEALTH_TIMEOUT_SECONDS = 90
ROLES = (ROLE_BACKEND, ROLE_MCP)


@dataclass
class InstallOptions:
    """The options of ``service install`` (as on Windows; ``user_sid`` is refused)."""

    port: int | str | None = None
    mcp: bool | None = None
    mcp_port: int | str | None = None
    mcp_token_from: Path | None = None
    user_sid: str | None = None
    start: bool = True


def _require_linux(env) -> None:
    if not env.is_linux():
        raise ServiceCommandError(
            ExitCode.USAGE, "The background service runs on Windows and Linux only."
        )


def _require_user(env) -> None:
    if env.uid() == 0:
        raise ServiceCommandError(
            ExitCode.ACCOUNT_MISMATCH,
            "Run this command as the account that uses RootMind, not as root: the service "
            "runs as that account's systemd user units (no root needed).",
        )


def _manager(env) -> systemd.Systemctl:
    manager = env.systemctl()
    try:
        manager.check()
    except systemd.UserManagerUnavailableError as e:
        raise ServiceCommandError(
            ExitCode.NO_USER_MANAGER,
            f"The systemd user manager of this account cannot be reached ({e}). Run the "
            "command in a desktop or SSH login of the account, not through su or sudo. "
            "RootMind needs systemd.",
        ) from None
    except systemd.SystemctlError as e:
        raise ServiceCommandError(ExitCode.UNEXPECTED, str(e)) from None
    return manager


def _foreign(unit: str, where: str) -> ServiceCommandError:
    return ServiceCommandError(
        ExitCode.FOREIGN_SERVICE,
        f"A systemd user unit named {unit} exists that RootMind did not write ({where}). It was "
        "not changed. Remove it, run systemctl --user daemon-reload, then try again.",
    )


def read_units(env, root: Path, manager: systemd.Systemctl | None) -> dict[str, Any]:
    """Our units by role (``systemd.Unit``), None where there is none.

    Raises:
        ServiceCommandError: exit 3 for a unit with our name that is not ours,
            in our folder or anywhere else systemd loads it from.
    """
    found: dict[str, Any] = {}
    unit_dir = env.unit_dir()
    for role in ROLES:
        path = systemd.unit_path(unit_dir, role)
        name = UNIT_BY_ROLE[role]
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        except (OSError, UnicodeDecodeError) as e:
            raise ServiceCommandError(ExitCode.UNEXPECTED, f"cannot read {path}: {e}") from None
        if text is None:
            found[role] = None
            if manager is not None:
                state = manager.show(name)
                # A unit systemd still knows from our folder is a stale entry
                # (deleted without daemon-reload), not another program's.
                if state.loaded and state.fragment_path and state.fragment_path != str(path):
                    raise _foreign(name, state.fragment_path)
            continue
        unit = systemd.parse_unit(text)
        if unit is None or unit.role != role or not linux_layout.exe_is_ours(unit.exe, root):
            raise _foreign(name, str(path))
        found[role] = unit
    return found


# --- install ---------------------------------------------------------------------------------


@dataclass
class _Plan:
    update: bool
    api_port: int
    mcp_enabled: bool
    mcp_port: int | None
    units: dict[str, Any]
    states: dict[str, systemd.UnitState]


def _plan(env, options: InstallOptions, manager, root: Path, report: Report) -> _Plan:
    units = read_units(env, root, manager)
    states = {role: manager.show(UNIT_BY_ROLE[role]) for role in ROLES}
    backend, mcp = units[ROLE_BACKEND], units[ROLE_MCP]
    if options.mcp is not None:
        mcp_enabled = options.mcp
    elif backend is not None or mcp is not None:
        mcp_enabled = mcp is not None
    else:
        mcp_enabled = True
    if options.mcp_token_from is not None and not mcp_enabled:
        raise ServiceCommandError(ExitCode.USAGE, "--mcp-token-from needs --mcp-http")

    if backend is not None:
        api_port = backend.port
        if isinstance(options.port, int) and options.port != api_port:
            report.warn(f"the REST API keeps port {api_port}; uninstall first to change it")
    else:
        api_port = ports.choose_port(
            options.port,
            DEFAULT_API_PORT,
            API_FALLBACK_PORTS,
            what="REST API",
            flag="--port",
            taken=[mcp.port] if mcp is not None else [],
            bindable=env.can_bind,
        )
    mcp_port = None
    if mcp_enabled:
        if mcp is not None:
            mcp_port = mcp.port
            if isinstance(options.mcp_port, int) and options.mcp_port != mcp_port:
                report.warn(f"the MCP server keeps port {mcp_port}; turn it off first to change it")
        else:
            mcp_port = ports.choose_port(
                options.mcp_port,
                DEFAULT_MCP_PORT,
                MCP_FALLBACK_PORTS,
                what="MCP server",
                flag="--mcp-port",
                taken=[api_port],
                bindable=env.can_bind,
            )
    return _Plan(
        update=backend is not None or mcp is not None,
        api_port=api_port,
        mcp_enabled=mcp_enabled,
        mcp_port=mcp_port,
        units=units,
        states=states,
    )


def _registered_exe(plan: _Plan) -> str | None:
    for role in ROLES:
        if plan.units[role] is not None:
            return plan.units[role].exe
    return None


def _check_source(env, plan: _Plan) -> None:
    """Refuse to copy the program from a folder another account can change (10)."""
    registered = _registered_exe(plan)
    if registered is not None and env.same_path(env.executable, registered):
        return
    folder = posixpath.dirname(env.executable)
    problems = env.program_source_problems(folder)
    if problems:
        detail = "".join(f"\n  {p}" for p in problems[:20])
        raise ServiceCommandError(
            ExitCode.UNSAFE_PROGRAM_FOLDER,
            f"{folder} can be changed by accounts other than root and this one, so the service "
            f"is not installed from it. Install RootMind from its package or AppImage, or "
            f"unpack the release build into a folder only you can change.{detail}",
        )


def _wait_stopped(env, manager, name: str) -> None:
    deadline = env.monotonic() + STOP_TIMEOUT_SECONDS
    while manager.show(name).state not in (systemd.STOPPED, systemd.OTHER):
        if env.monotonic() >= deadline:
            raise ServiceCommandError(
                ExitCode.UNEXPECTED, f"{name} did not stop within {STOP_TIMEOUT_SECONDS} seconds."
            )
        env.sleep(0.5)


def _logs_hint(root: Path, role: str) -> str:
    return f"See journalctl --user -u {UNIT_BY_ROLE[role]} and the log in {root / 'logs' / role}."


def _wait_ready(env, manager, role: str, url: str, root: Path) -> int:
    """Wait until the unit is active and ``/health`` answers; returns its main PID."""
    name = UNIT_BY_ROLE[role]
    deadline = env.monotonic() + HEALTH_TIMEOUT_SECONDS
    while True:
        state = manager.show(name)
        if state.active_state in ("failed", "inactive"):
            code = f" (exit code {state.exec_main_status})" if state.exec_main_status else ""
            raise ServiceCommandError(
                ExitCode.HEALTH_CHECK_FAILED, f"{name} stopped{code}. {_logs_hint(root, role)}"
            )
        if state.state == systemd.RUNNING and state.main_pid:
            status, _body = env.http("GET", f"{url}/health")
            if status == 200:
                return state.main_pid
        if env.monotonic() >= deadline:
            raise ServiceCommandError(
                ExitCode.HEALTH_CHECK_FAILED,
                f"{name} did not answer on {url} within {HEALTH_TIMEOUT_SECONDS} seconds. "
                f"{_logs_hint(root, role)}",
            )
        env.sleep(0.5)


def _check_listener(env, name: str, port: int, pid: int) -> None:
    if not env.port_owned_by(port, pid):
        raise ServiceCommandError(
            ExitCode.HEALTH_CHECK_FAILED,
            f"Another program listens on 127.0.0.1:{port}, not {name} (PID {pid}). "
            "No token was sent to it. Stop that program, then run this command again.",
        )


def _start(manager, role: str, root: Path) -> None:
    name = UNIT_BY_ROLE[role]
    try:
        manager.start(name)
    except systemd.SystemctlError as e:
        raise ServiceCommandError(
            ExitCode.HEALTH_CHECK_FAILED, f"{name} did not start: {e}. {_logs_hint(root, role)}"
        ) from None


def _start_and_check(env, manager, plan: _Plan, root: Path, tokens: dict[str, str]) -> None:
    api_url = f"http://127.0.0.1:{plan.api_port}"
    name = UNIT_BY_ROLE[ROLE_BACKEND]
    _start(manager, ROLE_BACKEND, root)
    pid = _wait_ready(env, manager, ROLE_BACKEND, api_url, root)
    _check_listener(env, name, plan.api_port, pid)
    check_auth(
        name,
        [
            ("GET /status without the token", env.http("GET", f"{api_url}/status")[0], 401),
            (
                "GET /status with the token",
                env.http("GET", f"{api_url}/status", token=tokens["rest"])[0],
                200,
            ),
        ],
    )
    if not plan.mcp_enabled:
        return
    mcp_url = f"http://127.0.0.1:{plan.mcp_port}"
    name = UNIT_BY_ROLE[ROLE_MCP]
    _start(manager, ROLE_MCP, root)
    pid = _wait_ready(env, manager, ROLE_MCP, mcp_url, root)
    _check_listener(env, name, plan.mcp_port, pid)
    check_auth(
        name,
        [
            ("MCP initialize without the token", mcp_initialize(env, mcp_url, None), 401),
            ("MCP initialize with the token", mcp_initialize(env, mcp_url, tokens["mcp"]), 200),
        ],
    )


def _prepare_files(
    env, plan: _Plan, root: Path, mcp_token: str | None, report: Report
) -> tuple[str, str | None, dict[str, str]]:
    """Steps 6-8: program, config and tokens. Returns the exe, the new folder and the tokens."""
    registered = _registered_exe(plan)
    new_folder = None
    if registered is not None and env.same_path(env.executable, registered):
        exe = registered
    else:
        try:
            new_folder = env.install_program(posixpath.dirname(env.executable), env.version)
        except linux_layout.LayoutError as e:
            detail = "".join(f"\n  {p}" for p in e.problems[:20])
            raise ServiceCommandError(ExitCode.UNEXPECTED, f"{e}{detail}") from None
        exe = posixpath.join(new_folder, LINUX_EXE_NAME)

    config_file = root / "config.yaml"
    if not config_file.exists():
        write_atomic(config_file, DEFAULT_CONFIG)
    elif not database_in_service_folder(config_file, root, posixpath):
        from ..config import import_config

        try:
            import_config(config_file, root, database_path=SERVICE_DATABASE_PATH)
            report.info(f"The index moves to {root / 'data' / 'db'}; it is rebuilt.")
        except (OSError, ValueError) as e:
            report.warn(f"could not move the index into {root / 'data' / 'db'}: {e}")

    tokens = {"rest": ensure_token(root / "data" / "rest_token.txt")}
    if plan.mcp_enabled:
        mcp_token_file = root / "data" / "api_token.txt"
        if mcp_token is not None:
            write_atomic(mcp_token_file, mcp_token)
            tokens["mcp"] = mcp_token
        else:
            tokens["mcp"] = ensure_token(mcp_token_file)
        ensure_status_file(root / "data" / "remote_access.json", 0o600)
    else:
        turn_remote_access_off(config_file, report)
    # Files written by hand or by an older version get their modes.
    env.normalize_data(root)
    return exe, new_folder, tokens


def _write_units(env, manager, plan: _Plan, exe: str) -> None:
    unit_dir = env.unit_dir()
    unit_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    roles = [ROLE_BACKEND] + ([ROLE_MCP] if plan.mcp_enabled else [])
    for role in roles:
        port = plan.api_port if role == ROLE_BACKEND else (plan.mcp_port or 0)
        write_atomic(
            systemd.unit_path(unit_dir, role), systemd.build_unit(exe, role, port), mode=0o644
        )
    removed = not plan.mcp_enabled and plan.units[ROLE_MCP] is not None
    mcp = UNIT_BY_ROLE[ROLE_MCP]
    if removed:
        manager.stop(mcp)
        manager.disable(mcp)
        with contextlib.suppress(FileNotFoundError):
            systemd.unit_path(unit_dir, ROLE_MCP).unlink()
    manager.daemon_reload()
    if removed:
        manager.reset_failed(mcp)
    manager.enable(*(UNIT_BY_ROLE[role] for role in roles))


def write_discovery(root: Path, exe: str, version: str) -> None:
    """``service.json``: where the program copy is, for the Claude Desktop bundle (no secret)."""
    payload = {"executable": exe, "version": version}
    write_atomic(root / linux_layout.DISCOVERY_FILE, json.dumps(payload, indent=2) + "\n")


def _install(env, options: InstallOptions, report: Report) -> None:
    _require_linux(env)
    if options.user_sid is not None:
        raise ServiceCommandError(
            ExitCode.USAGE,
            "--user-sid is for Windows; on Linux the service belongs to the account that runs "
            "this command.",
        )
    if not env.is_release_build():
        raise ServiceCommandError(
            ExitCode.USAGE,
            "Install the service from the release build: " + release_linux_url(env.version),
        )
    _require_user(env)
    mcp_token = read_token_file(options.mcp_token_from) if options.mcp_token_from else None
    manager = _manager(env)
    root = env.data_root()
    plan = _plan(env, options, manager, root, report)
    _check_source(env, plan)

    running = [
        UNIT_BY_ROLE[role]
        for role in ROLES
        if plan.units[role] is not None and plan.states[role].state != systemd.STOPPED
    ]
    try:
        for name in running:
            manager.stop(name)
        for name in running:
            _wait_stopped(env, manager, name)
        for role in ROLES:
            pid = plan.states[role].main_pid
            if plan.units[role] is not None and pid:
                if not env.wait_process_exit(pid, STOP_TIMEOUT_SECONDS):
                    report.warn(f"the process of {UNIT_BY_ROLE[role]} (PID {pid}) is still running")
        try:
            env.prepare_data(root)
        except linux_layout.LayoutError as e:
            detail = "".join(f"\n  {p}" for p in e.problems[:20])
            raise ServiceCommandError(
                ExitCode.DATA_FOLDER_REFUSED, f"{root}: {e}{detail}"
            ) from None
        report.log_path = root / "logs" / "install.log"
        exe, new_folder, tokens = _prepare_files(env, plan, root, mcp_token, report)
    except BaseException:
        # Nothing is written yet: the old version keeps serving.
        for name in running:
            with contextlib.suppress(Exception):
                manager.start(name)
        raise

    try:
        _write_units(env, manager, plan, exe)
    except systemd.SystemctlError as e:
        raise ServiceCommandError(ExitCode.UNEXPECTED, str(e)) from None
    if options.start:
        _start_and_check(env, manager, plan, root, tokens)

    left = []
    if new_folder is not None:
        left = env.remove_old_programs(new_folder)
    write_discovery(root, exe, env.version)
    if left:
        report.warn(f"could not delete {len(left)} older program folder(s): {', '.join(left)}")

    verb = "updated" if plan.update else "installed"
    report.info(f"RootMind background service {verb} (version {env.version}).")
    report.info(f"  REST API: http://127.0.0.1:{plan.api_port}")
    report.info(f"  Token:    {root / 'data' / 'rest_token.txt'}")
    if plan.mcp_enabled:
        report.info(f"  MCP:      http://127.0.0.1:{plan.mcp_port}/mcp")
        report.info(f"  Token:    {root / 'data' / 'api_token.txt'}")
    report.info(f"  Program:  {posixpath.dirname(exe)}")
    report.info(f"  Units:    {env.unit_dir()}")
    report.info(f"  Logs:     {root / 'logs'}")
    if not options.start:
        report.info("The units were not started (--no-start).")
    if env.linger_enabled() is False:
        report.info(
            "The service runs while you are logged in. To keep it running without a login "
            "(for example for n8n), run: loginctl enable-linger"
        )


def install(env, options: InstallOptions, out=print, err=None) -> int:
    """Run ``service install`` on Linux; returns the exit code."""
    report = Report(out, err or out)
    return run_command("install", report, lambda: _install(env, options, report))


# --- uninstall -------------------------------------------------------------------------------


def _uninstall(env, purge: bool, report: Report) -> None:
    _require_linux(env)
    _require_user(env)
    manager = _manager(env)
    root = env.data_root()
    units = read_units(env, root, manager)
    installed = [role for role in ROLES if units[role] is not None]
    names = [UNIT_BY_ROLE[role] for role in installed]
    states = {role: manager.show(UNIT_BY_ROLE[role]) for role in installed}
    try:
        for name in names:
            manager.stop(name)
        if names:
            manager.disable(*names)
    except systemd.SystemctlError as e:
        report.warn(str(e))
    for role in installed:
        with contextlib.suppress(FileNotFoundError):
            systemd.unit_path(env.unit_dir(), role).unlink()
    if names:
        manager.daemon_reload()
        for name in names:
            manager.reset_failed(name)
    for role in installed:
        pid = states[role].main_pid
        if pid and not env.wait_process_exit(pid, STOP_TIMEOUT_SECONDS):
            report.warn(f"the process of {UNIT_BY_ROLE[role]} (PID {pid}) is still running")

    data_exists = root.is_dir()
    problems = env.data_problems(root) if data_exists else []
    protected = data_exists and not problems
    if protected and not purge:
        report.log_path = root / "logs" / "install.log"
    if data_exists and not protected:
        report.warn(
            f"{root} is not protected like a folder that service install created, so it was "
            "left unchanged. Remove it by hand if you no longer need it."
        )

    removed_token = False
    if protected:
        with contextlib.suppress(FileNotFoundError):
            (root / "data" / "rest_token.txt").unlink()
            removed_token = True
        with contextlib.suppress(FileNotFoundError):
            (root / linux_layout.DISCOVERY_FILE).unlink()
        left = env.remove_all_programs(root)
        if left:
            report.warn(f"could not delete {', '.join(left)}")
        if purge:
            env.remove_data(root)
            report.info(f"Deleted {root}")

    if not installed and not removed_token and not (purge and protected):
        report.info("The background service is not installed; nothing to do.")
    elif installed:
        report.info(f"Removed {', '.join(names)}.")
    if not purge and (root / "data" / "api_token.txt").exists():
        report.info(f"The MCP token was kept in {root / 'data' / 'api_token.txt'}")
    if not purge and (root / "data" / "ngrok_authtoken.txt").exists():
        report.info(
            "The ngrok authtoken for remote access was kept in "
            f"{root / 'data' / 'ngrok_authtoken.txt'}"
        )


def uninstall(env, purge: bool = False, out=print, err=None) -> int:
    """Run ``service uninstall [--purge]`` on Linux; returns the exit code."""
    report = Report(out, err or out)
    return run_command(
        "uninstall --purge" if purge else "uninstall",
        report,
        lambda: _uninstall(env, purge, report),
    )


# --- status ----------------------------------------------------------------------------------


def status(env) -> dict[str, Any]:
    """The state of the background service, in the shape of the Windows status.

    ``user_sid`` is always None; ``unit_dir`` and ``data_dir`` are added. When
    the account's service manager cannot be reached, the states are unknown
    (None) and a warning says why.
    """
    _require_linux(env)
    root = env.data_root()
    warnings: list[str] = []
    manager = env.systemctl()
    try:
        manager.check()
    except systemd.SystemctlError as e:
        warnings.append(f"the systemd user manager cannot be reached: {e}")
        manager = None
    try:
        units = read_units(env, root, manager)
    except ServiceCommandError as e:
        if e.code != ExitCode.FOREIGN_SERVICE:
            raise
        warnings.append(e.message)
        units = {role: _foreign_marker(env, role) for role in ROLES}

    def state(role: str) -> systemd.UnitState | None:
        if manager is None:
            return None
        try:
            return manager.show(UNIT_BY_ROLE[role])
        except systemd.SystemctlError as e:
            warnings.append(str(e))
            return None

    backend = units[ROLE_BACKEND]
    backend_state = state(ROLE_BACKEND) if backend is not None else None
    result: dict[str, Any] = {
        "installed": backend is not None,
        "ours": isinstance(backend, systemd.Unit),
        "state": backend_state.state if backend_state else None,
        "port": None,
        "version": None,
        "binary": None,
        "user_sid": None,
        "token_file": None,
        "token_readable": False,
    }
    if isinstance(backend, systemd.Unit):
        token_file = root / "data" / "rest_token.txt"
        result.update(
            port=backend.port,
            binary=backend.exe,
            token_file=str(token_file),
            token_readable=token_readable(token_file),
        )
        if backend_state is not None and backend_state.state == systemd.RUNNING:
            code, body = env.http("GET", f"http://127.0.0.1:{backend.port}/")
            if code == 200 and isinstance(body, dict) and body.get("name") == "RootMind API":
                result["version"] = body.get("version")
        elif backend_state is not None:
            warnings.append("service is stopped")
        if not result["token_readable"]:
            warnings.append("token not readable for this account")

    mcp = units[ROLE_MCP]
    mcp_state = state(ROLE_MCP) if mcp is not None else None
    mcp_info: dict[str, Any] = {
        "installed": mcp is not None,
        "state": mcp_state.state if mcp_state else None,
        "port": None,
        "token_file": None,
        "token_readable": False,
    }
    if isinstance(mcp, systemd.Unit):
        token_file = root / "data" / "api_token.txt"
        mcp_info.update(
            port=mcp.port, token_file=str(token_file), token_readable=token_readable(token_file)
        )
        if mcp_state is not None and mcp_state.state != systemd.RUNNING:
            warnings.append("MCP service is stopped")
    result["mcp_http"] = mcp_info
    result["unit_dir"] = str(env.unit_dir())
    result["data_dir"] = str(root)
    result["warnings"] = warnings
    return result


def _foreign_marker(env, role: str) -> object | None:
    """For status: a unit file that is not ours stands for "installed, not ours"."""
    path = systemd.unit_path(env.unit_dir(), role)
    if not path.exists():
        return None
    try:
        unit = systemd.parse_unit(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return object()
    if (
        unit is not None
        and unit.role == role
        and linux_layout.exe_is_ours(unit.exe, env.data_root())
    ):
        return unit
    return object()


def format_status(info: dict[str, Any]) -> str:
    """``status`` without ``--json``: a short readable summary."""
    if not info["installed"]:
        lines = ["The RootMind background service is not installed."]
    elif not info["ours"]:
        lines = [
            "A systemd user unit named rootmind-backend.service exists that RootMind did not write."
        ]
    else:
        lines = [
            f"RootMind background service: {info['state'] or 'unknown'}",
            f"  REST API: http://127.0.0.1:{info['port']}"
            + (f" (version {info['version']})" if info["version"] else ""),
            f"  Program:  {info['binary']}",
            f"  Token:    {info['token_file']}"
            + ("" if info["token_readable"] else " (not readable for this account)"),
            f"  Units:    {info['unit_dir']}",
        ]
    mcp = info["mcp_http"]
    if mcp["installed"] and mcp["port"]:
        lines.append(
            f"  MCP:      http://127.0.0.1:{mcp['port']}/mcp ({mcp['state'] or 'unknown'})"
        )
    lines.extend(f"warning: {w}" for w in info["warnings"])
    return "\n".join(lines)
