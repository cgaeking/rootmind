"""A thin adapter over the Service Control Manager (pywin32 ``win32service``).

The install logic talks to :class:`Scm` (or a fake in tests) only through
:class:`ServiceStatus`, :class:`ServiceSpec` and the methods below, so it stays
testable on every platform. Querying works without administrator rights; every
change needs them.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# SvcState names shared with the desktop app (ServiceInfo.state).
STOPPED = "stopped"
START_PENDING = "start_pending"
STOP_PENDING = "stop_pending"
RUNNING = "running"
OTHER = "other"

_STATE_NAMES = {1: STOPPED, 2: START_PENDING, 3: STOP_PENDING, 4: RUNNING}

LOCAL_SYSTEM = "LocalSystem"

# Win32 error codes.
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_CANNOT_ACCEPT_CTRL = 1061
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_MARKED_FOR_DELETE = 1072

# Restart after 5 s, 30 s and 60 s; the failure count resets after one day.
# They apply only when the process ends without reporting SERVICE_STOPPED, so a
# clean stop with a service-specific error code (config, token or access
# error) is not restarted.
FAILURE_RESET_SECONDS = 24 * 60 * 60
FAILURE_RESTART_DELAYS_MS = (5_000, 30_000, 60_000)

SERVICE_SID_TYPE_NONE = 0
SERVICE_SID_TYPE_UNRESTRICTED = 1


class ScmError(OSError):
    """A Service Control Manager call failed (``winerror`` holds the Win32 code)."""


@dataclass(frozen=True)
class ServiceStatus:
    """What the SCM reports about one service."""

    name: str
    image_path: str
    account: str
    start_type: int
    state: str
    pid: int = 0
    win32_exit_code: int = 0
    service_exit_code: int = 0


@dataclass(frozen=True)
class ServiceSpec:
    """How a service is registered (create or update)."""

    name: str
    display_name: str
    description: str
    image_path: str
    account: str  # "LocalSystem" or "NT SERVICE\\<name>"
    sid_type: int = SERVICE_SID_TYPE_NONE
    # None leaves the account's default privileges.
    required_privileges: tuple[str, ...] | None = None
    failure_delays_ms: tuple[int, ...] = field(default=FAILURE_RESTART_DELAYS_MS)


def state_name(code: int) -> str:
    """SvcState name of a ``SERVICE_*`` state code."""
    return _STATE_NAMES.get(code, OTHER)


def _wrap(error: Exception) -> ScmError:
    code = getattr(error, "winerror", None)
    function = getattr(error, "funcname", "") or "SCM"
    message = getattr(error, "strerror", "") or str(error)
    return ScmError(None, f"{function}: {message}", None, code)


class Scm:
    """The local Service Control Manager.

    Args:
        write: Open with full access (needs administrator rights). Without
            it only queries work.
    """

    def __init__(self, *, write: bool = False) -> None:
        import win32service

        self._ws = win32service
        access = win32service.SC_MANAGER_ALL_ACCESS if write else win32service.SC_MANAGER_CONNECT
        try:
            self._handle = win32service.OpenSCManager(None, None, access)
        except Exception as e:
            raise _wrap(e) from e

    def close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._ws.CloseServiceHandle(self._handle)
            self._handle = None

    def __enter__(self) -> Scm:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- queries -------------------------------------------------------------

    def _open(self, name: str, access: int):
        try:
            return self._ws.OpenService(self._handle, name, access)
        except Exception as e:
            raise _wrap(e) from e

    def query(self, name: str) -> ServiceStatus | None:
        """The service's configuration and state, or None when it does not exist."""
        ws = self._ws
        try:
            handle = self._open(name, ws.SERVICE_QUERY_CONFIG | ws.SERVICE_QUERY_STATUS)
        except ScmError as e:
            if e.winerror == ERROR_SERVICE_DOES_NOT_EXIST:
                return None
            raise
        try:
            config = ws.QueryServiceConfig(handle)
            status = ws.QueryServiceStatusEx(handle)
        except Exception as e:
            raise _wrap(e) from e
        finally:
            ws.CloseServiceHandle(handle)
        return ServiceStatus(
            name=name,
            image_path=config[3] or "",
            account=config[7] or "",
            start_type=config[1],
            state=state_name(status["CurrentState"]),
            pid=status.get("ProcessId", 0) or 0,
            win32_exit_code=status.get("Win32ExitCode", 0) or 0,
            service_exit_code=status.get("ServiceSpecificExitCode", 0) or 0,
        )

    # --- changes -------------------------------------------------------------

    def _configure(self, handle, spec: ServiceSpec) -> None:
        ws = self._ws
        ws.ChangeServiceConfig2(handle, ws.SERVICE_CONFIG_DESCRIPTION, spec.description)
        ws.ChangeServiceConfig2(
            handle,
            ws.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": FAILURE_RESET_SECONDS,
                "RebootMsg": None,
                "Command": None,
                "Actions": [(ws.SC_ACTION_RESTART, delay) for delay in spec.failure_delays_ms],
            },
        )
        # Failure actions only for crashes, never for a stop with an error code.
        ws.ChangeServiceConfig2(handle, ws.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, False)
        ws.ChangeServiceConfig2(handle, ws.SERVICE_CONFIG_SERVICE_SID_INFO, spec.sid_type)
        if spec.required_privileges is not None:
            ws.ChangeServiceConfig2(
                handle, ws.SERVICE_CONFIG_REQUIRED_PRIVILEGES_INFO, list(spec.required_privileges)
            )

    def create(self, spec: ServiceSpec) -> None:
        """Register a new service: own process, automatic start, the given account."""
        ws = self._ws
        try:
            handle = ws.CreateService(
                self._handle,
                spec.name,
                spec.display_name,
                ws.SERVICE_ALL_ACCESS,
                ws.SERVICE_WIN32_OWN_PROCESS,
                ws.SERVICE_AUTO_START,
                ws.SERVICE_ERROR_NORMAL,
                spec.image_path,
                None,
                0,
                None,
                spec.account,
                None,
            )
        except Exception as e:
            raise _wrap(e) from e
        try:
            self._configure(handle, spec)
        except Exception as e:
            raise _wrap(e) from e
        finally:
            ws.CloseServiceHandle(handle)

    def update(self, spec: ServiceSpec) -> None:
        """Point an existing service at ``spec`` (ImagePath, account, settings)."""
        ws = self._ws
        handle = self._open(spec.name, ws.SERVICE_ALL_ACCESS)
        try:
            ws.ChangeServiceConfig(
                handle,
                ws.SERVICE_WIN32_OWN_PROCESS,
                ws.SERVICE_AUTO_START,
                ws.SERVICE_ERROR_NORMAL,
                spec.image_path,
                None,
                0,
                None,
                spec.account,
                None,
                spec.display_name,
            )
            self._configure(handle, spec)
        except Exception as e:
            raise _wrap(e) from e
        finally:
            ws.CloseServiceHandle(handle)

    def start(self, name: str) -> None:
        ws = self._ws
        handle = self._open(name, ws.SERVICE_START)
        try:
            ws.StartService(handle, None)
        except Exception as e:
            if getattr(e, "winerror", None) != ERROR_SERVICE_ALREADY_RUNNING:
                raise _wrap(e) from e
        finally:
            ws.CloseServiceHandle(handle)

    def stop(self, name: str) -> None:
        """Ask the service to stop (no error when it is not running)."""
        ws = self._ws
        handle = self._open(name, ws.SERVICE_STOP | ws.SERVICE_QUERY_STATUS)
        try:
            ws.ControlService(handle, ws.SERVICE_CONTROL_STOP)
        except Exception as e:
            if getattr(e, "winerror", None) not in (
                ERROR_SERVICE_NOT_ACTIVE,
                ERROR_SERVICE_CANNOT_ACCEPT_CTRL,  # already stopping, or still starting
            ):
                raise _wrap(e) from e
        finally:
            ws.CloseServiceHandle(handle)

    def delete(self, name: str) -> None:
        """Mark the service for deletion (it disappears once stopped and unused)."""
        ws = self._ws
        handle = self._open(name, ws.DELETE)
        try:
            ws.DeleteService(handle)
        except Exception as e:
            if getattr(e, "winerror", None) != ERROR_SERVICE_MARKED_FOR_DELETE:
                raise _wrap(e) from e
        finally:
            ws.CloseServiceHandle(handle)


def wait_for(
    query: Callable[[], ServiceStatus | None],
    done: Callable[[ServiceStatus | None], bool],
    timeout: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    interval: float = 0.25,
) -> ServiceStatus | None:
    """Poll ``query`` until ``done`` holds or ``timeout`` seconds pass; returns the last result.

    Raises:
        TimeoutError: ``done`` never held.
    """
    deadline = clock() + timeout
    while True:
        current = query()
        if done(current):
            return current
        if clock() >= deadline:
            raise TimeoutError
        sleep(interval)


def wait_for_process_exit(pid: int, timeout: float) -> bool:
    """Wait until process ``pid`` has exited; True when it did (or never existed)."""
    if not pid:
        return True
    import win32api
    import win32con
    import win32event

    try:
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
    except Exception:
        return True  # gone already (or not ours to wait for)
    try:
        result = win32event.WaitForSingleObject(handle, int(timeout * 1000))
    finally:
        win32api.CloseHandle(handle)
    return result == win32event.WAIT_OBJECT_0
