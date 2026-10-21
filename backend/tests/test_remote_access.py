"""Remote access (rootmind.remote_access): the tunnel manager with a fake ngrok SDK, and helpers.

No test opens a real tunnel or needs an ngrok account: the manager gets the
fake SDK from ``fake_ngrok`` through its ``sdk_loader``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fake_ngrok import FAKE_AUTHTOKEN, OTHER_AUTHTOKEN, FakeNgrok

from rootmind import remote_access as ra

PORT = 3456
DOMAIN = "rootmind-test.ngrok-free.app"


def wait_for(condition, timeout: float = 10.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def write_config(home: Path, text: str) -> None:
    # Atomic like save_config: an in-place write lets the manager's poll read
    # the empty file between the truncate and the write.
    tmp = home / "config.yaml.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, home / "config.yaml")


def write_settings(home: Path, enabled: bool, domain: str | None = None) -> None:
    data = {
        "database": {"path": "./data/db/index.db"},
        "remote_access": {"enabled": enabled, "domain": domain},
    }
    write_config(home, yaml.safe_dump(data))


def write_token(home: Path, token: str) -> None:
    path = ra.authtoken_path(home)
    temporary = path.with_name("token.tmp")
    temporary.write_text(token, encoding="utf-8")
    os.replace(temporary, path)  # a new file, as the backend writes it


def status(home: Path) -> dict | None:
    # The manager rewrites the file in place, so a read can catch it just
    # after the truncate; read again like the backend does.
    for _ in range(20):
        try:
            return json.loads(ra.status_path(home).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.01)
    return None


def state_is(home: Path, state: str):
    def check():
        current = status(home)
        return current if current and current["state"] == state else None

    return check


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "RootMind"
    (root / "data").mkdir(parents=True)
    write_settings(root, False)
    return root


@pytest.fixture
def sdk():
    return FakeNgrok()


@pytest.fixture
def start(home, sdk):
    """Start a manager with the fake SDK and short timings; stopped after the test."""
    managers: list[ra.RemoteAccessManager] = []

    def start_manager(**options) -> ra.RemoteAccessManager:
        settings = {
            "home": home,
            "port": PORT,
            "sdk_loader": sdk.loader(),
            "poll_seconds": 0.02,
            "backoff_first": 0.05,
            "backoff_max": 0.2,
            "connect_timeout": 2.0,
        }
        settings.update(options)
        manager = ra.RemoteAccessManager(**settings)
        managers.append(manager)
        manager.start()
        return manager

    yield start_manager
    for manager in managers:
        manager.stop()


def record_statuses(manager: ra.RemoteAccessManager) -> list[dict]:
    """Every status the manager writes, in order."""
    seen: list[dict] = []
    original = manager._write_status

    def spy() -> None:
        seen.append(dict(manager._status))
        original()

    manager._write_status = spy
    return seen


# --- the manager ------------------------------------------------------------------------------


def test_off_by_default_never_loads_the_sdk(home, sdk, start):
    manager = start()
    wait_for(state_is(home, ra.OFF), what="status off")
    time.sleep(0.1)
    assert sdk.loaded == 0 and sdk.attempts == []
    manager.stop()
    assert status(home)["state"] == ra.STOPPED


def test_turning_it_on_opens_a_tunnel_to_the_mcp_port(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True, DOMAIN)
    manager = start()
    current = wait_for(state_is(home, ra.CONNECTED), what="connected")
    assert current["url"] == f"https://{DOMAIN}"
    assert current["error"] is None and current["updated"]
    [session] = sdk.sessions
    assert session.authtoken == FAKE_AUTHTOKEN
    assert session.domain == DOMAIN
    assert session.listener.target == f"http://127.0.0.1:{PORT}"
    assert session.client_info[0] == "rootmind"
    assert sdk.loaded == 1
    assert FAKE_AUTHTOKEN not in ra.status_path(home).read_text(encoding="utf-8")
    assert FAKE_AUTHTOKEN not in repr(manager.desired())


def test_without_a_domain_ngrok_assigns_one(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start()
    current = wait_for(state_is(home, ra.CONNECTED), what="connected")
    assert sdk.sessions[0].domain is None
    assert current["url"].startswith("https://generated-")


def test_turning_it_off_closes_the_tunnel(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    write_settings(home, False)
    wait_for(state_is(home, ra.OFF), what="off")
    [session] = sdk.sessions
    assert session.closed and session.listener.closed
    assert status(home)["url"] is None


def test_a_new_authtoken_reconnects_with_it(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    write_token(home, OTHER_AUTHTOKEN)
    wait_for(lambda: len(sdk.sessions) == 2, what="a second session")
    wait_for(state_is(home, ra.CONNECTED), what="connected again")
    first, second = sdk.sessions
    assert first.closed and first.listener.closed
    assert second.authtoken == OTHER_AUTHTOKEN and not second.closed


def test_a_new_domain_reconnects_on_it(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    write_settings(home, True, DOMAIN)
    wait_for(lambda: len(sdk.sessions) == 2, what="a second session")
    current = wait_for(state_is(home, ra.CONNECTED), what="connected again")
    assert current["url"] == f"https://{DOMAIN}"
    assert sdk.sessions[0].closed and sdk.sessions[1].domain == DOMAIN


def test_without_an_authtoken_it_reports_an_error_and_waits(home, sdk, start):
    write_settings(home, True)
    start()
    current = wait_for(state_is(home, ra.ERROR), what="error")
    assert current["error"] == ra.NO_AUTHTOKEN
    time.sleep(0.1)
    assert sdk.attempts == []
    write_token(home, FAKE_AUTHTOKEN)
    wait_for(state_is(home, ra.CONNECTED), what="connected")


def test_errors_are_retried_and_never_show_the_authtoken(home, sdk, start, caplog):
    caplog.set_level(logging.INFO)
    sdk.fail_next = [
        ValueError(f"authentication failed: {FAKE_AUTHTOKEN} ERR_NGROK_105"),
        RuntimeError("network unreachable"),
    ]
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    manager = ra.RemoteAccessManager(
        home=home,
        port=PORT,
        sdk_loader=sdk.loader(),
        poll_seconds=0.02,
        backoff_first=0.05,
        backoff_max=0.2,
    )
    seen = record_statuses(manager)
    manager.start()
    try:
        wait_for(state_is(home, ra.CONNECTED), what="connected after two failures")
    finally:
        manager.stop()
    assert len(sdk.attempts) == 3
    errors = [s["error"] for s in seen if s["state"] == ra.ERROR]
    assert errors == ["authentication failed: [authtoken] ERR_NGROK_105", "network unreachable"]
    assert FAKE_AUTHTOKEN not in json.dumps(seen)
    assert "ERR_NGROK_105" in caplog.text
    assert FAKE_AUTHTOKEN not in caplog.text


def test_a_failed_retry_waits_longer_each_time(home, sdk, start):
    now = [0.0]
    sdk.fail_next = [RuntimeError(f"failure {i}") for i in range(4)]
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start(clock=lambda: now[0], backoff_first=5.0, backoff_max=20.0)

    def attempts_after(t: float, expected: int) -> None:
        now[0] = t
        wait_for(lambda: len(sdk.attempts) == expected, what=f"{expected} attempts at {t}")
        time.sleep(0.08)  # a few more polls: no early retry
        assert len(sdk.attempts) == expected

    attempts_after(0.0, 1)  # next try after 5 s
    attempts_after(4.9, 1)
    attempts_after(5.0, 2)  # then 10 s
    attempts_after(14.9, 2)
    attempts_after(15.0, 3)  # then 20 s (the maximum)
    attempts_after(35.0, 4)  # 20 s again
    attempts_after(55.0, 5)  # this one succeeds
    wait_for(state_is(home, ra.CONNECTED), what="connected")


def test_a_hanging_connect_times_out(home, sdk, start):
    sdk.connect_delay = 1.0
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start(connect_timeout=0.05, backoff_first=10.0)
    current = wait_for(state_is(home, ra.ERROR), what="error")
    assert "did not answer" in current["error"]


def test_a_failed_endpoint_closes_its_session(home, sdk, start):
    sdk.fail_listen = [RuntimeError("ERR_NGROK_334: the endpoint is already online")]
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True, DOMAIN)
    start()
    current = wait_for(state_is(home, ra.ERROR), what="error")
    assert "already online" in current["error"]
    assert sdk.sessions[0].closed
    wait_for(state_is(home, ra.CONNECTED), what="connected on the retry")


def test_a_lost_connection_shows_reconnecting_until_the_next_heartbeat(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True, DOMAIN)
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    session = sdk.sessions[0]
    threading.Thread(
        target=session.on_disconnect, args=("connect.ngrok-agent.com:443", "connection reset")
    ).start()
    current = wait_for(state_is(home, ra.CONNECTING), what="reconnecting")
    assert "connection reset" in current["error"]
    threading.Thread(target=session.on_heartbeat, args=(25,)).start()
    current = wait_for(state_is(home, ra.CONNECTED), what="connected again")
    assert current["url"] == f"https://{DOMAIN}"
    assert len(sdk.sessions) == 1  # the SDK reconnects the same session


def test_callbacks_of_a_closed_session_are_ignored(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    write_token(home, OTHER_AUTHTOKEN)
    wait_for(lambda: len(sdk.sessions) == 2, what="a second session")
    wait_for(state_is(home, ra.CONNECTED), what="connected again")
    sdk.sessions[0].on_disconnect("addr", "old session")
    sdk.sessions[0].on_stop()
    time.sleep(0.1)
    assert status(home)["state"] == ra.CONNECTED
    assert not sdk.sessions[1].closed


def test_a_stop_command_from_ngrok_closes_the_tunnel_until_it_is_turned_on_again(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    sdk.sessions[0].on_stop()
    current = wait_for(state_is(home, ra.ERROR), what="stopped by ngrok")
    assert current["error"] == ra.STOPPED_BY_NGROK
    assert sdk.sessions[0].closed
    time.sleep(0.1)
    assert len(sdk.attempts) == 1  # no reconnect on its own
    write_settings(home, False)
    wait_for(state_is(home, ra.OFF), what="off")
    write_settings(home, True)
    wait_for(state_is(home, ra.CONNECTED), what="connected again")
    assert len(sdk.sessions) == 2


def test_stop_closes_the_tunnel_and_reports_stopped(home, sdk, start):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    manager = start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    manager.stop()
    assert sdk.sessions[0].closed and sdk.sessions[0].listener.closed
    final = status(home)
    assert (final["state"], final["url"]) == (ra.STOPPED, None)


def test_the_status_is_rewritten_as_a_heartbeat(home, sdk, start):
    start(heartbeat_seconds=0.05)
    first = wait_for(state_is(home, ra.OFF), what="off")["updated"]
    assert first
    # The time has a resolution of one second.
    wait_for(lambda: status(home)["updated"] != first, timeout=5, what="a newer status")


def test_library_log_messages_never_show_the_authtoken(home, sdk, start, caplog):
    caplog.set_level(logging.INFO)
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    manager = start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    logging.getLogger("ngrok.session").warning("authenticating with %s", FAKE_AUTHTOKEN)
    assert "authenticating with [authtoken]" in caplog.text
    assert FAKE_AUTHTOKEN not in caplog.text
    manager.stop()
    root = logging.getLogger()
    assert all(manager.redactor not in handler.filters for handler in root.handlers)


def test_an_unwritable_status_file_only_warns_once(tmp_path, sdk, caplog):
    home = tmp_path / "RootMind"  # no data folder: the status file cannot be written
    home.mkdir()
    write_settings(home, True)
    manager = ra.RemoteAccessManager(
        home=home, port=PORT, sdk_loader=sdk.loader(), poll_seconds=0.02, heartbeat_seconds=0.02
    )
    manager.start()
    try:
        time.sleep(0.2)
    finally:
        manager.stop()
    warnings = [r for r in caplog.records if "Cannot write the remote access status" in r.message]
    assert len(warnings) == 1


def test_an_unreadable_config_keeps_the_settings(home, sdk, start, caplog):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True)
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    write_config(home, "remote_access: [not, a, mapping\n")
    wait_for(lambda: "keeping the remote access settings" in caplog.text, what="a warning")
    assert status(home)["state"] == ra.CONNECTED
    assert not sdk.sessions[0].closed


def test_an_invalid_domain_in_the_config_is_ignored(home, sdk, start, caplog):
    write_token(home, FAKE_AUTHTOKEN)
    write_settings(home, True, "not a domain/path")
    start()
    wait_for(state_is(home, ra.CONNECTED), what="connected")
    assert sdk.sessions[0].domain is None
    assert "remote_access.domain" in caplog.text


# --- helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "short", "has spaces in it 1234567", "x" * 257, "token/with/slashes/123456", "ü" * 20],
)
def test_check_authtoken_refuses_without_echoing(value):
    with pytest.raises(ValueError) as error:
        ra.check_authtoken(value)
    assert value not in str(error.value) or value == ""


def test_check_authtoken_accepts_ngrok_tokens():
    assert ra.check_authtoken(f"  {FAKE_AUTHTOKEN}\n") == FAKE_AUTHTOKEN
    assert ra.check_authtoken("2abcDEF_ghiJKL-mno.pqrSTU") == "2abcDEF_ghiJKL-mno.pqrSTU"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("Example.NGROK-free.app", "example.ngrok-free.app"),
        ("https://example.ngrok.app/", "example.ngrok.app"),
        ("http://rootmind.example.com", "rootmind.example.com"),
    ],
)
def test_normalize_domain(value, expected):
    assert ra.normalize_domain(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "example.com:443",
        "example.com/mcp",
        "-bad.example.com",
        "a b.com",
        "exa_mple.com",
    ],
)
def test_normalize_domain_refuses(value):
    with pytest.raises(ValueError, match="host name"):
        ra.normalize_domain(value)


def test_redact():
    assert (
        ra.redact(f"bad token {FAKE_AUTHTOKEN}\nERR", FAKE_AUTHTOKEN) == "bad token [authtoken] ERR"
    )
    assert len(ra.redact("x" * 2000, None)) == ra.MAX_ERROR_CHARS


def stamp(seconds_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_status(state: str, *, url=None, error=None, updated=None) -> dict:
    return {"state": state, "url": url, "error": error, "updated": updated or stamp()}


def test_effective_status():
    effective = ra.effective_status
    connected = file_status(ra.CONNECTED, url="https://a.ngrok.app")
    assert effective(False, connected)["state"] == ra.OFF
    assert effective(False, connected)["url"] is None
    assert effective(True, None)["state"] == ra.UNAVAILABLE
    assert effective(True, connected) == {**connected, "error": None}
    assert effective(True, file_status(ra.OFF))["state"] == ra.CONNECTING
    assert effective(True, file_status(ra.STOPPED))["state"] == ra.UNAVAILABLE
    stale = file_status(
        ra.CONNECTED, url="https://a.ngrok.app", updated=stamp(ra.STALE_SECONDS + 5)
    )
    assert effective(True, stale)["state"] == ra.UNAVAILABLE
    never = {**file_status(ra.OFF), "updated": None}
    assert effective(True, never)["state"] == ra.UNAVAILABLE
    failed = effective(True, file_status(ra.ERROR, error="ERR_NGROK_105"))
    assert (failed["state"], failed["error"], failed["url"]) == (ra.ERROR, "ERR_NGROK_105", None)


def test_read_status_checks_every_field(tmp_path):
    files = ra.RemoteAccessFiles(tmp_path)
    (tmp_path / "data").mkdir()
    assert files.read_status() is None
    path = files.status_path
    path.write_text(json.dumps(file_status(ra.CONNECTED, url="http://plain.example")), "utf-8")
    assert files.read_status()["url"] is None  # only https addresses
    path.write_text(json.dumps({"state": "hacked"}), "utf-8")
    assert files.read_status() is None
    path.write_text("{not json", "utf-8")
    assert files.read_status() is None
    path.write_bytes(b" " * (ra.MAX_STATUS_BYTES + 1))
    assert files.read_status() is None
    path.write_text(json.dumps(file_status(ra.ERROR, error="a\x00b" + "c" * 900)), "utf-8")
    assert len(files.read_status()["error"]) == ra.MAX_ERROR_CHARS


def test_private_files(tmp_path):
    files = ra.RemoteAccessFiles(tmp_path)
    assert not files.authtoken_saved()
    files.save_authtoken(FAKE_AUTHTOKEN)
    assert files.authtoken_saved()
    assert files.authtoken_path.read_text(encoding="utf-8") == FAKE_AUTHTOKEN
    files.save_authtoken(OTHER_AUTHTOKEN)
    assert files.authtoken_path.read_text(encoding="utf-8") == OTHER_AUTHTOKEN
    files.prepare_status()
    assert json.loads(files.status_path.read_text(encoding="utf-8"))["state"] == ra.OFF
    files.status_path.write_text(json.dumps(file_status(ra.CONNECTED)), encoding="utf-8")
    files.prepare_status()  # an existing status stays
    assert json.loads(files.status_path.read_text(encoding="utf-8"))["state"] == ra.CONNECTED
    files.clear_authtoken()
    files.clear_authtoken()
    assert not files.authtoken_saved()


def test_the_real_sdk_is_installed():
    """The ngrok SDK is a dependency (only imported; no tunnel)."""
    available, version = ra.sdk_info()
    assert available and version
