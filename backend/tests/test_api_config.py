"""GET/POST /config: key masking, round trips, provider validation, live reload, folder removal."""

from __future__ import annotations

import json
import os
import stat

import anthropic
import httpx
import openai
import pytest
import yaml
from helpers import OTHER_TEST_KEY, SDK_TEST_KEY, TEST_API_KEY, rest_client, sync_app_state

from rootmind import config as config_module
from rootmind.api import routes
from rootmind.config import check_api_key, is_masked, mask_secret

MASKED = "…abcd"


def _saved(rlm_home) -> dict:
    return yaml.safe_load((rlm_home / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def models_spy(monkeypatch):
    """Replace the provider call behind /llm/models with a recorder."""
    seen: list[tuple] = []

    def fake_list(provider, api_key):
        seen.append((provider, api_key))
        return [f"{provider}-model"]

    monkeypatch.setattr(routes, "_list_chat_models", fake_list)
    return seen


# --- masking ---------------------------------------------------------------------------------


def test_mask_helpers():
    assert mask_secret(TEST_API_KEY) == MASKED
    assert mask_secret("") == ""
    assert mask_secret(None) == ""
    assert mask_secret("short") == "…"  # short secrets reveal nothing
    assert is_masked(MASKED) and is_masked("…")
    assert not is_masked(TEST_API_KEY)
    assert not is_masked("…" + "x" * 10)  # longer than a mask: not a mask
    assert not is_masked(None)
    check_api_key(MASKED)
    check_api_key(TEST_API_KEY)
    check_api_key(None)
    with pytest.raises(ValueError, match="paste the whole key"):
        check_api_key(MASKED + "sk-test-pasted-after-the-mask")


def test_key_pasted_after_the_mask_is_rejected(api, rlm_home):
    """Regression: pasting into a field that still shows the mask stored a corrupt key."""
    body = api.get("/config").json()
    body["llm"]["api_keys"]["anthropic"] = MASKED + "sk-test-new-key-0000"

    response = api.post("/config", json=body)

    assert response.status_code == 400
    assert "paste the whole key" in response.json()["detail"]
    assert _saved(rlm_home)["llm"]["api_keys"] == {"anthropic": TEST_API_KEY}
    corrupt = {"provider": "anthropic", "api_key": MASKED + "x" * 20}
    assert api.post("/llm/models", json=corrupt).status_code == 400


def test_get_config_never_returns_a_plaintext_key(api):
    body = api.get("/config").json()

    assert TEST_API_KEY not in json.dumps(body)
    assert TEST_API_KEY[:-4] not in json.dumps(body)
    assert body["llm"]["api_keys"] == {"anthropic": MASKED}
    assert body["llm"]["api_key"] == MASKED


def test_environment_keys_are_masked_and_not_persisted(rlm_home, monkeypatch):
    env_key = "env-only-fake-openai-key-wxyz"  # pragma: allowlist secret
    monkeypatch.setenv("OPENAI_API_KEY", env_key)
    from rootmind.api.app import create_app

    with rest_client(create_app()) as client:
        body = client.get("/config").json()
        assert body["llm"]["api_keys"]["openai"] == "…wxyz"
        assert env_key not in json.dumps(body)
        assert client.post("/config", json=body).status_code == 200

    stored = _saved(rlm_home)["llm"]["api_keys"]
    assert stored == {"anthropic": TEST_API_KEY}  # the env key stays in the environment


# --- round trips -----------------------------------------------------------------------------


def test_masked_round_trip_keeps_the_key(api, rlm_home):
    body = api.get("/config").json()
    body["llm"]["model"] = "claude-test-model"

    response = api.post("/config", json=body)

    assert response.json() == {"ok": True, "reloaded": True, "restart_required": False}
    saved = _saved(rlm_home)
    assert saved["llm"]["api_keys"] == {"anthropic": TEST_API_KEY}
    assert saved["llm"]["api_key"] == TEST_API_KEY
    assert saved["llm"]["model"] == "claude-test-model"
    assert "…" not in (rlm_home / "config.yaml").read_text(encoding="utf-8")
    assert (rlm_home / "config.yaml.bak").exists()
    assert api.get("/config").json()["llm"]["model"] == "claude-test-model"
    assert not list(rlm_home.glob(".*.tmp"))  # the atomic write cleaned up


@pytest.mark.skipif(os.name != "posix", reason="mode bits; Windows uses the folder's ACL")
def test_saved_config_is_owner_only(api, rlm_home):
    api.post("/config", json=api.get("/config").json())
    for name in ("config.yaml", "config.yaml.bak"):
        assert stat.S_IMODE((rlm_home / name).stat().st_mode) == 0o600


def test_failed_write_keeps_the_previous_config(api, rlm_home, monkeypatch):
    before = (rlm_home / "config.yaml").read_text(encoding="utf-8")
    body = api.get("/config").json()
    body["llm"]["model"] = "never-written"

    def disk_full(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(config_module.os, "replace", disk_full)
    assert api.post("/config", json=body).status_code == 400

    assert (rlm_home / "config.yaml").read_text(encoding="utf-8") == before
    assert not list(rlm_home.glob(".*.tmp"))


def test_masked_single_api_key_keeps_the_key(api, rlm_home):
    body = api.get("/config").json()
    del body["llm"]["api_keys"]  # an older client that only knows api_key

    assert api.post("/config", json=body).status_code == 200

    assert _saved(rlm_home)["llm"]["api_keys"] == {"anthropic": TEST_API_KEY}


def test_new_key_replaces_and_empty_key_removes(api, rlm_home):
    body = api.get("/config").json()
    body["llm"]["api_keys"] = {"anthropic": "sk-test-replacement-key-9876", "openai": ""}
    api.post("/config", json=body)
    assert _saved(rlm_home)["llm"]["api_keys"] == {"anthropic": "sk-test-replacement-key-9876"}
    assert api.get("/config").json()["llm"]["api_keys"] == {"anthropic": "…9876"}

    body = api.get("/config").json()
    body["llm"]["api_keys"]["anthropic"] = ""
    api.post("/config", json=body)
    saved = _saved(rlm_home)["llm"]
    assert saved.get("api_keys") == {}
    assert saved.get("api_key") is None


def test_switching_provider_keeps_each_providers_key(api, rlm_home):
    body = api.get("/config").json()
    body["llm"]["provider"] = "openai"
    body["llm"]["api_keys"]["openai"] = OTHER_TEST_KEY
    api.post("/config", json=body)

    saved = _saved(rlm_home)["llm"]
    assert saved["api_keys"] == {
        "anthropic": TEST_API_KEY,
        "openai": OTHER_TEST_KEY,
    }
    assert saved["api_key"] == OTHER_TEST_KEY  # mirrors the active provider


def test_partial_update_keeps_folders_and_keys(api, rlm_home):
    """Regression: omitted sections used to be reset, wiping all folders and stored keys."""
    sync_app_state()
    before = api.get("/statistics").json()["indexed_documents"]

    response = api.post("/config", json={"llm": {"model": "claude-other-model"}})

    assert response.status_code == 200
    saved = _saved(rlm_home)
    assert saved["llm"]["model"] == "claude-other-model"
    assert saved["llm"]["provider"] == "anthropic"
    assert saved["llm"]["api_keys"] == {"anthropic": TEST_API_KEY}
    assert [c["name"] for c in saved["connectors"]] == ["docs"]
    assert api.get("/statistics").json()["indexed_documents"] == before == 7


def test_config_path_from_init_config_is_used_for_saves(tmp_path, tiny_corpus):
    from rootmind.api.app import create_app

    custom = tmp_path / "custom.yaml"
    custom.write_text(yaml.safe_dump({"llm": {"model": "m1"}}), encoding="utf-8")
    config_module.init_config(str(custom))

    with rest_client(create_app()) as client:
        body = client.get("/config").json()
        assert body["llm"]["model"] == "m1"
        body["llm"]["model"] = "m2"
        client.post("/config", json=body)
        assert client.get("/config").json()["llm"]["model"] == "m2"

    assert yaml.safe_load(custom.read_text(encoding="utf-8"))["llm"]["model"] == "m2"
    assert not (config_module.app_home() / "config.yaml").exists()


# --- validation -----------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["ollama", "gemini", ""])
def test_unknown_provider_rejected(api, rlm_home, provider):
    body = api.get("/config").json()
    body["llm"]["provider"] = provider

    assert api.post("/config", json=body).status_code == 422
    assert api.post("/llm/models", json={"provider": provider}).status_code == 422
    assert _saved(rlm_home)["llm"]["provider"] == "anthropic"


def test_invalid_body_rejected(api):
    assert api.post("/config", json={"connectors": [{"path": "/x"}]}).status_code == 422
    assert api.post("/config", json={"llm": {}, "indexer": {"include": "x"}}).status_code == 422


# --- /llm/models -------------------------------------------------------------------------------


@pytest.mark.parametrize("sent_key", [MASKED, None, ""])
def test_models_use_the_stored_key_for_masked_or_missing_keys(api, models_spy, sent_key):
    response = api.post("/llm/models", json={"provider": "anthropic", "api_key": sent_key})

    assert response.json() == {"provider": "anthropic", "models": ["anthropic-model"]}
    assert models_spy == [("anthropic", TEST_API_KEY)]


def test_models_with_a_new_key_and_other_provider(api, models_spy):
    api.post("/llm/models", json={"provider": "xai", "api_key": SDK_TEST_KEY})
    no_key = api.post("/llm/models", json={"provider": "openai"})  # no stored openai key
    api.post("/llm/models")

    assert no_key.status_code == 400
    assert no_key.json()["detail"] == "No API key for provider openai"
    assert models_spy == [
        ("xai", SDK_TEST_KEY),
        ("anthropic", TEST_API_KEY),  # defaults to the configured provider
    ]


_REQUEST = httpx.Request("GET", "http://127.0.0.1:9/v1/models")


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (
            anthropic.AuthenticationError(
                "invalid x-api-key", response=httpx.Response(401, request=_REQUEST), body=None
            ),
            502,
            "The LLM provider (anthropic) returned HTTP 401; check the API key for anthropic",
        ),
        (
            openai.APIConnectionError(request=_REQUEST),
            502,
            "Could not reach the LLM provider (anthropic)",
        ),
        (anthropic.APITimeoutError(request=_REQUEST), 504, "LLM request timed out"),
        (RuntimeError("bug in the client code"), 500, "Listing models failed; see the backend log"),
    ],
)
def test_models_provider_errors_are_mapped(api, monkeypatch, error, status, detail):
    def failing(provider, api_key):
        raise error

    monkeypatch.setattr(routes, "_list_chat_models", failing)
    response = api.post("/llm/models", json={"provider": "anthropic"})

    assert response.status_code == status
    assert response.json()["detail"] == detail


# --- folders ---------------------------------------------------------------------------------


def test_remove_folder_hides_docs(api, rlm_home, tmp_path):
    from rootmind.api.app import app_state

    other = tmp_path / "other"
    other.mkdir()
    (other / "attic.md").write_text("The silver lantern is in the attic.", encoding="utf-8")
    body = api.get("/config").json()
    body["connectors"].append({"name": "other", "type": "local", "path": str(other)})
    assert api.post("/config", json=body).json()["reloaded"] is True
    sync_app_state()
    assert api.get("/statistics").json()["indexed_documents"] == 8
    attic_id = api.post("/search", json={"query": "attic"}).json()["results"][0]["doc_id"]

    body["connectors"] = [c for c in body["connectors"] if c["name"] != "other"]
    assert api.post("/config", json=body).json()["reloaded"] is True

    # Gone right after the reload, before any new sync.
    assert api.get("/statistics").json()["indexed_documents"] == 7
    assert api.post("/search", json={"query": "attic"}).json()["results"] == []
    assert api.get(f"/documents/{attic_id}").status_code == 404
    assert "other" not in api.get("/status").json()["connectors"]
    assert set(app_state.connectors) == {"docs"}


def test_failed_reload_keeps_the_previous_state(api, monkeypatch):
    from rootmind.api import app as app_module

    before = app_module.app_state.sync_manager

    def broken(*args, **kwargs):
        raise RuntimeError("cannot open the database")

    monkeypatch.setattr(app_module, "_new_state", broken)
    body = api.get("/config").json()
    body["llm"]["model"] = "claude-not-applied-yet"

    response = api.post("/config", json=body).json()

    assert response["reloaded"] is False and response["restart_required"] is True
    assert app_module.app_state.sync_manager is before
    assert api.get("/status").status_code == 200
