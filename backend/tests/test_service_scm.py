"""The Service Control Manager adapter: state names, waiting, read-only queries on Windows."""

import os
import sys

import pytest

from rootmind.service import scm

windows = pytest.mark.skipif(sys.platform != "win32", reason="Service Control Manager")


@pytest.mark.parametrize(
    ("code", "name"),
    [(1, "stopped"), (2, "start_pending"), (3, "stop_pending"), (4, "running"), (7, "other")],
)
def test_state_names(code, name):
    assert scm.state_name(code) == name


def test_failure_actions():
    spec = scm.ServiceSpec(
        "x", "x", "x", '"C:\\x.exe" service run mcp-http --port 1', "LocalSystem"
    )
    assert spec.failure_delays_ms == (5_000, 30_000, 60_000)
    assert scm.FAILURE_RESET_SECONDS == 86_400


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def test_wait_for_returns_once_done():
    clock = Clock()
    states = iter(["start_pending", "start_pending", "running"])
    result = scm.wait_for(
        lambda: next(states), lambda s: s == "running", 10, sleep=clock.sleep, clock=clock
    )
    assert result == "running"
    assert clock.now == 0.5


def test_wait_for_times_out():
    clock = Clock()
    with pytest.raises(TimeoutError):
        scm.wait_for(lambda: None, lambda s: False, 5, sleep=clock.sleep, clock=clock)
    assert clock.now >= 5


@windows
def test_query_a_missing_service():
    with scm.Scm() as manager:
        assert manager.query("RootMind-Test-Does-Not-Exist") is None


@windows
def test_query_a_running_system_service():
    with scm.Scm() as manager:
        status = manager.query("EventLog")
    assert status is not None
    assert status.state == scm.RUNNING
    assert status.pid > 0
    assert status.image_path
    assert status.start_type == 2  # automatic


@windows
def test_changes_need_administrator_rights():
    from rootmind.service.system import WindowsSystem

    if WindowsSystem().is_elevated():
        pytest.skip("runs elevated; the e2e test covers changes")
    with pytest.raises(scm.ScmError) as raised:
        scm.Scm(write=True)
    assert raised.value.winerror == 5  # ERROR_ACCESS_DENIED


@windows
def test_wait_for_process_exit():
    assert scm.wait_for_process_exit(0, 1) is True
    # This process does not exit within 0.1 s.
    assert scm.wait_for_process_exit(os.getpid(), 0.1) is False
