"""Indexing the tiny corpus end to end: parsers, markers, incremental sync, patterns, pruning."""

from __future__ import annotations

import pytest
from helpers import doc_id

from rootmind.api.app import create_connectors
from rootmind.config import ConnectorConfig
from rootmind.database.repository import DocumentRepository
from rootmind.indexer import Indexer, SyncManager
from rootmind.indexer.indexer import SyncCancelledError
from scripts import corpus_writers as w

ALL_FILES = {
    "agreement.docx",
    "camp_notes.md",
    "handbook.txt",
    "invoice.pdf",
    "orders.csv",
    "prices.xlsx",
    "roadmap.pptx",
}


def _manager(cfg) -> SyncManager:
    repo = DocumentRepository(cfg.database.path)
    connectors = create_connectors(cfg)
    return SyncManager(repo, connectors, cfg, Indexer(repo, connectors, cfg))


def _sync(manager: SyncManager, full: bool = False) -> dict:
    return manager.full_sync() if full else manager.incremental_sync()


def _content(repo, name: str) -> str:
    return repo.get_document_content(doc_id(repo, name))["content"]


def test_indexes_all_formats(app_config):
    manager = _manager(app_config)

    result = _sync(manager)

    counts = result["results"]["docs"]
    assert counts == {"total": 7, "indexed": 7, "skipped": 0, "errors": 0, "removed": 0}
    repo = manager.db
    docs = repo.list_documents()
    assert {d["file_name"] for d in docs} == ALL_FILES
    assert {d["file_type"] for d in docs} == {"docx", "md", "txt", "pdf", "csv", "xlsx", "pptx"}
    status = manager.get_status()
    assert status["indexed_documents"] == 7
    assert status["connectors"]["docs"]["documents_indexed"] == 7
    repo.engine.dispose()


def test_location_markers(indexed_repo):
    pdf = _content(indexed_repo, "invoice.pdf")
    assert pdf.startswith("[Page 1]\nInvoice INV-7")
    assert "[Page 2]\nInvoice INV-7, page 2\nTotal due: 1,179.36" in pdf
    invoice = doc_id(indexed_repo, "invoice.pdf")
    assert indexed_repo.resolve_page(invoice, pdf.index("Total due")) == "Page 2"

    xlsx = _content(indexed_repo, "prices.xlsx")
    assert (
        "=== Sheet: Retail ===\nSKU | Product | Retail price\nFH-TENT-2P | Trailhead tent | 249"
        in xlsx
    )
    assert "=== Sheet: Wholesale ===" in xlsx
    prices = doc_id(indexed_repo, "prices.xlsx")
    assert indexed_repo.resolve_page(prices, xlsx.index("174.3")) == "Sheet: Wholesale"

    pptx = _content(indexed_repo, "roadmap.pptx")
    assert "=== Slide 2 ===\n\nUltralight tent\n\nTarget weight: 1.1 kg" in pptx
    roadmap = doc_id(indexed_repo, "roadmap.pptx")
    assert indexed_repo.resolve_page(roadmap, pptx.index("1.1 kg")) == "Slide 2"

    docx = _content(indexed_repo, "agreement.docx")
    assert "Either party may terminate this agreement with 60 days notice." in docx
    assert "Liability cap | 12 months of fees" in docx  # table rows


def test_page_count_comes_from_the_markers(indexed_repo):
    """Regression: page_count was len(text) // 3000, so a 2-page invoice had 1 page."""
    counts = {d["file_name"]: d["page_count"] for d in indexed_repo.list_documents()}
    assert counts["invoice.pdf"] == 2
    assert counts["roadmap.pptx"] == 3
    assert counts["handbook.txt"] is None
    assert counts["prices.xlsx"] is None


def test_incremental_sync_skips_reindexes_and_prunes(app_config, tiny_corpus):
    manager = _manager(app_config)
    _sync(manager)
    before = manager.db.get_document_by_path("docs", "camp_notes.md").indexed_at

    unchanged = _sync(manager)["results"]["docs"]
    assert (unchanged["indexed"], unchanged["skipped"], unchanged["removed"]) == (0, 7, 0)
    assert manager.db.get_document_by_path("docs", "camp_notes.md").indexed_at == before

    w.write_text(tiny_corpus / "handbook.txt", "Paid time off: 25 days per year.\n")
    (tiny_corpus / "orders.csv").unlink()
    changed = _sync(manager)["results"]["docs"]

    assert (changed["indexed"], changed["skipped"], changed["removed"]) == (1, 5, 1)
    assert manager.db.search_fulltext("25 days")[0]["file_name"] == "handbook.txt"
    assert _content(manager.db, "handbook.txt") == "Paid time off: 25 days per year."
    assert manager.db.search_fulltext("accrue") == []  # a word only the old text had
    assert manager.db.search_fulltext("Hilltop") == []
    manager.db.engine.dispose()


def test_full_sync_reindexes_everything(app_config):
    manager = _manager(app_config)
    _sync(manager)

    full = _sync(manager, full=True)

    assert full["type"] == "full"
    assert full["results"]["docs"]["indexed"] == 7
    assert manager.db.get_sync_status("docs").last_full_sync_at is not None
    manager.db.engine.dispose()


def test_include_and_exclude_patterns(app_config, tiny_corpus):
    (tiny_corpus / "node_modules").mkdir()
    w.write_text(tiny_corpus / "node_modules" / "pkg.md", "dependency readme")
    w.write_text(tiny_corpus / ".hidden.md", "hidden file")
    w.write_text(tiny_corpus / "~$lock.md", "office lock file")
    app_config.indexer.exclude = [*app_config.indexer.exclude, "orders*"]

    manager = _manager(app_config)
    _sync(manager)

    names = {d["file_name"] for d in manager.db.list_documents()}
    assert names == ALL_FILES - {"orders.csv"}
    manager.db.engine.dispose()


def test_connector_patterns_override_global_ones(app_config):
    app_config.connectors[0].include = ["*.md", "*.txt"]
    manager = _manager(app_config)
    _sync(manager)

    assert {d["file_name"] for d in manager.db.list_documents()} == {
        "camp_notes.md",
        "handbook.txt",
    }
    manager.db.engine.dispose()


def test_files_over_the_size_limit_are_skipped(app_config, tiny_corpus):
    w.write_text(tiny_corpus / "huge.txt", "needle " + "x" * (1024 * 1024 + 10))
    app_config.indexer.max_file_size_mb = 1

    manager = _manager(app_config)
    _sync(manager)

    huge = manager.db.get_document_by_path("docs", "huge.txt")
    assert huge.status == "skipped"
    assert "File too large" in huge.error_message
    assert huge.content_text is None
    assert manager.db.search_fulltext("needle") == []
    manager.db.engine.dispose()


def test_unparseable_file_is_recorded_as_error_and_sync_continues(app_config, tiny_corpus):
    (tiny_corpus / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")

    manager = _manager(app_config)
    counts = _sync(manager)["results"]["docs"]

    assert counts == {"total": 8, "indexed": 7, "skipped": 0, "errors": 1, "removed": 0}
    broken = manager.db.get_document_by_path("docs", "broken.pdf")
    assert broken.status == "error"
    status = manager.get_status()
    assert status["error_documents"] == 1
    assert status["connectors"]["docs"]["documents_error"] == 1  # the desktop's error badge

    # Unchanged, the file is skipped by hash next time, but it is still an error document.
    again = _sync(manager)["results"]["docs"]
    assert (again["errors"], again["skipped"]) == (0, 8)
    assert manager.get_status()["connectors"]["docs"]["documents_error"] == 1
    manager.db.engine.dispose()


def test_removed_connector_pruned(app_config, tmp_path):
    other = tmp_path / "other"
    w.write_text(other / "attic.md", "The silver lantern is in the attic.")
    app_config.connectors.append(ConnectorConfig(name="other", type="local", path=str(other)))
    manager = _manager(app_config)
    _sync(manager)
    assert manager.db.search_fulltext("attic")

    # The folder is removed from the config; the next sync prunes its documents.
    app_config.connectors = [c for c in app_config.connectors if c.name != "other"]
    manager = _manager(app_config)
    _sync(manager)

    assert manager.db.search_fulltext("attic") == []
    assert manager.db.search_fuzzy("attic") == []
    assert manager.db.get_sync_status("other") is None
    assert manager.db.count_documents() == 7
    manager.db.engine.dispose()


def test_unreachable_folder_keeps_its_index(app_config, tiny_corpus):
    """Pruning goes by configured names, not by whether a folder could be opened."""
    manager = _manager(app_config)
    _sync(manager)
    app_config.connectors[0].path = str(tiny_corpus.parent / "unplugged-drive")

    manager = _manager(app_config)  # the connector cannot be created now
    assert manager.connectors == {}
    _sync(manager)

    assert manager.db.count_documents(status="indexed") == 7
    manager.db.engine.dispose()


def test_folder_that_disappears_while_running_keeps_its_index(app_config, tiny_corpus):
    """Regression: an unplugged drive emptied the index of its folder on the next sync."""
    manager = _manager(app_config)
    _sync(manager)
    unplugged = tiny_corpus.with_name("unplugged")
    tiny_corpus.rename(unplugged)

    result = _sync(manager)["results"]["docs"]

    assert "error" in result
    assert manager.db.count_documents(status="indexed") == 7
    status = manager.get_status()["connectors"]["docs"]
    assert status["error_message"] and "not found" in status["error_message"].lower()
    assert status["is_syncing"] is False

    # Plugged in again: the next sync clears the error and keeps everything.
    unplugged.rename(tiny_corpus)
    again = _sync(manager)["results"]["docs"]
    assert (again["skipped"], again["removed"]) == (7, 0)
    assert manager.get_status()["connectors"]["docs"]["error_message"] is None
    manager.db.engine.dispose()


def test_cleanup_refuses_to_run_without_the_folder(app_config, tiny_corpus):
    manager = _manager(app_config)
    _sync(manager)
    tiny_corpus.rename(tiny_corpus.with_name("gone"))

    with pytest.raises(FileNotFoundError):
        manager.indexer.cleanup_deleted_files("docs")
    assert manager.db.count_documents() == 7
    manager.db.engine.dispose()


def test_a_cancelled_sync_stops_before_the_next_file(app_config):
    manager = _manager(app_config)
    seen = []

    def cancel_after_two(progress):
        seen.append(progress["processed"])
        if progress["processed"] == 2:
            manager.cancel_running()

    assert manager._try_start()
    try:
        with pytest.raises(SyncCancelledError):
            manager.indexer.index_connector("docs", progress_callback=cancel_after_two)
    finally:
        manager._finish()

    assert seen == [1, 2]
    assert manager.db.count_documents() == 2
    assert manager.get_status()["connectors"]["docs"]["is_syncing"] is False
    assert _sync(manager)["results"]["docs"]["indexed"] == 5  # the next sync starts fresh
    manager.db.engine.dispose()


def test_only_one_sync_at_a_time(app_config):
    manager = _manager(app_config)
    assert manager._try_start()
    with pytest.raises(RuntimeError, match="already in progress"):
        _sync(manager)
    manager._finish()
    assert _sync(manager)["type"] == "incremental"
    manager.db.engine.dispose()


def test_unknown_connector(app_config):
    manager = _manager(app_config)
    with pytest.raises(ValueError, match="Unknown connector"):
        manager.incremental_sync("nope")
    assert manager.is_running is False
    manager.db.engine.dispose()
