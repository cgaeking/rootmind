"""Background text recognition (OCR) of the pages the indexer planned.

The indexer stores a document with its text layer and records in
``ocr_documents`` which pages need OCR (see :mod:`rootmind.ocr`). One worker
thread per process works through that list, a page at a time, with Tesseract
at low priority and on one core. Recognized pages are cached by content hash
and merged into the document's text and search index when the document is
done. Pausing (or a config reload) stops it between pages; the work resumes
from the cache later.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .. import ocr
from ..config import AppConfig
from ..connectors.base import BaseConnector
from ..database.models import OcrDocument
from ..database.repository import DocumentRepository
from .indexer import page_count

logger = logging.getLogger(__name__)

#: How long the worker sleeps when there is nothing to do (a planned file wakes it).
IDLE_SECONDS = 60
#: How long it waits before looking for Tesseract again when none is installed.
NO_ENGINE_SECONDS = 300

# Shared by every worker of the process: a config reload replaces the worker,
# and the user's "pause" must survive that. One document is recognized at a
# time, also while an old worker finishes its page after a reload.
_paused = threading.Event()
_work_lock = threading.Lock()


def pause() -> None:
    _paused.set()


def resume() -> None:
    _paused.clear()


def is_paused() -> bool:
    return _paused.is_set()


class _StoppedError(Exception):
    """The worker is stopped or paused; the document stays pending."""


class OcrWorker:
    """The background thread that recognizes the planned pages."""

    def __init__(
        self,
        db: DocumentRepository,
        connectors: dict[str, BaseConnector],
        config: AppConfig,
    ):
        self.db = db
        self.connectors = connectors
        self.config = config
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: ``{"file_path", "page", "pages"}`` of the document being recognized.
        self.current: dict[str, Any] | None = None

    def folders(self) -> list[str]:
        """The folders with OCR on."""
        return [c.name for c in self.config.connectors if c.ocr and c.name in self.connectors]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="rootmind-ocr", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop after the current page (does not wait for it)."""
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """New work was planned, or the worker was resumed."""
        self._wake.set()

    def status(self) -> dict[str, Any]:
        engine = ocr.tesseract()
        return {
            "available": engine is not None,
            "version": engine.version if engine else None,
            "languages": list(engine.languages) if engine else [],
            "paused": is_paused(),
            "folders": self.folders(),
            "pending": self.db.ocr_pending(self.folders()),
            "current": dict(self.current) if self.current else None,
        }

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self) -> None:
        # Work of folders whose OCR was turned off is dropped, not kept for later.
        try:
            dropped = self.db.ocr_drop_pending(self.folders())
            if dropped:
                logger.info("OCR: dropped the pending work of %d file(s) (OCR turned off)", dropped)
        except Exception as e:
            logger.error("OCR: could not drop pending work: %s", e)
        ocr.reset_tesseract_lookup()
        while not self._stop.is_set():
            try:
                if is_paused():
                    self._sleep(IDLE_SECONDS)
                    continue
                folders = self.folders()
                job = self.db.ocr_next(folders) if folders else None
                if job is None:
                    self._sleep(IDLE_SECONDS)
                    continue
                engine = ocr.tesseract()
                if engine is None:
                    self._sleep(NO_ENGINE_SECONDS)
                    ocr.reset_tesseract_lookup()
                    continue
                with _work_lock:
                    self.process(job, engine)
            except Exception as e:  # never let the thread die
                logger.exception("OCR worker: %s", e)
                self._sleep(IDLE_SECONDS)

    def _check(self) -> None:
        if self._stop.is_set() or is_paused():
            raise _StoppedError

    def process(self, job: OcrDocument, engine: ocr.Tesseract) -> None:
        """Recognize the pages of one document and merge them into its text."""
        connector = self.connectors.get(job.connector_name)
        if connector is None:
            self.db.ocr_forget(job.doc_id)
            return
        pages = job.page_numbers
        try:
            metadata = connector.get_metadata(job.file_path)
            if metadata.content_hash != job.content_hash:
                # Changed since the sync planned it: the next sync plans it again.
                self.db.ocr_forget(job.doc_id)
                return
            data = connector.read_file(job.file_path)
            recognized: dict[int, str] = {}
            for i, page in enumerate(pages):
                self._check()
                self.current = {"file_path": job.file_path, "page": i + 1, "pages": len(pages)}
                text = self.db.ocr_cached(job.content_hash, page)
                if text is None:
                    png = ocr.as_png(data) if page == 0 else ocr.render_page(data, page)
                    text = ocr.recognize(png, engine)
                    self.db.ocr_store(job.content_hash, page, text)
                recognized[page] = text
            self._check()
            self._merge(job, recognized)
            self.db.ocr_finish(job.doc_id, "done")
            logger.info("OCR: %s (%d page(s))", job.file_path, len(pages))
        except _StoppedError:
            return
        except FileNotFoundError:
            # Deleted meanwhile; the sync's cleanup removes the document.
            self.db.ocr_forget(job.doc_id)
        except Exception as e:
            logger.warning("OCR failed for %s: %s", job.file_path, e)
            self.db.ocr_finish(job.doc_id, "error", str(e) or type(e).__name__)
        finally:
            self.current = None

    def _merge(self, job: OcrDocument, recognized: dict[int, str]) -> None:
        if ocr.is_image(job.file_path):
            self.db.set_ocr_text(job.doc_id, job.content_hash, recognized.get(0, ""), None)
            return
        doc = self.db.get_document(job.doc_id)
        if doc is None:
            return
        content = ocr.merge_pages(doc.content_text or "", recognized)
        self.db.set_ocr_text(job.doc_id, job.content_hash, content, page_count(content, "pdf"))
