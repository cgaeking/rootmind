"""A fake of the ngrok SDK (PyPI ``ngrok``) for offline tests: no network, no account.

It mimics the part :class:`rootmind.remote_access.RemoteAccessManager` uses:
``SessionBuilder().authtoken(...).client_info(...).handle_*(...).connect()``,
``session.http_endpoint().domain(...).listen_and_forward(url)``,
``listener.url()``, ``listener.close()``, ``session.close()`` and ``log_level``.
Every session is recorded in ``.sessions``; ``fail_next`` makes the next
connects raise (an exception instance each).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

# Obviously fake ngrok authtokens.
FAKE_AUTHTOKEN = "fake_ngrok_authtoken_0000000000000001"  # pragma: allowlist secret
OTHER_AUTHTOKEN = "fake_ngrok_authtoken_0000000000000002"  # pragma: allowlist secret


class FakeListener:
    def __init__(self, session: FakeSession, url: str, target: str) -> None:
        self.session = session
        self._url = url
        self.target = target
        self.closed = False

    def url(self) -> str:
        return self._url

    async def close(self) -> None:
        self.closed = True


class FakeEndpoint:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self._domain: str | None = None

    def domain(self, domain: str) -> FakeEndpoint:
        self._domain = domain
        return self

    async def listen_and_forward(self, url: str) -> FakeListener:
        sdk = self.session.sdk
        if sdk.fail_listen:
            raise sdk.fail_listen.pop(0)
        self.session.domain = self._domain
        host = self._domain or f"generated-{len(sdk.sessions)}.ngrok-free.app"
        listener = FakeListener(self.session, f"https://{host}", url)
        self.session.listener = listener
        return listener


class FakeSession:
    def __init__(self, sdk: FakeNgrok, builder: FakeSessionBuilder) -> None:
        self.sdk = sdk
        self.authtoken = builder._authtoken
        self.client_info = builder._client_info
        self.on_disconnect = builder._on_disconnect
        self.on_heartbeat = builder._on_heartbeat
        self.on_stop = builder._on_stop
        self.domain: str | None = None
        self.listener: FakeListener | None = None
        self.closed = False

    def http_endpoint(self) -> FakeEndpoint:
        return FakeEndpoint(self)

    async def close(self) -> None:
        self.closed = True


class FakeSessionBuilder:
    def __init__(self, sdk: FakeNgrok) -> None:
        self.sdk = sdk
        self._authtoken: str | None = None
        self._client_info: tuple[str, ...] | None = None
        self._on_disconnect = None
        self._on_heartbeat = None
        self._on_stop = None

    def authtoken(self, token: str) -> FakeSessionBuilder:
        self._authtoken = token
        return self

    def client_info(self, client_type: str, version: str, comments: str | None = None):
        self._client_info = (client_type, version)
        return self

    def handle_disconnection(self, handler) -> FakeSessionBuilder:
        self._on_disconnect = handler
        return self

    def handle_heartbeat(self, handler) -> FakeSessionBuilder:
        self._on_heartbeat = handler
        return self

    def handle_stop_command(self, handler) -> FakeSessionBuilder:
        self._on_stop = handler
        return self

    async def connect(self) -> FakeSession:
        sdk = self.sdk
        if sdk.connect_delay:
            await asyncio.sleep(sdk.connect_delay)
        with sdk.lock:
            sdk.attempts.append(self._authtoken)
            if sdk.fail_next:
                raise sdk.fail_next.pop(0)
            session = FakeSession(sdk, self)
            sdk.sessions.append(session)
        return session


class FakeNgrok:
    """Stands in for the ``ngrok`` module."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: list[FakeSession] = []
        self.attempts: list[str | None] = []
        self.fail_next: list[BaseException] = []
        self.fail_listen: list[BaseException] = []
        self.log_levels: list[str] = []
        self.connect_delay = 0.0
        self.loaded = 0

    def SessionBuilder(self) -> FakeSessionBuilder:  # noqa: N802 (the SDK's name)
        return FakeSessionBuilder(self)

    def log_level(self, level: str) -> None:
        self.log_levels.append(level)

    def loader(self):
        """The ``sdk_loader`` for the manager: counts how often the SDK was needed."""

        def load() -> Any:
            self.loaded += 1
            return self

        return load

    @property
    def open_sessions(self) -> list[FakeSession]:
        return [s for s in self.sessions if not s.closed]
