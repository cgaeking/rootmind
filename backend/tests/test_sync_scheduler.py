"""Background indexing schedule: survives a live config reload and honors RLM_DISABLE_SCHEDULER."""

from __future__ import annotations

import asyncio
import threading

import pytest
from helpers import rest_client

from rootmind.api.app import app_state, create_app
from rootmind.config import AppConfig
from rootmind.database.repository import DocumentRepository
from rootmind.indexer import SyncManager


@pytest.fixture
def scheduler_enabled(monkeypatch):
    monkeypatch.delenv("RLM_DISABLE_SCHEDULER", raising=False)


def _job(manager: SyncManager):
    scheduler = manager.scheduler
    assert scheduler is not None and scheduler.running
    return scheduler.get_job("scheduled_sync")


def test_scheduler_runs_from_startup_to_shutdown(rlm_home, scheduler_enabled):
    with rest_client(create_app()):
        manager = app_state.sync_manager
        job = _job(manager)
        assert job is not None
        assert "interval" in str(job.trigger)
    assert manager.scheduler is None  # shut down by the lifespan


def test_scheduler_survives_config_reload(api, scheduler_enabled):
    """Regression: the reload runs in a worker thread without an event loop."""
    body = api.get("/config").json()

    assert api.post("/config", json=body).json()["reloaded"] is True

    assert _job(app_state.sync_manager) is not None
    # Reload twice: the previous scheduler is shut down, never leaked.
    previous = app_state.sync_manager.scheduler
    api.post("/config", json=body)
    assert previous.running is False
    assert _job(app_state.sync_manager) is not None


def test_reload_honors_disable_flag(api, monkeypatch):
    body = api.get("/config").json()
    monkeypatch.delenv("RLM_DISABLE_SCHEDULER")
    api.post("/config", json=body)
    assert app_state.sync_manager.scheduler is not None

    monkeypatch.setenv("RLM_DISABLE_SCHEDULER", "1")
    api.post("/config", json=body)

    assert app_state.sync_manager.scheduler is None


def test_disabled_at_startup(api):
    assert app_state.sync_manager.scheduler is None


@pytest.fixture
def manager(tmp_path):
    repo = DocumentRepository(tmp_path / "db" / "s.db")
    yield SyncManager(repo, {}, AppConfig())
    repo.engine.dispose()


def test_setup_from_a_worker_thread_with_the_loop(manager):
    async def scenario():
        loop = asyncio.get_running_loop()
        worker = threading.Thread(target=manager.setup_scheduler, kwargs={"loop": loop})
        worker.start()
        worker.join()
        await asyncio.sleep(0)
        job = _job(manager)
        manager.shutdown_scheduler()
        return job

    assert asyncio.run(scenario()) is not None


def test_cron_schedule_when_no_interval(manager):
    manager.config.indexer.sync_interval_hours = 0
    manager.config.indexer.sync_schedule = "15 4 * * 1-5"

    async def scenario():
        manager.setup_scheduler()
        trigger = str(_job(manager).trigger)
        manager.shutdown_scheduler()
        return trigger

    trigger = asyncio.run(scenario())
    assert "cron" in trigger
    assert "minute='15'" in trigger and "hour='4'" in trigger


def test_no_schedule_configured(manager):
    manager.config.indexer.sync_interval_hours = 0
    manager.config.indexer.sync_schedule = None

    async def scenario():
        manager.setup_scheduler()

    asyncio.run(scenario())
    assert manager.scheduler is None
