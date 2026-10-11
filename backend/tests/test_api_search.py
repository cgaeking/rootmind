"""REST search and document endpoints: limits, paging contracts, special characters, refresh."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from helpers import sync_app_state


def _ids(response) -> list[str]:
    return [r["doc_id"] for r in response.json()["results"]]


def _seed_lantern_docs(db) -> None:
    """30 documents that match a two-word query through different FTS strategies.

    Exact phrases, both words apart, and one word only (repeated) give each
    document a different mix of strategy scores, which is what used to make
    the ranking depend on the requested limit.
    """
    for i in range(10):
        db.upsert_document(
            "docs", f"p{i}.txt", f"p{i}.txt", "txt",
            content_text="alpine lantern " + "filler " * (i * 7),
        )  # fmt: skip
    for i in range(10):
        db.upsert_document(
            "docs", f"a{i}.txt", f"a{i}.txt", "txt",
            content_text="alpine " + "word " * (i * 3) + "lantern " * (1 + i % 4),
        )  # fmt: skip
    for i in range(10):
        db.upsert_document(
            "docs", f"lantern_{i}.txt", f"lantern_{i}.txt", "txt",
            content_text="lantern " * (2 + i) + "tail " * (10 - i),
        )  # fmt: skip


# --- limits ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("limit", "status"), [(0, 422), (1001, 422), (1000, 200), (1, 200)])
def test_post_search_limit_bounds(api, limit, status):
    assert api.post("/search", json={"query": "tent", "limit": limit}).status_code == status


@pytest.mark.parametrize(("limit", "status"), [(0, 422), (101, 422), (100, 200)])
def test_get_search_limit_bounds(api, limit, status):
    assert api.get("/search", params={"q": "tent", "limit": limit}).status_code == status


def test_search_without_query_is_rejected(api):
    assert api.get("/search").status_code == 422
    assert api.post("/search", json={}).status_code == 422


# --- results ---------------------------------------------------------------------------------


def test_search_result_shape(api):
    sync_app_state()

    fast = api.post("/search", json={"query": "Total due"}).json()
    rich = api.post("/search", json={"query": "Total due", "with_snippets": True}).json()

    assert fast["query"] == "Total due"
    assert fast["count"] == len(fast["results"]) >= 1
    top_fast, top_rich = fast["results"][0], rich["results"][0]
    assert top_fast["file_name"] == top_rich["file_name"] == "invoice.pdf"
    assert top_fast["snippet"] == "" and top_fast["page"] is None
    assert "**Total due**" in top_rich["snippet"]
    assert top_rich["page"] == "Page 2"
    assert set(top_rich) >= {
        "doc_id", "file_name", "file_path", "file_type", "snippet", "score",
        "content_length", "page_count", "position", "page",
    }  # fmt: skip

    get = api.get("/search", params={"q": "Total due"}).json()
    assert get["results"][0]["page"] is None
    rich_get = api.get("/search", params={"q": "Total due", "snippets": True}).json()
    assert rich_get["results"][0]["page"] == "Page 2"
    assert "**Total due**" in rich_get["results"][0]["snippet"]
    assert _ids_from(get) == _ids_from(fast)


def _ids_from(body) -> list[str]:
    return [r["doc_id"] for r in body["results"]]


@pytest.mark.parametrize("query", ["foo-bar", "AND", '"unbalanced', "*", "NEAR(", "OR", "(", "-"])
def test_fts_special_chars(api, query):
    sync_app_state()
    for response in (
        api.post("/search", json={"query": query}),
        api.post("/search", json={"query": query, "with_snippets": True}),
        api.get("/search", params={"q": query}),
    ):
        assert response.status_code == 200, response.text


# --- paging contracts ---------------------------------------------------------------------


def test_search_paging_is_prefix_stable(api):
    """The desktop "load more" asks again with a larger limit: the first page must not move."""
    from rootmind.api.app import app_state

    _seed_lantern_docs(app_state.db)

    for with_snippets in (False, True):
        first = api.post(
            "/search", json={"query": "alpine lantern", "limit": 10, "with_snippets": with_snippets}
        )
        more = api.post(
            "/search", json={"query": "alpine lantern", "limit": 20, "with_snippets": with_snippets}
        )
        assert len(_ids(first)) == 10
        assert _ids(more)[:10] == _ids(first)


def test_search_ranking_does_not_depend_on_the_limit(repo):
    _seed_lantern_docs(repo)
    for query in ("alpine lantern", "lantern", "alpine"):
        full = [r["doc_id"] for r in repo.search_fulltext(query, limit=30, with_snippets=False)]
        for limit in range(1, 30):
            page = [r["doc_id"] for r in repo.search_fulltext(query, limit, with_snippets=False)]
            assert page == full[:limit], (query, limit)


def test_documents_pages_are_disjoint(api):
    from rootmind.api.app import app_state

    _seed_lantern_docs(app_state.db)
    pages = [
        api.get("/documents", params={"limit": 7, "offset": offset}).json()
        for offset in range(0, 35, 7)
    ]

    ids = [d["doc_id"] for page in pages for d in page["documents"]]
    assert len(ids) == len(set(ids)) == 30
    assert pages[1]["limit"] == 7 and pages[1]["offset"] == 7
    assert api.get("/documents", params={"offset": 1000}).json()["documents"] == []
    assert api.get("/documents", params={"limit": 0}).status_code == 422
    assert api.get("/documents", params={"offset": -1}).status_code == 422


def test_documents_filters(api):
    sync_app_state()
    pdfs = api.get("/documents", params={"file_type": "pdf"}).json()
    assert [d["file_name"] for d in pdfs["documents"]] == ["invoice.pdf"]
    named = api.get("/documents", params={"search_filename": "hand"}).json()
    assert [d["file_name"] for d in named["documents"]] == ["handbook.txt"]


# --- single documents ---------------------------------------------------------------------


def test_document_content_endpoint(api):
    from rootmind.api.app import app_state

    doc = app_state.db.upsert_document("docs", "a.txt", "a.txt", "txt", content_text="0123456789")

    assert api.get(f"/documents/{doc.id}").json()["file_name"] == "a.txt"
    assert api.get(f"/documents/{doc.id}/content").json()["content"] == "0123456789"
    window = api.get(f"/documents/{doc.id}/content", params={"start": 2, "end": 5}).json()
    assert window["content"] == "234"
    assert window["content_length"] == 10
    assert api.get(f"/documents/{doc.id}/content", params={"start": -1}).status_code == 422
    assert api.get(f"/documents/{doc.id}/content", params={"end": -1}).status_code == 422
    assert api.get("/documents/missing/content").status_code == 404
    assert api.get("/documents/missing").status_code == 404


def test_statistics_and_status(api):
    sync_app_state()
    assert api.get("/statistics").json()["indexed_documents"] == 7
    status = api.get("/status").json()
    assert status["is_running"] is False
    assert status["connectors"]["docs"]["documents_indexed"] == 7
    recent = api.get("/index/recent", params={"limit": 3}).json()["documents"]
    assert len(recent) == 3
    assert api.get("/connectors").json()["connectors"][0]["name"] == "docs"
    assert api.get("/connectors/nope").status_code == 404


# --- refresh ---------------------------------------------------------------------------------


@pytest.fixture
def blocked_sync(api, monkeypatch):
    """Replace the sync body with one that blocks until the test releases it."""
    from rootmind.api.app import app_state

    manager = app_state.sync_manager
    release = threading.Event()
    started = threading.Event()

    def blocked(full, connector_name=None):
        started.set()
        release.wait(10)
        return {"type": "full" if full else "incremental", "results": {}}

    monkeypatch.setattr(manager, "_run_locked", blocked)
    yield manager, started, release
    release.set()
    _wait_until_idle(manager)


def _wait_until_idle(manager, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.is_running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not manager.is_running, "sync flag was never released"


def test_refresh_conflict_409(api, blocked_sync):
    manager, started, release = blocked_sync

    first = api.post("/index/refresh", json={})
    assert first.json() == {"status": "incremental_sync_started", "connector": "all"}
    assert started.wait(5)
    assert api.post("/index/refresh", json={"full": True}).status_code == 409

    release.set()
    _wait_until_idle(manager)
    again = api.post("/index/refresh", json={"full": True, "connector_name": "docs"})
    assert again.json() == {"status": "full_sync_started", "connector": "docs"}


def test_parallel_refresh_requests_start_exactly_one_sync(api, blocked_sync):
    manager, _, release = blocked_sync

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda _: api.post("/index/refresh").status_code, range(6)))

    assert sorted(codes) == [200, 409, 409, 409, 409, 409]
    release.set()
    _wait_until_idle(manager)


def test_refresh_releases_the_flag_on_the_manager_that_claimed_it(api, blocked_sync):
    """Regression: a config reload during a sync must not leave the old manager stuck."""
    from rootmind.api.app import app_state

    manager, started, release = blocked_sync
    assert api.post("/index/refresh").status_code == 200
    assert started.wait(5)

    assert api.post("/config", json=api.get("/config").json()).json()["reloaded"] is True
    assert app_state.sync_manager is not manager

    release.set()
    _wait_until_idle(manager)
    assert app_state.sync_manager.is_running is False


def test_config_reload_during_a_sync_still_allows_only_one(api, blocked_sync):
    """Regression: the manager built by a reload did not see the old manager's sync."""
    from rootmind.api.app import app_state

    manager, started, release = blocked_sync
    assert api.post("/index/refresh").status_code == 200
    assert started.wait(5)

    assert api.post("/config", json=api.get("/config").json()).json()["reloaded"] is True
    assert app_state.sync_manager is not manager

    assert api.get("/status").json()["is_running"] is True
    assert api.post("/index/refresh").status_code == 409
    assert app_state.sync_manager.start_background() is False  # what the scheduler does

    release.set()
    _wait_until_idle(manager)
    assert api.get("/status").json()["is_running"] is False


def test_reload_cancels_the_running_sync_only_when_folders_change(api, blocked_sync, tmp_path):
    manager, started, _ = blocked_sync
    assert api.post("/index/refresh").status_code == 200
    assert started.wait(5)

    body = api.get("/config").json()
    body["llm"]["model"] = "claude-other-model"
    api.post("/config", json=body)
    assert not manager.indexer.cancel_event.is_set()  # an LLM-only change keeps the sync

    other = tmp_path / "other"
    other.mkdir()
    body["connectors"].append({"name": "other", "type": "local", "path": str(other)})
    api.post("/config", json=body)
    assert manager.indexer.cancel_event.is_set()


def test_refresh_unknown_connector_404(api):
    response = api.post("/index/refresh", json={"connector_name": "nope"})
    assert response.status_code == 404
    from rootmind.api.app import app_state

    assert app_state.sync_manager.is_running is False
