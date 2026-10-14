"""The service host: ``service run backend|mcp-http``, started by the SCM.

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

A config, token or access error stops the service cleanly with the
service-specific exit code 2, which the failure actions do not restart. Any
other failure ends the process without reporting a stop, so the SCM restarts
it (after 5, 30 and 60 seconds).
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import sys
import tempfile
import threading
from collections.abc import Callable
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
        self.remote_access: Any = None
        self._stop_requested = False
        self._lock = threading.Lock()
        self.server: Any = None
        self.sock: Any = None

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
            if not self.user_sid:
                raise ServiceConfigError("the backend service needs --user-sid")
            try:
                token, source = auth.resolve_token(
                    auth.REST_TOKEN_ENV, data / auth.REST_TOKEN_FILE_NAME, generate=True
                )
            except (auth.TokenError, OSError) as e:
                raise ServiceConfigError(str(e)) from None
            from ..winaccess import AccessContextError

            try:
                access = self._access_factory(self.user_sid)
            except AccessContextError as e:
                raise ServiceConfigError(
                    f"cannot check folder access for {self.user_sid}: {e}"
                ) from None
            from ..api.app import create_app
            from ..remote_access import RemoteAccessFiles

            app = create_app(
                api_token=token,
                folder_access=access,
                remote_access_files=RemoteAccessFiles(self.home, self._protection_factory()),
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

        self.sock = bind_socket("127.0.0.1", self.port)
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
            if self._stop_requested:
                self.server.should_exit = True

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
                    self.server.run(sockets=[self.sock])
                finally:
                    # The tunnel never outlives the server it forwards to.
                    if self.remote_access is not None:
                        self.remote_access.stop()
        finally:
            self.sock.close()
        return self._stop_requested

    def stop(self) -> None:
        """Ask the server to shut down (from the SCM's control thread)."""
        with self._lock:
            self._stop_requested = True
            if self.server is not None:
                self.server.should_exit = True

    @property
    def port_in_use(self) -> int | None:
        return self.sock.getsockname()[1] if self.sock is not None else None


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
    if sys.platform != "win32":
        print("error: the background service is Windows only", file=sys.stderr)
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


__all__ = ["ROLE_BACKEND", "ROLE_MCP", "ServiceConfigError", "ServiceRunner", "run_service"]
