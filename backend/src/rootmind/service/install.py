"""``service install``, ``uninstall`` and ``status``.

The functions here hold the logic only. Everything that touches the machine
goes through ``env`` (:class:`~.system.WindowsSystem`, or a fake in tests):
the process token, the Service Control Manager, folders and permissions,
ports and HTTP checks. The token and config files are written directly.

Install (and update, when our services already exist):

1. Preconditions: Windows and the release build (2), elevated (5), the
   account is the approving one (8), its access check can be built (9).
2. A service with our name that we did not install: exit 3, untouched. Our
   services: update mode, with port, MCP on/off and MCP port read back from
   the ImagePath ("sticky"); the stored account must be the approving one (8).
3. Ports for new services (:mod:`.ports`; busy: 4).
4. Update mode: stop the services first, so nothing writes into the data
   folder while it is checked.
5. The data folder: created, or validated and normalized (refused: 6).
6. Copy the program into a new version folder (skipped when this executable
   is already the registered copy).
7. Config: ``--import-config``, or a default config when there is none; the
   database is moved into ``data\\db`` when it lies elsewhere.
8. Tokens: ``data\\rest_token.txt``; with MCP ``data\\api_token.txt``
   (``--mcp-token-from``, or kept, or created).
9. Register the services; ``--no-mcp-http`` removes the MCP service.
10. Start them and check health and authentication (failure: 7). A token is
    sent only once the service's own process owns the listening socket.
11. Delete older version folders, append to ``logs\\install.log``.
"""

from __future__ import annotations

import contextlib
import json
import ntpath
import os
import secrets
import stat
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import (
    API_FALLBACK_PORTS,
    BACKEND_SERVICE,
    DEFAULT_API_PORT,
    DEFAULT_MCP_PORT,
    DESCRIPTIONS,
    DISPLAY_NAMES,
    EXE_NAME,
    MCP_FALLBACK_PORTS,
    MCP_SERVICE,
    ROLE_BACKEND,
    ROLE_MCP,
    SERVICE_DATABASE_PATH,
    ExitCode,
    ServiceCommandError,
    imagepath,
    layout,
    ports,
    release_zip_url,
    scm,
)
from .imagepath import ImagePath

STOP_TIMEOUT_SECONDS = 60
DELETE_TIMEOUT_SECONDS = 30
HEALTH_TIMEOUT_SECONDS = 90
MAX_MCP_TOKEN_BYTES = 4096

ROLE_BY_SERVICE = {BACKEND_SERVICE: ROLE_BACKEND, MCP_SERVICE: ROLE_MCP}
MCP_ACCOUNT = f"NT SERVICE\\{MCP_SERVICE}"
MCP_PRIVILEGES = ("SeChangeNotifyPrivilege",)

DEFAULT_CONFIG = f"database:\n  path: {SERVICE_DATABASE_PATH}\n"


@dataclass
class InstallOptions:
    """The options of ``service install``."""

    port: int | str | None = None  # number, "auto", or None (strict default)
    mcp: bool | None = None  # None: keep the current state (off for a new install)
    mcp_port: int | str | None = None
    import_config: Path | None = None
    mcp_token_from: Path | None = None
    user_sid: str | None = None
    start: bool = True


@dataclass
class Report:
    """Messages for the console and ``logs\\install.log``.

    The log is written only once the data folder is known to be ours
    (created or validated in this run), never into an unchecked folder.
    """

    out: Callable[[str], None]
    err: Callable[[str], None]
    lines: list[str] = field(default_factory=list)
    log_path: Path | None = None

    def info(self, message: str) -> None:
        self.lines.append(message)
        self.out(message)

    def warn(self, message: str) -> None:
        self.lines.append(f"warning: {message}")
        self.err(f"warning: {message}")

    def error(self, message: str) -> None:
        self.lines.append(f"error: {message}")
        self.err(f"error: {message}")

    def write_log(self, command: str, code: int, detail: str | None = None) -> None:
        if self.log_path is None:
            return
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = f"{stamp} {command}: exit {int(code)}\n"
        text += "".join(f"  {line}\n" for line in self.lines)
        if detail:
            text += "".join(f"  {line}\n" for line in detail.rstrip().splitlines())
        layout.safe_append(self.log_path, text)


# --- helpers -------------------------------------------------------------------------


def _require_windows(env) -> None:
    if not env.is_windows():
        raise ServiceCommandError(ExitCode.USAGE, "The background service is Windows only.")


def _require_elevated(env, report: Report) -> None:
    if not env.is_elevated():
        raise ServiceCommandError(
            ExitCode.NOT_ELEVATED,
            "This command needs administrator rights. Run it from an elevated prompt "
            '("Run as administrator").',
        )
    # Files this run creates must be owned by Administrators, as the checks of
    # the program and data folders require. An elevated token usually has that
    # default already; an administrator with UAC turned off may not.
    try:
        env.make_administrators_default_owner()
    except OSError as e:
        report.warn(f"cannot make Administrators the owner of new files: {e}")


def _foreign(name: str) -> ServiceCommandError:
    return ServiceCommandError(
        ExitCode.FOREIGN_SERVICE,
        f"A service named {name} exists that RootMind did not install. It was not changed. "
        f"To remove it, run from an elevated prompt:\n  sc.exe stop {name}\n  sc.exe delete {name}",
    )


def _ours(status: scm.ServiceStatus | None, program_files: str) -> ImagePath | None:
    """The parsed ImagePath when ``status`` is one of our services, else None."""
    if status is None:
        return None
    image = imagepath.parse(status.image_path)
    if image is None or image.role != ROLE_BY_SERVICE[status.name]:
        return None
    return image if imagepath.exe_is_ours(image.exe, program_files) else None


def _same_sid(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.upper() == b.upper()


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` (temporary file in the same folder, then a rename)."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _read_token(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _ensure_token(path: Path) -> str:
    """The token in ``path``; a new one is created when the file is missing or empty."""
    token = _read_token(path)
    if token:
        return token
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    token = secrets.token_urlsafe(32)
    from ..auth import write_private_file

    write_private_file(path, token)
    return token


def read_token_file(source: Path) -> str:
    """The token in ``--mcp-token-from FILE``: a regular file of at most 4 KB, one line.

    Raises:
        ServiceCommandError: exit 2 when the file is refused.
    """

    def refuse(reason: str) -> ServiceCommandError:
        return ServiceCommandError(ExitCode.USAGE, f"--mcp-token-from {source}: {reason}")

    try:
        info = os.lstat(source)
    except OSError as e:
        raise refuse(e.strerror or "cannot be read") from None
    attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
        raise refuse("is a link or reparse point")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise refuse("is not a regular file")
    if info.st_size > MAX_MCP_TOKEN_BYTES:
        raise refuse("is larger than 4 KB")
    try:
        token = source.read_bytes().decode("utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        raise refuse("cannot be read as UTF-8 text") from None
    if not token:
        raise refuse("is empty")
    if any(c.isspace() for c in token):
        raise refuse("must hold a single token on one line")
    return token


def _wait_stopped(env, manager, name: str) -> None:
    try:
        scm.wait_for(
            lambda: manager.query(name),
            lambda s: s is None or s.state == scm.STOPPED,
            STOP_TIMEOUT_SECONDS,
            sleep=env.sleep,
            clock=env.monotonic,
        )
    except TimeoutError:
        raise ServiceCommandError(
            ExitCode.UNEXPECTED, f"{name} did not stop within {STOP_TIMEOUT_SECONDS} seconds."
        ) from None


def _wait_deleted(env, manager, name: str, report: Report) -> None:
    try:
        scm.wait_for(
            lambda: manager.query(name),
            lambda s: s is None,
            DELETE_TIMEOUT_SECONDS,
            sleep=env.sleep,
            clock=env.monotonic,
        )
    except TimeoutError:
        report.warn(f"{name} is marked for deletion; it disappears once nothing uses it")


# --- install -------------------------------------------------------------------------


@dataclass
class _Plan:
    update: bool
    user_sid: str
    api_port: int
    mcp_enabled: bool
    mcp_port: int | None
    current: dict[str, scm.ServiceStatus | None]
    images: dict[str, ImagePath]


def _plan(env, options: InstallOptions, manager, report: Report) -> _Plan:
    """Steps 1-3: preconditions, existing services and ports. Nothing is changed."""
    me = env.process_user_sid()
    user_sid = options.user_sid or me
    if not imagepath.is_sid(user_sid):
        raise ServiceCommandError(ExitCode.USAGE, f"--user-sid {user_sid!r} is not a SID")
    if not _same_sid(user_sid, me):
        raise ServiceCommandError(
            ExitCode.ACCOUNT_MISMATCH,
            f"The account that approved this command ({me}) is not the account {user_sid}. "
            "Approve it with the account that uses RootMind; it must be an administrator.",
        )
    from ..winaccess import AccessContextError

    try:
        env.check_access(user_sid)
    except AccessContextError as e:
        raise ServiceCommandError(
            ExitCode.ACCESS_CONTEXT,
            f"Cannot set up the folder access check for this account: {e}",
        ) from None

    program_files = env.program_files()
    current = {name: manager.query(name) for name in (BACKEND_SERVICE, MCP_SERVICE)}
    images: dict[str, ImagePath] = {}
    for name, status in current.items():
        if status is None:
            continue
        image = _ours(status, program_files)
        if image is None:
            raise _foreign(name)
        images[name] = image

    backend = images.get(BACKEND_SERVICE)
    mcp = images.get(MCP_SERVICE)
    if backend is not None and not _same_sid(backend.user_sid, user_sid):
        raise ServiceCommandError(
            ExitCode.ACCOUNT_MISMATCH,
            "This background service belongs to another account on this computer. "
            "Uninstall it first (service uninstall).",
        )

    mcp_enabled = options.mcp if options.mcp is not None else mcp is not None
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
        update=bool(images),
        user_sid=user_sid,
        api_port=api_port,
        mcp_enabled=mcp_enabled,
        mcp_port=mcp_port,
        current=current,
        images=images,
    )


def _specs(plan: _Plan, exe: str) -> dict[str, scm.ServiceSpec]:
    specs = {
        BACKEND_SERVICE: scm.ServiceSpec(
            name=BACKEND_SERVICE,
            display_name=DISPLAY_NAMES[BACKEND_SERVICE],
            description=DESCRIPTIONS[BACKEND_SERVICE],
            image_path=imagepath.build(exe, ROLE_BACKEND, plan.api_port, plan.user_sid),
            account=scm.LOCAL_SYSTEM,
            sid_type=scm.SERVICE_SID_TYPE_NONE,
        )
    }
    if plan.mcp_enabled and plan.mcp_port is not None:
        specs[MCP_SERVICE] = scm.ServiceSpec(
            name=MCP_SERVICE,
            display_name=DISPLAY_NAMES[MCP_SERVICE],
            description=DESCRIPTIONS[MCP_SERVICE],
            image_path=imagepath.build(exe, ROLE_MCP, plan.mcp_port),
            account=MCP_ACCOUNT,
            sid_type=scm.SERVICE_SID_TYPE_UNRESTRICTED,
            required_privileges=MCP_PRIVILEGES,
        )
    return specs


def _wait_ready(env, manager, name: str, url: str, root: Path, log_folder: str) -> int:
    """Wait until the SCM reports ``name`` running and ``/health`` answers; returns its PID.

    The service reports running only after it has bound its port.
    """
    deadline = env.monotonic() + HEALTH_TIMEOUT_SECONDS
    logs = root / "logs" / log_folder
    while True:
        current = manager.query(name)
        if current is None or current.state == scm.STOPPED:
            code = ""
            if current is not None:
                code = f" (exit code {current.win32_exit_code}/{current.service_exit_code})"
            raise ServiceCommandError(
                ExitCode.HEALTH_CHECK_FAILED, f"{name} stopped{code}. See the log in {logs}."
            )
        if current.state == scm.RUNNING and current.pid:
            status, _body = env.http("GET", f"{url}/health")
            if status == 200:
                return current.pid
        if env.monotonic() >= deadline:
            raise ServiceCommandError(
                ExitCode.HEALTH_CHECK_FAILED,
                f"{name} did not answer on {url} within {HEALTH_TIMEOUT_SECONDS} seconds. "
                f"See the log in {logs}.",
            )
        env.sleep(0.5)


def _check_listener(env, name: str, port: int, pid: int) -> None:
    """Refuse to send a token unless the service's own process listens on ``port``.

    Another program (of any account, or WSL port forwarding) that bound the
    port while the service was stopped would otherwise receive the token.
    """
    if not env.port_owned_by(port, pid):
        raise ServiceCommandError(
            ExitCode.HEALTH_CHECK_FAILED,
            f"Another program listens on 127.0.0.1:{port}, not {name} (PID {pid}). "
            "No token was sent to it. Stop that program, then run this command again.",
        )


def _check_auth(name: str, results: list[tuple[str, int, int]]) -> None:
    for what, status, expected in results:
        if status != expected:
            raise ServiceCommandError(
                ExitCode.HEALTH_CHECK_FAILED,
                f"{name}: {what} answered {status or 'nothing'} instead of {expected}.",
            )


def _mcp_initialize(env, url: str, token: str | None) -> int:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "rootmind-service-install", "version": env.version},
        },
    }
    status, _payload = env.http("POST", f"{url}/mcp", token=token, body=body)
    return status


def _start_and_check(env, manager, plan: _Plan, root: Path, tokens: dict[str, str]) -> None:
    api_url = f"http://127.0.0.1:{plan.api_port}"
    try:
        manager.start(BACKEND_SERVICE)
    except scm.ScmError as e:
        raise ServiceCommandError(
            ExitCode.HEALTH_CHECK_FAILED, f"{BACKEND_SERVICE} did not start: {e}"
        ) from None
    pid = _wait_ready(env, manager, BACKEND_SERVICE, api_url, root, "backend")
    _check_listener(env, BACKEND_SERVICE, plan.api_port, pid)
    _check_auth(
        BACKEND_SERVICE,
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
    try:
        manager.start(MCP_SERVICE)
    except scm.ScmError as e:
        raise ServiceCommandError(
            ExitCode.HEALTH_CHECK_FAILED, f"{MCP_SERVICE} did not start: {e}"
        ) from None
    pid = _wait_ready(env, manager, MCP_SERVICE, mcp_url, root, "mcp-http")
    _check_listener(env, MCP_SERVICE, plan.mcp_port, pid)
    _check_auth(
        MCP_SERVICE,
        [
            ("MCP initialize without the token", _mcp_initialize(env, mcp_url, None), 401),
            ("MCP initialize with the token", _mcp_initialize(env, mcp_url, tokens["mcp"]), 200),
        ],
    )


def _database_in_service_folder(config_file: Path, root: Path) -> bool:
    """Whether the config's ``database.path`` lies in ``<root>\\data\\db``.

    An unreadable config counts as fine here: the service reports it.
    """
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return True
    database = data.get("database") if isinstance(data, dict) else None
    raw = database.get("path") if isinstance(database, dict) else None
    if not isinstance(raw, str) or not raw:
        return False  # the default, ./data/index.db
    folder = ntpath.normcase(ntpath.normpath(str(root / "data" / "db")))
    path = raw if ntpath.isabs(raw) else ntpath.join(str(root), raw)
    path = ntpath.normcase(ntpath.normpath(path))
    return path.startswith(folder + "\\")


def _prepare_files(
    env, options: InstallOptions, plan: _Plan, root: Path, mcp_token: str | None, report: Report
) -> tuple[str, str | None, dict[str, str]]:
    """Steps 6-8: program, config and tokens. Returns the exe, the new folder and the tokens."""
    # 6. Program.
    registered = next(iter(plan.images.values())).exe if plan.images else None
    new_folder = None
    if registered is not None and env.same_path(env.executable, registered):
        exe = registered
    else:
        try:
            new_folder = env.install_program(ntpath.dirname(env.executable), env.version)
        except layout.LayoutError as e:
            detail = "".join(f"\n  {p}" for p in e.problems[:20])
            raise ServiceCommandError(ExitCode.UNEXPECTED, f"{e}{detail}") from None
        exe = ntpath.join(new_folder, EXE_NAME)

    # 7. Config.
    config_file = root / "config.yaml"
    if options.import_config is not None:
        from ..config import import_config

        try:
            import_config(options.import_config, root, database_path=SERVICE_DATABASE_PATH)
            report.info(f"Imported the settings from {options.import_config}")
        except (OSError, ValueError) as e:
            report.warn(f"could not import the settings from {options.import_config}: {e}")
    if not config_file.exists():
        _write_atomic(config_file, DEFAULT_CONFIG)
    elif not _database_in_service_folder(config_file, root):
        # A config kept from an earlier setup (or edited by hand) may name a
        # database the MCP account cannot open. The index is rebuilt there.
        from ..config import import_config

        try:
            import_config(config_file, root, database_path=SERVICE_DATABASE_PATH)
            report.info(f"The index moves to {root / 'data' / 'db'}; it is rebuilt.")
        except (OSError, ValueError) as e:
            report.warn(f"could not move the index into {root / 'data' / 'db'}: {e}")

    # 8. Tokens.
    tokens = {"rest": _ensure_token(root / "data" / "rest_token.txt")}
    if plan.mcp_enabled:
        mcp_token_file = root / "data" / "api_token.txt"
        if mcp_token is not None:
            _write_atomic(mcp_token_file, mcp_token)
            tokens["mcp"] = mcp_token
        else:
            tokens["mcp"] = _ensure_token(mcp_token_file)
    # The token files exist now; give them their entries (M may read api_token.txt).
    env.normalize_data(root, plan.user_sid, plan.mcp_enabled)
    return exe, new_folder, tokens


def _install(env, options: InstallOptions, report: Report) -> None:
    _require_windows(env)
    if not env.is_release_build():
        raise ServiceCommandError(
            ExitCode.USAGE,
            "Install the service from the release build: " + release_zip_url(env.version),
        )
    _require_elevated(env, report)
    mcp_token = read_token_file(options.mcp_token_from) if options.mcp_token_from else None

    with env.scm(write=True) as manager:
        plan = _plan(env, options, manager, report)
        root = env.data_root()

        # 4. Update mode: stop what runs, and let the processes end. Nothing
        # writes into the data folder while it is checked, and the old program
        # folder can be deleted afterwards.
        was_running = [name for name in plan.images if plan.current[name].state != scm.STOPPED]
        try:
            for name in plan.images:
                manager.stop(name)
            for name in plan.images:
                _wait_stopped(env, manager, name)
            for name in plan.images:
                pid = plan.current[name].pid
                if pid and not env.wait_process_exit(pid, STOP_TIMEOUT_SECONDS):
                    report.warn(f"the process of {name} (PID {pid}) is still running")

            # 5. Data folder.
            try:
                env.prepare_data(root, plan.user_sid, plan.mcp_enabled)
            except layout.LayoutError as e:
                detail = "".join(f"\n  {p}" for p in e.problems[:20])
                raise ServiceCommandError(ExitCode.DATA_FOLDER_REFUSED, f"{e}{detail}") from None
            report.log_path = root / "logs" / "install.log"

            exe, new_folder, tokens = _prepare_files(env, options, plan, root, mcp_token, report)
        except BaseException:
            # Nothing is registered yet: the old version keeps serving.
            for name in was_running:
                with contextlib.suppress(Exception):
                    manager.start(name)
            raise

        # 9. Register.
        for name, spec in _specs(plan, exe).items():
            if plan.current[name] is not None:
                manager.update(spec)
            else:
                manager.create(spec)
        if not plan.mcp_enabled and plan.current[MCP_SERVICE] is not None:
            manager.stop(MCP_SERVICE)
            _wait_stopped(env, manager, MCP_SERVICE)
            manager.delete(MCP_SERVICE)
            _wait_deleted(env, manager, MCP_SERVICE, report)

        # 10. Start and check.
        if options.start:
            _start_and_check(env, manager, plan, root, tokens)

    # 11. Older program folders.
    if new_folder is not None:
        pending = env.remove_old_programs(new_folder)
        if pending:
            report.info(f"{len(pending)} old file(s) in use are deleted at the next restart")

    verb = "updated" if plan.update else "installed"
    report.info(f"RootMind background service {verb} (version {env.version}).")
    report.info(f"  REST API: http://127.0.0.1:{plan.api_port}")
    report.info(f"  Token:    {root / 'data' / 'rest_token.txt'}")
    if plan.mcp_enabled:
        report.info(f"  MCP:      http://127.0.0.1:{plan.mcp_port}/mcp")
        report.info(f"  Token:    {root / 'data' / 'api_token.txt'}")
    report.info(f"  Program:  {ntpath.dirname(exe)}")
    report.info(f"  Logs:     {root / 'logs'}")
    if not options.start:
        report.info("The services were not started (--no-start).")


def install(env, options: InstallOptions, out=print, err=None) -> int:
    """Run ``service install``; returns the exit code."""
    report = Report(out, err or out)
    return _run("install", report, lambda: _install(env, options, report))


# --- uninstall ------------------------------------------------------------------------


def _uninstall(env, purge: bool, report: Report) -> None:
    _require_windows(env)
    _require_elevated(env, report)
    program_files = env.program_files()
    root = env.data_root()

    with env.scm(write=True) as manager:
        current = {name: manager.query(name) for name in (BACKEND_SERVICE, MCP_SERVICE)}
        installed = [name for name, status in current.items() if status is not None]
        for name in installed:
            if _ours(current[name], program_files) is None:
                raise _foreign(name)
        for name in installed:
            manager.stop(name)
        for name in installed:
            try:
                _wait_stopped(env, manager, name)
            except ServiceCommandError as e:
                report.warn(e.message)
        for name in installed:
            manager.delete(name)
        for name in installed:
            pid = current[name].pid
            if pid and not env.wait_process_exit(pid, STOP_TIMEOUT_SECONDS):
                report.warn(f"the process of {name} (PID {pid}) is still running")
        for name in installed:
            _wait_deleted(env, manager, name, report)

    data_exists = root.is_dir()
    # The data folder is touched only when it is protected like the one
    # install creates: elevated deletes must not act inside a folder that
    # other accounts can change.
    protected = data_exists and not env.data_problems(root)
    if protected and not purge:
        report.log_path = root / "logs" / "install.log"
    if data_exists and not protected:
        report.warn(
            f"{root} is not protected like a folder that service install created, so it was "
            "left unchanged. Remove it by hand from an elevated prompt if you no longer need it."
        )

    rest_token = root / "data" / "rest_token.txt"
    removed_token = False
    if protected:
        with contextlib.suppress(FileNotFoundError):
            rest_token.unlink()
            removed_token = True

    pending = env.remove_all_programs()

    if purge and protected:
        try:
            env.remove_data(root)
        except layout.LayoutError as e:
            raise ServiceCommandError(ExitCode.DATA_FOLDER_REFUSED, str(e)) from None
        report.info(f"Deleted {root}")

    if not installed and not removed_token and not (purge and protected):
        report.info("The background service is not installed; nothing to do.")
    elif installed:
        report.info(f"Removed {', '.join(installed)}.")
    if pending:
        report.info(f"{len(pending)} file(s) in use are deleted at the next restart")
    mcp_token = root / "data" / "api_token.txt"
    if not purge and mcp_token.exists():
        report.info(f"The MCP token was kept in {mcp_token}")


def uninstall(env, purge: bool = False, out=print, err=None) -> int:
    """Run ``service uninstall [--purge]``; returns the exit code."""
    report = Report(out, err or out)
    return _run(
        "uninstall --purge" if purge else "uninstall",
        report,
        lambda: _uninstall(env, purge, report),
    )


# --- status ------------------------------------------------------------------------------


def _token_readable(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def status(env) -> dict[str, Any]:
    """The state of the background service (no administrator rights needed)."""
    _require_windows(env)
    program_files = env.program_files()
    root = env.data_root()
    with env.scm(write=False) as manager:
        backend = manager.query(BACKEND_SERVICE)
        mcp = manager.query(MCP_SERVICE)

    warnings: list[str] = []
    result: dict[str, Any] = {
        "installed": backend is not None,
        "ours": False,
        "state": backend.state if backend else None,
        "port": None,
        "version": None,
        "binary": None,
        "user_sid": None,
        "token_file": None,
        "token_readable": False,
    }
    image = _ours(backend, program_files)
    if backend is not None and image is None:
        warnings.append("service is not ours")
    if image is not None:
        token_file = root / "data" / "rest_token.txt"
        result.update(
            ours=True,
            port=image.port,
            binary=image.exe,
            user_sid=image.user_sid,
            token_file=str(token_file),
            token_readable=_token_readable(token_file),
        )
        if backend.state == scm.RUNNING:
            code, body = env.http("GET", f"http://127.0.0.1:{image.port}/")
            if code == 200 and isinstance(body, dict) and body.get("name") == "RootMind API":
                result["version"] = body.get("version")
        else:
            warnings.append("service is stopped")
        if not result["token_readable"]:
            warnings.append("token not readable for this account")
        try:
            me = env.process_user_sid()
        except OSError:
            me = None
        if _same_sid(me, image.user_sid) and not env.app_installed_for_user():
            warnings.append(
                "RootMind is not installed for this account. If you uninstalled it, remove the "
                f'background service with "{image.exe}" service uninstall from an elevated prompt'
            )

    mcp_image = _ours(mcp, program_files)
    mcp_info: dict[str, Any] = {
        "installed": mcp is not None,
        "state": mcp.state if mcp else None,
        "port": None,
        "token_file": None,
        "token_readable": False,
    }
    if mcp is not None and mcp_image is None:
        warnings.append("MCP service is not ours")
    if mcp_image is not None:
        token_file = root / "data" / "api_token.txt"
        mcp_info.update(
            port=mcp_image.port,
            token_file=str(token_file),
            token_readable=_token_readable(token_file),
        )
        if mcp.state != scm.RUNNING:
            warnings.append("MCP service is stopped")
    result["mcp_http"] = mcp_info
    result["warnings"] = warnings
    return result


def format_status(info: dict[str, Any]) -> str:
    """``status`` without ``--json``: a short readable summary."""
    if not info["installed"]:
        lines = ["The RootMind background service is not installed."]
    elif not info["ours"]:
        lines = ["A service named RootMind-Backend exists that RootMind did not install."]
    else:
        lines = [
            f"RootMind background service: {info['state']}",
            f"  REST API: http://127.0.0.1:{info['port']}"
            + (f" (version {info['version']})" if info["version"] else ""),
            f"  Program:  {info['binary']}",
            f"  Token:    {info['token_file']}"
            + ("" if info["token_readable"] else " (not readable for this account)"),
        ]
    mcp = info["mcp_http"]
    if mcp["installed"] and mcp["port"]:
        lines.append(f"  MCP:      http://127.0.0.1:{mcp['port']}/mcp ({mcp['state']})")
    lines.extend(f"warning: {w}" for w in info["warnings"])
    return "\n".join(lines)


def status_json(info: dict[str, Any]) -> str:
    return json.dumps(info, indent=2)


# --- shared ------------------------------------------------------------------------------


def _run(command: str, report: Report, body: Callable[[], None]) -> int:
    code: int = ExitCode.OK
    detail = None
    try:
        body()
    except ServiceCommandError as e:
        report.error(e.message)
        code = e.code
    except Exception as e:
        report.error(f"unexpected error: {e}")
        detail = traceback.format_exc()
        code = ExitCode.UNEXPECTED
    report.write_log(command, code, detail)
    return int(code)
