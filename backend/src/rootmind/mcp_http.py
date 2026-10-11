"""MCP server over HTTP, protected by a bearer token.

For remote or server-side MCP clients (e.g. n8n's MCP Client Tool). JSON-RPC
is handled in-process on top of FastAPI and serves the same read-only tools as
the agent loop (see :mod:`rootmind.tools`).

Endpoints:

- ``POST /mcp``          JSON-RPC over plain HTTP (streamable-HTTP style, JSON responses)
- ``GET  /mcp/sse``      legacy SSE transport: event stream
- ``POST /mcp/message``  legacy SSE transport: client-to-server messages
- ``GET  /health``       liveness check, no auth

There are no OpenAPI or docs pages: everything except ``/health`` needs the token.

Like the stdio server, it only serves by default and never indexes. Keep the
index current with the REST API or ``rootmind index``. ``--index`` also runs
the background indexer in this process; use it only when no other process
(such as the REST API) indexes the same database, because each indexing
process removes the documents of folders missing from *its* copy of the config.

The folders whose documents the tools may return are read from the config
at startup. Restart the server after adding or removing folders.

Security model:

- Every MCP endpoint requires ``Authorization: Bearer <token>``. The token
  comes from ``MCP_API_TOKEN`` or ``<RLM_HOME>/data/api_token.txt`` (generated
  on first start, owner-only permissions where the OS supports them). An empty
  token refuses to start (fail closed). Tokens are compared in constant time
  and never logged or printed; logs show only a short SHA-256 fingerprint.
- Requests with an ``Origin`` header from a non-loopback origin are rejected
  (DNS rebinding protection), unless listed in ``MCP_ALLOWED_ORIGINS``.
- Binds to 127.0.0.1 by default. To reach it from elsewhere, put it behind a
  TLS-terminating proxy or tunnel; never expose plain HTTP.

Usage::

    rootmind-mcp-http [--host 127.0.0.1] [--port 3000] [--config config.yaml] [--index]
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
import uuid
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__, tools
from .config import AppConfig, app_home, get_config, init_config
from .database.repository import DocumentRepository

logger = logging.getLogger("rootmind.mcp_http")

SERVER_NAME = "rootmind"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3000
TOKEN_FILE_NAME = "api_token.txt"  # noqa: S105 (a file name, not a secret)

# Handshake-era protocol versions this transport implements, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Compatibility shim: some clients (n8n) send tool arguments flat in `params`
# instead of nested under `params.arguments`. These keys are client metadata,
# not tool arguments, and are dropped when flattening.
_NON_ARGUMENT_PARAMS = {"name", "tool", "toolCallId", "sessionId", "action", "chatInput"}


class TokenError(RuntimeError):
    """The bearer token is missing, empty or unreadable; the server must not start."""


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- token handling --------------------------------------------------------------


def token_file_path() -> Path:
    """Default location of the generated token: ``<RLM_HOME or cwd>/data/api_token.txt``."""
    return app_home() / "data" / TOKEN_FILE_NAME


def token_fingerprint(token: str) -> str:
    """Short, non-reversible identifier for logs (first 8 hex chars of SHA-256)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _write_private_file(path: Path, content: str) -> None:
    """Create ``path`` with owner-only permissions (0600 on POSIX).

    On Windows the mode bits are ignored and the file inherits the ACL of its
    folder; the service scripts restrict that folder to SYSTEM and Administrators.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


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
    env_token = os.environ.get("MCP_API_TOKEN")
    if env_token is not None:
        token = env_token.strip()
        if not token:
            raise TokenError(
                "MCP_API_TOKEN is set but empty; refusing to start without authentication"
            )
        return token, "environment variable MCP_API_TOKEN"

    path = token_file or token_file_path()
    if path.exists():
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise TokenError(f"Cannot read token file {path}: {e}") from e
        if not token:
            raise TokenError(
                f"Token file {path} is empty; delete it to generate a new token, "
                "or set MCP_API_TOKEN"
            )
        return token, f"file {path}"

    token = secrets.token_urlsafe(32)
    _write_private_file(path, token)
    logger.info("Generated a new MCP API token and saved it to %s", path)
    return token, f"file {path} (newly generated)"


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


def create_app(
    token: str,
    config: AppConfig | None = None,
    *,
    allowed_origins: Iterable[str] = (),
    enable_scheduler: bool = False,
) -> FastAPI:
    """Create the MCP HTTP app.

    Args:
        token: Bearer token every MCP request must present. Empty is refused.
        config: Configuration; defaults to the global config at startup.
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

    state = SimpleNamespace(repo=None, allowed=None, sync_manager=None, sse_sessions={})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = config or get_config()
        state.repo = DocumentRepository(cfg.database.path)
        state.allowed = frozenset(c.name for c in cfg.connectors)
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
    ) -> None:
        if not origin_allowed(request.headers.get("origin"), extra_origins):
            raise HTTPException(status_code=403, detail="Origin not allowed")
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not secrets.compare_digest(credentials.credentials.encode("utf-8"), expected):
            raise HTTPException(
                status_code=401,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": _negotiate_version(params),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        if method in ("ping", "initialized", "notifications/initialized"):
            return {}
        if method == "tools/list":
            return {"tools": tools.mcp_tools()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                raise JsonRpcError(-32602, "Missing tool name")
            ctx = tools.ToolContext(allowed_connectors=state.allowed)
            logger.info("Tool call: %s", name)
            # Tool execution is blocking SQLite I/O; keep it off the event loop.
            result = await asyncio.to_thread(
                tools.execute, name, _tool_arguments(params), state.repo, ctx
            )
            return {
                "content": [{"type": "text", "text": result.text}],
                "isError": result.is_error,
            }
        raise JsonRpcError(-32601, f"Method not found: {method}")

    async def handle_message(body: Any) -> dict[str, Any] | None:
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
            result = await dispatch(method, params)
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

    @app.post("/mcp", dependencies=[Depends(authorize)])
    async def mcp_post(request: Request):
        """JSON-RPC endpoint. Notifications get 202 with an empty body."""
        try:
            body = await read_json(request)
        except JsonRpcError as e:
            return JSONResponse(_error(None, e.code, e.message), status_code=400)
        if isinstance(body, list):
            return JSONResponse(
                _error(None, -32600, "Batch requests are not supported"), status_code=400
            )
        response = await handle_message(body)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    @app.get("/mcp/sse", dependencies=[Depends(authorize)])
    async def mcp_sse(request: Request):
        """Legacy SSE transport: announce the message endpoint, then stream responses."""
        session_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        state.sse_sessions[session_id] = queue
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

    @app.post("/mcp/message", dependencies=[Depends(authorize)])
    async def mcp_sse_message(request: Request, sessionId: str):  # noqa: N803 (wire name)
        """Legacy SSE transport: accept a message; the response goes out on the stream."""
        queue = state.sse_sessions.get(sessionId)
        if queue is None:
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            body = await read_json(request)
        except JsonRpcError as e:
            raise HTTPException(status_code=400, detail=e.message) from e
        response = await handle_message(body)
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
            "process (such as the REST API) indexes the same database."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = init_config(args.config)

    try:
        token, source = resolve_token()
        app = create_app(
            token, config, allowed_origins=_env_allowed_origins(), enable_scheduler=args.index
        )
    except TokenError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.host not in _LOOPBACK_HOSTS:
        logger.warning(
            "Binding MCP HTTP to %s. Expose it only behind TLS (reverse proxy or tunnel).",
            args.host,
        )

    print("RootMind MCP HTTP server")
    print(f"  Endpoint: http://{args.host}:{args.port}/mcp")
    print(f"  Health:   http://{args.host}:{args.port}/health")
    print(f"  Auth:     bearer token from {source}")
    print(f"            fingerprint sha256:{token_fingerprint(token)}")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
