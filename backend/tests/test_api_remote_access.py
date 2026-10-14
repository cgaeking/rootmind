"""GET/POST /remote-access: settings, the write-only authtoken, the tunnel's status.

The backend never opens a tunnel itself (the MCP service does), so these tests
need no ngrok SDK fake: the status file is written here as the MCP service
would write it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
import yaml
from fake_ngrok import FAKE_AUTHTOKEN, OTHER_AUTHTOKEN
from helpers import TEST_TOKEN, rest_client

from rootmind import remote_access as ra

DOMAIN = "rootmind-test.ngrok-free.app"


def saved_config(home) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def write_status(home, state: str, **fields) -> None:
    payload = {
        "state": state,
        "url": None,
        "error": None,
        "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **fields,
    }
    ra.status_path(home).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def secrets_log(caplog):
    caplog.set_level(logging.DEBUG)
    yield caplog
    for token in (FAKE_AUTHTOKEN, OTHER_AUTHTOKEN):
        assert token not in caplog.text


def test_off_by_default(api, rlm_home):
    body = api.get("/remote-access").json()
    assert body["enabled"] is False
    assert body["domain"] is None
    assert body["authtoken_saved"] is False
    assert body["status"]["state"] == "off"
    assert body["sdk"]["available"] is True and body["sdk"]["version"]


def test_turning_it_on_needs_an_authtoken(api, rlm_home):
    response = api.post("/remote-access", json={"enabled": True})
    assert response.status_code == 400
    assert "authtoken" in response.json()["detail"]
    assert "remote_access" not in saved_config(rlm_home)


def test_saving_the_authtoken_and_turning_it_on(api, rlm_home, secrets_log):
    response = api.post(
        "/remote-access",
        json={"enabled": True, "authtoken": FAKE_AUTHTOKEN, "domain": f"https://{DOMAIN}/"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert FAKE_AUTHTOKEN not in response.text
    assert body["enabled"] is True and body["domain"] == DOMAIN
    assert body["authtoken_saved"] is True
    # The MCP service has not reported yet.
    assert body["status"]["state"] == "unavailable"

    # enabled and domain in config.yaml; the authtoken only in its own file.
    assert saved_config(rlm_home)["remote_access"] == {"enabled": True, "domain": DOMAIN}
    assert FAKE_AUTHTOKEN not in (rlm_home / "config.yaml").read_text(encoding="utf-8")
    assert ra.authtoken_path(rlm_home).read_text(encoding="utf-8") == FAKE_AUTHTOKEN
    # The status file exists for the MCP service to write.
    assert ra.status_path(rlm_home).is_file()
    # Other settings are kept.
    assert saved_config(rlm_home)["connectors"][0]["name"] == "docs"

    again = api.get("/remote-access")
    assert FAKE_AUTHTOKEN not in again.text
    assert again.json()["enabled"] is True
    assert FAKE_AUTHTOKEN not in api.get("/config").text
    assert "Remote access: authtoken saved, turned on, domain" in secrets_log.text


def test_the_status_comes_from_the_mcp_service(api, rlm_home):
    api.post("/remote-access", json={"enabled": True, "authtoken": FAKE_AUTHTOKEN})
    write_status(rlm_home, "off")
    assert api.get("/remote-access").json()["status"]["state"] == "connecting"
    write_status(rlm_home, "connected", url=f"https://{DOMAIN}")
    status = api.get("/remote-access").json()["status"]
    assert (status["state"], status["url"]) == ("connected", f"https://{DOMAIN}")
    write_status(rlm_home, "error", error="ERR_NGROK_105: authentication failed")
    status = api.get("/remote-access").json()["status"]
    assert (status["state"], status["error"]) == ("error", "ERR_NGROK_105: authentication failed")
    write_status(rlm_home, "stopped")
    assert api.get("/remote-access").json()["status"]["state"] == "unavailable"

    api.post("/remote-access", json={"enabled": False})
    write_status(rlm_home, "connected", url=f"https://{DOMAIN}")
    status = api.get("/remote-access").json()["status"]
    assert (status["state"], status["url"]) == ("off", None)


def test_turning_it_off_keeps_the_authtoken(api, rlm_home):
    api.post("/remote-access", json={"enabled": True, "authtoken": FAKE_AUTHTOKEN})
    body = api.post("/remote-access", json={"enabled": False}).json()
    assert body["enabled"] is False and body["authtoken_saved"] is True
    assert saved_config(rlm_home)["remote_access"]["enabled"] is False
    # And on again without sending it.
    assert api.post("/remote-access", json={"enabled": True}).json()["enabled"] is True


def test_a_new_authtoken_replaces_the_old_one(api, rlm_home, secrets_log):
    api.post("/remote-access", json={"authtoken": FAKE_AUTHTOKEN})
    response = api.post("/remote-access", json={"authtoken": f" {OTHER_AUTHTOKEN}\n"})
    assert response.status_code == 200
    assert ra.authtoken_path(rlm_home).read_text(encoding="utf-8") == OTHER_AUTHTOKEN
    assert response.json()["enabled"] is False  # saving it does not turn it on


@pytest.mark.parametrize("cleared", ["", None])
def test_removing_the_authtoken_turns_remote_access_off(api, rlm_home, cleared):
    api.post("/remote-access", json={"enabled": True, "authtoken": FAKE_AUTHTOKEN})
    body = api.post("/remote-access", json={"authtoken": cleared}).json()
    assert body["authtoken_saved"] is False and body["enabled"] is False
    assert not ra.authtoken_path(rlm_home).exists()
    assert saved_config(rlm_home)["remote_access"]["enabled"] is False


def test_removing_the_authtoken_while_turning_it_on_is_refused(api, rlm_home):
    api.post("/remote-access", json={"authtoken": FAKE_AUTHTOKEN})
    response = api.post("/remote-access", json={"enabled": True, "authtoken": ""})
    assert response.status_code == 400
    assert ra.authtoken_path(rlm_home).exists()


@pytest.mark.parametrize(
    "token", ["too-short", "with spaces in the middle of it", "a" * 300, "token/with/slash/1234"]
)
def test_an_invalid_authtoken_is_refused_without_echoing_it(api, rlm_home, token, caplog):
    caplog.set_level(logging.DEBUG)
    response = api.post("/remote-access", json={"enabled": True, "authtoken": token})
    assert response.status_code == 400
    assert token not in response.text and token not in caplog.text
    assert not ra.authtoken_path(rlm_home).exists()


def test_the_domain_is_checked_and_can_be_removed(api, rlm_home):
    response = api.post("/remote-access", json={"domain": "example.com/mcp"})
    assert response.status_code == 400
    assert "host name" in response.json()["detail"]
    assert api.post("/remote-access", json={"domain": DOMAIN.upper()}).json()["domain"] == DOMAIN
    assert api.post("/remote-access", json={"domain": ""}).json()["domain"] is None
    assert saved_config(rlm_home)["remote_access"]["domain"] is None


def test_a_change_without_fields_changes_nothing(api, rlm_home):
    api.post(
        "/remote-access", json={"enabled": True, "authtoken": FAKE_AUTHTOKEN, "domain": DOMAIN}
    )
    body = api.post("/remote-access", json={}).json()
    assert (body["enabled"], body["domain"], body["authtoken_saved"]) == (True, DOMAIN, True)


def test_a_form_post_is_refused(api, rlm_home):
    """Like POST /config: a web page cannot send JSON cross-origin without a preflight."""
    response = api.post(
        "/remote-access",
        content="enabled=true",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    assert "remote_access" not in saved_config(rlm_home)


def test_the_routes_need_the_rest_token(rlm_home):
    from rootmind.api.app import create_app

    with rest_client(create_app(api_token=TEST_TOKEN)) as client:
        assert client.get("/remote-access").status_code == 401
        refused = client.post("/remote-access", json={"authtoken": FAKE_AUTHTOKEN})
        assert refused.status_code == 401
        assert not ra.authtoken_path(rlm_home).exists()
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        assert client.get("/remote-access", headers=headers).status_code == 200


class SpyProtection:
    """Records how the files are written (the Windows service passes its layout here)."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def write_secret(self, path, text):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("write_secret", path.name))
        path.write_text(text, encoding="utf-8")

    def share_status(self, path):
        self.calls.append(("share_status", path.name))
        path.write_text('{"state": "off"}', encoding="utf-8")


def test_the_files_are_written_through_the_given_protection(rlm_home):
    from rootmind.api.app import create_app

    spy = SpyProtection()
    files = ra.RemoteAccessFiles(rlm_home, spy)
    (rlm_home / "data").mkdir(exist_ok=True)
    with rest_client(create_app(remote_access_files=files)) as client:
        response = client.post(
            "/remote-access", json={"enabled": True, "authtoken": FAKE_AUTHTOKEN}
        )
    assert response.status_code == 200
    assert spy.calls == [
        ("write_secret", ra.AUTHTOKEN_FILE_NAME),
        ("share_status", ra.STATUS_FILE_NAME),
    ]


def test_a_failed_save_reports_no_secret(rlm_home, caplog):
    from rootmind.api.app import create_app

    caplog.set_level(logging.DEBUG)
    spy = SpyProtection(fail=OSError(f"cannot write {FAKE_AUTHTOKEN}"))
    files = ra.RemoteAccessFiles(rlm_home, spy)
    with rest_client(create_app(remote_access_files=files)) as client:
        response = client.post(
            "/remote-access", json={"enabled": True, "authtoken": FAKE_AUTHTOKEN}
        )
    assert response.status_code == 500
    assert FAKE_AUTHTOKEN not in response.text
    assert FAKE_AUTHTOKEN not in caplog.text
    assert "cannot write [authtoken]" in caplog.text
    assert "remote_access" not in saved_config(rlm_home)
