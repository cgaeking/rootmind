"""Shared fixtures. Tests run fully offline: constructing a real LLM client fails.

- ``no_real_service`` (autouse): the client commands never find the machine's
  RootMind service; tests pass a fake probe or ``--url``.
- ``offline_llm`` / ``isolated_config`` (autouse): no real SDK clients, no API
  keys or tokens, no background scheduler, a fresh ``RLM_HOME`` and working
  directory per test, so a developer's own ``.env`` or ``config.yaml`` is never
  read, and no ``RLM_HOST``/``RLM_PORT`` from the environment.
- ``tiny_corpus``: one small file per supported format, copied per test.
- ``app_config`` / ``repo`` / ``indexed_repo``: config, empty and indexed repository.
- ``rlm_home`` / ``api``: a ``config.yaml`` in ``RLM_HOME`` and a ``TestClient``
  for the REST app with its real lifespan.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from helpers import TEST_API_KEY, TINY_INCLUDE, index_config, rest_client

from rootmind import config as config_module
from rootmind.config import AppConfig, ConnectorConfig
from rootmind.database.repository import DocumentRepository
from scripts import corpus_writers as w


class RealLLMClientError(RuntimeError):
    pass


def _refuse(*args, **kwargs):
    raise RealLLMClientError("real LLM client constructed in tests")


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    """Make any real Anthropic/OpenAI client construction fail loudly.

    Tests inject fakes via ``KnowledgeBaseEngine(client=...)``. A test that
    needs to observe SDK construction overrides these attributes itself.
    """
    import anthropic
    import openai

    for module, names in (
        (anthropic, ("Anthropic", "AsyncAnthropic")),
        (openai, ("OpenAI", "AsyncOpenAI")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, _refuse)
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "MCP_API_TOKEN",
        "RLM_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


class RealServiceProbeError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_real_service(monkeypatch):
    """The client commands never look up (or read the token of) the real RootMind service.

    Tests pass a fake probe or ``--url``; finding the machine's service fails loudly.
    """
    from rootmind.client import commands, discovery

    def refuse():
        raise RealServiceProbeError("the real RootMind service was looked up in a test")

    monkeypatch.setattr(discovery, "default_probe", refuse)
    monkeypatch.setattr(commands, "default_probe", refuse)
    for var in ("RLM_URL", "RLM_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    """Fresh global config per test, no background scheduler, cwd-independent paths."""
    monkeypatch.setenv("RLM_DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("RLM_HOME", str(tmp_path / "rlm_home"))
    for var in ("CONFIG_PATH", "RLM_DEV", "MCP_ALLOWED_ORIGINS", "DEBUG", "RLM_HOST", "RLM_PORT"):
        monkeypatch.delenv(var, raising=False)
    # Settings() reads ".env" from the working directory: never pick up a real one.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    config_module.reset_config()
    yield
    config_module.reset_config()


# --- corpus ----------------------------------------------------------------------------


def _write_tiny_corpus(root: Path) -> None:
    """One file per format, with a few planted facts the tests search for."""
    w.write_text(
        root / "camp_notes.md",
        "# Camp notes\n\nThe brass lantern stays in the blue crate.\n"
        "Bring spare batteries for the headlamps.\n",
    )
    w.write_text(
        root / "handbook.txt",
        "Staff handbook\n\nPaid time off: full-time staff accrue 21 days per year.\n"
        "Remote work is possible two days per week.\n",
    )
    w.write_text(
        root / "orders.csv",
        "order_id,customer,total\nA-1,Bluebird Trail Club,120.00\nA-2,Hilltop Scouts,80.00\n",
    )
    w.write_docx(
        root / "agreement.docx",
        "Service agreement",
        ["## 4. Term", "Either party may terminate this agreement with 60 days notice."],
        table=[["Clause", "Value"], ["Liability cap", "12 months of fees"]],
    )
    w.write_xlsx(
        root / "prices.xlsx",
        {
            "Retail": [["SKU", "Product", "Retail price"], ["FH-TENT-2P", "Trailhead tent", 249.0]],
            "Wholesale": [
                ["SKU", "Product", "Wholesale price"],
                ["FH-TENT-2P", "Trailhead tent", 174.3],
            ],
        },
    )
    w.write_pptx(
        root / "roadmap.pptx",
        [
            ("Roadmap", ["Second half"]),
            ("Ultralight tent", ["Target weight: 1.1 kg"]),
            ("Pop-up store", ["Opens in September"]),
        ],
    )
    w.write_pdf(
        root / "invoice.pdf",
        [
            ["Invoice INV-7", "Bill to: Bluebird Trail Club", "3 tents at 249.00"],
            ["Invoice INV-7, page 2", "Total due: 1,179.36", "Due date: 2026-04-30"],
        ],
        title="Invoice INV-7",
    )


@pytest.fixture(scope="session")
def _tiny_corpus_template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("tiny_corpus_template")
    _write_tiny_corpus(root)
    return root


@pytest.fixture
def tiny_corpus(tmp_path, _tiny_corpus_template) -> Path:
    """A per-test copy of the tiny corpus (tests may modify or delete files)."""
    target = tmp_path / "corpus"
    shutil.copytree(_tiny_corpus_template, target)
    return target


# --- config, repository, API ------------------------------------------------------------


@pytest.fixture
def app_config(tmp_path, tiny_corpus) -> AppConfig:
    """Config with one connector ``docs`` over the tiny corpus and a temp database."""
    cfg = AppConfig(connectors=[ConnectorConfig(name="docs", type="local", path=str(tiny_corpus))])
    cfg.database.path = str(tmp_path / "db" / "index.db")
    cfg.indexer.include = list(TINY_INCLUDE)
    return cfg


@pytest.fixture
def repo(tmp_path):
    """An empty repository in a temp folder."""
    repository = DocumentRepository(tmp_path / "db" / "repo.db")
    yield repository
    repository.engine.dispose()


@pytest.fixture
def indexed_repo(app_config):
    """The tiny corpus indexed with the real indexer (connector ``docs``)."""
    repository = index_config(app_config)
    yield repository
    repository.engine.dispose()


@pytest.fixture
def rlm_home(tiny_corpus) -> Path:
    """``RLM_HOME`` with a config.yaml that indexes the tiny corpus as connector ``docs``."""
    home = config_module.app_home()
    data = {
        "llm": {"provider": "anthropic", "api_keys": {"anthropic": TEST_API_KEY}},
        "connectors": [{"name": "docs", "type": "local", "path": str(tiny_corpus)}],
        "indexer": {"include": list(TINY_INCLUDE)},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return home


@pytest.fixture
def api(rlm_home):
    """TestClient for the REST app; runs the real lifespan against ``rlm_home``."""
    from rootmind.api.app import create_app

    with rest_client(create_app()) as client:
        yield client
