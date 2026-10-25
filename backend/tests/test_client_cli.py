"""The client commands (``rootmind status``, ``folders``, ``index``, ``search``, ``ask``,
``config llm``, ``mcp tokens``, ``remote-access``) against a fake REST API.

No real service is looked up (see the ``no_real_service`` fixture): the
commands get ``--url`` and ``--token-file``, or a fake probe that stands for
``service status``, the service manager and the listener table.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from fake_rest import FAKE_REST_TOKEN, STORED_KEY, FakeRestServer
from helpers import TEST_API_KEY, rest_client

from rootmind.client import commands, discovery
from rootmind.client.rest import ClientError, ClientExit, RestClient, _detail, check_url
from rootmind.main import build_parser, main

NEW_KEY = "sk-fake-new-key-from-file-1234"  # pragma: allowlist secret
NGROK_TOKEN = "fake_ngrok_authtoken_for_cli_tests_01"  # pragma: allowlist secret
MCP_TOKEN = "fake-mcp-token-for-cli-tests"  # pragma: allowlist secret


@pytest.fixture
def fake():
    with FakeRestServer() as server:
        yield server


@pytest.fixture
def token_file(tmp_path) -> Path:
    path = tmp_path / "rest_token.txt"
    path.write_text(FAKE_REST_TOKEN + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(commands, "POLL_SECONDS", 0)


def cli(capsys, *argv: str, stdin: str | None = None, monkeypatch=None) -> tuple[int, str, str]:
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        main(list(argv))
        code = 0
    except SystemExit as e:
        code = int(e.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def remote(fake: FakeRestServer, token_file: Path) -> list[str]:
    return ["--url", fake.url, "--token-file", str(token_file)]


class FakeProbe:
    """``service status``, the service manager and the listener table of a fake machine."""

    platform = "linux"

    def __init__(
        self,
        port: int,
        token_file: Path,
        *,
        installed: bool = True,
        ours: bool = True,
        state: str | None = "running",
        pid: int | None = 4242,
        owner_pid: int = 4242,
        mcp_token_file: Path | None = None,
    ) -> None:
        self.port = port
        self.token_file = token_file
        self.installed = installed
        self.ours = ours
        self.state = state
        self.pid = pid
        self.owner_pid = owner_pid
        self.mcp_token_file = mcp_token_file
        self.ownership_checks: list[tuple[int, int]] = []

    def status(self) -> dict[str, Any]:
        mcp = {"installed": False, "state": None, "port": None, "token_file": None}
        if self.mcp_token_file is not None:
            mcp = {
                "installed": True,
                "state": "running",
                "port": 18400,
                "token_file": str(self.mcp_token_file),
                "token_readable": True,
            }
        return {
            "installed": self.installed,
            "ours": self.ours,
            "state": self.state if self.installed else None,
            "port": self.port if self.ours else None,
            "version": "9.9.9",
            "token_file": str(self.token_file) if self.ours else None,
            "token_readable": True,
            "mcp_http": mcp,
            "warnings": [],
        }

    def backend_pid(self) -> int | None:
        return self.pid

    def port_owned_by(self, port: int, pid: int) -> bool:
        self.ownership_checks.append((port, pid))
        return port == self.port and pid == self.owner_pid


@pytest.fixture
def use_probe(monkeypatch):
    def install(probe: FakeProbe) -> FakeProbe:
        monkeypatch.setattr(discovery, "default_probe", lambda: probe)
        monkeypatch.setattr(commands, "default_probe", lambda: probe)
        return probe

    return install


def no_secret_in(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text
        assert secret[-4:] not in text  # not even the masked form


# --- addresses and the REST client ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000", "http://localhost:8000/", "http://[::1]:8000", "https://rm.example"],
)
def test_accepted_addresses(url):
    assert check_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.5:8000",  # the token would cross the network in clear text
        "http://rootmind.example",
        "ftp://127.0.0.1",
        "127.0.0.1:8000",
        "http://user:pw@127.0.0.1:8000",  # pragma: allowlist secret
        "http://127.0.0.1:8000/?x=1",
    ],
)
def test_refused_addresses(url):
    with pytest.raises(ClientError) as exc:
        check_url(url)
    assert exc.value.code == ClientExit.USAGE


def test_plain_http_to_another_host_is_refused_before_any_request(capsys, token_file):
    code, out, err = cli(
        capsys, "status", "--url", "http://10.0.0.7:8000", "--token-file", str(token_file)
    )
    assert code == ClientExit.USAGE
    assert "unencrypted" in err


def test_the_client_sends_the_token_and_never_shows_it(fake, token_file):
    client = RestClient(fake.url, FAKE_REST_TOKEN)
    assert client.get("/statistics")["indexed_documents"] == 11
    assert fake.requests[-1].authorized
    assert FAKE_REST_TOKEN not in repr(client)


def test_a_wrong_token_is_exit_4(fake):
    with pytest.raises(ClientError) as exc:
        RestClient(fake.url, "wrong-token").get("/statistics")
    assert exc.value.code == ClientExit.NOT_ALLOWED
    assert "wrong-token" not in exc.value.message


def test_redirects_are_not_followed(fake):
    fake.state.redirect_paths.add("/statistics")
    with pytest.raises(ClientError) as exc:
        RestClient(fake.url, FAKE_REST_TOKEN).get("/statistics")
    assert exc.value.status == 307
    assert [r.path for r in fake.requests] == ["/statistics"]


def test_no_answer_is_exit_3():
    with pytest.raises(ClientError) as exc:
        RestClient("http://127.0.0.1:9", None, timeout=2).get("/")
    assert exc.value.code == ClientExit.NO_SERVICE


def test_validation_errors_name_fields_not_values():
    payload = {"detail": [{"loc": ["body", "llm", "api_key"], "msg": "bad", "input": "sk-x"}]}
    assert _detail(payload) == "llm.api_key: bad"


# --- discovery ---------------------------------------------------------------------


def test_status_through_the_discovered_service(capsys, fake, token_file, tmp_path, use_probe):
    mcp_file = tmp_path / "api_token.txt"
    mcp_file.write_text(MCP_TOKEN, encoding="utf-8")
    probe = use_probe(FakeProbe(fake.port, token_file, mcp_token_file=mcp_file))

    code, out, _err = cli(capsys, "status", "--json")

    assert code == 0
    data = json.loads(out)
    assert data["service"]["state"] == "running"
    assert data["api"] == {"url": f"http://127.0.0.1:{fake.port}", "version": "9.9.9"}
    assert data["index"]["documents"] == 11
    assert data["folders"][0]["name"] == "docs"
    assert data["mcp_http"]["url"] == "http://127.0.0.1:18400/mcp"
    assert data["remote_access"]["enabled"] is False
    # Checked at discovery, and again before every request that carries the token.
    assert set(probe.ownership_checks) == {(fake.port, 4242)}
    assert len(probe.ownership_checks) == len(fake.requests) + 1
    assert all(r.authorized for r in fake.requests)
    no_secret_in(out, FAKE_REST_TOKEN, MCP_TOKEN)


def test_status_text(capsys, fake, token_file, use_probe):
    use_probe(FakeProbe(fake.port, token_file))
    code, out, _err = cli(capsys, "status")
    assert code == 0
    assert "RootMind service: running (version 9.9.9)" in out
    assert "11 documents, 1 with errors" in out
    assert "docs: /srv/docs" in out
    assert "MCP:       off" in out


def test_no_token_goes_to_another_program_on_the_port(capsys, fake, token_file, use_probe):
    use_probe(FakeProbe(fake.port, token_file, owner_pid=999))
    code, out, err = cli(capsys, "folders", "list")
    assert code == ClientExit.NO_SERVICE
    assert "no token was sent" in err
    assert fake.requests == []


def test_no_token_after_the_port_changed_hands_while_waiting(
    capsys, fake, token_file, use_probe, monkeypatch
):
    # index --wait polls for as long as indexing runs. If the service restarts
    # meanwhile and another program takes its port, the next poll must not
    # carry the token there.
    probe = use_probe(FakeProbe(fake.port, token_file))
    fake.state.running_polls = 5
    real_owned = probe.port_owned_by

    def owned(port: int, pid: int) -> bool:
        if len([r for r in fake.requests if r.path == "/index/status"]) >= 2:
            probe.owner_pid = 999  # the service restarted; another process holds the port
        return real_owned(port, pid)

    probe.port_owned_by = owned
    monkeypatch.delenv("RLM_HOME", raising=False)  # index goes to the service
    code, _out, err = cli(capsys, "index", "--wait")
    assert code == ClientExit.NO_SERVICE
    assert "no token was sent" in err
    polls = [r for r in fake.requests if r.path == "/index/status"]
    assert len(polls) == 2  # nothing after the port changed hands
    assert all(r.authorized for r in fake.requests)


def test_no_token_without_a_service_pid(capsys, fake, token_file, use_probe):
    use_probe(FakeProbe(fake.port, token_file, pid=None))
    code, _out, _err = cli(capsys, "search", "x")
    assert code == ClientExit.NO_SERVICE
    assert fake.requests == []


@pytest.mark.parametrize(
    ("probe_options", "message"),
    [
        ({"installed": False}, "not set up for this account"),
        ({"ours": False}, "RootMind did not set it up"),
        ({"state": "stopped"}, "systemctl --user start rootmind-backend"),
    ],
)
def test_no_usable_service(capsys, fake, token_file, use_probe, probe_options, message):
    use_probe(FakeProbe(fake.port, token_file, **probe_options))
    code, out, err = cli(capsys, "status", "--json")
    assert code == ClientExit.NO_SERVICE
    data = json.loads(out)
    assert message in data["error"]["message"]
    assert data["service"]["installed"] is probe_options.get("installed", True)
    assert fake.requests == []


def test_stopped_windows_service_hint(capsys, fake, token_file, use_probe):
    probe = FakeProbe(fake.port, token_file, state="stopped")
    probe.platform = "windows"
    use_probe(probe)
    code, _out, err = cli(capsys, "folders", "list")
    assert code == ClientExit.NO_SERVICE
    assert "Start-Service RootMind-Backend" in err


def test_unreadable_token_names_the_account(capsys, fake, token_file, use_probe, monkeypatch):
    use_probe(FakeProbe(fake.port, token_file))
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        if self == token_file:
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    code, _out, err = cli(capsys, "folders", "list")
    assert code == ClientExit.NOT_ALLOWED
    assert "cannot read the REST token file" in err
    assert "not with sudo" in err
    assert fake.requests == []


def test_windows_hint_for_an_unreadable_token(fake, token_file, monkeypatch):
    probe = FakeProbe(fake.port, token_file)
    probe.platform = "windows"
    monkeypatch.setattr(
        discovery,
        "read_token_file",
        lambda path, what="": (_ for _ in ()).throw(
            ClientError("this account cannot read it", ClientExit.NOT_ALLOWED)
        ),
    )
    with pytest.raises(ClientError) as exc:
        discovery.discover(probe=probe)
    assert "account that set up the RootMind service" in exc.value.message


def test_url_from_the_environment(capsys, fake, token_file, monkeypatch):
    monkeypatch.setenv("RLM_URL", fake.url)
    monkeypatch.setenv("RLM_TOKEN_FILE", str(token_file))
    code, out, _err = cli(capsys, "folders", "list", "--json")
    assert code == 0
    assert json.loads(out)["folders"][0]["name"] == "docs"


def test_a_token_file_with_two_tokens_is_refused(capsys, fake, tmp_path):
    bad = tmp_path / "token.txt"
    bad.write_text("one two", encoding="utf-8")
    code, _out, err = cli(capsys, "status", "--url", fake.url, "--token-file", str(bad))
    assert code == ClientExit.USAGE
    assert "one token" in err


def test_unsupported_platform_needs_url(monkeypatch):
    monkeypatch.setattr(discovery, "default_probe", lambda: None)
    with pytest.raises(ClientError) as exc:
        discovery.discover()
    assert "--url" in exc.value.message


# --- folders -----------------------------------------------------------------------


def test_folders_list(capsys, fake, token_file):
    code, out, _err = cli(capsys, "folders", "list", *remote(fake, token_file))
    assert code == 0
    assert "docs: /srv/docs  (11 documents, 1 errors)" in out


def test_folders_add_keeps_the_stored_key(capsys, fake, token_file, tmp_path):
    folder = tmp_path / "Shared Projects"
    folder.mkdir()
    code, out, _err = cli(capsys, "folders", "add", str(folder), *remote(fake, token_file))
    assert code == 0
    assert "Added Shared-Projects" in out
    assert fake.state.connectors[-1] == {
        "name": "Shared-Projects",
        "type": "local",
        "path": os.path.normpath(str(folder)),
    }
    # The LLM settings went back masked; the service kept its key.
    post = [r for r in fake.requests if r.method == "POST"][0]
    assert post.body["llm"]["api_keys"] == {"anthropic": "…9999"}
    assert fake.state.llm["api_keys"] == {"anthropic": STORED_KEY}
    no_secret_in(out, STORED_KEY)


def test_folders_add_resolves_a_relative_path(capsys, fake, token_file, tmp_path, monkeypatch):
    (tmp_path / "rel").mkdir()
    monkeypatch.chdir(tmp_path)
    code, _out, _err = cli(
        capsys, "folders", "add", "rel", "--name", "projects", *remote(fake, token_file)
    )
    assert code == 0
    assert fake.state.connectors[-1]["path"] == os.path.normpath(str(tmp_path / "rel"))


@pytest.mark.parametrize(
    ("extra", "message"),
    [(["--name", "docs"], "exists already"), (["--name", "a b"], "may contain only")],
)
def test_folders_add_refusals(capsys, fake, token_file, tmp_path, extra, message):
    code, _out, err = cli(
        capsys, "folders", "add", str(tmp_path), *extra, *remote(fake, token_file)
    )
    assert code == ClientExit.USAGE
    assert message in err
    assert not [r for r in fake.requests if r.method == "POST"]


def test_folders_add_refuses_a_folder_indexed_already(capsys, fake, token_file, tmp_path):
    fake.state.connectors[0]["path"] = str(tmp_path)
    code, _out, err = cli(capsys, "folders", "add", str(tmp_path), *remote(fake, token_file))
    assert code == ClientExit.USAGE
    assert "indexed already as 'docs'" in err


def test_folders_remove(capsys, fake, token_file):
    code, out, _err = cli(capsys, "folders", "remove", "docs", "--json", *remote(fake, token_file))
    assert code == 0
    assert json.loads(out)["removed"] == "docs"
    assert fake.state.connectors == []


def test_folders_remove_unknown(capsys, fake, token_file):
    code, _out, err = cli(capsys, "folders", "remove", "nope", *remote(fake, token_file))
    assert code == ClientExit.USAGE
    assert "no folder named 'nope'" in err


# --- index -------------------------------------------------------------------------


def test_index_starts_and_returns(capsys, fake, token_file):
    code, out, _err = cli(capsys, "index", "--full", "--folder", "docs", *remote(fake, token_file))
    assert code == 0
    assert "Started full indexing of docs" in out
    post = [r for r in fake.requests if r.path == "/index/refresh"][0]
    assert post.body == {"full": True, "connector_name": "docs"}


def test_index_wait_polls_until_idle(capsys, fake, token_file):
    code, out, _err = cli(capsys, "index", "--wait", "--json", *remote(fake, token_file))
    assert code == 0
    data = json.loads(out)
    assert data["waited"] is True
    assert data["status"]["is_running"] is False
    polls = [r for r in fake.requests if r.path == "/index/status"]
    assert len(polls) == 3  # running, running, idle


def test_index_wait_waits_for_a_running_sync_first(capsys, fake, token_file):
    fake.state.refresh_conflicts = 1
    fake.state.running_polls = 1
    code, _out, err = cli(capsys, "index", "--wait", *remote(fake, token_file))
    assert code == 0
    assert "running already; waiting" in err
    assert len([r for r in fake.requests if r.path == "/index/refresh"]) == 2


def test_index_conflict_without_wait(capsys, fake, token_file):
    fake.state.refresh_conflicts = 1
    code, _out, err = cli(capsys, "index", *remote(fake, token_file))
    assert code == ClientExit.FAILED
    assert "--wait waits for it" in err


def test_index_unknown_folder(capsys, fake, token_file):
    code, _out, err = cli(capsys, "index", "--folder", "nope", *remote(fake, token_file))
    assert code == ClientExit.USAGE
    assert "no folder named 'nope'" in err


def _index_args(*argv: str):
    return build_parser().parse_args(["index", *argv])


def test_index_runs_locally_when_asked_or_configured(monkeypatch):
    monkeypatch.delenv("RLM_HOME")
    assert commands.index_runs_locally(_index_args("--local")) == (True, None)
    args = build_parser().parse_args(["--config", "x.yaml", "index"])
    assert commands.index_runs_locally(args) == (True, None)
    monkeypatch.setenv("RLM_HOME", "/somewhere")
    assert commands.index_runs_locally(_index_args()) == (True, None)


def test_index_goes_to_the_service_when_one_is_set_up(monkeypatch, tmp_path):
    monkeypatch.delenv("RLM_HOME")
    probe = FakeProbe(18999, tmp_path / "t")
    assert commands.index_runs_locally(_index_args(), lambda: probe) == (False, None)
    assert commands.index_runs_locally(_index_args("--url", "http://127.0.0.1:1")) == (False, None)


def test_index_without_a_service_runs_locally_with_a_note(monkeypatch, tmp_path):
    monkeypatch.delenv("RLM_HOME")
    probe = FakeProbe(18999, tmp_path / "t", installed=False)
    local, note = commands.index_runs_locally(_index_args(), lambda: probe)
    assert local
    assert "No RootMind service is set up" in note
    assert commands.index_runs_locally(_index_args(), lambda: None) == (True, None)


def test_local_index_without_service_prints_the_note(capsys, monkeypatch, use_probe, tmp_path):
    monkeypatch.delenv("RLM_HOME")
    use_probe(FakeProbe(18999, tmp_path / "t", installed=False))
    (Path.cwd() / "config.yaml").write_text("connectors: []\n", encoding="utf-8")
    code, out, err = cli(capsys, "index")
    assert code == 0
    assert "No connectors configured!" in out
    assert "No RootMind service is set up" in err


def test_local_index_json(capsys, rlm_home):
    code, out, _err = cli(capsys, "index", "--local", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["type"] == "incremental"
    assert data["results"]["docs"]["indexed"] > 0


# --- search and ask ----------------------------------------------------------------


def test_search(capsys, fake, token_file):
    code, out, _err = cli(
        capsys, "search", "total due", "--limit", "5", "--snippets", *remote(fake, token_file)
    )
    assert code == 0
    assert "1. invoice.pdf (Page 2)  [d1]" in out
    assert "Total due: 1,179.36" in out
    body = [r for r in fake.requests if r.path == "/search"][0].body
    assert body == {"query": "total due", "limit": 5, "file_type": None, "with_snippets": True}


def test_search_limit_is_checked(capsys):
    code, _out, err = cli(capsys, "search", "x", "--limit", "0")
    assert code == 2
    assert "from 1 to 1000" in err


def test_ask(capsys, fake, token_file):
    code, out, _err = cli(capsys, "ask", "What is due?", *remote(fake, token_file))
    assert code == 0
    assert out.startswith("The answer to: What is due?")
    assert "invoice.pdf, Page 2 (x/invoice.pdf)" in out


def test_ask_from_stdin(capsys, fake, token_file, monkeypatch):
    code, out, _err = cli(
        capsys,
        "ask",
        "-",
        "--json",
        *remote(fake, token_file),
        stdin="Which tents?\n",
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert json.loads(out)["answer"] == "The answer to: Which tents?"


# --- config llm --------------------------------------------------------------------


def test_config_llm_shows_settings_without_keys(capsys, fake, token_file):
    code, out, _err = cli(capsys, "config", "llm", "--json", *remote(fake, token_file))
    assert code == 0
    assert json.loads(out) == {
        "provider": "anthropic",
        "model": "claude-test",
        "base_url": "http://localhost:11434",
        "keys_saved": ["anthropic"],
    }
    no_secret_in(out, STORED_KEY)


def test_config_llm_key_from_a_file(capsys, fake, token_file, tmp_path):
    key_file = tmp_path / "openai.key"
    key_file.write_text(NEW_KEY + "\n", encoding="utf-8")
    if os.name == "posix":
        key_file.chmod(0o600)
    code, out, err = cli(
        capsys,
        "config",
        "llm",
        "--provider",
        "openai",
        "--model",
        "gpt-test",
        "--key-file",
        str(key_file),
        *remote(fake, token_file),
    )
    assert code == 0
    assert fake.state.llm["provider"] == "openai"
    assert fake.state.llm["model"] == "gpt-test"
    # The new key is saved and the other provider's key is kept.
    assert fake.state.llm["api_keys"] == {"anthropic": STORED_KEY, "openai": NEW_KEY}
    assert "Saved the API key for openai." in out
    no_secret_in(out + err, NEW_KEY, STORED_KEY)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_config_llm_warns_about_a_readable_key_file(capsys, fake, token_file, tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text(NEW_KEY, encoding="utf-8")
    key_file.chmod(0o644)
    code, _out, err = cli(
        capsys, "config", "llm", "--key-file", str(key_file), *remote(fake, token_file)
    )
    assert code == 0
    assert "chmod 600" in err


def test_config_llm_key_from_stdin(capsys, fake, token_file, monkeypatch):
    code, out, err = cli(
        capsys,
        "config",
        "llm",
        "--key-file",
        "-",
        *remote(fake, token_file),
        stdin=NEW_KEY + "\n",
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert fake.state.llm["api_keys"]["anthropic"] == NEW_KEY
    no_secret_in(out + err, NEW_KEY)


def test_config_llm_never_takes_a_key_argument(capsys):
    code, _out, err = cli(capsys, "config", "llm", "--api-key", NEW_KEY)
    assert code == 2
    assert "unrecognized arguments" in err


def test_config_llm_refuses_a_masked_key(capsys, fake, token_file, tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text("…9999", encoding="utf-8")
    code, _out, err = cli(
        capsys, "config", "llm", "--key-file", str(key_file), *remote(fake, token_file)
    )
    assert code == ClientExit.USAGE
    assert "masked value" in err
    assert not [r for r in fake.requests if r.method == "POST"]


def test_config_llm_switch_provider_warns_about_the_model(capsys, fake, token_file):
    code, out, _err = cli(capsys, "config", "llm", "--provider", "xai", *remote(fake, token_file))
    assert code == 0
    assert "the model stays 'claude-test'" in out


def test_config_llm_remove_key(capsys, fake, token_file):
    code, _out, _err = cli(capsys, "config", "llm", "--remove-key", *remote(fake, token_file))
    assert code == 0
    assert fake.state.llm["api_keys"] == {}


def test_config_llm_ollama_has_no_key(capsys, fake, token_file, tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text(NEW_KEY, encoding="utf-8")
    code, _out, err = cli(
        capsys,
        "config",
        "llm",
        "--provider",
        "ollama",
        "--key-file",
        str(key_file),
        *remote(fake, token_file),
    )
    assert code == ClientExit.USAGE
    assert "Ollama needs no API key" in err


# --- remote access -----------------------------------------------------------------


def test_remote_access_on_needs_an_authtoken(capsys, fake, token_file):
    code, _out, err = cli(capsys, "remote-access", "on", *remote(fake, token_file))
    assert code == ClientExit.FAILED
    assert "Save an ngrok authtoken" in err


def test_remote_access_on_with_an_authtoken_file(capsys, fake, token_file, tmp_path):
    authtoken = tmp_path / "ngrok.txt"
    authtoken.write_text(NGROK_TOKEN, encoding="utf-8")
    code, out, err = cli(
        capsys,
        "remote-access",
        "on",
        "--authtoken-file",
        str(authtoken),
        "--domain",
        "rm.ngrok.app",
        "--json",
        *remote(fake, token_file),
    )
    assert code == 0
    data = json.loads(out)
    assert data["enabled"] is True
    assert data["authtoken_saved"] is True
    assert fake.state.remote == {
        "enabled": True,
        "domain": "rm.ngrok.app",
        "authtoken": NGROK_TOKEN,
    }
    no_secret_in(out + err, NGROK_TOKEN)


def test_remote_access_off_and_status(capsys, fake, token_file):
    fake.state.remote.update(enabled=True, authtoken=NGROK_TOKEN)
    code, out, _err = cli(capsys, "remote-access", "off", *remote(fake, token_file))
    assert code == 0
    assert "Remote access: off" in out
    code, out, _err = cli(capsys, "remote-access", "status", *remote(fake, token_file))
    assert code == 0
    assert "Authtoken: saved" in out
    assert [r.method for r in fake.requests] == ["POST", "GET"]


# --- mcp tokens --------------------------------------------------------------------


def test_mcp_tokens_list_shows_a_fingerprint_only(capsys, tmp_path, use_probe):
    from rootmind.auth import token_fingerprint

    mcp_file = tmp_path / "api_token.txt"
    mcp_file.write_text(MCP_TOKEN, encoding="utf-8")
    use_probe(FakeProbe(18999, tmp_path / "rest", mcp_token_file=mcp_file))
    code, out, _err = cli(capsys, "mcp", "tokens", "list", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["tokens"] == [
        {
            "name": "default",
            "file": str(mcp_file),
            "readable": True,
            "fingerprint": f"sha256:{token_fingerprint(MCP_TOKEN)}",
        }
    ]
    assert MCP_TOKEN not in out


def test_mcp_tokens_list_without_mcp_over_http(capsys, tmp_path, use_probe):
    use_probe(FakeProbe(18999, tmp_path / "rest"))
    code, out, _err = cli(capsys, "mcp", "tokens", "list")
    assert code == 0
    assert "MCP over HTTP is off" in out


def test_mcp_without_tokens_still_runs_the_stdio_server(monkeypatch):
    from rootmind import mcp_stdio

    seen = {}
    monkeypatch.setattr(mcp_stdio, "main", lambda argv: seen.setdefault("argv", argv))
    main(["mcp", "--config", "x.yaml"])
    assert seen["argv"] == ["--config", "x.yaml"]


# --- against the real REST app -----------------------------------------------------


class _AppClient:
    """The client interface over a TestClient of the real REST app."""

    url = "http://127.0.0.1:8000"

    def __init__(self, client) -> None:
        self._client = client

    def request(self, method, path, *, body=None, params=None, timeout=None):
        response = self._client.request(method, path, json=body, params=params)
        payload = response.json() if response.content else None
        if response.status_code >= 400:
            raise ClientError(
                f"HTTP {response.status_code}: {_detail(payload)}",
                ClientExit.FAILED,
                response.status_code,
            )
        return payload

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body=body if body is not None else {}, **kwargs)


def _run_against_app(app_client, *argv: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    args = build_parser().parse_args([*argv, "--url", "http://127.0.0.1:8000"])
    code = commands.run(
        args, out=lines.append, err=lines.append, client_factory=lambda _t: app_client
    )
    return code, lines


def test_folders_and_llm_against_the_real_api(rlm_home, tmp_path):
    from rootmind.api.app import create_app

    extra = tmp_path / "extra"
    extra.mkdir()
    key_file = tmp_path / "key.txt"
    key_file.write_text(NEW_KEY, encoding="utf-8")
    with rest_client(create_app()) as client:
        app_client = _AppClient(client)
        code, lines = _run_against_app(app_client, "folders", "add", str(extra), "--name", "extra")
        assert code == 0, lines
        code, lines = _run_against_app(
            app_client, "config", "llm", "--provider", "openai", "--model", "gpt-test",
            "--key-file", str(key_file),
        )  # fmt: skip
        assert code == 0, lines
        code, lines = _run_against_app(app_client, "folders", "list", "--json")
        assert code == 0, lines
        names = [f["name"] for f in json.loads(lines[0])["folders"]]
        assert names == ["docs", "extra"]
        no_secret_in("\n".join(lines), NEW_KEY, TEST_API_KEY)

    saved = yaml.safe_load((rlm_home / "config.yaml").read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["connectors"]] == ["docs", "extra"]
    assert saved["llm"]["api_keys"] == {"anthropic": TEST_API_KEY, "openai": NEW_KEY}
    assert saved["llm"]["provider"] == "openai"
    assert saved["llm"]["model"] == "gpt-test"


def test_status_and_search_against_the_real_api(rlm_home):
    from rootmind.api.app import create_app

    with rest_client(create_app()) as client:
        app_client = _AppClient(client)
        client.post("/index/refresh", json={"full": True})
        args = build_parser().parse_args(["index", "--wait", "--url", app_client.url])
        out: list[str] = []
        assert commands.run(args, out=out.append, client_factory=lambda _t: app_client) == 0
        # The sync of the refresh above may still run; --wait waited for it.
        code, lines = _run_against_app(app_client, "search", "lantern", "--json")
        assert code == 0
        assert json.loads(lines[0])["results"][0]["file_name"] == "camp_notes.md"
        code, lines = _run_against_app(app_client, "status", "--json")
        assert code == 0
        data = json.loads(lines[0])
        assert data["index"]["documents"] >= 7
        assert data["folders"][0]["name"] == "docs"


def test_stdin_secret_is_limited(fake, token_file):
    args = build_parser().parse_args(
        ["config", "llm", "--key-file", "-", *remote(fake, token_file)]
    )
    errors: list[str] = []
    code = commands.run(args, out=lambda _s: None, err=errors.append, stdin=io.StringIO("x" * 5000))
    assert code == ClientExit.USAGE
    assert "longer than 4 KB" in errors[0]


# --- the machine probe -------------------------------------------------------------


class _FakeEnv:
    def __init__(self, linux: bool) -> None:
        self.linux = linux
        self.owned: list[tuple[int, int]] = []

    def systemctl(self):
        from types import SimpleNamespace

        return SimpleNamespace(show=lambda name: SimpleNamespace(main_pid=321, name=name))

    def scm(self, *, write: bool):
        from contextlib import contextmanager
        from types import SimpleNamespace

        assert write is False

        @contextmanager
        def manager():
            yield SimpleNamespace(query=lambda name: SimpleNamespace(pid=654, name=name))

        return manager()

    def port_owned_by(self, port: int, pid: int) -> bool:
        self.owned.append((port, pid))
        return True


@pytest.mark.parametrize(("linux", "pid"), [(True, 321), (False, 654)])
def test_machine_probe_asks_the_service_manager_for_the_pid(linux, pid):
    env = _FakeEnv(linux)
    probe = discovery._MachineProbe(env, linux)
    assert probe.platform == ("linux" if linux else "windows")
    assert probe.backend_pid() == pid
    assert probe.port_owned_by(18000, pid)
    assert env.owned == [(18000, pid)]
