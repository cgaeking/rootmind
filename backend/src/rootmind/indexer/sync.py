"""Sync manager for scheduled and manual index synchronization."""

import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..connectors.base import BaseConnector
from ..database.repository import DocumentRepository
from .indexer import Indexer, SyncCancelledError

logger = logging.getLogger(__name__)


class _SyncGuard:
    """The "a sync is running" flag of one database file, shared within the process.

    A config reload replaces the SyncManager while a sync of the old one may
    still run in its worker thread. Keeping the flag per database (not per
    manager) means the new manager sees that sync: ``/status`` reports it,
    and a second sync on the same file is refused instead of running
    concurrently.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.indexer: Indexer | None = None  # the indexer of the running sync


_GUARDS: dict[str, _SyncGuard] = {}
_GUARDS_LOCK = threading.Lock()


def _guard_for(db: DocumentRepository) -> _SyncGuard:
    db_path = getattr(db, "db_path", None)
    key = os.path.normcase(str(Path(db_path).resolve())) if db_path else f"object:{id(db)}"
    with _GUARDS_LOCK:
        return _GUARDS.setdefault(key, _SyncGuard())


class SyncManager:
    """Runs full and incremental syncs, one at a time per database, and the schedule.

    The sync methods block (file and database I/O); the API runs them in a
    worker thread through :meth:`start_background`.
    """

    def __init__(
        self,
        db: DocumentRepository,
        connectors: dict[str, BaseConnector],
        config: AppConfig,
        indexer: Indexer | None = None,
    ):
        """Initialize the sync manager.

        Args:
            db: Document repository.
            connectors: Dictionary of connectors.
            config: Application configuration.
            indexer: Optional indexer instance (created if not provided).
        """
        self.db = db
        self.connectors = connectors
        self.config = config
        self.indexer = indexer or Indexer(db, connectors, config)
        self._guard = _guard_for(db)
        self._scheduler = None

    @property
    def is_running(self) -> bool:
        """True while any sync runs on this database in this process."""
        return self._guard.running

    @property
    def progress(self) -> dict[str, Any]:
        """Progress of the running sync, also when another manager started it."""
        active = self._guard.indexer
        return (active or self.indexer).progress

    @property
    def scheduler(self):
        """The running APScheduler instance, or None."""
        return self._scheduler

    def _try_start(self) -> bool:
        """Atomically check-and-set the running flag. Returns False if already running."""
        with self._guard.lock:
            if self._guard.running:
                return False
            self._guard.running = True
            self._guard.indexer = self.indexer
            self.indexer.cancel_event.clear()
            return True

    def _finish(self) -> None:
        with self._guard.lock:
            self._guard.running = False
            self._guard.indexer = None

    def cancel_running(self) -> None:
        """Ask the sync running on this database to stop before its next file."""
        with self._guard.lock:
            if self._guard.indexer is not None:
                self._guard.indexer.cancel_event.set()

    def _prune_removed_connectors(self) -> None:
        """Drop index data of connectors that are no longer in the config."""
        try:
            self.db.prune_connectors({c.name for c in self.config.connectors})
        except Exception as e:
            logger.error(f"Error pruning removed connectors: {e}")

    def full_sync(self, connector_name: str | None = None) -> dict:
        """Re-index every file (claims the running flag itself).

        Args:
            connector_name: Optional specific connector to sync.

        Returns:
            Sync results dictionary.

        Raises:
            RuntimeError: a sync is already running.
        """
        return self._claim_and_run(full=True, connector_name=connector_name)

    def incremental_sync(self, connector_name: str | None = None) -> dict:
        """Index new and changed files only (claims the running flag itself).

        Files whose content hash is unchanged since the last sync are skipped.

        Args:
            connector_name: Optional specific connector to sync.

        Returns:
            Sync results dictionary.

        Raises:
            RuntimeError: a sync is already running.
        """
        return self._claim_and_run(full=False, connector_name=connector_name)

    def _claim_and_run(self, full: bool, connector_name: str | None) -> dict:
        if not self._try_start():
            raise RuntimeError("Sync already in progress")
        try:
            return self._run_locked(full, connector_name)
        finally:
            self._finish()

    def start_background(self, full: bool = False, connector_name: str | None = None) -> bool:
        """Start a sync in a worker thread and return at once.

        Returns:
            False when a sync is already running on this database.
        """
        if not self._try_start():
            return False

        def work() -> None:
            try:
                self._run_locked(full, connector_name)
            except SyncCancelledError:
                logger.info("Sync cancelled")
            except Exception:
                logger.exception("Background sync failed")
            finally:
                self._finish()

        try:
            threading.Thread(target=work, name="rootmind-sync").start()
        except BaseException:
            self._finish()
            raise
        return True

    def _run_locked(self, full: bool, connector_name: str | None = None) -> dict:
        """Sync body; the caller holds the running flag."""
        start_time = datetime.now()
        self._prune_removed_connectors()

        if connector_name:
            results = {connector_name: self.indexer.index_connector(connector_name, force=full)}
        else:
            results = self.indexer.index_all(force=full)

        for name, counts in results.items():
            if "error" in counts:
                # The folder could not be listed. Keep its index: removing
                # "deleted" files now would remove every file of the folder.
                continue
            try:
                counts["removed"] = self.indexer.cleanup_deleted_files(name)
            except Exception as e:
                logger.error(f"Error cleaning up {name}: {e}")
            if full:
                self.db.update_sync_status(name, last_full_sync_at=datetime.now())

        duration = (datetime.now() - start_time).total_seconds()
        kind = "full" if full else "incremental"
        logger.info(f"{kind.capitalize()} sync completed in {duration:.2f}s")

        return {"type": kind, "duration_seconds": duration, "results": results}

    def get_status(self) -> dict:
        """Get current sync status for all connectors.

        Returns:
            Status dictionary with connector info and counts.
        """
        statuses = self.db.get_all_sync_statuses()

        connector_statuses = {}
        for status in statuses:
            connector_statuses[status.connector_name] = status.to_dict()

        # Add connectors without status
        for name in self.connectors:
            if name not in connector_statuses:
                connector_statuses[name] = {
                    "connector_name": name,
                    "last_sync_at": None,
                    "last_full_sync_at": None,
                    "documents_total": 0,
                    "documents_indexed": 0,
                    "documents_error": 0,
                    "is_syncing": False,
                    "error_message": None,
                }

        return {
            "is_running": self.is_running,
            "connectors": connector_statuses,
            "total_documents": self.db.count_documents(),
            "indexed_documents": self.db.count_documents(status="indexed"),
            "error_documents": self.db.count_documents(status="error"),
        }

    def setup_scheduler(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Set up APScheduler for automatic sync.

        Prefers a fixed interval (config.indexer.sync_interval_hours); falls back
        to the cron schedule (config.indexer.sync_schedule) if no interval is set.
        Runs an initial sync shortly after startup.

        Args:
            loop: The event loop the scheduler runs on. Pass the server's main
                loop when calling from a worker thread (e.g. a config reload
                triggered by a sync route); without it, APScheduler looks for
                a running loop in the current thread and fails there.
        """
        interval_hours = getattr(self.config.indexer, "sync_interval_hours", None)
        schedule = self.config.indexer.sync_schedule

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            scheduler = AsyncIOScheduler(event_loop=loop) if loop else AsyncIOScheduler()

            if interval_hours and interval_hours > 0:
                from apscheduler.triggers.interval import IntervalTrigger

                trigger = IntervalTrigger(hours=interval_hours)
                desc = f"every {interval_hours}h"
            elif schedule and len(schedule.split()) == 5:
                from apscheduler.triggers.cron import CronTrigger

                parts = schedule.split()
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                )
                desc = f"cron {schedule}"
            else:
                logger.info("No sync interval/schedule configured, skipping scheduler setup")
                return

            scheduler.add_job(
                self._scheduled_sync,
                trigger,
                id="scheduled_sync",
                name="Scheduled Index Sync",
                # Run a first sync ~30s after start so new folders get indexed soon.
                next_run_time=datetime.now() + timedelta(seconds=30),
                replace_existing=True,
            )
            scheduler.start()
            self._scheduler = scheduler
            logger.info(f"Scheduler started: {desc} (initial sync in ~30s)")

        except ImportError:
            logger.warning("APScheduler not installed, scheduled sync disabled")
        except Exception as e:
            logger.error(f"Error setting up scheduler: {e}")

    async def _scheduled_sync(self) -> None:
        """Start an incremental sync in a worker thread, unless one is running."""
        logger.info("Starting scheduled sync")
        if not self.start_background():
            logger.info("Scheduled sync skipped: another sync is still running")

    def shutdown_scheduler(self) -> None:
        """Shut down the scheduler if running."""
        if self._scheduler:
            self._scheduler.shutdown()
            self._scheduler = None
            logger.info("Scheduler shutdown")
