"""Built-in remote access: an ngrok tunnel to MCP over HTTP.

Cloud assistants (Grok Bot, a cloud-hosted n8n, other web agents) cannot
reach ``127.0.0.1``. With remote access on, the RootMind service's MCP server
(``RootMind-MCP-HTTP``) opens a tunnel with the open-source ngrok SDK
(PyPI ``ngrok``, MIT or Apache-2.0, running in-process; no ngrok agent
program is shipped) and forwards a public HTTPS address to its own
``127.0.0.1`` port. Requests through the tunnel are ordinary MCP requests: they
need the MCP bearer token and pass the same ``Origin`` check. Off by default.

Three files, written by different accounts:

- ``config.yaml``, section ``remote_access`` (``enabled``, ``domain``): written
  by the backend (``POST /remote-access``), read by the MCP server, which
  checks it every few seconds.
- ``data/ngrok_authtoken.txt``: the ngrok authtoken, written by the backend
  only. In the service's data folder only SYSTEM, Administrators and the MCP
  service account can read it (see :mod:`rootmind.service.layout`). It is
  never part of ``config.yaml``, never returned by the API and never logged.
- ``data/remote_access.json``: the tunnel's status (state, public URL, last
  error, time; no secrets), written by the MCP server in place and read by
  the backend. The MCP service account may write exactly this file in the
  data folder.

:class:`RemoteAccessFiles` is the backend's side, :class:`RemoteAccessManager`
the MCP server's: it starts, stops and reconnects the tunnel when the
settings or the authtoken change, and writes the status file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("rootmind.remote_access")

AUTHTOKEN_FILE_NAME = "ngrok_authtoken.txt"
STATUS_FILE_NAME = "remote_access.json"

# States in the status file. "stopped": the MCP server ended (no tunnel).
OFF = "off"
CONNECTING = "connecting"
CONNECTED = "connected"
ERROR = "error"
STOPPED = "stopped"
FILE_STATES = frozenset({OFF, CONNECTING, CONNECTED, ERROR, STOPPED})
# What the API reports besides the file's states: the MCP server does not
# report (not running, or an old status).
UNAVAILABLE = "unavailable"

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 30.0
# The API treats a status older than this as "the MCP server is not running".
STALE_SECONDS = 90.0
CONNECT_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 5.0
BACKOFF_FIRST_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
MAX_STATUS_BYTES = 64 * 1024
MAX_ERROR_CHARS = 500

NO_AUTHTOKEN = "No ngrok authtoken is saved. Enter it under Settings → RootMind service."
STOPPED_BY_NGROK = (
    "ngrok stopped the tunnel (from the ngrok dashboard or API). Turn remote access off "
    "and on again to reconnect."
)

_AUTHTOKEN = re.compile(r"[A-Za-z0-9_\-.]{16,256}")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"(?=.{{1,253}}$)(?:{_LABEL}\.)+{_LABEL}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def authtoken_path(home: Path) -> Path:
    return home / "data" / AUTHTOKEN_FILE_NAME


def status_path(home: Path) -> Path:
    return home / "data" / STATUS_FILE_NAME


def check_authtoken(value: str) -> str:
    """The authtoken without surrounding whitespace.

    Raises:
        ValueError: it does not look like an ngrok authtoken. The message
            never contains the value.
    """
    token = value.strip()
    if not _AUTHTOKEN.fullmatch(token):
        raise ValueError(
            "This does not look like an ngrok authtoken (16 to 256 letters, digits, '_', '-' "
            "or '.'). Copy it from the ngrok dashboard (Your Authtoken)."
        )
    return token


def normalize_domain(value: str | None) -> str | None:
    """A reserved ngrok domain as a lower-case host name; None for an empty value.

    ``https://`` in front and a ``/`` at the end are accepted and removed.

    Raises:
        ValueError: it is not a host name.
    """
    if value is None:
        return None
    domain = value.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    domain = domain.rstrip("/")
    if not domain:
        return None
    if not _DOMAIN.fullmatch(domain):
        raise ValueError(
            "The domain must be a host name such as example.ngrok-free.app, without a path or port"
        )
    return domain


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text: str, secret: str | None) -> str:
    """``text`` with ``secret`` replaced, control characters removed and the length bounded."""
    if secret:
        text = text.replace(secret, "[authtoken]")
    text = _CONTROL.sub(" ", text).strip()
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 1] + "…"
    return text


class RedactingFilter(logging.Filter):
    """Replaces the authtoken in every log record that passes a handler.

    RootMind never logs the authtoken itself; this guards against a library
    message (the ngrok SDK logs into Python logging) that would contain it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.secret: str | None = None

    def filter(self, record: logging.LogRecord) -> bool:
        secret = self.secret
        if not secret:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if secret in message:
            record.msg = message.replace(secret, "[authtoken]")
            record.args = None
        if record.exc_text and secret in record.exc_text:
            record.exc_text = record.exc_text.replace(secret, "[authtoken]")
        return True


# --- the backend's side -----------------------------------------------------------------


class FileProtection(Protocol):
    """How the remote-access files get their permissions."""

    def write_secret(self, path: Path, text: str) -> None:
        """Replace ``path`` with ``text``, readable only by the accounts that may read it."""

    def share_status(self, path: Path) -> None:
        """Create the status file when it is missing, writable by the MCP server."""


class PrivateFiles:
    """Owner-only files (0600 on POSIX), for a backend outside the service.

    On Windows the files inherit the permissions of their folder, as the MCP
    token does there.
    """

    def write_secret(self, path: Path, text: str) -> None:
        from .config import _write_private_atomic

        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private_atomic(path, text)

    def share_status(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"state": OFF, "url": None, "error": None, "updated": None}))


class RemoteAccessFiles:
    """The authtoken and status files of a RootMind home (``<home>/data``)."""

    def __init__(self, home: Path, protection: FileProtection | None = None) -> None:
        self.home = home
        self.protection = protection or PrivateFiles()

    @property
    def authtoken_path(self) -> Path:
        return authtoken_path(self.home)

    @property
    def status_path(self) -> Path:
        return status_path(self.home)

    def authtoken_saved(self) -> bool:
        """Whether an authtoken is saved (the file is not read)."""
        try:
            return self.authtoken_path.stat().st_size > 0
        except OSError:
            return False

    def save_authtoken(self, token: str) -> None:
        self.protection.write_secret(self.authtoken_path, token)

    def clear_authtoken(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.authtoken_path.unlink()

    def prepare_status(self) -> None:
        self.protection.share_status(self.status_path)

    def read_status(self) -> dict[str, Any] | None:
        """The status the MCP server wrote, checked field by field; None when there is none."""
        for attempt in range(2):
            try:
                with open(self.status_path, "rb") as f:
                    raw = f.read(MAX_STATUS_BYTES + 1)
            except OSError:
                return None
            if len(raw) > MAX_STATUS_BYTES:
                return None
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # The MCP server rewrites the file in place; read again once.
                if attempt == 0:
                    time.sleep(0.05)
                    continue
                return None
            return _checked_status(data)
        return None


def _checked_status(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("state") not in FILE_STATES:
        return None
    url = data.get("url")
    if not (isinstance(url, str) and url.startswith("https://") and len(url) <= 2048):
        url = None
    error = data.get("error")
    error = redact(error, None) if isinstance(error, str) and error else None
    updated = data.get("updated")
    return {
        "state": data["state"],
        "url": url,
        "error": error,
        "updated": updated if isinstance(updated, str) else None,
    }


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def effective_status(
    enabled: bool, status: dict[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """The status for the API: the MCP server's report, read against the settings.

    - off while remote access is off, whatever the file says;
    - ``unavailable`` when the MCP server does not report (no file, stopped,
      or no update for :data:`STALE_SECONDS`);
    - ``connecting`` while the MCP server has not picked up the change yet.
    """
    now = now or datetime.now(UTC)
    updated = status.get("updated") if status else None
    if not enabled:
        return {"state": OFF, "url": None, "error": None, "updated": updated}
    when = _parse_time(updated)
    if (
        status is None
        or status["state"] == STOPPED
        or when is None
        or (now - when).total_seconds() > STALE_SECONDS
    ):
        return {"state": UNAVAILABLE, "url": None, "error": None, "updated": updated}
    state = CONNECTING if status["state"] == OFF else status["state"]
    return {
        "state": state,
        "url": status["url"] if state == CONNECTED else None,
        "error": status["error"] if state in (ERROR, CONNECTING) else None,
        "updated": updated,
    }


_sdk_info: tuple[bool, str | None] | None = None


def sdk_info() -> tuple[bool, str | None]:
    """Whether the ngrok SDK can be imported, and its version (cached)."""
    global _sdk_info
    if _sdk_info is None:
        try:
            import ngrok  # noqa: F401
        except Exception:
            _sdk_info = (False, None)
        else:
            from importlib.metadata import PackageNotFoundError, version

            try:
                _sdk_info = (True, version("ngrok"))
            except PackageNotFoundError:
                _sdk_info = (True, None)
    return _sdk_info


# --- the MCP server's side ----------------------------------------------------------------


@dataclass(frozen=True)
class Desired:
    """What the settings ask for. The authtoken never appears in a repr."""

    enabled: bool = False
    domain: str | None = None
    authtoken: str | None = field(default=None, repr=False)
    problem: str | None = None


def _load_sdk():
    import ngrok

    # The SDK bridges its own log into Python logging; warnings and errors are enough.
    with contextlib.suppress(Exception):
        ngrok.log_level("WARN")
    return ngrok


class RemoteAccessManager:
    """Runs the tunnel in the MCP server's process, in a thread with its own event loop.

    Every :data:`POLL_SECONDS` it reads ``config.yaml`` (when it changed) and
    the authtoken file. When remote access is on, it opens an ngrok session
    with the authtoken and an HTTP endpoint (on the reserved domain, if any)
    that forwards to ``http://127.0.0.1:<port>``. A change of the settings or
    the authtoken closes the tunnel and opens it again with the new values;
    turning remote access off closes it. A failed connect is retried after
    5, 10, 20 ... seconds (at most 5 minutes); a lost connection is restored
    by the SDK, which reports it. :meth:`stop` closes the tunnel.

    Args:
        home: The RootMind home (``%ProgramData%\\RootMind`` in the service).
        port: The MCP server's port on 127.0.0.1.
        config_path: ``config.yaml`` (default: ``<home>/config.yaml``).
        sdk_loader: Returns the ngrok module (tests pass a fake).
    """

    def __init__(
        self,
        *,
        home: Path,
        port: int,
        config_path: Path | None = None,
        sdk_loader: Callable[[], Any] = _load_sdk,
        poll_seconds: float = POLL_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        backoff_first: float = BACKOFF_FIRST_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.home = home
        self.port = port
        self.config_path = config_path or home / "config.yaml"
        self.status_file = status_path(home)
        self.authtoken_file = authtoken_path(home)
        self.target = f"http://127.0.0.1:{port}"
        self._sdk_loader = sdk_loader
        self._sdk: Any = None
        self._poll = poll_seconds
        self._heartbeat_every = heartbeat_seconds
        self._backoff_first = backoff_first
        self._backoff_max = backoff_max
        self._connect_timeout = connect_timeout
        self._clock = clock

        self.redactor = RedactingFilter()
        self._filtered_handlers: list[logging.Handler] = []
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False

        # (file id, modification time, size): a replaced file has a new id.
        self._config_signature: tuple[int, int, int] | None = None
        self._settings: tuple[bool, str | None] = (False, None)
        self._token_signature: tuple[int, int, int] | None = None
        self._token: str | None = None
        self._token_problem: str | None = None

        self._session: Any = None
        self._listener: Any = None
        # Callbacks of an earlier session are ignored.
        self._generation = 0
        self.url: str | None = None
        self._halt = False
        self._status: dict[str, Any] = {}
        self._status_written_at = 0.0
        self._status_warned = False

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        """Start the manager's thread (it writes the first status at once)."""
        root = logging.getLogger()
        for handler in root.handlers:
            if self.redactor not in handler.filters:
                handler.addFilter(self.redactor)
                self._filtered_handlers.append(handler)
        self._thread = threading.Thread(
            target=self._thread_main, name="rootmind-remote-access", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Close the tunnel and end the thread."""
        self._stopping = True
        self._notify()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.warning("Remote access did not stop within %.0f seconds", timeout)
        for handler in self._filtered_handlers:
            handler.removeFilter(self.redactor)
        self._filtered_handlers.clear()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            logger.exception("Remote access failed")

    def _notify(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            with contextlib.suppress(RuntimeError):  # the loop has ended
                loop.call_soon_threadsafe(wake.set)

    # --- settings ----------------------------------------------------------------------

    def _read_settings(self) -> tuple[bool, str | None]:
        try:
            info = self.config_path.stat()
        except FileNotFoundError:
            self._config_signature = None
            self._settings = (False, None)
            return self._settings
        except OSError as e:
            logger.warning(
                "Cannot check %s; keeping the remote access settings: %s", self.config_path, e
            )
            return self._settings
        signature = (info.st_ino, info.st_mtime_ns, info.st_size)
        if signature == self._config_signature:
            return self._settings
        from .config import load_config

        try:
            section = load_config(self.config_path).remote_access
        except Exception as e:
            logger.warning(
                "Cannot read %s; keeping the remote access settings: %s", self.config_path, e
            )
            return self._settings
        try:
            domain = normalize_domain(section.domain)
        except ValueError as e:
            logger.warning("remote_access.domain in %s is ignored: %s", self.config_path, e)
            domain = None
        self._config_signature = signature
        self._settings = (section.enabled, domain)
        return self._settings

    def _read_authtoken(self) -> tuple[str | None, str | None]:
        """``(authtoken, problem)``: the saved token, or why it cannot be used."""
        try:
            info = self.authtoken_file.stat()
        except FileNotFoundError:
            self._token_signature, self._token, self._token_problem = None, None, None
            return None, None
        except OSError as e:
            return None, f"Cannot read the ngrok authtoken file: {e.strerror or e}"
        signature = (info.st_ino, info.st_mtime_ns, info.st_size)
        if signature == self._token_signature:
            return self._token, self._token_problem
        try:
            text = self.authtoken_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            reason = e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__
            self._token, self._token_problem = (
                None,
                f"Cannot read the ngrok authtoken file: {reason}",
            )
        else:
            self._token, self._token_problem = (text or None), None
        self._token_signature = signature
        self.redactor.secret = self._token
        return self._token, self._token_problem

    def desired(self) -> Desired:
        enabled, domain = self._read_settings()
        if not enabled:
            return Desired(False, domain)
        token, problem = self._read_authtoken()
        return Desired(True, domain, token, problem)

    # --- status file -------------------------------------------------------------------

    def _set_status(self, state: str, *, url: str | None = None, error: str | None = None) -> None:
        status = {"state": state, "url": url, "error": error}
        if status != self._status:
            self._status = status
            self._write_status()

    def _write_status(self) -> None:
        payload = dict(self._status, updated=_now())
        data = json.dumps(payload).encode("utf-8")
        binary = getattr(os, "O_BINARY", 0)
        try:
            # In place: the MCP service account may write this file, but not
            # create or replace files in the data folder.
            try:
                fd = os.open(self.status_file, os.O_WRONLY | os.O_TRUNC | binary)
            except FileNotFoundError:
                fd = os.open(self.status_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError as e:
            if not self._status_warned:
                self._status_warned = True
                logger.warning(
                    "Cannot write the remote access status to %s: %s. Settings will not show "
                    "the tunnel's state; repair the service to fix the file.",
                    self.status_file,
                    e.strerror or e,
                )
            return
        self._status_warned = False
        self._status_written_at = self._clock()

    def _heartbeat(self) -> None:
        if self._clock() - self._status_written_at >= self._heartbeat_every:
            self._write_status()

    # --- the tunnel --------------------------------------------------------------------

    def _from_sdk(self, generation: int, kind: str, *args: Any) -> None:
        """A callback of the SDK (another thread): hand it to the event loop.

        It must never raise: the SDK would end the session instead of reconnecting.
        """
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(self._on_event, generation, kind, args)

    def _on_event(self, generation: int, kind: str, args: tuple[Any, ...]) -> None:
        if self._session is None or generation != self._generation:
            return
        if kind == "disconnected":
            message = redact(str(args[1]) if len(args) > 1 else "", self._token)
            logger.warning("Remote access: connection to ngrok lost, reconnecting: %s", message)
            self._set_status(CONNECTING, error=f"Connection lost, reconnecting: {message}")
        elif kind == "heartbeat":
            if self._status.get("state") != CONNECTED:
                logger.info("Remote access: connected again (%s)", self.url)
                self._set_status(CONNECTED, url=self.url)
        elif kind == "stop":
            logger.warning("Remote access: ngrok asked to stop the tunnel")
            self._halt = True
            if self._wake is not None:
                self._wake.set()

    async def _open(self, desired: Desired) -> None:
        if self._sdk is None:
            self._sdk = self._sdk_loader()
        sdk = self._sdk
        from . import __version__

        self._generation += 1
        generation = self._generation

        def disconnected(addr: str, error: str) -> None:
            self._from_sdk(generation, "disconnected", addr, error)

        def heartbeat(latency: int) -> None:
            self._from_sdk(generation, "heartbeat", latency)

        def stop_command() -> None:
            self._from_sdk(generation, "stop")

        builder = (
            sdk.SessionBuilder()
            .authtoken(desired.authtoken)
            .client_info("rootmind", __version__)
            .handle_disconnection(disconnected)
            .handle_heartbeat(heartbeat)
            .handle_stop_command(stop_command)
        )
        session = await asyncio.wait_for(builder.connect(), self._connect_timeout)
        try:
            endpoint = session.http_endpoint()
            if desired.domain:
                endpoint = endpoint.domain(desired.domain)
            listener = await asyncio.wait_for(
                endpoint.listen_and_forward(self.target), self._connect_timeout
            )
            url = listener.url()
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.close(), CLOSE_TIMEOUT_SECONDS)
            raise
        self._session, self._listener, self.url = session, listener, url

    async def _close(self) -> None:
        session, listener = self._session, self._listener
        self._session = self._listener = self.url = None
        if listener is not None:
            try:
                await asyncio.wait_for(listener.close(), CLOSE_TIMEOUT_SECONDS)
            except Exception as e:
                logger.warning(
                    "Remote access: closing the endpoint failed: %s", redact(str(e), self._token)
                )
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), CLOSE_TIMEOUT_SECONDS)
            except Exception as e:
                logger.warning(
                    "Remote access: closing the session failed: %s", redact(str(e), self._token)
                )
            logger.info("Remote access: tunnel closed")

    def _describe(self, error: BaseException, desired: Desired) -> str:
        if isinstance(error, TimeoutError):
            return f"ngrok did not answer within {self._connect_timeout:.0f} seconds"
        if isinstance(error, ImportError):
            return "The ngrok SDK is not available in this build"
        return redact(str(error) or type(error).__name__, desired.authtoken)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        applied: Desired | None = None
        retry_at = 0.0
        delay = 0.0
        halted = False
        try:
            while not self._stopping:
                desired = self.desired()
                if desired != applied:
                    if self._session is not None:
                        await self._close()
                    applied, retry_at, delay, halted, self._halt = desired, 0.0, 0.0, False, False
                    if not desired.enabled:
                        self._set_status(OFF)
                    elif desired.problem:
                        logger.warning("Remote access: %s", desired.problem)
                        self._set_status(ERROR, error=desired.problem)
                        halted = True
                    elif not desired.authtoken:
                        self._set_status(ERROR, error=NO_AUTHTOKEN)
                        halted = True
                    else:
                        logger.info(
                            "Remote access on: forwarding %s (domain: %s)",
                            self.target,
                            desired.domain or "assigned by ngrok",
                        )
                if self._halt and not halted:
                    await self._close()
                    self._set_status(ERROR, error=STOPPED_BY_NGROK)
                    halted = True
                if (
                    desired.enabled
                    and not halted
                    and self._session is None
                    and self._clock() >= retry_at
                ):
                    self._set_status(CONNECTING)
                    try:
                        await self._open(desired)
                    except Exception as e:
                        delay = min(max(delay * 2, self._backoff_first), self._backoff_max)
                        retry_at = self._clock() + delay
                        message = self._describe(e, desired)
                        logger.warning(
                            "Remote access: could not open the tunnel (next try in %.0f s): %s",
                            delay,
                            message,
                        )
                        self._set_status(ERROR, error=message)
                    else:
                        delay = 0.0
                        logger.info("Remote access: connected at %s", self.url)
                        self._set_status(CONNECTED, url=self.url)
                self._heartbeat()
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._poll)
        finally:
            await self._close()
            self._status = {"state": STOPPED, "url": None, "error": None}
            self._write_status()
