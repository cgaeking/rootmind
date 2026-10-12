"""FastAPI application factory for the localhost REST API.

The REST API is meant to be reached only by the local desktop app, so the
defense is layered:

- bind to 127.0.0.1 (see ``APIConfig``);
- refuse connections from non-loopback addresses, in case it is bound to a
  network interface anyway;
- reject foreign ``Host`` headers (DNS rebinding);
- allow cross-origin requests only from the desktop app's own origins (no
  credentials);
- with a token (``create_app(api_token=...)``), require
  ``Authorization: Bearer <token>`` on every route except ``GET``/``HEAD`` on
  ``/`` and ``/health``. The docs pages and the OpenAPI schema are then off.

The desktop app's built-in backend and the Windows service always run with a
token. The plain CLI (``rootmind api``) runs without one unless
``RLM_API_TOKEN`` or ``--require-token`` asks for it.

Requests pass the checks in this order: loopback peer (403), CORS, Host header
(400), bearer token (401), routes.
"""

import asyncio
import ipaddress
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from .. import __version__
from ..access import UNRESTRICTED, FolderAccess
from ..auth import TokenError, check_bearer
from ..config import AppConfig, app_home, get_config
from ..connectors import LocalConnector
from ..database import DocumentRepository
from ..indexer import Indexer, SyncManager
from .routes import create_router

logger = logging.getLogger(__name__)

# Routes that answer without a token, for GET and HEAD only: the service name
# and version (and whether auth is on), and the liveness check.
OPEN_PATHS = frozenset({"/", "/health"})
OPEN_METHODS = frozenset({"GET", "HEAD"})

# Host header values accepted by the API (port is ignored by the middleware).
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "tauri.localhost"]

# Origins of the desktop app's webview: Tauri 2 on Windows uses
# http(s)://tauri.localhost, macOS and Linux use tauri://localhost. The API's
# own pages (/docs) are same-origin and need no CORS entry.
ALLOWED_ORIGINS = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]
# Vite dev server, allowed only when RLM_DEV is set (for `pnpm tauri dev`).
DEV_ORIGINS = ["http://localhost:1420", "http://127.0.0.1:1420"]


class AppState:
    """Application state container shared with the route handlers."""

    db: DocumentRepository
    connectors: dict
    indexer: Indexer
    sync_manager: SyncManager
    rlm_engine: Any = None
    config: AppConfig
    # The server's event loop, captured at startup so a config reload running
    # in a worker thread can still attach the scheduler to it.
    loop: asyncio.AbstractEventLoop | None = None
    # Set by create_app(): whether requests need a bearer token, and the policy
    # for reading indexed folders.
    auth_enabled: bool = False
    folder_access: FolderAccess = UNRESTRICTED
    # Folders from the config that could not be used, by connector name, with
    # the reason (refused by the folder access policy, missing, not a folder).
    connector_errors: dict[str, str]

    def __init__(self) -> None:
        self.connector_errors = {}


app_state = AppState()

# Serializes POST /config (save + reload). Re-entrant, so the route can hold it
# around save_config() and reload_app_state() together.
config_update_lock = threading.RLock()


def scheduler_disabled() -> bool:
    """True when RLM_DISABLE_SCHEDULER is set: serve only, never index in this process.

    Used for a process that shares the database with another process that owns
    indexing, so files are not indexed twice.
    """
    return bool(os.environ.get("RLM_DISABLE_SCHEDULER"))


def protected_dirs(config: AppConfig) -> list[Path]:
    """Folders that are never indexed: RootMind's own data.

    That is the ``data`` folder (index, MCP token ``api_token.txt``), the
    database's folder and, when ``RLM_HOME`` is set, all of it (config with API
    keys, logs). Their content must not become searchable, or reach an LLM
    provider, through an indexed folder that contains them.
    """
    folders = {app_home() / "data"}
    if config.database.path:
        folders.add(Path(config.database.path).parent)
    if os.environ.get("RLM_HOME"):
        folders.add(app_home())
    return sorted(folders)


def create_connectors(
    config: AppConfig,
    access: FolderAccess = UNRESTRICTED,
    errors: dict[str, str] | None = None,
) -> dict:
    """Create connector instances from configuration.

    Args:
        config: The configuration.
        access: Folder access policy that every local connector reads through.
        errors: When given, receives the reason for every local folder that
            could not be used (refused by the policy, missing, not a folder),
            by connector name.
    """
    connectors = {}
    protected = protected_dirs(config)

    for connector_config in config.connectors:
        if connector_config.type == "local":
            try:
                connector = LocalConnector(
                    name=connector_config.name,
                    root_path=connector_config.path,
                    include_patterns=connector_config.include
                    if connector_config.include is not None
                    else config.indexer.include,
                    exclude_patterns=connector_config.exclude
                    if connector_config.exclude is not None
                    else config.indexer.exclude,
                    protected_dirs=protected,
                    access=access,
                )
                connectors[connector_config.name] = connector
            except Exception as e:
                logger.warning(f"Could not create connector {connector_config.name!r}: {e}")
                if errors is not None:
                    errors[connector_config.name] = str(e) or type(e).__name__
        else:
            logger.warning(
                f"Unsupported connector type {connector_config.type!r} for connector "
                f"{connector_config.name!r}; only 'local' is supported"
            )

    return connectors


def _new_state(config: AppConfig, reuse_db: DocumentRepository | None = None) -> SimpleNamespace:
    """Build all config-dependent objects without touching ``app_state``."""
    from ..rlm_engine.engine import KnowledgeBaseEngine

    if reuse_db is not None and Path(reuse_db.db_path) == Path(config.database.path):
        db = reuse_db
    else:
        db = DocumentRepository(config.database.path)
    errors: dict[str, str] = {}
    connectors = create_connectors(config, access=app_state.folder_access, errors=errors)
    # Indexing needs no LLM; search uses FTS5.
    indexer = Indexer(db=db, connectors=connectors, config=config)
    return SimpleNamespace(
        config=config,
        db=db,
        connectors=connectors,
        connector_errors=errors,
        indexer=indexer,
        sync_manager=SyncManager(db=db, connectors=connectors, config=config, indexer=indexer),
        rlm_engine=KnowledgeBaseEngine(db=db, config=config),
    )


def _publish(state: SimpleNamespace) -> None:
    for name in (
        "config",
        "db",
        "connectors",
        "connector_errors",
        "indexer",
        "sync_manager",
        "rlm_engine",
    ):
        setattr(app_state, name, getattr(state, name))


def _start_scheduler(sync_manager: SyncManager) -> None:
    if scheduler_disabled():
        logger.info("Scheduler disabled (RLM_DISABLE_SCHEDULER set): serving only, no indexing")
    else:
        sync_manager.setup_scheduler(loop=app_state.loop)


def _index_settings_changed(old: AppConfig, new: AppConfig) -> bool:
    return (
        old.connectors != new.connectors
        or old.indexer != new.indexer
        or old.database != new.database
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    app_state.loop = asyncio.get_running_loop()
    _publish(_new_state(get_config()))
    _start_scheduler(app_state.sync_manager)
    logger.info(f"RootMind API started with {len(app_state.connectors)} connector(s)")

    yield

    app_state.sync_manager.shutdown_scheduler()
    # A running sync stops before its next file instead of delaying the exit.
    app_state.sync_manager.cancel_running()
    _close_llm_client(app_state.rlm_engine)
    _dispose_db(getattr(app_state, "db", None))
    logger.info("RootMind API shut down")


def reload_app_state() -> None:
    """Reload config from disk and rebuild config-dependent state.

    Lets configuration changes (LLM, connectors, schedule) take effect without
    restarting the process. Runs in a worker thread (called from a sync
    route), hence the captured event loop for the scheduler.

    The new state is built completely before it replaces the old one; if
    building fails, the old state stays in place with its scheduler running.
    A sync that is still running is cancelled when the folders or indexer
    settings changed. Its flag is shared per database, so no second sync can
    start on the same file until it has stopped.

    The replaced engine's LLM client is not closed: a question that is still
    running holds it. Garbage collection releases it afterwards.
    """
    from ..config import active_config_path, init_config, load_config

    with config_update_lock:
        config = load_config(active_config_path())
        old_db = getattr(app_state, "db", None)
        old_config = getattr(app_state, "config", None)
        old_manager = getattr(app_state, "sync_manager", None)

        if old_manager is not None:
            try:
                old_manager.shutdown_scheduler()
            except Exception as e:
                logger.warning(f"Scheduler shutdown during reload failed: {e}")

        try:
            new = _new_state(config, reuse_db=old_db)
        except Exception:
            if old_manager is not None:
                _start_scheduler(old_manager)
            raise

        if old_config is not None and _index_settings_changed(old_config, config):
            new.sync_manager.cancel_running()

        _publish(new)
        init_config(config)

        # Folders removed from the config must stop being searchable right away,
        # not only after the next sync.
        try:
            new.db.prune_connectors({c.name for c in config.connectors})
        except Exception as e:
            logger.error(f"Pruning removed connectors failed: {e}")

        _start_scheduler(new.sync_manager)
        if old_db is not None and new.db is not old_db:
            _dispose_db(old_db)

    logger.info(f"RootMind config reloaded ({len(app_state.connectors)} connector(s))")


def _dispose_db(db: DocumentRepository | None) -> None:
    """Dispose a replaced repository's SQLAlchemy engine (closes its connection pool)."""
    if db is None:
        return
    try:
        db.engine.dispose()
    except Exception as e:
        logger.warning(f"Failed to dispose old database engine: {e}")


def _close_llm_client(rlm_engine) -> None:
    """Close an engine's cached LLM SDK client, if one was created (used on shutdown)."""
    if rlm_engine is None or getattr(rlm_engine, "_client", None) is None:
        return
    try:
        rlm_engine._client.close()
    except Exception as e:
        logger.warning(f"Failed to close the LLM client: {e}")


def _is_loopback(host: str | None) -> bool:
    if host is None:  # e.g. a Unix domain socket: local by definition
        return True
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def _authorization_header(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            return value.decode("latin-1")
    return None


class BearerAuthMiddleware:
    """Pure ASGI middleware: every request needs ``Authorization: Bearer <token>``.

    - HTTP: ``GET``/``HEAD`` on ``/`` and ``/health`` pass without a token.
      Everything else without the right token gets 401 with
      ``WWW-Authenticate: Bearer``.
    - WebSocket: the handshake is refused (close code 1008) without the token.
    - Other scopes (lifespan) pass through.

    The token is read only from the ``Authorization`` header, never from the
    query string or a cookie, and compared in constant time.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = token.strip().encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if scope["method"] in OPEN_METHODS and scope["path"] in OPEN_PATHS:
                await self.app(scope, receive, send)
                return
            reason = check_bearer(_authorization_header(scope), self._expected)
            if reason is None:
                await self.app(scope, receive, send)
                return
            response = JSONResponse(
                {"detail": reason}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return
        if scope["type"] == "websocket":
            if check_bearer(_authorization_header(scope), self._expected) is None:
                await self.app(scope, receive, send)
                return
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(
    config: AppConfig | None = None,
    *,
    api_token: str | None = None,
    folder_access: FolderAccess | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    Args:
        config: Optional configuration object. When omitted, ``config.yaml``
            is loaded from the app home at startup.
        api_token: Bearer token every request needs (except ``GET``/``HEAD``
            on ``/`` and ``/health``). None means no authentication, as for
            the plain CLI. With a token, ``/docs``, ``/redoc`` and
            ``/openapi.json`` are off.
        folder_access: Policy for reading indexed folders (see
            :mod:`rootmind.access`). None means :data:`~rootmind.access.UNRESTRICTED`.

    Returns:
        Configured FastAPI application.

    Raises:
        TokenError: ``api_token`` is empty or whitespace.
    """
    if api_token is not None and not api_token.strip():
        raise TokenError("The REST API token is empty; refusing to start without authentication")

    if config is not None:
        from ..config import init_config

        init_config(config)

    app_state.auth_enabled = api_token is not None
    app_state.folder_access = folder_access if folder_access is not None else UNRESTRICTED
    app_state.connector_errors = {}

    docs: dict[str, Any] = {}
    if api_token is not None:
        # The docs and the schema would be the only pages besides / and
        # /health that answer without the token.
        docs = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(
        title="RootMind API",
        description="Local document search and agentic question answering over your files",
        version=__version__,
        lifespan=lifespan,
        **docs,
    )

    # Added first, so it runs last: the loopback, CORS and Host checks answer
    # first, CORS preflights never need the token, and a 401 carries CORS headers.
    if api_token is not None:
        app.add_middleware(BearerAuthMiddleware, token=api_token)

    # Reject requests whose Host header isn't a loopback name. Without this, a
    # web page could reach the API through DNS rebinding despite the 127.0.0.1
    # bind and the CORS policy.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    origins = list(ALLOWED_ORIGINS)
    if os.environ.get("RLM_DEV"):
        origins += DEV_ORIGINS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Added last, so it runs first. The Host header is chosen by the client, so
    # it says nothing about where a request comes from; the peer address does.
    # If the API is bound to a network interface by mistake, this still keeps
    # other machines out.
    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        client_host = request.client.host if request.client else None
        if not _is_loopback(client_host):
            return JSONResponse({"detail": "Only local connections are accepted"}, 403)
        return await call_next(request)

    app.include_router(create_router(app_state))

    return app
