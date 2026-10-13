"""The ImagePath of the RootMind services: how it is written and read back.

The installer writes exactly (interface contract)::

    "<PF>\\RootMind Service\\<ver>-<id>\\rootmind-backend.exe" service run backend --port <P> --user-sid <SID>
    "<PF>\\RootMind Service\\<ver>-<id>\\rootmind-backend.exe" service run mcp-http --port <M>

The desktop app parses the same strings (``imagepath.rs``), with the same
rules: anything else is a service this package did not install. Updates read
the port and the account back from here, so they are "sticky".
"""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass

from . import EXE_NAME, PROGRAM_DIR_NAME, ROLE_BACKEND, ROLE_MCP

_SID = re.compile(r"S-1-\d+(?:-\d+)+")
_PORT = re.compile(r"\d+")


@dataclass(frozen=True)
class ImagePath:
    """A parsed ImagePath."""

    exe: str  # without quotes
    role: str  # "backend" or "mcp-http"
    port: int
    user_sid: str | None = None  # backend role only


def is_sid(value: str) -> bool:
    """A string SID such as ``S-1-5-21-1-2-3-1001``."""
    return len(value) <= 184 and _SID.fullmatch(value) is not None


def build(exe: str, role: str, port: int, user_sid: str | None = None) -> str:
    """The ImagePath for ``role``. The backend needs ``user_sid``, MCP must not have one."""
    if '"' in exe or not exe:
        raise ValueError("the executable path must not be empty or contain quotes")
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port {port}")
    if role == ROLE_BACKEND:
        if user_sid is None or not is_sid(user_sid):
            raise ValueError("the backend service needs the account's SID")
        return f'"{exe}" service run backend --port {port} --user-sid {user_sid}'
    if role == ROLE_MCP:
        if user_sid is not None:
            raise ValueError("the MCP service has no account SID")
        return f'"{exe}" service run mcp-http --port {port}'
    raise ValueError(f"unknown role {role!r}")


def parse(raw: str | None) -> ImagePath | None:
    """Parse an ImagePath value; None if it does not follow the contract exactly."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith('"'):
        return None
    close = raw.find('"', 1)
    if close < 0:
        return None
    exe = raw[1:close]
    args = raw[close + 1 :]
    # The quoted path must be followed by whitespace and arguments.
    if not exe or args[:1] not in (" ", "\t"):
        return None

    tokens = args.split()
    if tokens[:2] != ["service", "run"] or len(tokens) < 3:
        return None
    role = tokens[2]
    if role not in (ROLE_BACKEND, ROLE_MCP):
        return None
    options = tokens[3:]
    if len(options) % 2:
        return None

    port: int | None = None
    user_sid: str | None = None
    for flag, value in zip(options[::2], options[1::2], strict=True):
        if flag == "--port" and port is None:
            if not _PORT.fullmatch(value) or not 1 <= int(value) <= 65535:
                return None
            port = int(value)
        elif flag == "--user-sid" and user_sid is None and role == ROLE_BACKEND:
            if not is_sid(value):
                return None
            user_sid = value
        else:
            return None
    if port is None or (role == ROLE_BACKEND and user_sid is None):
        return None
    return ImagePath(exe=exe, role=role, port=port, user_sid=user_sid)


def program_base(program_files: str) -> str:
    """``<program_files>\\RootMind Service``."""
    return ntpath.join(program_files.rstrip("\\/"), PROGRAM_DIR_NAME)


def exe_is_ours(exe: str, program_files: str) -> bool:
    """Whether ``exe`` is exactly ``<program_files>\\RootMind Service\\<folder>\\rootmind-backend.exe``.

    ``program_files`` comes from the known-folder API, never from the environment.
    """
    if not program_files or "/" in exe:
        return False
    prefix = program_base(program_files).lower() + "\\"
    lowered = exe.lower()
    if not lowered.startswith(prefix):
        return False
    parts = lowered[len(prefix) :].split("\\")
    if len(parts) != 2:
        return False
    folder, name = parts
    return bool(folder) and folder not in (".", "..") and ":" not in folder and name == EXE_NAME
