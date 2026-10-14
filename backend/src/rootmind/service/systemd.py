"""systemd user units of the RootMind services: the unit files and ``systemctl --user``.

``service install`` writes exactly this unit for each role (interface
contract; the desktop app parses the same lines, ``src-tauri/src/linux/``)::

    # RootMind background service: written by "rootmind-backend service install"; ...
    [Unit]
    Description=...
    [Service]
    Type=notify
    ExecStart="<data>/runtime/<ver>-<id>/rootmind-backend" service run backend --port <P>
    ...
    [Install]
    WantedBy=default.target

The first line marks the unit as ours. Updates read the port back from
``ExecStart`` (sticky ports, as the ImagePath on Windows). In ``ExecStart``
the program path is quoted; ``\\`` and ``"`` are escaped with a backslash,
``%`` (a systemd specifier) is written ``%%`` and ``$`` (a variable) ``$$``.
A path with a control character is refused.

The service reports ready (``Type=notify``) once it has bound its port. A
config, token or folder error ends it with exit code 2, which
``RestartPreventExitStatus`` keeps from being restarted; any other failure is
restarted after 5 seconds. ``UMask=0077`` keeps every file it creates private.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import ROLE_BACKEND, ROLE_MCP, SERVICE_SPECIFIC_CONFIG_ERROR, UNIT_BY_ROLE

MARKER = (
    '# RootMind background service: written by "rootmind-backend service install"; '
    "it rewrites this file."
)
DOCUMENTATION = "https://github.com/cgaeking/rootmind#linux"
DESCRIPTIONS = {
    ROLE_BACKEND: "RootMind backend (REST API and document indexing for the RootMind app)",
    ROLE_MCP: "RootMind MCP server over HTTP (bearer token) for agents such as n8n",
}
START_TIMEOUT_SECONDS = 90
STOP_TIMEOUT_SECONDS = 30
RESTART_SECONDS = 5

# States as the Windows side names them (scm.STOPPED, ...): the status JSON
# and the desktop app use these names on both platforms.
STOPPED = "stopped"
START_PENDING = "start_pending"
STOP_PENDING = "stop_pending"
RUNNING = "running"
OTHER = "other"

_PORT = re.compile(r"[0-9]{1,5}")


@dataclass(frozen=True)
class Unit:
    """A parsed RootMind unit file."""

    role: str
    exe: str
    port: int


def _escape(path: str) -> str:
    if not path or any(ord(c) < 0x20 or c == "\x7f" for c in path):
        raise ValueError("the program path must not be empty or contain control characters")
    return path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")


def _unescape(text: str) -> str | None:
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c in "\\%$":
            if i + 1 >= len(text):
                return None
            following = text[i + 1]
            if (c == "\\" and following in '\\"') or (c in "%$" and following == c):
                out.append(following)
                i += 2
                continue
            return None
        if c == '"':
            return None
        out.append(c)
        i += 1
    return "".join(out)


def exec_start(exe: str, role: str, port: int) -> str:
    """The ``ExecStart=`` value for ``role``."""
    if role not in UNIT_BY_ROLE:
        raise ValueError(f"unknown role {role!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port {port}")
    return f'"{_escape(exe)}" service run {role} --port {port}'


def build_unit(exe: str, role: str, port: int) -> str:
    """The unit file for ``role``."""
    return "\n".join(
        [
            MARKER,
            "[Unit]",
            f"Description={DESCRIPTIONS[role]}",
            f"Documentation={DOCUMENTATION}",
            "",
            "[Service]",
            "Type=notify",
            f"ExecStart={exec_start(exe, role, port)}",
            "Restart=on-failure",
            f"RestartSec={RESTART_SECONDS}",
            f"RestartPreventExitStatus={SERVICE_SPECIFIC_CONFIG_ERROR}",
            f"TimeoutStartSec={START_TIMEOUT_SECONDS}",
            f"TimeoutStopSec={STOP_TIMEOUT_SECONDS}",
            "UMask=0077",
            "NoNewPrivileges=yes",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def parse_exec_start(value: str) -> Unit | None:
    """Parse an ``ExecStart=`` value; None if it does not follow the contract exactly."""
    value = value.strip()
    if not value.startswith('"'):
        return None
    # The closing quote is the first one not escaped by a backslash.
    i = 1
    while i < len(value):
        if value[i] == "\\":
            i += 2
            continue
        if value[i] == '"':
            break
        i += 1
    else:
        return None
    exe = _unescape(value[1:i])
    rest = value[i + 1 :]
    if not exe or rest[:1] != " ":
        return None
    tokens = rest.split()
    if len(tokens) != 5 or tokens[:2] != ["service", "run"] or tokens[3] != "--port":
        return None
    role = tokens[2]
    if role not in UNIT_BY_ROLE or not _PORT.fullmatch(tokens[4]):
        return None
    port = int(tokens[4])
    if not 1 <= port <= 65535:
        return None
    return Unit(role=role, exe=exe, port=port)


def parse_unit(text: str) -> Unit | None:
    """The unit's role, program and port when it is a RootMind unit file, else None.

    Ours means: the marker is the first line, and the ``[Service]`` section
    has exactly one ``ExecStart=`` that follows the contract.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != MARKER:
        return None
    section = None
    starts = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
        elif section == "[Service]" and stripped.startswith("ExecStart="):
            starts.append(stripped[len("ExecStart=") :])
    if len(starts) != 1:
        return None
    return parse_exec_start(starts[0])


# --- systemctl --user ------------------------------------------------------------------


class SystemctlError(OSError):
    """A ``systemctl --user`` call failed; the message has its output."""


class UserManagerUnavailableError(SystemctlError):
    """``systemctl --user`` cannot reach the account's service manager."""


@dataclass(frozen=True)
class UnitState:
    """What ``systemctl --user show`` reports about one unit."""

    name: str
    load_state: str  # loaded, not-found, ...
    active_state: str  # active, activating, deactivating, inactive, failed, reloading
    sub_state: str
    main_pid: int = 0
    exec_main_status: int = 0
    result: str = ""
    fragment_path: str = ""

    @property
    def loaded(self) -> bool:
        return self.load_state not in ("", "not-found")

    @property
    def state(self) -> str:
        """The state by the names the status JSON and the app use."""
        return {
            "active": RUNNING,
            "reloading": RUNNING,
            "activating": START_PENDING,
            "deactivating": STOP_PENDING,
            "inactive": STOPPED,
            "failed": STOPPED,
        }.get(self.active_state, OTHER)


SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainStatus",
    "Result",
    "FragmentPath",
)


def parse_show(name: str, text: str) -> UnitState:
    """Parse ``systemctl show -p ...`` output (``Key=Value`` lines)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()

    def number(key: str) -> int:
        try:
            return int(values.get(key, "0") or 0)
        except ValueError:
            return 0

    return UnitState(
        name=name,
        load_state=values.get("LoadState", ""),
        active_state=values.get("ActiveState", ""),
        sub_state=values.get("SubState", ""),
        main_pid=number("MainPID"),
        exec_main_status=number("ExecMainStatus"),
        result=values.get("Result", ""),
        fragment_path=values.get("FragmentPath", ""),
    )


_NO_MANAGER = re.compile(
    r"Failed to connect to (user scope )?bus|\$DBUS_SESSION_BUS_ADDRESS|\$XDG_RUNTIME_DIR"
    r"|System has not been booted with systemd|Failed to connect to user scope",
    re.IGNORECASE,
)

Runner = Callable[..., subprocess.CompletedProcess]


def find_systemctl() -> str | None:
    for candidate in ("/usr/bin/systemctl", "/bin/systemctl"):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("systemctl")


def user_environment(env: Mapping[str, str] | None = None, uid: int | None = None) -> dict:
    """The environment for ``systemctl --user``: ``XDG_RUNTIME_DIR`` filled in when unset.

    A login session sets it; a shell from ``su`` or ``sudo -u`` often does
    not, although the account's manager runs at ``/run/user/<uid>``.
    """
    result = dict(os.environ if env is None else env)
    if uid is None and hasattr(os, "getuid"):
        uid = os.getuid()
    if not result.get("XDG_RUNTIME_DIR") and uid is not None:
        candidate = f"/run/user/{uid}"
        if os.path.isdir(candidate):
            result["XDG_RUNTIME_DIR"] = candidate
    # Plain output, never a pager or colours.
    result["SYSTEMD_PAGER"] = ""
    result["SYSTEMD_COLORS"] = "0"
    result["LC_ALL"] = "C"
    return result


class Systemctl:
    """``systemctl --user`` for the account that runs this process."""

    TIMEOUT_SECONDS = 120

    def __init__(
        self,
        executable: str | None = None,
        *,
        run: Runner = subprocess.run,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable if executable is not None else find_systemctl()
        self._run = run
        self._env = user_environment(env)

    def _call(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        if not self.executable:
            raise UserManagerUnavailableError("systemctl was not found; RootMind needs systemd")
        try:
            # A fixed program and arguments built here; no shell.
            result = self._run(
                [self.executable, "--user", "--no-ask-password", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.TIMEOUT_SECONDS,
                env=self._env,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise SystemctlError(f"systemctl --user {' '.join(args)} did not finish") from None
        except OSError as e:
            raise UserManagerUnavailableError(
                f"systemctl could not run: {e.strerror or e}"
            ) from None
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            error = UserManagerUnavailableError if _NO_MANAGER.search(message) else SystemctlError
            raise error(f"systemctl --user {' '.join(args)}: {message or result.returncode}")
        return result

    def check(self) -> None:
        """Raise :class:`UserManagerUnavailableError` when the account's manager cannot be reached."""
        self._call("show", "--property=Version")

    def show(self, unit: str) -> UnitState:
        result = self._call("show", unit, "--property=" + ",".join(SHOW_PROPERTIES))
        return parse_show(unit, result.stdout)

    def daemon_reload(self) -> None:
        self._call("daemon-reload")

    def enable(self, *units: str) -> None:
        self._call("enable", *units)

    def disable(self, *units: str) -> None:
        self._call("disable", *units)

    def start(self, unit: str) -> None:
        self._call("start", unit)

    def stop(self, unit: str) -> None:
        self._call("stop", unit)

    def reset_failed(self, unit: str) -> None:
        self._call("reset-failed", unit, check=False)


def unit_path(unit_dir: Path, role: str) -> Path:
    return unit_dir / UNIT_BY_ROLE[role]


__all__ = [
    "MARKER",
    "ROLE_BACKEND",
    "ROLE_MCP",
    "Systemctl",
    "SystemctlError",
    "Unit",
    "UnitState",
    "UserManagerUnavailableError",
    "build_unit",
    "exec_start",
    "parse_exec_start",
    "parse_show",
    "parse_unit",
    "unit_path",
]
