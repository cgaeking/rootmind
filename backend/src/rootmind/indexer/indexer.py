"""Document indexer: reads files from connectors and stores their text (no LLM involved)."""

import enum
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePath
from typing import Any

from ..config import AppConfig
from ..connectors.base import BaseConnector
from ..database.repository import DocumentRepository
from .parser import DocumentParser

logger = logging.getLogger(__name__)

# Location markers written by the parsers (see parser.py). The highest number
# is the page or slide count; pages and slides without text have no marker.
_PAGE_COUNT_MARKERS = {
    "pdf": re.compile(r"^\[Page (\d+)\]$", re.MULTILINE),
    "pptx": re.compile(r"^=== Slide (\d+) ===$", re.MULTILINE),
}


class IndexOutcome(enum.Enum):
    """What happened to one file during a sync."""

    INDEXED = "indexed"
    #: Unchanged since the last sync, too large, of an unsupported type, or gone.
    SKIPPED = "skipped"
    #: Could not be read or parsed; stored with status "error" and a message.
    ERROR = "error"


class SyncCancelledError(Exception):
    """The sync was cancelled, e.g. because a config reload replaced its settings."""


def page_count(content: str, file_type: str) -> int | None:
    """Number of pages (PDF) or slides (PPTX), taken from the parser's markers.

    Other formats have no pages and return None.
    """
    pattern = _PAGE_COUNT_MARKERS.get(file_type)
    if pattern is None:
        return None
    numbers = [int(n) for n in pattern.findall(content)]
    return max(numbers) if numbers else None


class Indexer:
    """Indexes documents from connectors into the database.

    The full text of each document is stored without LLM processing. The query
    engine reads it on demand through FTS5 search and windowed reads. Files are
    processed one after another; callers run a whole sync in a worker thread.
    """

    def __init__(
        self,
        db: DocumentRepository,
        connectors: dict[str, BaseConnector],
        config: AppConfig,
    ):
        """Initialize the indexer.

        Args:
            db: Document repository for storing index data.
            connectors: Dictionary of connector name to connector instance.
            config: Application configuration.
        """
        self.db = db
        self.connectors = connectors
        self.config = config
        # Set from another thread to stop a running sync before its next file.
        self.cancel_event = threading.Event()

        # Progress tracking
        self._current_progress = self._empty_progress()

    @staticmethod
    def _empty_progress() -> dict[str, Any]:
        return {
            "total": 0,
            "processed": 0,
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "current_file": "",
        }

    @property
    def progress(self) -> dict[str, Any]:
        """Get current indexing progress."""
        return self._current_progress.copy()

    def index_file(
        self,
        connector: BaseConnector,
        file_path: str,
        force: bool = False,
    ) -> IndexOutcome:
        """Index a single file.

        Args:
            connector: The connector to read the file from.
            file_path: Path to the file within the connector.
            force: If True, re-index even if hash matches.

        Returns:
            Whether the file was indexed, skipped or recorded as an error.
        """
        try:
            self._current_progress["current_file"] = file_path

            # Get metadata
            metadata = connector.get_metadata(file_path)

            # Check if update is needed
            if not force:
                existing = self.db.get_document_by_path(connector.name, file_path)
                if existing and existing.content_hash == metadata.content_hash:
                    logger.debug(f"Skipping unchanged file: {file_path}")
                    return IndexOutcome.SKIPPED

            # Check file size limit
            max_size = self.config.indexer.max_file_size_mb * 1024 * 1024
            if metadata.size_bytes > max_size:
                logger.warning(
                    f"File too large, skipping: {file_path} ({metadata.size_bytes} bytes)"
                )
                self.db.upsert_document(
                    connector_name=connector.name,
                    file_path=file_path,
                    file_name=metadata.name,
                    file_type=metadata.file_type,
                    size_bytes=metadata.size_bytes,
                    created_at=metadata.created_at,
                    modified_at=metadata.modified_at,
                    content_hash=metadata.content_hash,
                    status="skipped",
                    error_message=f"File too large: {metadata.size_bytes} bytes (max: {max_size})",
                )
                return IndexOutcome.SKIPPED

            # Check if file type is supported
            if not DocumentParser.is_supported(file_path):
                logger.debug(f"Unsupported file type, skipping: {file_path}")
                return IndexOutcome.SKIPPED

            # Read and parse content
            content_bytes = connector.read_file(file_path)
            content_text = DocumentParser.parse(content_bytes, file_path)

            if not content_text:
                logger.warning(f"Could not extract text from: {file_path}")
                self.db.upsert_document(
                    connector_name=connector.name,
                    file_path=file_path,
                    file_name=metadata.name,
                    file_type=metadata.file_type,
                    size_bytes=metadata.size_bytes,
                    created_at=metadata.created_at,
                    modified_at=metadata.modified_at,
                    content_hash=metadata.content_hash,
                    status="error",
                    error_message="Could not extract text content",
                )
                return IndexOutcome.ERROR

            # Store in database (full text, no LLM processing)
            self.db.upsert_document(
                connector_name=connector.name,
                file_path=file_path,
                file_name=metadata.name,
                file_type=metadata.file_type,
                size_bytes=metadata.size_bytes,
                created_at=metadata.created_at,
                modified_at=metadata.modified_at,
                content_hash=metadata.content_hash,
                content_text=content_text,
                page_count=page_count(content_text, metadata.file_type),
                status="indexed",
            )

            logger.info(f"Indexed: {file_path} ({len(content_text)} chars)")
            return IndexOutcome.INDEXED

        except FileNotFoundError:
            # Deleted between listing and reading; the cleanup pass removes it.
            logger.warning(f"File not found: {file_path}")
            return IndexOutcome.SKIPPED
        except Exception as e:
            logger.error(f"Error indexing {file_path}: {e}")
            name = PurePath(file_path.replace("\\", "/")).name
            try:
                self.db.upsert_document(
                    connector_name=connector.name,
                    file_path=file_path,
                    file_name=name,
                    file_type=PurePath(name).suffix.lower().lstrip(".") or "unknown",
                    status="error",
                    error_message=str(e),
                )
            except Exception as db_error:
                logger.error(f"Could not record the error for {file_path}: {db_error}")
            return IndexOutcome.ERROR

    def index_connector(
        self,
        connector_name: str,
        force: bool = False,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, int]:
        """Index all files from a connector.

        Args:
            connector_name: Name of the connector to index.
            force: If True, re-index all files.
            progress_callback: Optional callback for progress updates.

        Returns:
            Counts for this run: total, indexed, skipped, errors.

        Raises:
            ValueError: unknown connector.
            SyncCancelledError: ``cancel_event`` was set.
            OSError: the connector's folder cannot be listed. The error is
                recorded in the folder's sync status.
        """
        connector = self.connectors.get(connector_name)
        if not connector:
            raise ValueError(f"Unknown connector: {connector_name}")

        self.db.update_sync_status(connector_name, is_syncing=True)
        self._current_progress = self._empty_progress()
        counts = {"total": 0, "indexed": 0, "skipped": 0, "errors": 0}
        count_key = {
            IndexOutcome.INDEXED: "indexed",
            IndexOutcome.SKIPPED: "skipped",
            IndexOutcome.ERROR: "errors",
        }

        try:
            logger.info(f"Scanning files in connector: {connector_name}")
            files = list(connector.list_files_recursive())
            counts["total"] = self._current_progress["total"] = len(files)
            logger.info(f"Found {len(files)} files in connector: {connector_name}")

            for file_info in files:
                if self.cancel_event.is_set():
                    raise SyncCancelledError(f"Sync of {connector_name} cancelled")
                outcome = self.index_file(connector, file_info.path, force=force)
                key = count_key[outcome]
                counts[key] += 1
                self._current_progress[key] += 1
                self._current_progress["processed"] += 1
                if progress_callback:
                    progress_callback(self._current_progress)

        except SyncCancelledError:
            self.db.update_sync_status(connector_name, is_syncing=False)
            raise
        except Exception as e:
            logger.error(f"Error indexing connector {connector_name}: {e}")
            self.db.update_sync_status(connector_name, is_syncing=False, error_message=str(e))
            raise

        # The folder totals describe the index, not only this run: an
        # unchanged file that failed to parse before is skipped by hash now,
        # but it is still an error document.
        self.db.update_sync_status(
            connector_name,
            last_sync_at=datetime.now(),
            documents_total=counts["total"],
            documents_indexed=self.db.count_documents(connector_name, status="indexed"),
            documents_error=self.db.count_documents(connector_name, status="error"),
            is_syncing=False,
            error_message="",  # clears an error from an earlier run
        )

        logger.info(
            f"Indexed connector {connector_name}: "
            f"{counts['indexed']} indexed, {counts['skipped']} skipped, "
            f"{counts['errors']} errors"
        )
        return counts

    def index_all(self, force: bool = False) -> dict[str, dict[str, Any]]:
        """Index all files from all connectors.

        A connector that fails (for example, its folder is unreachable) gets
        ``{"error": message}`` and the others still run.

        Args:
            force: If True, re-index all files.

        Returns:
            Dictionary mapping connector names to their counts.
        """
        results: dict[str, dict[str, Any]] = {}

        for connector_name in self.connectors:
            try:
                results[connector_name] = self.index_connector(connector_name, force=force)
            except SyncCancelledError:
                raise
            except Exception as e:
                logger.error(f"Error indexing connector {connector_name}: {e}")
                results[connector_name] = {"error": str(e)}

        return results

    def cleanup_deleted_files(self, connector_name: str) -> int:
        """Remove index entries for files that no longer exist.

        Args:
            connector_name: Name of the connector to check.

        Returns:
            Number of documents removed.

        Raises:
            FileNotFoundError: the connector's folder is not reachable. Every
                file would look deleted then, so nothing is removed.
        """
        connector = self.connectors.get(connector_name)
        if not connector:
            raise ValueError(f"Unknown connector: {connector_name}")
        if not connector.is_available():
            raise FileNotFoundError(f"Folder of connector {connector_name} is not reachable")

        # Only ids and paths are needed; loading full rows would pull in the
        # extracted text of every document of the connector.
        indexed = self.db.list_document_paths(connector_name)

        stale_ids = [doc_id for doc_id, path in indexed if not connector.file_exists(path)]
        if not stale_ids:
            return 0

        removed = self.db.delete_documents(stale_ids)
        logger.info(f"Removed {removed} deleted files from index for connector {connector_name}")
        return removed
