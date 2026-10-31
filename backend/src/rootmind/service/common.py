"""Helpers shared by the Windows and the Linux ``service`` commands.

Nothing here touches a service manager, an access list or a Windows API, so
the Linux commands (:mod:`.linux`) use it without importing the Windows
modules (:mod:`.install`, :mod:`.layout`, :mod:`.scm`, ``rootmind.winaccess``).
"""

from __future__ import annotations

import contextlib
import json
import ntpath
import os
import secrets
import stat
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..auth import proves_token
from . import SERVICE_DATABASE_PATH, ExitCode, ServiceCommandError

MAX_MCP_TOKEN_BYTES = 4096

DEFAULT_CONFIG = f"database:\n  path: {SERVICE_DATABASE_PATH}\n"

# The status file as service install and the backend create it (the MCP
# server overwrites it when it starts).
INITIAL_STATUS = b'{"state": "off", "url": null, "error": null, "updated": null}'

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def safe_append(path: Path, text: str) -> bool:
    """Append ``text`` to a log file in the data folder, refusing links and odd files.

    Returns False (and writes nothing) when the file or its folder is a link
    or reparse point, or the file has more than one hard link. A new file is
    owner-only on POSIX (0600).
    """
    try:
        folder = os.lstat(path.parent)
        if _is_reparse(folder) or not stat.S_ISDIR(folder.st_mode):
            return False
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            before = None
        if before is not None and (
            _is_reparse(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink > 1
        ):
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            opened = os.fstat(fd)
            if before is not None and (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                return False
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


@dataclass
class Report:
    """Messages for the console and ``logs/install.log``.

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
        safe_append(self.log_path, text)


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """Replace ``path`` with ``text`` (temporary file in the same folder, then a rename).

    The temporary file is created with ``mode`` (POSIX; Windows ignores it and
    the file inherits its folder's permissions), so the new file never has
    weaker permissions than that, not even for a moment.
    """
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as f:
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)  # the umask may have taken bits away
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def read_token(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def ensure_token(path: Path) -> str:
    """The token in ``path``; a new one is created when the file is missing or empty."""
    token = read_token(path)
    if token:
        return token
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    token = secrets.token_urlsafe(32)
    from ..auth import write_private_file

    write_private_file(path, token)
    return token


def token_readable(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


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
    if _is_reparse(info):
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


def ensure_status_file(path: Path, mode: int = 0o644) -> None:
    """Create the remote-access status file (:data:`INITIAL_STATUS`) when it is missing."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as f:
        f.write(INITIAL_STATUS)


def turn_remote_access_off(config_file: Path, report: Report) -> None:
    """Without MCP over HTTP there is no tunnel: turn remote access off in the config.

    Otherwise turning MCP over HTTP on again later would open the public
    address again without asking. The authtoken is kept.
    """
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return  # the service reports an unreadable config
    section = data.get("remote_access") if isinstance(data, dict) else None
    if not isinstance(section, dict) or section.get("enabled") is not True:
        return
    section["enabled"] = False
    write_atomic(
        config_file,
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
    )
    report.info("Remote access was turned off with MCP over HTTP.")


def database_in_service_folder(config_file: Path, root: Path, pathmod: Any = ntpath) -> bool:
    """Whether the config's ``database.path`` lies in ``<root>/data/db``.

    ``pathmod`` is ``ntpath`` (Windows) or ``posixpath`` (Linux). An
    unreadable config counts as fine here: the service reports it.
    """
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return True
    database = data.get("database") if isinstance(data, dict) else None
    raw = database.get("path") if isinstance(database, dict) else None
    if not isinstance(raw, str) or not raw:
        return False  # the default, ./data/index.db
    folder = pathmod.normcase(pathmod.normpath(pathmod.join(str(root), "data", "db")))
    path = raw if pathmod.isabs(raw) else pathmod.join(str(root), raw)
    path = pathmod.normcase(pathmod.normpath(path))
    return path.startswith(folder + pathmod.sep)


def confirm_owner(env, name: str, url: str, port: int, pid: int, token: str) -> None:
    """Refuse to send ``token`` unless the service itself answers on ``port``.

    Another program (of any account, or WSL port forwarding) that bound the
    port while the service was stopped would otherwise receive the token. The
    service proves that it knows the token (:func:`proves_token`); only a
    server without ``/proof`` is checked through the listener table instead,
    which on some Windows systems does not show the service's socket at all.

    Raises:
        ServiceCommandError: exit 7, another program answers on the port.
    """
    proved = proves_token(env.http, url, token)
    if proved:
        return
    if proved is None and env.port_owned_by(port, pid):
        return
    raise ServiceCommandError(
        ExitCode.HEALTH_CHECK_FAILED,
        f"The program that answers on 127.0.0.1:{port} could not prove that it is {name} "
        f"(PID {pid}): another program may use the port. No token was sent to it. "
        "Stop that program, then run this command again.",
    )


def check_auth(name: str, results: list[tuple[str, int, int]]) -> None:
    for what, status, expected in results:
        if status != expected:
            raise ServiceCommandError(
                ExitCode.HEALTH_CHECK_FAILED,
                f"{name}: {what} answered {status or 'nothing'} instead of {expected}.",
            )


def mcp_initialize(env, url: str, token: str | None) -> int:
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


def status_json(info: dict[str, Any]) -> str:
    return json.dumps(info, indent=2)


def run_command(command: str, report: Report, body: Callable[[], None]) -> int:
    """Run one ``service`` command: report its error, write the log, return the exit code."""
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


HTTP_TIMEOUT_SECONDS = 5


def http_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """Send one request to a local service; returns ``(status, JSON body or None)``.

    Status 0 means no answer (refused, reset or timed out). Proxy settings
    from the environment are ignored: the target is always 127.0.0.1.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 (loopback URL built by the caller)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, None
    try:
        return status, json.loads(payload) if payload else None
    except ValueError:
        return status, None
