"""Small helpers shared by the test modules (fixtures live in conftest.py)."""

from __future__ import annotations

from typing import Any

from rootmind.config import AppConfig
from rootmind.database.repository import DocumentRepository

# Obviously fake, low-entropy key; only its masked form ("…abcd") may leave the API.
TEST_API_KEY = "sk-test-not-a-real-key-abcd"  # pragma: allowlist secret
TEST_TOKEN = "test-token"  # pragma: allowlist secret
OTHER_TEST_KEY = "sk-test-openai-fake-1111"  # pragma: allowlist secret
SDK_TEST_KEY = "unused-fake-key"  # pragma: allowlist secret

TINY_INCLUDE = ("*.md", "*.txt", "*.csv", "*.docx", "*.xlsx", "*.pptx", "*.pdf")

# The REST API refuses non-loopback peers; TestClient's default peer is "testclient".
LOCAL_PEER = ("127.0.0.1", 50000)
REST_BASE_URL = "http://127.0.0.1:8000"


def rest_client(app, token: str | None = None):
    """A TestClient for the REST app that connects like the local desktop app.

    With ``token``, every request carries ``Authorization: Bearer <token>``.
    """
    from fastapi.testclient import TestClient

    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    return TestClient(app, base_url=REST_BASE_URL, client=LOCAL_PEER, headers=headers)


def capture_serving(monkeypatch) -> dict[str, Any]:
    """Make ``rootmind.main`` record what it would serve instead of binding a socket.

    After ``main(["api", ...])`` the returned dict holds ``bind`` (host, port)
    and ``app`` (the FastAPI app).
    """
    from rootmind import main as main_module

    seen: dict[str, Any] = {}
    sock = object()

    def fake_bind(host: str, port: int):
        seen["bind"] = (host, port)
        return sock

    def fake_serve(app, bound):
        assert bound is sock
        seen["app"] = app

    monkeypatch.setattr(main_module, "bind_socket", fake_bind)
    monkeypatch.setattr(main_module, "serve", fake_serve)
    return seen


def index_config(cfg: AppConfig, full: bool = False) -> DocumentRepository:
    """Index every connector in ``cfg`` with the real indexer; return the repository."""
    from rootmind.api.app import create_connectors
    from rootmind.indexer import Indexer, SyncManager

    repo = DocumentRepository(cfg.database.path)
    connectors = create_connectors(cfg)
    manager = SyncManager(repo, connectors, cfg, Indexer(repo, connectors, cfg))
    sync = manager.full_sync if full else manager.incremental_sync
    sync()
    return repo


def sync_app_state() -> dict:
    """Run an incremental sync of the REST app's current state (inside a TestClient session)."""
    from rootmind.api.app import app_state

    return app_state.sync_manager.incremental_sync()


def doc_id(repo: DocumentRepository, file_name: str) -> str:
    """The id of the indexed document called ``file_name``."""
    for doc in repo.list_documents(limit=1000):
        if doc["file_name"] == file_name:
            return doc["doc_id"]
    raise AssertionError(f"{file_name} is not indexed")


def make_engine(cfg: AppConfig, repo: DocumentRepository, client: Any):
    """A KnowledgeBaseEngine wired to a fake LLM client."""
    from rootmind.rlm_engine.engine import KnowledgeBaseEngine

    return KnowledgeBaseEngine(repo, cfg, client=client)
