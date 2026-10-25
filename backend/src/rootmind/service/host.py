"""The service host: ``service run backend|mcp-http``, started by the SCM or systemd.

The ImagePath runs the executable with ``service run <role> --port <P>
[--user-sid <SID>]``. The host hands a pywin32 ``ServiceFramework`` class to
``servicemanager`` (``PrepareToHostSingle`` + ``StartServiceCtrlDispatcher``)
and then, inside the service:

1. points ``RLM_HOME`` and the working folder at ``%ProgramData%\\RootMind``
   (known folder), and ``TEMP``/``TMP`` at its ``tmp\\`` for the backend;
2. sends ``sys.stdout``/``sys.stderr`` and all logging to a rotating log
   (10 MB x 5) in ``logs\\<role>\\``;
3. builds the app: the REST API with the token from ``data\\rest_token.txt``,
   the folder access policy of the installing account and the protected
   remote-access files, or the MCP server with the token from
   ``data\\api_token.txt`` (serving only);
4. binds ``127.0.0.1:<port>`` exclusively and runs uvicorn until the SCM asks
   it to stop. The MCP server also runs remote access
   (:class:`rootmind.remote_access.RemoteAccessManager`), which keeps an ngrok
   tunnel to that port open while remote access is on and closes it on stop.
   With ``mcp_http.network.bind`` in ``config.yaml`` it also listens there for
   other machines (a second uvicorn server on the same app, TLS unless
   ``insecure_http``, behind :class:`rootmind.mcp_http.NetworkListener`); the
   loopback listener stays as it is.

A config, token or access error stops the service cleanly with the
service-specific exit code 2, which the failure actions do not restart. Any
other failure ends the process without reporting a stop, so the SCM restarts
it (after 5, 30 and 60 seconds).

On Linux systemd starts the same command from the account's user unit
(:mod:`.systemd`); :func:`run_linux_service` runs the runner in the foreground
instead of the SCM glue. The service runs as the account itself
(``per_user``): its home is the data folder next to its program copy, it
reads folders with the account's own rights (``UNRESTRICTED``) and writes the
remote-access files owner-only. It checks the data folder's owner and modes
first, tells systemd it is ready (``sd_notify``) once it has bound its port,
and ends with exit code 2 on a config, token or folder error, which the unit
does not restart (``RestartPreventExitStatus=2``).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import logging.handlers
import os
import signal
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import (
    DISPLAY_NAMES,
    ROLE_BACKEND,
    ROLE_MCP,
    SERVICE_BY_ROLE,
    SERVICE_SPECIFIC_CONFIG_ERROR,
    ExitCode,
)

logger = logging.getLogger("rootmind.service.host")

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
GRACEFUL_SHUTDOWN_SECONDS = 10
STOP_WAIT_HINT_MS = 30_000
START_WAIT_HINT_MS = 60_000

# Removed from the service's environment: the tokens come from their files,
# the config from <home>\config.yaml, and the bind from the ImagePath.
_IGNORED_ENV = ("RLM_API_TOKEN", "MCP_API_TOKEN", "CONFIG_PATH", "RLM_DEV", "RLM_HOST", "RLM_PORT")

ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063
ERROR_SERVICE_SPECIFIC_ERROR = 1066


class ServiceConfigError(Exception):
    """A config, token or access error: the service stops and is not restarted."""


class _LogStream(io.TextIOBase):
    """A text stream that writes each line to a logger (replaces stdout and stderr)."""

    def __init__(self, target: logging.Logger, level: int) -> None:
        super().__init__()
        self._logger = target
        self._level = level
        self._buffer = ""
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        with self._lock:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            if line.strip():
                self._logger.log(self._level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        with self._lock:
            rest, self._buffer = self._buffer, ""
        if rest.strip():
            self._logger.log(self._level, rest.rstrip())


def configure_logging(log_dir: Path, role: str) -> logging.Handler | None:
    """Send logging and ``sys.stdout``/``sys.stderr`` to ``<log_dir>\\<role>.log`` (rotating).

    The folder is never created here: ``service install`` creates it with the
    right permissions, and ``service run`` started by hand must not create
    ``%ProgramData%\\RootMind``. Returns the file handler, or None when the log
    cannot be opened; output is then discarded rather than failing.
    """
    # A handler that cannot write must not print to stderr (which is the log).
    logging.raiseExceptions = False
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler: logging.Handler | None
    try:
        if not log_dir.is_dir():
            raise FileNotFoundError(log_dir)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{role}.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    except OSError:
        handler = None
        root.addHandler(logging.NullHandler())
    root.setLevel(logging.INFO)
    # uvicorn's loggers keep no handlers of their own (log_config=None) and
    # propagate to the root logger's file.
    for name in ("uvicorn", "uvicorn.error"):
        named = logging.getLogger(name)
        named.handlers.clear()
        named.setLevel(logging.INFO)
        named.propagate = True
    sys.stdout = _LogStream(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _LogStream(logging.getLogger("stderr"), logging.ERROR)
    return handler


def _within(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _default_access_factory(user_sid: str):
    from ..winaccess import WindowsUserAccess

    return WindowsUserAccess(user_sid)


def _mcp_service_installed() -> bool:
    """Whether the MCP service exists (its account then gets its file entries)."""
    from . import MCP_SERVICE, scm

    try:
        with scm.Scm(write=False) as manager:
            return manager.query(MCP_SERVICE) is not None
    except OSError as e:
        logger.warning("Cannot query %s; its account gets no entries: %s", MCP_SERVICE, e)
        return False


def _default_protection_factory():
    from .layout import DataFileProtection

    return DataFileProtection(_mcp_service_installed)


def _default_remote_access_factory(home: Path, port: int):
    from ..remote_access import RemoteAccessManager

    return RemoteAccessManager(home=home, port=port, config_path=home / "config.yaml")


class ServiceRunner:
    """Builds and runs the app of one role; independent of the SCM (testable).

    Args:
        role: ``backend`` or ``mcp-http``.
        port: The port to bind on 127.0.0.1 (0 picks a free one, for tests).
        home: ``%ProgramData%\\RootMind``.
        user_sid: The installing account (backend only).
        access_factory: Builds the folder access policy for ``user_sid``
            (default: :class:`~rootmind.winaccess.WindowsUserAccess`).
        protection_factory: Builds how the backend writes the remote-access
            files (default: :class:`~rootmind.service.layout.DataFileProtection`).
        remote_access_factory: Builds the MCP server's remote access from
            the home and the bound port (default:
            :class:`~rootmind.remote_access.RemoteAccessManager`).
        per_user: The service runs as the account itself (Linux): no
            ``user_sid``, no folder access policy, owner-only remote-access
            files.
        home_check: Returns why the home must not be used (Linux: owner and
            modes); a non-empty list stops the service (no restart).
    """

    def __init__(
        self,
        role: str,
        port: int,
        *,
        home: Path,
        user_sid: str | None = None,
        access_factory: Callable[[str], Any] | None = None,
        protection_factory: Callable[[], Any] | None = None,
        remote_access_factory: Callable[[Path, int], Any] | None = None,
        per_user: bool = False,
        home_check: Callable[[Path], list[str]] | None = None,
    ) -> None:
        if role not in SERVICE_BY_ROLE:
            raise ValueError(f"unknown role {role!r}")
        self.role = role
        self.port = port
        self.home = home
        self.user_sid = user_sid
        self._access_factory = access_factory or _default_access_factory
        self._protection_factory = protection_factory or _default_protection_factory
        self._remote_access_factory = remote_access_factory or _default_remote_access_factory
        self.per_user = per_user
        self._home_check = home_check
        self.remote_access: Any = None
        self._stop_requested = False
        self._lock = threading.Lock()
        self.server: Any = None
        self.sock: Any = None
        # The MCP server's listener for other machines (mcp_http.network), or None.
        self.network_server: Any = None
        self.network_sock: Any = None

    # --- setup ---------------------------------------------------------------------

    def prepare_environment(self) -> None:
        """Step 1: ``RLM_HOME``, working folder and (backend) ``TEMP``/``TMP``."""
        for name in _IGNORED_ENV:
            os.environ.pop(name, None)
        os.environ["RLM_HOME"] = str(self.home)
        if self.role == ROLE_BACKEND:
            # The backend indexes; a stray machine-wide setting must not stop it.
            os.environ.pop("RLM_DISABLE_SCHEDULER", None)
            temp = self.home / "tmp"
            os.environ["TEMP"] = os.environ["TMP"] = str(temp)
            if self.per_user:
                os.environ["TMPDIR"] = str(temp)
            tempfile.tempdir = None
        os.chdir(self.home)

    def prepare(self) -> None:
        """Steps 1 and 3: environment, config, token, access policy, app and socket.

        Raises:
            ServiceConfigError: a config, token or access error (no restart).
            OSError: the port cannot be bound (the SCM restarts the service).
        """
        from .. import auth
        from .. import config as config_module

        if self._home_check is not None:
            problems = self._home_check(self.home)
            if problems:
                raise ServiceConfigError(
                    f"{self.home} is not protected: {'; '.join(problems[:10])}. "
                    "Run service install again to repair it."
                )
        self.prepare_environment()
        config_module.reset_config()
        try:
            config = config_module.init_config(self.home / "config.yaml")
        except Exception as e:
            raise ServiceConfigError(f"cannot read {self.home / 'config.yaml'}: {e}") from None
        data = self.home / "data"
        if not _within(Path(config.database.path), data):
            raise ServiceConfigError(
                f"database.path ({config.database.path}) must lie inside {data}; "
                "fix config.yaml or reinstall the service"
            )
        if self.role == ROLE_MCP and not _within(Path(config.database.path), data / "db"):
            # The MCP account may write (the WAL files) only in data\db.
            raise ServiceConfigError(
                f"database.path ({config.database.path}) must lie inside {data / 'db'} "
                "for MCP over HTTP; run service install again to move it"
            )

        if self.role == ROLE_BACKEND:
            if not self.user_sid and not self.per_user:
                raise ServiceConfigError("the backend service needs --user-sid")
            try:
                token, source = auth.resolve_token(
                    auth.REST_TOKEN_ENV, data / auth.REST_TOKEN_FILE_NAME, generate=True
                )
            except (auth.TokenError, OSError) as e:
                raise ServiceConfigError(str(e)) from None
            from ..api.app import create_app
            from ..remote_access import PrivateFiles, RemoteAccessFiles

            access: Any
            protection: Any
            if self.per_user:
                # Linux: the service is the account; files are owner-only.
                from ..access import UNRESTRICTED

                access = UNRESTRICTED
                protection = PrivateFiles()
            else:
                from ..winaccess import AccessContextError

                try:
                    access = self._access_factory(self.user_sid or "")
                except AccessContextError as e:
                    raise ServiceConfigError(
                        f"cannot check folder access for {self.user_sid}: {e}"
                    ) from None
                protection = self._protection_factory()
            app = create_app(
                api_token=token,
                folder_access=access,
                remote_access_files=RemoteAccessFiles(self.home, protection),
            )
        else:
            from .. import mcp_http

            try:
                token, source = auth.resolve_token(
                    mcp_http.TOKEN_ENV, data / mcp_http.TOKEN_FILE_NAME, generate=False
                )
            except (auth.TokenError, OSError) as e:
                raise ServiceConfigError(str(e)) from None
            app = mcp_http.create_app(
                token,
                config,
                allowed_origins=mcp_http._env_allowed_origins(),
                enable_scheduler=False,
            )
        logger.info(
            "Bearer token from %s (fingerprint sha256:%s)", source, auth.token_fingerprint(token)
        )

        import uvicorn

        from ..main import bind_socket

        network_config = None
        if self.role == ROLE_MCP:
            network_config = self._network_config(config, app)
        self.sock = bind_socket("127.0.0.1", self.port)
        if network_config is not None:
            network = config.mcp_http.network
            try:
                self.network_sock = bind_socket(network.bind, network.port)
            except OSError:
                self.sock.close()
                raise
        if self.role == ROLE_MCP:
            self.remote_access = self._remote_access_factory(self.home, self.sock.getsockname()[1])
        uvicorn_config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.sock.getsockname()[1],
            log_config=None,
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        with self._lock:
            self.server = uvicorn.Server(uvicorn_config)
            if network_config is not None:
                self.network_server = _follower_server(network_config)
            if self._stop_requested:
                self.stop_servers()

    def _network_config(self, config: Any, app: Any) -> Any:
        """The uvicorn config of the MCP listener for other machines, or None when it is off.

        Raises:
            ServiceConfigError: the settings are incomplete (no TLS and no
                ``insecure_http``, no ``allowed_hosts``) or the certificate or
                key cannot be loaded.
        """
        import uvicorn

        from ..config import LOOPBACK_ADDRESSES, mcp_network_problems
        from ..mcp_http import NetworkListener

        settings = config.mcp_http
        network = settings.network
        if not network.bind:
            return None
        problems = mcp_network_problems(settings)
        loopback = network.bind.strip().strip("[]").lower() in LOOPBACK_ADDRESSES
        # A wildcard address covers 127.0.0.1 as well: the same port cannot be bound twice.
        wildcard = network.bind in ("0.0.0.0", "::")  # noqa: S104 (compared, not bound)
        if (loopback or wildcard) and network.port == self.port:
            problems.append(
                f"mcp_http.network.port {network.port} is the loopback MCP port; choose another"
            )
        if problems:
            raise ServiceConfigError("; ".join(problems))
        tls: dict[str, Any] = {}
        if network.tls_cert:
            tls = {"ssl_certfile": network.tls_cert, "ssl_keyfile": network.tls_key}

        def allowed_hosts() -> list[str]:
            # The list as last read from config.yaml (re-read on change).
            return app.state.mcp.mcp.allowed_hosts

        network_config = uvicorn.Config(
            NetworkListener(app, allowed_hosts),
            host=network.bind,
            port=network.port,
            log_config=None,
            access_log=False,
            lifespan="off",
            # On a loopback address a reverse proxy on this machine connects:
            # trust its X-Forwarded-* headers (from 127.0.0.1 only). Elsewhere
            # clients connect directly and no forwarded header is trusted.
            proxy_headers=loopback,
            forwarded_allow_ips="127.0.0.1,::1",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
            **tls,
        )
        try:
            network_config.load()  # loads the certificate and key now, not on the first request
        except (OSError, ValueError) as e:
            what = f"{network.tls_cert} / {network.tls_key}" if tls else "the listener"
            raise ServiceConfigError(
                f"mcp_http.network: cannot load {what}: {getattr(e, 'strerror', None) or e}"
            ) from None
        scheme = "https" if tls else "http"
        shown = f"[{network.bind}]" if ":" in network.bind else network.bind
        logger.warning(
            "MCP over HTTP also listens on %s://%s:%d for other machines (%s; %s).",
            scheme,
            shown,
            network.port,
            "TLS"
            if tls
            else "for a reverse proxy on this machine"
            if loopback
            else "PLAIN HTTP: tokens cross the network unencrypted",
            "named tokens and the default token"
            if network.allow_default_token
            else "named tokens only",
        )
        return network_config

    # --- running -------------------------------------------------------------------

    def serve(self) -> bool:
        """Serve until stopped. True when :meth:`stop` ended it, False when it ended on its own."""
        if self.server is None or self.sock is None:
            raise RuntimeError("prepare() must run first")
        try:
            if not self._stop_requested:
                logger.info(
                    "%s listening on http://127.0.0.1:%d",
                    SERVICE_BY_ROLE[self.role],
                    self.sock.getsockname()[1],
                )
                if self.remote_access is not None:
                    self.remote_access.start()
                try:
                    if self.network_server is None:
                        self.server.run(sockets=[self.sock])
                    else:
                        asyncio.run(self._serve_both())
                finally:
                    # The tunnel never outlives the server it forwards to.
                    if self.remote_access is not None:
                        self.remote_access.stop()
        finally:
            self.sock.close()
            if self.network_sock is not None:
                self.network_sock.close()
        return self._stop_requested

    async def _serve_both(self) -> None:
        """Run the loopback and the network listener; when one ends, both end."""
        tasks = {
            asyncio.create_task(self.server.serve(sockets=[self.sock])),
            asyncio.create_task(self.network_server.serve(sockets=[self.network_sock])),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        self.stop_servers()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    def stop_servers(self) -> None:
        for server in (self.server, self.network_server):
            if server is not None:
                server.should_exit = True

    def stop(self) -> None:
        """Ask the server to shut down (from the SCM's control thread)."""
        with self._lock:
            self._stop_requested = True
            self.stop_servers()

    @property
    def port_in_use(self) -> int | None:
        return self.sock.getsockname()[1] if self.sock is not None else None


def _follower_server_class():
    import uvicorn

    class FollowerServer(uvicorn.Server):
        """A second uvicorn server that leaves signals to the first one."""

        def capture_signals(self):  # type: ignore[override]
            return contextlib.nullcontext()

    return FollowerServer


def _follower_server(config: Any) -> Any:
    return _follower_server_class()(config)


# --- SCM glue --------------------------------------------------------------------------


def _exit_process(code: int) -> None:
    """End the process now, after flushing the log."""
    logging.shutdown()
    os._exit(code)


def _service_class(role: str, port: int, user_sid: str | None, home: Path):
    import win32service
    import win32serviceutil

    class RootMindService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_BY_ROLE[role]
        _svc_display_name_ = DISPLAY_NAMES[SERVICE_BY_ROLE[role]]

        def __init__(self, args):
            super().__init__(args)
            self.runner = ServiceRunner(role, port, home=home, user_sid=user_sid)

        def SvcStop(self):  # noqa: N802 (pywin32 name)
            logger.info("Stop requested by the Service Control Manager")
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=STOP_WAIT_HINT_MS)
            self.runner.stop()

        def SvcShutdown(self):  # noqa: N802 (pywin32 name)
            self.SvcStop()

        def _stop_with_config_error(self) -> None:
            # Report the stop ourselves (the framework would report exit code 0),
            # then end the process; nothing may run after SERVICE_STOPPED.
            win32service.SetServiceStatus(
                self.ssh,
                (
                    win32service.SERVICE_WIN32_OWN_PROCESS,
                    win32service.SERVICE_STOPPED,
                    0,
                    ERROR_SERVICE_SPECIFIC_ERROR,
                    SERVICE_SPECIFIC_CONFIG_ERROR,
                    0,
                    0,
                ),
            )
            _exit_process(0)

        def SvcRun(self):  # noqa: N802 (pywin32 name)
            self.ReportServiceStatus(
                win32service.SERVICE_START_PENDING, waitHint=START_WAIT_HINT_MS
            )
            try:
                self.runner.prepare()
            except ServiceConfigError as e:
                logger.error("%s cannot start: %s", self._svc_name_, e)
                self._stop_with_config_error()
                return
            except BaseException:
                logger.exception("%s failed to start", self._svc_name_)
                _exit_process(1)
                return
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            try:
                stopped = self.runner.serve()
            except BaseException:
                logger.exception("%s failed", self._svc_name_)
                _exit_process(1)
                return
            if not stopped:
                logger.error("%s stopped unexpectedly; the SCM restarts it", self._svc_name_)
                _exit_process(1)
                return
            logger.info("%s stopped", self._svc_name_)
            # The framework reports SERVICE_STOPPED once this returns.

    return RootMindService


def run_service(role: str, port: int, user_sid: str | None) -> int:
    """``service run <role>``: hand the role to the Service Control Manager.

    Returns:
        0 after a normal stop, 2 when the process was not started by the SCM.
    """
    if sys.platform.startswith("linux"):
        return run_linux_service(role, port)
    if sys.platform != "win32":
        print("error: the background service runs on Windows and Linux only", file=sys.stderr)
        return ExitCode.USAGE
    from .layout import data_root

    home = data_root()
    configure_logging(home / "logs" / role, role)
    logger.info("Starting %s (port %d)", SERVICE_BY_ROLE[role], port)

    import pywintypes
    import servicemanager

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(_service_class(role, port, user_sid, home))
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except pywintypes.error as e:
        if e.winerror != ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            raise
        message = (
            "service run is started by the Windows Service Control Manager; "
            "use service install, then start the service"
        )
        logger.error(message)
        print(f"error: {message}", file=sys.__stderr__)
        return ExitCode.USAGE
    return ExitCode.OK


# --- systemd glue (Linux) ----------------------------------------------------------------


def sd_notify(message: str, environ: Mapping[str, str] | None = None) -> bool:
    """Send ``message`` to systemd's notification socket; False without one.

    The socket is ``$NOTIFY_SOCKET``: a path, or an abstract name starting
    with ``@``.
    """
    address = (os.environ if environ is None else environ).get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:  # type: ignore[attr-defined]
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
    except OSError as e:
        logger.warning("Cannot notify systemd: %s", e)
        return False
    return True


def linux_home() -> Path:
    """The data folder of the service: next to its program copy, else the XDG default."""
    from . import linux_layout

    return linux_layout.home_of_program(sys.executable) or linux_layout.data_root()


def run_linux_service(
    role: str,
    port: int,
    *,
    home: Path | None = None,
    runner_factory: Callable[..., Any] = ServiceRunner,
    notify: Callable[[str], bool] = sd_notify,
    configure: Callable[[Path, str], Any] = configure_logging,
) -> int:
    """``service run <role>`` on Linux, started by systemd from the user unit.

    Returns:
        0 after a stop that systemd (SIGTERM) or Ctrl+C asked for, 2 on a
        config, token or folder error (not restarted), 1 on any other failure
        (restarted).
    """
    from . import linux_layout

    home = home or linux_home()
    name = SERVICE_BY_ROLE[role]
    configure(home / "logs" / role, role)
    logger.info("Starting %s (port %d)", name, port)
    uid = os.geteuid()  # type: ignore[attr-defined]
    runner = runner_factory(
        role,
        port,
        home=home,
        per_user=True,
        home_check=lambda path: linux_layout.check_service_home(path, uid),
    )
    signalled = threading.Event()

    def on_signal(signum, _frame) -> None:
        logger.info("Stop requested (signal %d)", signum)
        signalled.set()
        runner.stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, on_signal)
    try:
        runner.prepare()
    except ServiceConfigError as e:
        logger.error("%s cannot start: %s", name, e)
        logging.shutdown()
        return SERVICE_SPECIFIC_CONFIG_ERROR
    except BaseException:
        logger.exception("%s failed to start", name)
        logging.shutdown()
        return 1
    notify(f"READY=1\nMAINPID={os.getpid()}\nSTATUS=Listening on 127.0.0.1:{runner.port_in_use}")
    try:
        stopped = runner.serve()
    except BaseException:
        logger.exception("%s failed", name)
        logging.shutdown()
        return 1
    notify("STOPPING=1")
    # uvicorn handles SIGTERM itself and raises it again afterwards; either way
    # a signal means systemd (or Ctrl+C) asked for the stop.
    if not (stopped or signalled.is_set()):
        logger.error("%s stopped unexpectedly; systemd restarts it", name)
        logging.shutdown()
        return 1
    logger.info("%s stopped", name)
    logging.shutdown()
    return ExitCode.OK


__all__ = [
    "ROLE_BACKEND",
    "ROLE_MCP",
    "ServiceConfigError",
    "ServiceRunner",
    "run_linux_service",
    "run_service",
    "sd_notify",
]
