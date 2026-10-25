"""REST API routes."""

import logging
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from .. import __version__
from ..access import UNRESTRICTED, FolderAccess, FolderNotAllowed
from ..config import (
    DEFAULT_MCP_TOKEN_NAME,
    DEFAULT_MODEL,
    MCP_TOKEN_NAME,
    OLLAMA_DEFAULT_BASE_URL,
    LlmProvider,
    active_config_path,
    app_home,
    check_api_key,
    check_base_url,
    get_config,
    is_masked,
    load_config,
    mask_secret,
    mcp_network_problems,
    save_config,
    save_mcp_tokens,
)
from ..remote_access import (
    RemoteAccessFiles,
    check_authtoken,
    effective_status,
    normalize_domain,
    redact,
    sdk_info,
)
from ..rlm_engine.engine import (
    LLMConfigError,
    LLMProviderError,
    LLMTimeoutError,
    provider_error,
)

logger = logging.getLogger(__name__)

# How many trailing conversation turns /query's optional `history` uses for
# the condense step. Older turns are dropped server-side regardless of what
# the client sends, keeping the condense call's token cost bounded.
MAX_HISTORY_TURNS = 4

# Request size limits. The engine also truncates each history turn before the
# condense step; these bounds only reject requests that are clearly too large.
MAX_QUESTION_CHARS = 10_000
MAX_HISTORY_TEXT_CHARS = 100_000
MAX_HISTORY_ITEMS = 50

# Timeout for listing the provider's models (the desktop gives up after 15 s).
MODELS_TIMEOUT_SECONDS = 15.0

Question = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]


class HistoryTurn(BaseModel):
    """One prior turn of a chat conversation, for multi-turn /query context."""

    role: Literal["user", "assistant"]
    text: str = Field(..., max_length=MAX_HISTORY_TEXT_CHARS, description="The message text")


class QueryRequest(BaseModel):
    """Request body for knowledge base queries."""

    question: Question = Field(..., description="The question to ask")
    history: list[HistoryTurn] | None = Field(
        None,
        max_length=MAX_HISTORY_ITEMS,
        description=(
            "Optional prior conversation turns, for follow-up questions like "
            "'and 2016?'. Omit for stateless questions. Only the last few turns "
            "are used server-side."
        ),
    )


class TitleRequest(BaseModel):
    """Request body for generating a short conversation title."""

    question: Question = Field(..., description="The first user message")
    answer: str | None = Field(
        None, max_length=MAX_HISTORY_TEXT_CHARS, description="The first answer (optional)"
    )


class Source(BaseModel):
    """A document an answer is based on."""

    id: str = Field(..., description="Document id (for GET /documents/{id})")
    file_name: str
    file_path: str = Field(..., description="Path relative to the indexed folder")
    page: str | None = Field(
        None, description="Location, e.g. 'Page 2', 'Slide 3' or 'Sheet: Costs', when known"
    )


class ToolCall(BaseModel):
    """One tool call the model made, in order."""

    tool: str
    input: dict[str, Any] | str = Field(
        ..., description="Arguments as sent; a string when they were not valid JSON"
    )


class QueryResponse(BaseModel):
    """Response for knowledge base queries."""

    answer: str
    sources: list[Source]
    tokens_used: int = 0
    tool_calls: list[ToolCall] = []
    stop_reasons: list[str] = Field(
        default_factory=list,
        description="Why each LLM call stopped, in order (e.g. tool_use, end_turn, max_tokens)",
    )


class SearchRequest(BaseModel):
    """Request body for document search."""

    query: str = Field(..., description="Search query")
    file_type: str | None = Field(None, description="Filter by file type")
    limit: int = Field(20, ge=1, le=1000, description="Maximum results")
    with_snippets: bool = Field(
        False, description="Include content snippets (slower; scans document text)"
    )


class SyncRequest(BaseModel):
    """Request body for sync operations."""

    full: bool = Field(False, description="Perform full sync instead of incremental")
    connector_name: str | None = Field(None, description="Sync specific connector only")


class StatusResponse(BaseModel):
    """System status response."""

    is_running: bool
    total_documents: int
    indexed_documents: int
    error_documents: int
    connectors: dict[str, Any]


class LlmConfigBody(BaseModel):
    """Editable LLM settings.

    ``GET /config`` returns API keys masked (see ``config.mask_secret``).
    Sending a masked value back keeps the stored key; an empty value removes it.
    """

    provider: LlmProvider = Field("anthropic", description="anthropic | openai | xai | ollama")
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    api_keys: dict[str, str] = Field(default_factory=dict)
    base_url: str = OLLAMA_DEFAULT_BASE_URL


class ConnectorBody(BaseModel):
    """Editable connector (indexed folder) settings."""

    name: str
    type: str = "local"
    path: str | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    # Minutes between re-scans of this folder (network shares); 0 removes the
    # setting, a missing value keeps the stored one.
    rescan_minutes: int | None = Field(None, ge=0, le=7 * 24 * 60)


class IndexerBody(BaseModel):
    """Editable indexer settings (include/exclude are global file patterns)."""

    sync_schedule: str | None = None
    sync_interval_hours: int = 24
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class ConfigBody(BaseModel):
    """Editable subset of the application config (for GET/POST /config)."""

    llm: LlmConfigBody
    connectors: list[ConnectorBody] = Field(default_factory=list)
    indexer: IndexerBody = Field(default_factory=IndexerBody)


class RemoteAccessUpdate(BaseModel):
    """Changes to remote access (``POST /remote-access``); omitted fields stay as they are.

    ``authtoken`` is write-only: a value saves it, ``""`` or null removes it
    (which also turns remote access off). ``domain``: ``""`` or null removes it.
    The values are checked in the route, so an invalid authtoken is never
    echoed in a validation error.
    """

    enabled: bool | None = None
    domain: str | None = None
    authtoken: str | None = None


class McpTokenCreate(BaseModel):
    """A new named MCP token (``POST /mcp/tokens``)."""

    name: str = Field(..., description="1-64 letters, digits, '.', '_' or '-'; not 'default'")
    folders: list[str] | None = Field(
        None, description="Folder names the token may see; omitted or null: every folder"
    )


class LlmModelsRequest(BaseModel):
    """Optional overrides for listing models (otherwise config values are used)."""

    provider: LlmProvider | None = None
    api_key: str | None = None
    base_url: str | None = None


def _list_chat_models(provider: str, api_key: str | None, base_url: str | None = None) -> list[str]:
    """Return available chat model IDs for a provider (best-effort, filtered).

    The client is closed after the call, and the timeout matches the desktop's,
    so a hung provider does not hold a worker thread for the SDK default of
    10 minutes.
    """
    options = {"api_key": api_key, "timeout": MODELS_TIMEOUT_SECONDS, "max_retries": 1}
    if provider == "anthropic":
        import anthropic

        with anthropic.Anthropic(**options) as client:
            return [m.id for m in client.models.list(limit=100).data if m.id.startswith("claude")]

    if provider == "openai":
        import openai

        skip = ("instruct", "audio", "realtime", "transcribe", "tts", "search", "embedding")
        with openai.OpenAI(**options) as client:
            models = [
                m.id
                for m in client.models.list().data
                if m.id.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))
                and not any(s in m.id for s in skip)
            ]
        return sorted(set(models))

    if provider == "xai":
        import openai

        from ..rlm_engine.engine import XAI_BASE_URL

        with openai.OpenAI(**options, base_url=XAI_BASE_URL) as client:
            models = [m.id for m in client.models.list().data if m.id.startswith("grok")]
        return sorted(set(models))

    if provider == "ollama":
        import openai

        base = (base_url or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
        # No key; the SDK only insists on a non-empty one.
        placeholder = "ollama"  # pragma: allowlist secret
        with openai.OpenAI(**{**options, "api_key": placeholder}, base_url=f"{base}/v1") as client:
            return sorted({m.id for m in client.models.list().data})

    raise ValueError(f"Unknown provider: {provider}")


def _refused_folder(body: ConfigBody, access: FolderAccess) -> str | None:
    """Why the folder access policy refuses a new folder in ``body``, or None.

    Only folders that are not configured yet are checked: a new folder that
    the policy refuses or that cannot be opened (missing, an unplugged drive)
    is an error. A folder that is already configured stays, even when it is
    unavailable or refused (for example a network folder carried over when
    the background service was turned on), so other settings can still be
    saved. It is listed as unavailable by ``GET /connectors``.
    """
    if access is UNRESTRICTED or "connectors" not in body.model_fields_set:
        return None
    configured = {os.path.normcase(c.path) for c in get_config().connectors if c.path}
    for connector in body.connectors:
        if connector.type != "local" or not connector.path:
            continue
        if os.path.normcase(connector.path) in configured:
            continue
        try:
            access.check_root(connector.path)
        except FolderNotAllowed as e:
            return f"Cannot use {connector.path}: {e}"
        except OSError as e:
            reason = "folder not found" if isinstance(e, FileNotFoundError) else e.strerror
            return f"Cannot use {connector.path}: {reason or e}"
    return None


def _llm_http_error(e: Exception, action: str) -> HTTPException:
    """Map an LLM failure to a status code and a short message without provider internals."""
    if isinstance(e, LLMTimeoutError):
        return HTTPException(status_code=504, detail="LLM request timed out")
    if isinstance(e, LLMConfigError):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, LLMProviderError):
        return HTTPException(status_code=502, detail=str(e))
    logger.exception("%s failed", action)
    return HTTPException(status_code=500, detail=f"{action} failed; see the backend log")


def create_router(app_state: Any) -> APIRouter:
    """Create API router with all endpoints.

    Args:
        app_state: Application state container.

    Returns:
        Configured APIRouter.
    """
    router = APIRouter()

    # GET and HEAD on / and /health answer without a token (see api.app).
    @router.get("/", tags=["Health"])
    @router.head("/", include_in_schema=False)
    async def root():
        """Root endpoint: service name, version and whether requests need a bearer token."""
        return {
            "name": "RootMind API",
            "version": __version__,
            "status": "running",
            "features": ["FTS5 full-text search", "agentic tool-use query"],
            "auth": "bearer" if getattr(app_state, "auth_enabled", False) else "none",
        }

    @router.get("/health", tags=["Health"])
    @router.head("/health", include_in_schema=False)
    async def health():
        """Health check endpoint."""
        return {"status": "healthy"}

    @router.get("/status", response_model=StatusResponse, tags=["Status"])
    def get_status():
        """Get system status including sync status and document counts."""
        return app_state.sync_manager.get_status()

    # Document endpoints

    @router.get("/documents", tags=["Documents"])
    def list_documents(
        connector_name: str | None = Query(None, description="Filter by connector"),
        file_type: str | None = Query(None, description="Filter by file type"),
        search_filename: str | None = Query(None, description="Search in file names"),
        limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
        offset: int = Query(0, ge=0, description="Offset for pagination"),
    ):
        """List indexed documents with optional filters."""
        docs = app_state.db.list_documents(
            connector_name=connector_name,
            file_type=file_type,
            search_filename=search_filename,
            limit=limit,
            offset=offset,
        )

        return {
            "documents": docs,
            "count": len(docs),
            "limit": limit,
            "offset": offset,
        }

    @router.get("/documents/{doc_id}", tags=["Documents"])
    def get_document(doc_id: str):
        """Get document details by ID."""
        doc = app_state.db.get_document(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc.to_dict()

    @router.get("/documents/{doc_id}/content", tags=["Documents"])
    def get_document_content(
        doc_id: str,
        start: int | None = Query(None, ge=0, description="Start character position"),
        end: int | None = Query(None, ge=0, description="End character position (exclusive)"),
    ):
        """Get text content of a document (optionally a range)."""
        result = app_state.db.get_document_content(doc_id, start, end)
        if not result:
            raise HTTPException(status_code=404, detail="Document not found")
        return result

    @router.get("/documents/{doc_id}/path", tags=["Documents"])
    def get_document_path(doc_id: str):
        """Resolve a document's absolute path on disk, so the local app can open it.

        The path is resolved through the owning connector, which enforces that it
        stays within the connector's configured root directory.
        """
        doc = app_state.db.get_document_meta(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        connector = app_state.connectors.get(doc["connector_name"])
        if connector is None or not hasattr(connector, "resolve_path"):
            raise HTTPException(status_code=400, detail="Connector not available for this document")
        try:
            abs_path = connector.resolve_path(doc["file_path"])
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not resolve path: {e}") from e
        return {"doc_id": doc_id, "abs_path": str(abs_path)}

    @router.get("/statistics", tags=["Documents"])
    def get_statistics():
        """Get database statistics."""
        return app_state.db.get_statistics()

    # Search endpoints (FTS5)

    @router.post("/search", tags=["Search"])
    def search_documents(request: SearchRequest):
        """Full-text search across all documents using FTS5."""
        results = app_state.db.search_fulltext(
            query=request.query,
            limit=request.limit,
            file_type=request.file_type,
            with_snippets=request.with_snippets,
        )

        return {
            "query": request.query,
            "results": results,
            "count": len(results),
        }

    @router.get("/search", tags=["Search"])
    def search_documents_get(
        q: str = Query(..., description="Search query"),
        file_type: str | None = Query(None, description="Filter by file type"),
        limit: int = Query(20, ge=1, le=100, description="Maximum results"),
        snippets: bool = Query(
            False, description="Include snippets, hit positions and pages (slower)"
        ),
    ):
        """Full-text search (GET variant)."""
        results = app_state.db.search_fulltext(
            query=q,
            limit=limit,
            file_type=file_type,
            with_snippets=snippets,
        )

        return {
            "query": q,
            "results": results,
            "count": len(results),
        }

    # Index/Sync endpoints

    @router.post("/index/refresh", tags=["Index"])
    def refresh_index(request: SyncRequest | None = None):
        """Start an index sync in the background; 409 while one is running.

        Use full=true for complete re-indexing.
        """
        if request is None:
            request = SyncRequest()
        manager = app_state.sync_manager
        if request.connector_name and request.connector_name not in manager.connectors:
            raise HTTPException(status_code=404, detail="Connector not found")

        # The sync does blocking file and database work in its own thread, so
        # /health and /status stay responsive meanwhile. Claiming the running
        # flag is atomic: of two simultaneous requests, one gets 409.
        if not manager.start_background(full=request.full, connector_name=request.connector_name):
            raise HTTPException(status_code=409, detail="Sync already in progress")

        kind = "full_sync_started" if request.full else "incremental_sync_started"
        return {"status": kind, "connector": request.connector_name or "all"}

    @router.get("/index/status", tags=["Index"])
    def get_index_status():
        """Get current index/sync status."""
        return app_state.sync_manager.get_status()

    @router.get("/index/progress", tags=["Index"])
    async def get_index_progress():
        """Get current indexing progress (when running)."""
        return app_state.sync_manager.progress

    @router.get("/index/recent", tags=["Index"])
    def get_recent_indexed(
        limit: int = Query(20, ge=1, le=100, description="Max documents to return"),
    ):
        """List the most recently indexed documents (newest first)."""
        return {"documents": app_state.db.recent_documents(limit)}

    # Connector endpoints

    @router.get("/connectors", tags=["Connectors"])
    def list_connectors():
        """List all configured connectors.

        Folders that could not be used (refused by the folder access policy,
        missing, not a folder) are listed after the others with
        ``"available": false`` and the reason in ``error``.
        """
        connectors = [c.status() for c in app_state.connectors.values()]
        errors = getattr(app_state, "connector_errors", None) or {}
        if errors:
            paths = {c.name: c.path for c in app_state.config.connectors}
            for name, reason in errors.items():
                if name in app_state.connectors:
                    continue
                connectors.append(
                    {
                        "name": name,
                        "type": "LocalConnector",
                        "root_path": paths.get(name),
                        "exists": False,
                        "available": False,
                        "error": reason,
                    }
                )
        return {"connectors": connectors}

    @router.get("/connectors/{connector_name}", tags=["Connectors"])
    def get_connector(connector_name: str):
        """Get connector details."""
        connector = app_state.connectors.get(connector_name)
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        return connector.status()

    # Query endpoint (agentic tool loop)

    @router.post("/query", response_model=QueryResponse, tags=["Query"])
    async def query_knowledge_base(request: QueryRequest):
        """Answer a question about the indexed documents.

        The agent loop:
        1. searches the documents (FTS5 full-text, fuzzy as fallback)
        2. reads relevant document sections
        3. answers with sources (file name plus page/slide/sheet when known)
        """
        # Cap to the last few turns and drop empty ones server-side, so a
        # client can pass its whole transcript without feeding an
        # ever-growing (and mostly irrelevant) history into the condense step.
        history = None
        if request.history:
            trimmed = [t for t in request.history if t.text and t.text.strip()]
            history = [
                {"role": t.role, "text": t.text} for t in trimmed[-MAX_HISTORY_TURNS:]
            ] or None

        try:
            result = await app_state.rlm_engine.query(request.question, history=history)
        except Exception as e:
            raise _llm_http_error(e, "Query") from e

        return QueryResponse(
            answer=result["answer"],
            sources=result["sources"],
            tokens_used=result["tokens_used"],
            tool_calls=result.get("tool_calls", []),
            stop_reasons=result.get("stop_reasons", []),
        )

    @router.post("/chat/title", tags=["Chat"])
    def generate_chat_title(request: TitleRequest):
        """Generate a short conversation title from the first exchange (uses the LLM)."""
        try:
            title = app_state.rlm_engine.generate_title(request.question, request.answer)
        except Exception as e:
            raise _llm_http_error(e, "Title generation") from e
        return {"title": title}

    # Config endpoints (editable subset, applied live)

    @router.get("/config", response_model=ConfigBody, tags=["Config"])
    async def get_config_endpoint():
        """Return the editable subset of the current configuration.

        API keys are masked: only the last 4 characters are shown.
        """
        cfg = get_config()
        return ConfigBody(
            llm=LlmConfigBody(
                provider=cfg.llm.provider,
                model=cfg.llm.model,
                api_key=mask_secret(cfg.llm.api_key) or None,
                api_keys={k: mask_secret(v) for k, v in cfg.llm.api_keys.items() if v},
                base_url=cfg.llm.base_url,
            ),
            connectors=[
                ConnectorBody(
                    name=c.name,
                    type=c.type,
                    path=c.path,
                    include=c.include,
                    exclude=c.exclude,
                    rescan_minutes=c.rescan_minutes,
                )
                for c in cfg.connectors
            ],
            indexer=IndexerBody(
                sync_schedule=cfg.indexer.sync_schedule,
                sync_interval_hours=cfg.indexer.sync_interval_hours,
                include=cfg.indexer.include,
                exclude=cfg.indexer.exclude,
            ),
        )

    @router.post("/config", tags=["Config"])
    def update_config_endpoint(body: ConfigBody):
        """Persist the editable config to config.yaml and apply it live.

        Validates and writes the file (with a .bak backup), then reloads the
        config-dependent state in-process so changes take effect immediately
        (no restart). If the live reload fails, the change is still saved and a
        restart would apply it. Masked API keys keep the stored key.

        Fields the client omits keep their stored values. (Filling them with
        the model defaults would turn a partial update into "remove every
        folder and every stored API key".)

        Saves are serialized, so two clients saving at the same time cannot
        interleave their read-modify-write of the file or the reload.

        Under a folder access policy (the Windows background service), every
        folder is checked first; a refused folder answers 400 with the reason
        and nothing is saved.
        """
        from .app import config_update_lock, reload_app_state

        with config_update_lock:
            refused = _refused_folder(body, getattr(app_state, "folder_access", UNRESTRICTED))
            if refused is not None:
                raise HTTPException(status_code=400, detail=refused)
            try:
                save_config(body.model_dump(exclude_unset=True), active_config_path())
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid config: {e}") from e

            try:
                reload_app_state()
            except Exception as e:
                logger.exception("Live config reload failed")
                return {"ok": True, "reloaded": False, "restart_required": True, "detail": str(e)}

        return {"ok": True, "reloaded": True, "restart_required": False}

    @router.post("/llm/models", tags=["Config"])
    def list_llm_models(body: LlmModelsRequest | None = None):
        """List available chat models for a provider.

        Uses the provider and API key from the request if given, otherwise
        falls back to the saved config. A missing or masked key resolves to the
        stored key of that provider. Lets the UI populate a model dropdown.
        Provider errors return 502, timeouts 504.
        """
        cfg = get_config()
        provider = (body.provider if body and body.provider else None) or cfg.llm.provider
        api_key = body.api_key if body and body.api_key else None
        try:
            check_api_key(api_key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not api_key or is_masked(api_key):
            api_key = cfg.llm.api_keys.get(provider)
        base_url = body.base_url if body and body.base_url else cfg.llm.base_url
        if provider == "ollama":
            try:
                check_base_url(base_url)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        elif not api_key:
            raise HTTPException(status_code=400, detail=f"No API key for provider {provider}")
        try:
            models = _list_chat_models(provider, api_key, base_url)
        except Exception as e:
            raise _llm_http_error(provider_error(e, provider), "Listing models") from e
        return {"provider": provider, "models": models}

    # Named MCP over HTTP tokens (stored as SHA-256 in config.yaml, section mcp_http)

    def mcp_token_list(cfg: Any) -> list[dict[str, Any]]:
        from ..auth import token_fingerprint
        from ..mcp_http import TOKEN_FILE_NAME

        path = app_home() / "data" / TOKEN_FILE_NAME
        fingerprint = None
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            value = ""
        if value:
            fingerprint = token_fingerprint(value)
        tokens: list[dict[str, Any]] = [
            {
                "name": DEFAULT_MCP_TOKEN_NAME,
                "kind": "file",
                "file": str(path),
                "folders": None,
                "fingerprint": fingerprint,
                "created": None,
            }
        ]
        for t in cfg.mcp_http.tokens:
            tokens.append(
                {
                    "name": t.name,
                    "kind": "named",
                    "file": None,
                    "folders": t.folders,
                    # The same fingerprint the MCP server logs: sha256(token)[:8].
                    "fingerprint": t.sha256[:8],
                    "created": t.created,
                }
            )
        return tokens

    def mcp_network_state(cfg: Any) -> dict[str, Any]:
        network = cfg.mcp_http.network
        return {
            "enabled": bool(network.bind),
            "bind": network.bind,
            "port": network.port,
            "tls": bool(network.tls_cert and network.tls_key),
            "insecure_http": network.insecure_http,
            "allow_default_token": network.allow_default_token,
            "allowed_hosts": list(cfg.mcp_http.allowed_hosts),
            "problems": mcp_network_problems(cfg.mcp_http),
        }

    @router.get("/mcp/tokens", tags=["MCP"])
    def list_mcp_tokens():
        """The MCP over HTTP tokens (never the tokens themselves) and the network listener.

        ``default`` is the token in ``data/api_token.txt`` (every folder; the
        app and the Claude Desktop bundle use it). Named tokens are stored as
        SHA-256 only; ``fingerprint`` is the first 8 hex characters, as the MCP
        server logs it.
        """
        cfg = load_config(active_config_path())
        return {"tokens": mcp_token_list(cfg), "network": mcp_network_state(cfg)}

    @router.post("/mcp/tokens", tags=["MCP"])
    def create_mcp_token(body: McpTokenCreate):
        """Create a named MCP token; the answer holds the token, which is not stored anywhere.

        Only its SHA-256 goes to ``config.yaml``. The MCP server accepts it with
        the next request. ``folders`` limits what the token sees; it must name
        configured folders.
        """
        import secrets
        from datetime import UTC, datetime

        from ..mcp_http import token_sha256
        from .app import config_update_lock

        name = body.name.strip()
        if not MCP_TOKEN_NAME.match(name) or name.lower() == DEFAULT_MCP_TOKEN_NAME:
            raise HTTPException(
                status_code=400,
                detail="A token name has 1-64 letters, digits, '.', '_' or '-' and is not "
                "'default'",
            )
        with config_update_lock:
            cfg = load_config(active_config_path())
            if any(t.name.lower() == name.lower() for t in cfg.mcp_http.tokens):
                raise HTTPException(status_code=409, detail=f"A token named {name} exists")
            folders = None
            if body.folders is not None:
                configured = {c.name for c in cfg.connectors}
                folders = list(dict.fromkeys(f.strip() for f in body.folders if f.strip()))
                if not folders:
                    raise HTTPException(
                        status_code=400,
                        detail="Name at least one folder, or leave folders out for every folder",
                    )
                unknown = [f for f in folders if f not in configured]
                if unknown:
                    raise HTTPException(
                        status_code=400, detail=f"No folder named {', '.join(unknown)}"
                    )
            token = secrets.token_urlsafe(32)
            entry: dict[str, Any] = {"name": name, "sha256": token_sha256(token)}
            if folders is not None:
                entry["folders"] = folders
            entry["created"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            existing = [t.model_dump(mode="json", exclude_none=True) for t in cfg.mcp_http.tokens]
            try:
                saved = save_mcp_tokens([*existing, entry], active_config_path())
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid config: {e}") from None
            get_config().mcp_http = saved.mcp_http
        logger.info(
            "MCP token %s created (fingerprint sha256:%s, %s)",
            name,
            entry["sha256"][:8],
            f"folders {', '.join(folders)}" if folders else "every folder",
        )
        return JSONResponse(
            {
                "name": name,
                "token": token,
                "folders": folders,
                "fingerprint": entry["sha256"][:8],
                "created": entry["created"],
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.delete("/mcp/tokens/{name}", tags=["MCP"])
    def revoke_mcp_token(name: str):
        """Revoke a named MCP token; the MCP server refuses it from its next request on."""
        from .app import config_update_lock

        if name.lower() == DEFAULT_MCP_TOKEN_NAME:
            raise HTTPException(
                status_code=400,
                detail="The default token (data/api_token.txt) cannot be revoked here: the app "
                "and the Claude Desktop bundle use it. To replace it, delete the file and run "
                "service install again.",
            )
        with config_update_lock:
            cfg = load_config(active_config_path())
            # Names are unique regardless of case (see create_mcp_token).
            kept = [t for t in cfg.mcp_http.tokens if t.name.lower() != name.lower()]
            if len(kept) == len(cfg.mcp_http.tokens):
                raise HTTPException(status_code=404, detail=f"No token named {name}")
            saved = save_mcp_tokens(
                [t.model_dump(mode="json", exclude_none=True) for t in kept],
                active_config_path(),
            )
            get_config().mcp_http = saved.mcp_http
        logger.info("MCP token %s revoked", name)
        return {"revoked": name}

    # Remote access (an ngrok tunnel run by the MCP over HTTP service)

    def remote_access_files() -> RemoteAccessFiles:
        files = getattr(app_state, "remote_access_files", None)
        return files if files is not None else RemoteAccessFiles(app_home())

    def remote_access_state(files: RemoteAccessFiles) -> dict[str, Any]:
        settings = load_config(active_config_path()).remote_access
        available, sdk_version = sdk_info()
        return {
            "enabled": settings.enabled,
            "domain": settings.domain,
            "authtoken_saved": files.authtoken_saved(),
            "sdk": {"available": available, "version": sdk_version},
            "status": effective_status(settings.enabled, files.read_status()),
        }

    @router.get("/remote-access", tags=["Remote access"])
    def get_remote_access():
        """Remote access settings and the tunnel's status.

        ``status.state``: ``off``, ``connecting``, ``connected`` (with ``url``),
        ``error`` (with ``error``) or ``unavailable`` (the MCP over HTTP
        service does not report: stopped, turned off or not updated). The
        ngrok authtoken is never returned, only whether one is saved.
        """
        return remote_access_state(remote_access_files())

    @router.post("/remote-access", tags=["Remote access"])
    def update_remote_access(body: RemoteAccessUpdate):
        """Change remote access: turn it on or off, save or remove the authtoken, set the domain.

        The authtoken goes to its own file in ``data`` (in the RootMind
        service readable only by SYSTEM, Administrators and the MCP service
        account); ``enabled`` and ``domain`` go to ``config.yaml``, which the
        MCP service checks every few seconds. Turning it on needs a saved
        authtoken (400 otherwise).
        """
        from .app import config_update_lock

        fields = body.model_fields_set
        files = remote_access_files()
        with config_update_lock:
            current = load_config(active_config_path()).remote_access
            enabled = current.enabled
            domain = current.domain
            new_token: str | None = None
            clear_token = False
            try:
                if "domain" in fields:
                    domain = normalize_domain(body.domain)
                if "authtoken" in fields:
                    if body.authtoken and body.authtoken.strip():
                        new_token = check_authtoken(body.authtoken)
                    else:
                        clear_token = True
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            if "enabled" in fields and body.enabled is not None:
                enabled = body.enabled
            if clear_token:
                if "enabled" in fields and body.enabled:
                    raise HTTPException(
                        status_code=400,
                        detail="Remote access needs an ngrok authtoken; it cannot be removed "
                        "while turning remote access on",
                    )
                enabled = False
            token_saved = new_token is not None or (not clear_token and files.authtoken_saved())
            if enabled and not token_saved:
                raise HTTPException(
                    status_code=400,
                    detail="Save an ngrok authtoken before turning remote access on",
                )

            changes = []
            try:
                # The authtoken first, so the MCP service finds it when it
                # sees remote access turned on.
                if new_token is not None:
                    files.save_authtoken(new_token)
                    changes.append("authtoken saved")
                if enabled:
                    files.prepare_status()
                saved = save_config(
                    {"remote_access": {"enabled": enabled, "domain": domain}},
                    active_config_path(),
                )
                if clear_token:
                    files.clear_authtoken()
                    changes.append("authtoken removed")
            except Exception as e:  # OSError, or pywintypes.error from the permissions
                logger.error(
                    "Saving the remote access settings failed: %s", redact(str(e), new_token)
                )
                raise HTTPException(
                    status_code=500,
                    detail="Saving the remote access settings failed; see the backend log",
                ) from None
            get_config().remote_access = saved.remote_access
        if enabled != current.enabled:
            changes.append("turned on" if enabled else "turned off")
        if domain != current.domain:
            changes.append(f"domain {domain or 'removed'}")
        logger.info("Remote access: %s", ", ".join(changes) or "no change")
        return remote_access_state(files)

    return router
