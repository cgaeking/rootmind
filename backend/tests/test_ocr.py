"""Text recognition (OCR): which files and pages get it, the background work, the API."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

from rootmind import ocr
from rootmind.api.app import create_connectors
from rootmind.config import AppConfig, ConnectorConfig
from rootmind.database.repository import DocumentRepository
from rootmind.indexer import Indexer, SyncManager, ocr_worker
from scripts import corpus_writers as w

needs_tesseract = pytest.mark.skipif(ocr.tesseract() is None, reason="Tesseract is not installed")


def _scan(text: str, size: tuple[int, int] = (1240, 1754)) -> Image.Image:
    """A white page with ``text`` in large black letters, like a scan."""
    image = Image.new("L", size, 255)
    font = ImageFont.load_default(size=56)
    ImageDraw.Draw(image).multiline_text((100, 200), text, fill=0, font=font, spacing=24)
    return image


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _scanned_pdf(*pages: str) -> bytes:
    images = [_scan(text) for text in pages]
    out = io.BytesIO()
    images[0].save(out, format="PDF", save_all=True, append_images=images[1:], resolution=150)
    return out.getvalue()


def _mixed_pdf() -> bytes:
    """Page 1 has a text layer, page 2 is a scan."""
    writer = PdfWriter()
    for part in (
        w.pdf_bytes([["Contract overview, typed text layer"]]),
        _scanned_pdf("Kaufvertrag Musterhaus"),
    ):
        for page in PdfReader(io.BytesIO(part)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def folder(tmp_path) -> Path:
    root = tmp_path / "files"
    root.mkdir()
    (root / "mixed.pdf").write_bytes(_mixed_pdf())
    w.write_pdf(root / "typed.pdf", [["Only a text layer here"]])
    (root / "letter.png").write_bytes(_png(_scan("Rechnung Gartenbau Schmidt")))
    (root / "icon.png").write_bytes(_png(Image.new("L", (64, 64), 255)))
    return root


def _config(tmp_path, root: Path, enabled: bool) -> AppConfig:
    cfg = AppConfig(
        connectors=[ConnectorConfig(name="files", type="local", path=str(root), ocr=enabled)]
    )
    cfg.database.path = str(tmp_path / "db" / "index.db")
    return cfg


def _manager(cfg: AppConfig) -> SyncManager:
    repo = DocumentRepository(cfg.database.path)
    connectors = create_connectors(cfg)
    return SyncManager(repo, connectors, cfg, Indexer(repo, connectors, cfg))


def _drain(manager: SyncManager) -> int:
    """Do all pending OCR work in this thread, like the worker does; the number of documents."""
    worker = manager.ocr_worker
    engine = ocr.tesseract()
    done = 0
    while (job := manager.db.ocr_next(worker.folders())) is not None:
        worker.process(job, engine)
        done += 1
    return done


def _names(repo: DocumentRepository) -> set[str]:
    return {d["file_name"] for d in repo.list_documents(limit=100)}


def _found(repo: DocumentRepository, query: str) -> set[str]:
    return {hit["file_name"] for hit in repo.search_fulltext(query)}


@pytest.fixture(autouse=True)
def _not_paused():
    ocr_worker.resume()
    yield
    ocr_worker.resume()


# --- pure functions ----------------------------------------------------------------


def test_merge_pages_puts_recognized_pages_in_order_and_is_idempotent():
    content = "[Page 1]\nfirst\n\n[Page 3]\nthird"
    merged = ocr.merge_pages(content, {2: "second", 4: "  ", 3: "third (ocr)"})
    assert merged == "[Page 1]\nfirst\n\n[Page 2]\nsecond\n\n[Page 3]\nthird (ocr)"
    assert ocr.merge_pages(merged, {2: "second"}) == merged
    assert ocr.merge_pages("", {1: "only"}) == "[Page 1]\nonly"


def test_scanned_pages_finds_only_pages_without_text_layer():
    assert ocr.scanned_pages(_mixed_pdf()) == [2]
    assert ocr.scanned_pages(w.pdf_bytes([["text"], ["more text on page two"]])) == []
    assert ocr.scanned_pages(b"not a pdf") == []


def test_small_images_are_not_ocr_candidates():
    assert ocr.large_enough(_png(_scan("x")))
    assert not ocr.large_enough(_png(Image.new("L", (120, 800), 255)))
    assert not ocr.large_enough(b"broken")


# --- indexing ------------------------------------------------------------------------


def test_folder_without_ocr_ignores_images_and_plans_nothing(tmp_path, folder):
    manager = _manager(_config(tmp_path, folder, enabled=False))
    manager.incremental_sync()
    assert _names(manager.db) == {"mixed.pdf", "typed.pdf"}
    assert manager.db.ocr_pending(["files"]) == {"documents": 0, "pages": 0}
    manager.db.engine.dispose()


def test_folder_with_ocr_plans_scanned_pages_and_large_images(tmp_path, folder):
    manager = _manager(_config(tmp_path, folder, enabled=True))
    woken = []
    manager.indexer.on_ocr_planned = lambda: woken.append(1)
    manager.incremental_sync()
    repo = manager.db
    assert _names(repo) == {"mixed.pdf", "typed.pdf", "letter.png"}  # the icon is skipped
    assert repo.ocr_pending(["files"]) == {"documents": 2, "pages": 2}
    assert woken
    planned = {
        name: repo.ocr_state(repo.generate_doc_id("files", name))
        for name in ("mixed.pdf", "typed.pdf", "letter.png", "icon.png")
    }
    assert planned["mixed.pdf"].page_numbers == [2]
    assert planned["letter.png"].page_numbers == [0]
    assert planned["typed.pdf"].state == "none"
    assert planned["icon.png"].state == "none"
    icon = repo.get_document_by_path("files", "icon.png")
    assert icon.status == "skipped"
    repo.engine.dispose()


@needs_tesseract
def test_recognized_text_becomes_searchable(tmp_path, folder):
    manager = _manager(_config(tmp_path, folder, enabled=True))
    manager.incremental_sync()
    assert _drain(manager) == 2
    repo = manager.db

    assert "letter.png" in _found(repo, "Gartenbau")
    assert "mixed.pdf" in _found(repo, "Musterhaus")
    mixed = repo.get_document_by_path("files", "mixed.pdf")
    assert mixed.content_text.startswith("[Page 1]\nContract overview")
    assert "[Page 2]\nKaufvertrag Musterhaus" in mixed.content_text
    assert mixed.page_count == 2
    assert repo.ocr_pending(["files"]) == {"documents": 0, "pages": 0}

    # Unchanged files are not planned again on the next sync.
    manager.incremental_sync()
    assert repo.ocr_pending(["files"]) == {"documents": 0, "pages": 0}
    repo.engine.dispose()


@needs_tesseract
def test_reindexing_uses_the_cache_instead_of_recognizing_again(tmp_path, folder, monkeypatch):
    manager = _manager(_config(tmp_path, folder, enabled=True))
    manager.incremental_sync()
    _drain(manager)

    def fail(*_args, **_kwargs):
        raise AssertionError("OCR ran again")

    monkeypatch.setattr(ocr, "recognize", fail)
    manager.full_sync()  # re-parses every file: text layer only again
    assert manager.db.ocr_pending(["files"])["documents"] == 2
    assert _drain(manager) == 2
    assert "letter.png" in _found(manager.db, "Gartenbau")
    manager.db.engine.dispose()


def test_turning_ocr_on_later_plans_unchanged_files(tmp_path, folder):
    off = _manager(_config(tmp_path, folder, enabled=False))
    off.incremental_sync()
    off.db.engine.dispose()

    on = _manager(_config(tmp_path, folder, enabled=True))
    on.incremental_sync()
    assert on.db.ocr_pending(["files"]) == {"documents": 2, "pages": 2}
    on.db.engine.dispose()


def test_turning_ocr_off_drops_pending_work_and_images(tmp_path, folder):
    on = _manager(_config(tmp_path, folder, enabled=True))
    on.incremental_sync()
    on.db.engine.dispose()

    off = _manager(_config(tmp_path, folder, enabled=False))
    assert off.db.ocr_drop_pending(off.ocr_worker.folders()) == 2
    off.incremental_sync()
    assert _names(off.db) == {"mixed.pdf", "typed.pdf"}
    off.db.engine.dispose()


def test_a_file_changed_after_planning_is_not_recognized(tmp_path, folder, monkeypatch):
    manager = _manager(_config(tmp_path, folder, enabled=True))
    manager.incremental_sync()
    (folder / "letter.png").write_bytes(_png(_scan("Something else entirely")))
    monkeypatch.setattr(ocr, "recognize", lambda *_a, **_k: "stale")
    repo = manager.db
    job = repo.ocr_state(repo.generate_doc_id("files", "letter.png"))
    manager.ocr_worker.process(job, ocr.Tesseract("tesseract", None, ("eng",), "test"))
    assert repo.ocr_state(job.doc_id) is None  # the next sync plans it again
    manager.incremental_sync()
    assert repo.ocr_state(job.doc_id).state == "pending"
    repo.engine.dispose()


def test_pause_keeps_the_work_pending(tmp_path, folder, monkeypatch):
    manager = _manager(_config(tmp_path, folder, enabled=True))
    manager.incremental_sync()
    monkeypatch.setattr(ocr, "recognize", lambda *_a, **_k: "text")
    ocr_worker.pause()
    repo = manager.db
    job = repo.ocr_next(["files"])
    manager.ocr_worker.process(job, ocr.Tesseract("tesseract", None, ("eng",), "test"))
    assert repo.ocr_state(job.doc_id).state == "pending"
    repo.engine.dispose()


def test_a_failing_recognition_is_recorded(tmp_path, folder, monkeypatch):
    manager = _manager(_config(tmp_path, folder, enabled=True))
    manager.incremental_sync()

    def broken(*_args, **_kwargs):
        raise RuntimeError("Tesseract failed: boom")

    monkeypatch.setattr(ocr, "recognize", broken)
    repo = manager.db
    job = repo.ocr_next(["files"])
    manager.ocr_worker.process(job, ocr.Tesseract("tesseract", None, ("eng",), "test"))
    state = repo.ocr_state(job.doc_id)
    assert (state.state, state.error_message) == ("error", "Tesseract failed: boom")
    repo.engine.dispose()


def test_deleting_a_document_deletes_its_ocr_state(tmp_path, folder):
    manager = _manager(_config(tmp_path, folder, enabled=True))
    manager.incremental_sync()
    (folder / "letter.png").unlink()
    manager.incremental_sync()
    repo = manager.db
    assert repo.ocr_state(repo.generate_doc_id("files", "letter.png")) is None
    repo.engine.dispose()


# --- configuration and API -----------------------------------------------------------


def test_ocr_setting_is_saved_and_kept_when_omitted(api):
    body = api.get("/config").json()
    assert body["connectors"][0]["ocr"] is False
    body["connectors"][0]["ocr"] = True
    assert api.post("/config", json=body).status_code == 200
    assert api.get("/config").json()["connectors"][0]["ocr"] is True

    # A client that does not know the field keeps the stored value.
    body["connectors"] = [{k: v for k, v in f.items() if k != "ocr"} for f in body["connectors"]]
    assert api.post("/config", json=body).status_code == 200
    assert api.get("/config").json()["connectors"][0]["ocr"] is True
    assert api.get("/ocr/status").json()["folders"] == ["docs"]


def test_ocr_status_pause_and_resume(api):
    status = api.get("/ocr/status").json()
    assert status["paused"] is False
    assert status["pending"] == {"documents": 0, "pages": 0}
    assert status["folders"] == []
    assert api.post("/ocr/pause").json()["paused"] is True
    assert api.get("/ocr/status").json()["paused"] is True
    assert api.post("/ocr/resume").json()["paused"] is False
