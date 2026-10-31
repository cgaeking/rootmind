"""MCP server over HTTP, protected by a bearer token.

For remote or server-side MCP clients (e.g. n8n's MCP Client Tool). JSON-RPC
is handled in-process on top of FastAPI and serves the same read-only tools as
the agent loop (see :mod:`rootmind.tools`).

Endpoints:

- ``POST /mcp``          JSON-RPC over plain HTTP (streamable-HTTP style, JSON responses)
- ``GET  /mcp/sse``      legacy SSE transport: event stream
- ``POST /mcp/message``  legacy SSE transport: client-to-server messages
- ``GET  /health``       liveness check, no auth
- ``GET  /proof``        proof that the server knows the token (loopback, no auth;
  see :mod:`rootmind.auth`)

There are no OpenAPI or docs pages: everything except ``/health`` and ``/proof``
needs the token.

Like the stdio server, it only serves by default and never indexes. Keep the
index current with the REST API or ``rootmind index``. ``--index`` also runs
the background indexer in this process; use it only when no other process
(such as the REST API) indexes the same database, because each indexing
process removes the documents of folders missing from *its* copy of the config.

The tools return only documents of the folders listed in the config. The
server re-reads that list when ``config.yaml`` changes (checked on every tool
call), so the tools follow an added or removed folder without a restart. A
change of ``database.path`` still needs one, and so does a folder change with
``--index``: the in-process indexer keeps the folders it started with, so a
new folder is not indexed and a removed one is still indexed until the
restart.

Security model:

- Every MCP endpoint requires ``Authorization: Bearer <token>``. The token
  named ``default`` comes from ``MCP_API_TOKEN`` or
  ``<RLM_HOME>/data/api_token.txt`` (generated on first start, owner-only
  permissions where the OS supports them) and sees every folder. An empty
  token refuses to start (fail closed). Tokens are compared in constant time
  and never logged or printed; logs show only a short SHA-256 fingerprint.
- Named tokens (``mcp_http.tokens`` in ``config.yaml``, managed through the
  REST API and ``rootmind mcp tokens``) are stored as SHA-256 hashes only,
  each with an optional folder scope that narrows
  :attr:`rootmind.tools.ToolContext.allowed_connectors` for its requests. The
  list is re-read when ``config.yaml`` changes (checked on every request), so
  a revoked token is refused with its next request.
- Requests with an ``Origin`` header from a non-loopback origin are rejected
  (DNS rebinding protection), unless listed in ``MCP_ALLOWED_ORIGINS`` or
  ``mcp_http.allowed_origins``.
- Binds to 127.0.0.1 by default. Another address needs TLS (``--tls-cert``
  and ``--tls-key``) or an explicit ``--insecure-http``. The RootMind service
  can add a second listener for other machines (``mcp_http.network``, see
  :class:`NetworkListener`): it checks the ``Host`` header against
  ``mcp_http.allowed_hosts`` and accepts only named tokens unless
  ``allow_default_token`` is set.

Usage::

    rootmind-mcp-http [--host 127.0.0.1] [--port 3000] [--config config.yaml] [--index]
                      [--tls-cert FILE --tls-key FILE | --insecure-http]
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import uuid
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__, auth, tools
from .auth import TokenError, token_fingerprint
from .config import (
    DEFAULT_MCP_TOKEN_NAME,
    LOOPBACK_ADDRESSES,
    AppConfig,
    McpHttpConfig,
    app_home,
    config_file_path,
    get_config,
    init_config,
    load_config,
)
from .database.repository import DocumentRepository

logger = logging.getLogger("rootmind.mcp_http")

SERVER_NAME = "rootmind"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3000
TOKEN_FILE_NAME = "api_token.txt"  # noqa: S105 (a file name, not a secret)
TOKEN_ENV = "MCP_API_TOKEN"  # noqa: S105 (an environment variable name)

# Handshake-era protocol versions this transport implements, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Compatibility shim: some clients (n8n) send tool arguments flat in `params`
# instead of nested under `params.arguments`. These keys are client metadata,
# not tool arguments, and are dropped when flattening.
_NON_ARGUMENT_PARAMS = {"name", "tool", "toolCallId", "sessionId", "action", "chatInput"}


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TokenIdentity:
    """Who a request's token belongs to, and which folders it may see (None: all)."""

    name: str
    folders: frozenset[str] | None = None


DEFAULT_IDENTITY = TokenIdentity(DEFAULT_MCP_TOKEN_NAME)
# Set in the ASGI scope by NetworkListener for requests from the network listener.
NETWORK_SCOPE_KEY = "rootmind.network"


def token_sha256(token: str) -> str:
    """The hex SHA-256 under which a named token is stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _named_tokens(mcp: McpHttpConfig) -> list[tuple[bytes, TokenIdentity]]:
    return [
        (
            t.sha256.encode("ascii"),
            TokenIdentity(t.name, frozenset(t.folders) if t.folders is not None else None),
        )
        for t in mcp.tokens
    ]


def host_without_port(value: str | None) -> str | None:
    """The host part of a ``Host`` header, lowercased (``[::1]:3443`` -> ``::1``)."""
    if not value:
        return None
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else None
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value or None


class NetworkListener:
    """ASGI wrapper for the listener that other machines reach.

    Refuses a ``Host`` that is not in ``allowed_hosts()`` (400) and marks the
    request as coming from the network (:data:`NETWORK_SCOPE_KEY`), where the
    default token is refused unless ``allow_default_token`` is set.
    """

    def __init__(self, app: Any, allowed_hosts: Callable[[], Iterable[str]]) -> None:
        self.app = app
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            host = host_without_port(headers.get(b"host", b"").decode("latin-1"))
            # Entries are names or addresses; a port given with one is ignored.
            allowed = {host_without_port(h) for h in self._allowed_hosts()} - {None}
            if host is None or host not in allowed:
                logger.warning("Network listener: refused a request for host %r", host)
                response = JSONResponse({"detail": "Host not allowed"}, status_code=400)
                await response(scope, receive, send)
                return
            scope = {**scope, NETWORK_SCOPE_KEY: True}
        await self.app(scope, receive, send)


# --- token handling --------------------------------------------------------------


def token_file_path() -> Path:
    """Default location of the generated token: ``<RLM_HOME or cwd>/data/api_token.txt``."""
    return app_home() / "data" / TOKEN_FILE_NAME


def _write_private_file(path: Path, content: str) -> None:
    """Create ``path`` with owner-only permissions (see :func:`rootmind.auth.write_private_file`)."""
    auth.write_private_file(path, content)


def resolve_token(token_file: Path | None = None) -> tuple[str, str]:
    """Load or create the bearer token.

    Priority: ``MCP_API_TOKEN`` env var, then the token file, then a newly
    generated token written to the token file.

    Returns:
        ``(token, source)`` where ``source`` describes where it came from
        (safe to log; never contains the token).

    Raises:
        TokenError: the env var or the file is present but empty (fail closed).
    """
    return auth.resolve_token(TOKEN_ENV, token_file or token_file_path(), generate=True)


def origin_allowed(origin: str | None, extra_origins: Iterable[str] = ()) -> bool:
    """Allow requests without an Origin (non-browser clients), "null", loopback origins
    and explicitly configured origins."""
    if origin is None or origin == "null":
        return True
    if origin in set(extra_origins):
        return True
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def _env_allowed_origins() -> list[str]:
    raw = os.environ.get("MCP_ALLOWED_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


# --- JSON-RPC --------------------------------------------------------------------


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _negotiate_version(params: dict[str, Any]) -> str:
    requested = params.get("protocolVersion")
    return requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]


def _tool_arguments(params: dict[str, Any]) -> dict[str, Any] | str | None:
    args = params.get("arguments")
    if args is None:
        args = {k: v for k, v in params.items() if k not in _NON_ARGUMENT_PARAMS}
    return args


def _config_signature(path: Path) -> tuple[int, int] | None:
    """``(st_mtime_ns, st_size)`` of the config file, or None when it does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def create_app(
    token: str,
    config: AppConfig | None = None,
    *,
    allowed_origins: Iterable[str] = (),
    enable_scheduler: bool = False,
) -> FastAPI:
    """Create the MCP HTTP app.

    Args:
        token: The default bearer token (every folder). Empty is refused.
        config: Configuration; defaults to the global config at startup. Its
            ``mcp_http`` section adds named tokens and allowlists; the file's
            current version wins once the server runs.
        allowed_origins: Extra browser origins to accept besides loopback.
        enable_scheduler: Also run background indexing in this process
            (``--index``). Off by default: serve only.

    Raises:
        TokenError: ``token`` is empty or whitespace.
    """
    if not token or not token.strip():
        raise TokenError("An MCP API token is required; refusing to start without authentication")
    expected = token.encode("utf-8")
    extra_origins = frozenset(allowed_origins)
    initial = config.mcp_http if config is not None else McpHttpConfig()

    state = SimpleNamespace(
        repo=None,
        allowed=None,
        sync_manager=None,
        sse_sessions={},
        config_path=None,
        config_signature=None,
        folders=[],
        connectors=[],
        named=_named_tokens(initial),
        mcp=initial,
    )
    allowed_lock = threading.Lock()

    def apply(cfg: AppConfig) -> None:
        state.allowed = frozenset(c.name for c in cfg.connectors)
        state.connectors = list(cfg.connectors)
        state.folders = tools.folder_labels(cfg.connectors)
        state.mcp = cfg.mcp_http
        state.named = _named_tokens(cfg.mcp_http)

    def current_allowed() -> frozenset[str] | None:
        """The configured folder names; folders and tokens are re-read when ``config.yaml`` changed.

        Called on every request (from worker threads). If the file cannot be
        read, the previous folders and tokens stay in place.
        """
        path = state.config_path
        if path is None:
            return state.allowed
        try:
            signature = _config_signature(path)
        except OSError as e:
            logger.warning("Cannot check %s for changes; keeping the folder list: %s", path, e)
            return state.allowed
        if signature is None or signature == state.config_signature:
            return state.allowed
        with allowed_lock:
            if signature == state.config_signature:
                return state.allowed
            try:
                cfg = load_config(path)
            except Exception as e:
                logger.warning("Cannot re-read %s; keeping the folder list: %s", path, e)
                return state.allowed
            before_folders, before_tokens = state.allowed, {i.name for _h, i in state.named}
            apply(cfg)
            if state.allowed != before_folders:
                logger.info("Folder list re-read from %s: %d folder(s)", path, len(state.allowed))
            tokens = {i.name for _h, i in state.named}
            if tokens != before_tokens:
                logger.info(
                    "MCP tokens re-read from %s: %d named token(s)%s",
                    path,
                    len(tokens),
                    f"; revoked: {', '.join(sorted(before_tokens - tokens))}"
                    if before_tokens - tokens
                    else "",
                )
            state.config_signature = signature
            return state.allowed

    def scope_for(identity: TokenIdentity) -> frozenset[str] | None:
        """The folders a request with ``identity`` may see."""
        allowed = current_allowed()
        if identity.folders is None:
            return allowed
        if allowed is None:
            return identity.folders
        return allowed & identity.folders

    def labels_for(allowed: frozenset[str] | None) -> list[str]:
        if allowed is None or not state.connectors:
            return state.folders
        return tools.folder_labels([c for c in state.connectors if c.name in allowed])

    def authenticate(presented: str, network: bool) -> TokenIdentity | None:
        """The identity of a presented token, or None. Every check runs in constant time."""
        raw = presented.encode("utf-8")
        match: TokenIdentity | None = None
        if secrets.compare_digest(raw, expected):
            if network and not state.mcp.network.allow_default_token:
                logger.warning(
                    "Network listener: refused the default token (only named tokens; see "
                    "mcp_http.network.allow_default_token)"
                )
            else:
                match = DEFAULT_IDENTITY
        if state.named:
            digest = token_sha256(presented).encode("ascii")
            for stored, identity in state.named:
                if secrets.compare_digest(digest, stored) and match is None:
                    match = identity
        return match

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Note the file's state before reading the folder list, so a change
        # made in between is picked up by the first tool call. The config
        # passed in may have been read earlier (before the socket was bound),
        # so the folder list is read from the file again here.
        state.config_path = config_file_path()
        try:
            state.config_signature = _config_signature(state.config_path)
        except OSError:
            state.config_signature = None
        cfg = config or get_config()
        state.repo = DocumentRepository(cfg.database.path)
        current = cfg
        if state.config_signature is not None:
            try:
                current = load_config(state.config_path)
            except Exception as e:
                logger.warning(
                    "Cannot re-read %s; using the folder list given: %s", state.config_path, e
                )
        apply(current)
        if state.named:
            logger.info("Named MCP tokens: %s", ", ".join(i.name for _h, i in state.named))
        stats = state.repo.get_statistics()
        logger.info(
            "Database ready: %d documents, %s characters",
            stats["indexed_documents"],
            f"{stats['total_content_chars']:,}",
        )

        if enable_scheduler:
            from .api.app import create_connectors
            from .indexer import Indexer, SyncManager

            connectors = create_connectors(cfg)
            state.sync_manager = SyncManager(
                state.repo, connectors, cfg, Indexer(state.repo, connectors, cfg)
            )
            state.sync_manager.setup_scheduler(loop=asyncio.get_running_loop())
        else:
            logger.info("Scheduler disabled: serving only, another process owns indexing")

        yield

        if state.sync_manager is not None:
            state.sync_manager.shutdown_scheduler()
        state.repo.engine.dispose()

    # No /docs, /redoc or /openapi.json: they would be the only pages besides
    # /health that answer without the token.
    app = FastAPI(
        title="RootMind MCP server",
        description="MCP over HTTP for document search and retrieval",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.mcp = state

    bearer = HTTPBearer(auto_error=False)

    async def authorize(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> TokenIdentity:
        # Re-read config.yaml if it changed: a revoked token is refused at once.
        await asyncio.to_thread(current_allowed)
        origins = extra_origins | frozenset(state.mcp.allowed_origins)
        if not origin_allowed(request.headers.get("origin"), origins):
            raise HTTPException(status_code=403, detail="Origin not allowed")
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        identity = authenticate(credentials.credentials, bool(request.scope.get(NETWORK_SCOPE_KEY)))
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return identity

    async def dispatch(
        method: str, params: dict[str, Any], identity: TokenIdentity
    ) -> dict[str, Any]:
        if method == "initialize":
            # Re-read the folder list if config.yaml changed, so the
            # instructions name the folders as they are now (and only those
            # this token may see).
            allowed = await asyncio.to_thread(scope_for, identity)
            return {
                "protocolVersion": _negotiate_version(params),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": tools.server_instructions(labels_for(allowed)),
            }
        if method in ("ping", "initialized", "notifications/initialized"):
            return {}
        if method == "tools/list":
            return {"tools": tools.mcp_tools()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                raise JsonRpcError(-32602, "Missing tool name")
            if identity is DEFAULT_IDENTITY:
                logger.info("Tool call: %s", name)
            else:
                logger.info("Tool call: %s (token %s)", name, identity.name)

            def run_tool(arguments: dict[str, Any] | str | None) -> tools.ToolResult:
                ctx = tools.ToolContext(allowed_connectors=scope_for(identity))
                return tools.execute(name, arguments, state.repo, ctx)

            # Tool execution (and the config check) is blocking I/O; keep it
            # off the event loop.
            result = await asyncio.to_thread(run_tool, _tool_arguments(params))
            return {
                "content": [{"type": "text", "text": result.text}],
                "isError": result.is_error,
            }
        raise JsonRpcError(-32601, f"Method not found: {method}")

    async def handle_message(body: Any, identity: TokenIdentity) -> dict[str, Any] | None:
        """Handle one JSON-RPC message. Returns None when no response is due."""
        if not isinstance(body, dict):
            return _error(None, -32600, "Invalid request")
        method = body.get("method")
        is_notification = "id" not in body
        if not isinstance(method, str):
            # A client response or a notification without method needs no reply.
            if is_notification or "result" in body or "error" in body:
                return None
            return _error(body.get("id"), -32600, "Invalid request")
        if is_notification:
            logger.debug("Notification: %s", method)
            return None

        request_id = body["id"]
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, -32602, "params must be an object")
        try:
            result = await dispatch(method, params, identity)
        except JsonRpcError as e:
            return _error(request_id, e.code, e.message)
        except Exception as e:
            logger.exception("Error handling %s", method)
            return _error(request_id, -32603, str(e))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def read_json(request: Request) -> Any:
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise JsonRpcError(-32700, f"Parse error: {e}") from e

    @app.get("/health")
    async def health():
        """Liveness check (no auth)."""
        return {"status": "ok", "server": {"name": SERVER_NAME, "version": __version__}}

    @app.get(auth.PROOF_PATH)
    async def proof(request: Request):
        """Proof that this server knows the default token, before a client sends it (no auth).

        Only on loopback: the clients that check it (service install) are local.
        """
        if request.scope.get(NETWORK_SCOPE_KEY):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        status, body = auth.proof_response(expected.strip(), request.query_params.get("nonce"))
        return JSONResponse(body, status_code=status)

    @app.post("/mcp")
    async def mcp_post(request: Request, identity: Annotated[TokenIdentity, Depends(authorize)]):
        """JSON-RPC endpoint. Notifications get 202 with an empty body."""
        try:
            body = await read_json(request)
        except JsonRpcError as e:
            return JSONResponse(_error(None, e.code, e.message), status_code=400)
        if isinstance(body, list):
            return JSONResponse(
                _error(None, -32600, "Batch requests are not supported"), status_code=400
            )
        response = await handle_message(body, identity)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    @app.get("/mcp/sse")
    async def mcp_sse(request: Request, identity: Annotated[TokenIdentity, Depends(authorize)]):
        """Legacy SSE transport: announce the message endpoint, then stream responses.

        The session belongs to the token that opened it: messages for it must
        carry the same token.
        """
        session_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        state.sse_sessions[session_id] = (queue, identity)
        logger.info("SSE session opened: %s", session_id)

        async def event_stream():
            endpoint_url = str(request.url).replace("/sse", "/message")
            yield f"event: endpoint\ndata: {endpoint_url}?sessionId={session_id}\n\n"
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"event: message\ndata: {json.dumps(message)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                state.sse_sessions.pop(session_id, None)
                logger.info("SSE session closed: %s", session_id)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/mcp/message")
    async def mcp_sse_message(
        request: Request,
        sessionId: str,  # noqa: N803 (wire name)
        identity: Annotated[TokenIdentity, Depends(authorize)],
    ):
        """Legacy SSE transport: accept a message; the response goes out on the stream."""
        session = state.sse_sessions.get(sessionId)
        if session is None or session[1] != identity:
            raise HTTPException(status_code=404, detail="Session not found")
        queue = session[0]
        try:
            body = await read_json(request)
        except JsonRpcError as e:
            raise HTTPException(status_code=400, detail=e.message) from e
        response = await handle_message(body, identity)
        if response is not None:
            await queue.put(response)
        return Response(status_code=202)

    return app


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``rootmind-mcp-http``."""
    parser = argparse.ArgumentParser(
        prog="rootmind-mcp-http", description="RootMind MCP server (HTTP, bearer auth)"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HTTP_HOST", DEFAULT_HOST),
        help=f"Host to bind to (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_HTTP_PORT", str(DEFAULT_PORT))),
        help=f"Port to bind to (default: {DEFAULT_PORT})",
    )
    parser.add_argument("--config", "-c", default=None, help="Path to config.yaml")
    parser.add_argument(
        "--index",
        action="store_true",
        help=(
            "Also run the background indexer in this process. Only when no other "
            "process (such as the REST API) indexes the same database. The indexer "
            "keeps the folders it started with; restart after changing them."
        ),
    )
    parser.add_argument("--tls-cert", metavar="FILE", help="Serve HTTPS with this certificate")
    parser.add_argument("--tls-key", metavar="FILE", help="The private key of --tls-cert")
    parser.add_argument(
        "--insecure-http",
        action="store_true",
        help=(
            "Allow plain HTTP on an address other than loopback (the token crosses the "
            "network unencrypted; only for a trusted network or a TLS proxy elsewhere)"
        ),
    )
    args = parser.parse_args(argv)
    problems = listen_problems(args.host, args.tls_cert, args.tls_key, args.insecure_http)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = init_config(args.config)

    try:
        try:
            token, source = resolve_token()
        finally:
            # The token is not needed in the environment once read; no process
            # started later inherits it.
            os.environ.pop(TOKEN_ENV, None)
        app = create_app(
            token, config, allowed_origins=_env_allowed_origins(), enable_scheduler=args.index
        )
    except TokenError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    scheme = "https" if args.tls_cert else "http"
    if args.host not in _LOOPBACK_HOSTS:
        logger.warning(
            "MCP over HTTP listens on %s:%d, reachable from other machines (%s).",
            args.host,
            args.port,
            "TLS" if args.tls_cert else "plain HTTP: tokens cross the network unencrypted",
        )

    print("RootMind MCP HTTP server")
    print(f"  Endpoint: {scheme}://{args.host}:{args.port}/mcp")
    print(f"  Health:   {scheme}://{args.host}:{args.port}/health")
    print(f"  Auth:     bearer token from {source}")
    print(f"            fingerprint sha256:{token_fingerprint(token)}")
    if config.mcp_http.tokens:
        print(f"            and {len(config.mcp_http.tokens)} named token(s) from config.yaml")

    import uvicorn

    tls = {}
    if args.tls_cert:
        tls = {"ssl_certfile": args.tls_cert, "ssl_keyfile": args.tls_key}
    uvicorn.run(app, host=args.host, port=args.port, **tls)


def listen_problems(
    host: str, tls_cert: str | None, tls_key: str | None, insecure_http: bool
) -> list[str]:
    """Why ``mcp-http`` must not listen on ``host`` with these TLS options; empty when it may."""
    problems = []
    if bool(tls_cert) != bool(tls_key):
        problems.append("--tls-cert and --tls-key go together")
    for option, path in (("--tls-cert", tls_cert), ("--tls-key", tls_key)):
        if path and not os.path.isfile(path):
            problems.append(f"{option} {path}: no such file")
    loopback = host.strip("[]").lower() in LOOPBACK_ADDRESSES
    if not loopback and not tls_cert and not insecure_http:
        problems.append(
            f"--host {host} is reachable from other machines: give --tls-cert and --tls-key, "
            "or --insecure-http for plain HTTP (only on a trusted network or behind a TLS proxy "
            "on another machine)"
        )
    return problems


if __name__ == "__main__":
    main()
