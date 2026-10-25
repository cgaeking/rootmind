"""A fake RootMind REST API on 127.0.0.1 (port 0) for the client command tests.

It answers the routes the client commands use with canned, in-memory state
and records every request (method, path, query, body, whether the bearer
token matched). Unlike the real API it never indexes, never calls an LLM and
never touches a config file.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

FAKE_REST_TOKEN = "fake-rest-token-for-tests"  # pragma: allowlist secret
STORED_KEY = "sk-fake-stored-anthropic-key-9999"  # pragma: allowlist secret


def _mask(value: str) -> str:
    return "…" + value[-4:] if value else ""


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    body: Any
    authorized: bool
    authorization: str | None


@dataclass
class FakeState:
    token: str | None = FAKE_REST_TOKEN
    llm: dict[str, Any] = field(
        default_factory=lambda: {
            "provider": "anthropic",
            "model": "claude-test",
            "api_keys": {"anthropic": STORED_KEY},
            "base_url": "http://localhost:11434",
        }
    )
    connectors: list[dict[str, Any]] = field(
        default_factory=lambda: [{"name": "docs", "type": "local", "path": "/srv/docs"}]
    )
    running_polls: int = 0  # /index/status reports running this many more times
    refresh_conflicts: int = 0  # /index/refresh answers 409 this many times
    remote: dict[str, Any] = field(
        default_factory=lambda: {"enabled": False, "domain": None, "authtoken": None}
    )
    requests: list[Recorded] = field(default_factory=list)
    redirect_paths: set[str] = field(default_factory=set)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: Any = None, headers: dict[str, str] | None = None):
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method: str) -> None:
        state = self.server.state
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        header = self.headers.get("Authorization")
        authorized = state.token is None or header == f"Bearer {state.token}"
        state.requests.append(
            Recorded(method, parts.path, parse_qs(parts.query), body, authorized, header)
        )
        if parts.path in state.redirect_paths:
            return self._send(307, None, {"Location": "http://127.0.0.1:9/elsewhere"})
        if parts.path not in ("/", "/health") and not authorized:
            return self._send(401, {"detail": "Invalid token"})
        route = getattr(
            self,
            f"_{method.lower()}_{parts.path.strip('/').replace('/', '_').replace('-', '_') or 'root'}",
            None,
        )
        if route is None:
            return self._send(404, {"detail": "Not Found"})
        status, payload = route(state, body, parse_qs(parts.query))
        return self._send(status, payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # --- routes --------------------------------------------------------------------

    def _get_root(self, state, body, query):
        return 200, {"name": "RootMind API", "version": "9.9.9", "auth": "bearer"}

    def _get_statistics(self, state, body, query):
        return 200, {
            "total_documents": 12,
            "indexed_documents": 11,
            "error_documents": 1,
            "total_content_chars": 4321,
        }

    def _index_status(self, state) -> dict[str, Any]:
        running = state.running_polls > 0
        if running:
            state.running_polls -= 1
        return {
            "is_running": running,
            "total_documents": 12,
            "indexed_documents": 11,
            "error_documents": 1,
            "connectors": {
                c["name"]: {
                    "connector_name": c["name"],
                    "documents_indexed": 11,
                    "documents_error": 1,
                    "last_sync_at": "2026-10-24T03:00:00",
                    "error_message": None,
                }
                for c in state.connectors
            },
        }

    def _get_index_status(self, state, body, query):
        return 200, self._index_status(state)

    def _post_index_refresh(self, state, body, query):
        name = (body or {}).get("connector_name")
        if name and name not in {c["name"] for c in state.connectors}:
            return 404, {"detail": "Connector not found"}
        if state.refresh_conflicts > 0:
            state.refresh_conflicts -= 1
            return 409, {"detail": "Sync already in progress"}
        full = bool((body or {}).get("full"))
        state.running_polls = max(state.running_polls, 2)
        kind = "full_sync_started" if full else "incremental_sync_started"
        return 200, {"status": kind, "connector": name or "all"}

    def _get_connectors(self, state, body, query):
        return 200, {
            "connectors": [
                {"name": c["name"], "root_path": c["path"], "exists": True, "available": True}
                for c in state.connectors
            ]
        }

    def _get_config(self, state, body, query):
        llm = dict(state.llm)
        keys = {k: _mask(v) for k, v in llm["api_keys"].items() if v}
        llm["api_keys"] = keys
        llm["api_key"] = keys.get(llm["provider"])
        return 200, {
            "llm": llm,
            "connectors": [dict(c, include=None, exclude=None) for c in state.connectors],
            "indexer": {"sync_schedule": None, "sync_interval_hours": 24},
        }

    def _post_config(self, state, body, query):
        if not isinstance(body, dict) or not isinstance(body.get("llm"), dict):
            return 422, {"detail": [{"loc": ["body", "llm"], "msg": "Field required"}]}
        llm = body["llm"]
        for key in ("provider", "model", "base_url"):
            if key in llm:
                state.llm[key] = llm[key]
        if llm.get("api_keys") is not None:
            merged = {}
            for provider, value in llm["api_keys"].items():
                if not value:
                    continue
                if value.startswith("…"):
                    if state.llm["api_keys"].get(provider):
                        merged[provider] = state.llm["api_keys"][provider]
                    continue
                merged[provider] = value
            state.llm["api_keys"] = merged
        if body.get("connectors") is not None:
            state.connectors = [
                {"name": c["name"], "type": c.get("type", "local"), "path": c.get("path")}
                for c in body["connectors"]
            ]
        return 200, {"ok": True, "reloaded": True, "restart_required": False}

    def _post_search(self, state, body, query):
        hits = [
            {
                "doc_id": "d1",
                "file_name": "invoice.pdf",
                "file_path": "invoices/invoice.pdf",
                "page": "Page 2",
                "snippet": "Total due: 1,179.36" if body.get("with_snippets") else "",
            }
        ]
        return 200, {"query": body["query"], "results": hits[: body["limit"]], "count": 1}

    def _post_query(self, state, body, query):
        return 200, {
            "answer": f"The answer to: {body['question']}",
            "sources": [
                {
                    "id": "d1",
                    "file_name": "invoice.pdf",
                    "file_path": "x/invoice.pdf",
                    "page": "Page 2",
                }
            ],
            "tokens_used": 42,
            "tool_calls": [],
            "stop_reasons": ["end_turn"],
        }

    def _remote_state(self, state) -> dict[str, Any]:
        remote = state.remote
        return {
            "enabled": remote["enabled"],
            "domain": remote["domain"],
            "authtoken_saved": bool(remote["authtoken"]),
            "sdk": {"available": True, "version": "1.7.0"},
            "status": {
                "state": "connecting" if remote["enabled"] else "off",
                "url": None,
                "error": None,
            },
        }

    def _get_remote_access(self, state, body, query):
        return 200, self._remote_state(state)

    def _post_remote_access(self, state, body, query):
        remote = state.remote
        if "authtoken" in body:
            remote["authtoken"] = body["authtoken"] or None
        if "domain" in body:
            remote["domain"] = body["domain"]
        if "enabled" in body:
            if body["enabled"] and not remote["authtoken"]:
                return 400, {"detail": "Save an ngrok authtoken before turning remote access on"}
            remote["enabled"] = body["enabled"]
        return 200, self._remote_state(state)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: FakeState) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state


class FakeRestServer:
    """``with FakeRestServer() as fake:`` serves on ``fake.url`` until the block ends."""

    def __init__(self, state: FakeState | None = None) -> None:
        self.state = state or FakeState()
        self._server = _Server(self.state)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list[Recorded]:
        return self.state.requests

    def __enter__(self) -> FakeRestServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
