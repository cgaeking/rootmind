"""Find the RootMind service of this account and its REST token.

The same steps as the desktop app and the Claude Desktop bundle:

1. ``service status`` (:func:`rootmind.service.install.status` on Windows,
   :func:`rootmind.service.linux.status` on Linux) names the REST port and the
   token file, without administrator rights;
2. the service must be running, and the socket on ``127.0.0.1:<port>`` must
   belong to the service's own process (the PID from the Service Control
   Manager or systemd). Otherwise another program could have taken the port
   and would receive the token;
3. the token is read from its file: on Windows
   ``%ProgramData%\\RootMind\\data\\rest_token.txt`` (readable by the account
   that installed RootMind, SYSTEM and administrators), on Linux
   ``~/.local/share/rootmind/data/rest_token.txt`` (mode 0600).

``--url`` (or ``RLM_URL``) skips the discovery, for a development backend
(``rootmind api``) or a tunnel; the token then comes from ``--token-file``
(or ``RLM_TOKEN_FILE``) or is not sent.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .rest import ClientError, ClientExit, check_url

URL_ENV = "RLM_URL"
TOKEN_FILE_ENV = "RLM_TOKEN_FILE"  # noqa: S105 (an environment variable name)
MAX_TOKEN_BYTES = 4096


class ServiceProbe(Protocol):
    """What discovery needs from the machine (tests pass a fake)."""

    platform: str  # "windows" or "linux"

    def status(self) -> dict[str, Any]: ...

    def backend_pid(self) -> int | None: ...

    def port_owned_by(self, port: int, pid: int) -> bool: ...


@dataclass
class Target:
    """Where the commands send their requests."""

    url: str
    token: str | None = field(repr=False)
    discovered: bool
    service: dict[str, Any] | None = None  # the service status, when discovered


class _MachineProbe:
    """The real machine: the Windows services or the systemd user units."""

    def __init__(self, env: Any, linux: bool) -> None:
        self._env = env
        self.platform = "linux" if linux else "windows"

    def _commands(self):
        if self.platform == "linux":
            from ..service import linux

            return linux
        from ..service import install

        return install

    def status(self) -> dict[str, Any]:
        return self._commands().status(self._env)

    def backend_pid(self) -> int | None:
        from ..service import BACKEND_SERVICE, ROLE_BACKEND, UNIT_BY_ROLE

        if self.platform == "linux":
            state = self._env.systemctl().show(UNIT_BY_ROLE[ROLE_BACKEND])
            return state.main_pid or None
        with self._env.scm(write=False) as manager:
            current = manager.query(BACKEND_SERVICE)
        return current.pid if current is not None and current.pid else None

    def port_owned_by(self, port: int, pid: int) -> bool:
        return bool(self._env.port_owned_by(port, pid))


def default_probe() -> ServiceProbe | None:
    """The probe of this machine, or None where RootMind has no service (not Windows or Linux)."""
    try:
        from ..service.cli import _default_env
    except ModuleNotFoundError:  # a build without the service package
        return None
    env = _default_env()
    if env is None:
        return None
    linux = bool(getattr(env, "is_linux", lambda: False)())
    if not linux and not env.is_windows():
        return None
    return _MachineProbe(env, linux)


def read_token_file(path: Path, what: str = "token file") -> str:
    """The token in ``path``: one line of at most 4 KB.

    Raises:
        ClientError: exit 4 when the file cannot be read (permissions), exit 2
            when it is missing, empty or not a single token.
    """
    try:
        data = path.read_bytes()[: MAX_TOKEN_BYTES + 1]
    except FileNotFoundError:
        raise ClientError(f"the {what} {path} does not exist", ClientExit.USAGE) from None
    except PermissionError:
        raise ClientError(
            f"this account cannot read the {what} {path}", ClientExit.NOT_ALLOWED
        ) from None
    except OSError as e:
        raise ClientError(
            f"cannot read the {what} {path}: {e.strerror or e}", ClientExit.NOT_ALLOWED
        ) from None
    if len(data) > MAX_TOKEN_BYTES:
        raise ClientError(f"the {what} {path} is larger than 4 KB", ClientExit.USAGE)
    try:
        token = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise ClientError(f"the {what} {path} is not UTF-8 text", ClientExit.USAGE) from None
    if not token or any(c.isspace() for c in token):
        raise ClientError(f"the {what} {path} must hold one token on one line", ClientExit.USAGE)
    return token


def _not_readable_hint(platform: str) -> str:
    if platform == "windows":
        return (
            "Only the account that set up the RootMind service can read it (administrators "
            "only from an elevated prompt). Run this command as that account."
        )
    return (
        "Only the account that owns the RootMind user service can read it (mode 0600). "
        "Run this command as that account, not with sudo."
    )


def _stopped_hint(platform: str) -> str:
    if platform == "windows":
        return (
            "Start it with Start-Service RootMind-Backend from an elevated prompt, or with "
            "Repair under Settings -> RootMind service in the app."
        )
    return "Start it with: systemctl --user start rootmind-backend"


def discover(
    *,
    url: str | None = None,
    token_file: str | Path | None = None,
    probe: ServiceProbe | None = None,
    need_token: bool = True,
) -> Target:
    """The REST API to talk to, with its token.

    Args:
        url: ``--url``; falls back to ``RLM_URL``. When set, no service is looked up.
        token_file: ``--token-file``; falls back to ``RLM_TOKEN_FILE``.
        probe: The machine (tests pass a fake); default :func:`default_probe`.
        need_token: False for commands that only read the service's state.

    Raises:
        ClientError: no usable service, or the token cannot be read.
    """
    url = url or os.environ.get(URL_ENV, "").strip() or None
    token_file = token_file or os.environ.get(TOKEN_FILE_ENV, "").strip() or None
    if url is not None:
        url = check_url(url)
        token = read_token_file(Path(token_file).expanduser()) if token_file else None
        return Target(url=url, token=token, discovered=False)

    if probe is None:
        probe = default_probe()
    if probe is None:
        raise ClientError(
            f"the RootMind service runs on Windows and Linux only; on {sys.platform} pass "
            "--url (and --token-file) of a RootMind API",
            ClientExit.NO_SERVICE,
        )
    try:
        info = probe.status()
    except ClientError:
        raise
    except Exception as e:  # ServiceCommandError, OSError from the service manager
        message = getattr(e, "message", None) or getattr(e, "strerror", None) or str(e)
        raise ClientError(
            f"cannot read the state of the RootMind service: {message}", ClientExit.NO_SERVICE
        ) from None
    try:
        return _target(info, probe, token_file, need_token)
    except ClientError as e:
        e.service = info
        raise


def _target(
    info: dict[str, Any], probe: ServiceProbe, token_file: str | Path | None, need_token: bool
) -> Target:
    """The target from the service status (see :func:`discover`)."""
    if not info.get("installed"):
        raise ClientError(
            "the RootMind service is not set up for this account. Install it with "
            "'rootmind-backend service install' (or start the RootMind app), or pass --url.",
            ClientExit.NO_SERVICE,
        )
    if not info.get("ours") or not isinstance(info.get("port"), int):
        raise ClientError(
            "a service named like RootMind's exists, but RootMind did not set it up; "
            "no token is sent to it",
            ClientExit.NO_SERVICE,
        )
    port = info["port"]
    service_url = f"http://127.0.0.1:{port}"
    if info.get("state") != "running":
        raise ClientError(
            f"the RootMind service is {info.get('state') or 'not running'}. "
            + _stopped_hint(probe.platform),
            ClientExit.NO_SERVICE,
        )

    token = None
    if need_token:
        if token_file:
            token = read_token_file(Path(token_file).expanduser())
        else:
            path = info.get("token_file")
            if not path:
                raise ClientError("the service status names no token file", ClientExit.NO_SERVICE)
            try:
                token = read_token_file(Path(path), "REST token file")
            except ClientError as e:
                if e.code == ClientExit.NOT_ALLOWED:
                    raise ClientError(
                        f"{e.message}. {_not_readable_hint(probe.platform)}",
                        ClientExit.NOT_ALLOWED,
                    ) from None
                raise
        try:
            pid = probe.backend_pid()
        except Exception:  # the service manager cannot be asked
            pid = None
        if not pid or not probe.port_owned_by(port, pid):
            raise ClientError(
                f"cannot confirm that the RootMind service itself listens on 127.0.0.1:{port} "
                "(another program may have taken the port); no token was sent. "
                + _stopped_hint(probe.platform),
                ClientExit.NO_SERVICE,
            )
    return Target(url=service_url, token=token, discovered=True, service=info)
