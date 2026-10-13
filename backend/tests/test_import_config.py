"""config.import_config (the service resets its database path with it) and config_file_path."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import yaml
from helpers import OTHER_TEST_KEY, TEST_API_KEY

from rootmind import config as config_module
from rootmind.config import MAX_IMPORT_BYTES, config_file_path, import_config, init_config

SERVICE_DB = "./data/db/index.db"
ENV_KEY = "sk-test-from-the-environment-0000"  # pragma: allowlist secret


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "target-home"


def _source(tmp_path: Path, data, name: str = "source.yaml") -> Path:
    path = tmp_path / name
    text = data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def _written(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def test_imports_the_config_and_resets_the_database_path(tmp_path, home):
    source = _source(
        tmp_path,
        {
            "llm": {"provider": "openai", "api_keys": {"openai": OTHER_TEST_KEY}},
            "connectors": [{"name": "docs", "type": "local", "path": "C:/Docs"}],
            "database": {"path": "D:/elsewhere/index.db"},
            "indexer": {"sync_interval_hours": 6},
            "api": {"port": 18123},
        },
    )

    config = import_config(source, home, database_path=SERVICE_DB)

    written = _written(home)
    assert written["database"] == {"path": SERVICE_DB}
    assert written["llm"] == {"provider": "openai", "api_keys": {"openai": OTHER_TEST_KEY}}
    assert written["connectors"] == [{"name": "docs", "type": "local", "path": "C:/Docs"}]
    assert written["indexer"] == {"sync_interval_hours": 6}
    assert written["api"] == {"port": 18123}
    assert config.database.path == SERVICE_DB
    assert [c.name for c in config.connectors] == ["docs"]


def test_a_missing_database_section_gets_the_path(tmp_path, home):
    import_config(_source(tmp_path, {"connectors": []}), home, database_path=SERVICE_DB)
    assert _written(home) == {"database": {"path": SERVICE_DB}, "connectors": []}


def test_an_empty_file_imports_defaults(tmp_path, home):
    import_config(_source(tmp_path, ""), home, database_path="./data/index.db")
    assert _written(home) == {"database": {"path": "./data/index.db"}}


def test_unknown_keys_are_dropped(tmp_path, home):
    source = _source(tmp_path, {"connectors": [], "surprise": {"x": 1}})
    import_config(source, home, database_path=SERVICE_DB)
    assert "surprise" not in _written(home)


def test_environment_keys_are_not_merged(tmp_path, home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)
    Path(".env").write_text(f"OPENAI_API_KEY={ENV_KEY}\n", encoding="utf-8")
    source = _source(tmp_path, {"llm": {"provider": "anthropic"}, "connectors": []})

    import_config(source, home, database_path=SERVICE_DB)

    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert ENV_KEY not in text
    assert _written(home)["llm"] == {"provider": "anthropic"}


def test_the_previous_config_is_kept_as_bak(tmp_path, home):
    home.mkdir()
    (home / "config.yaml").write_text("connectors: []\nllm:\n  provider: xai\n", encoding="utf-8")
    source = _source(tmp_path, {"llm": {"provider": "openai"}})

    import_config(source, home, database_path=SERVICE_DB)

    backup = yaml.safe_load((home / "config.yaml.bak").read_text(encoding="utf-8"))
    assert backup == {"connectors": [], "llm": {"provider": "xai"}}
    assert _written(home)["llm"] == {"provider": "openai"}
    assert not [p for p in home.iterdir() if p.name.endswith(".tmp")]


def test_importing_the_target_itself(home):
    home.mkdir()
    target = home / "config.yaml"
    target.write_text("connectors: []\ndatabase:\n  path: ./x.db\n", encoding="utf-8")
    import_config(target, home, database_path=SERVICE_DB)
    assert _written(home) == {"connectors": [], "database": {"path": SERVICE_DB}}


def test_a_utf8_bom_is_accepted(tmp_path, home):
    source = tmp_path / "bom.yaml"
    source.write_bytes(b"\xef\xbb\xbfllm:\n  provider: openai\n")
    import_config(source, home, database_path=SERVICE_DB)
    assert _written(home)["llm"] == {"provider": "openai"}


@pytest.mark.skipif(os.name != "posix", reason="mode bits; Windows uses the folder's ACL")
def test_written_owner_only(tmp_path, home):
    import_config(_source(tmp_path, {}), home, database_path=SERVICE_DB)
    assert stat.S_IMODE((home / "config.yaml").stat().st_mode) == 0o600


# --- refused sources -------------------------------------------------------------------


def _refused(source: Path, home: Path, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        import_config(source, home, database_path=SERVICE_DB)
    assert not (home / "config.yaml").exists()


def test_refuses_files_over_1_mb(tmp_path, home):
    source = tmp_path / "big.yaml"
    source.write_bytes(b"connectors: []\n" + b"#" * MAX_IMPORT_BYTES)
    _refused(source, home, "larger than 1 MB")


def test_accepts_exactly_1_mb(tmp_path, home):
    head = b"connectors: []\n#"
    source = tmp_path / "limit.yaml"
    source.write_bytes(head + b"x" * (MAX_IMPORT_BYTES - len(head)))
    assert source.stat().st_size == MAX_IMPORT_BYTES
    import_config(source, home, database_path=SERVICE_DB)
    assert _written(home)["connectors"] == []


def test_refuses_a_directory(tmp_path, home):
    folder = tmp_path / "folder.yaml"
    folder.mkdir()
    _refused(folder, home, "not a regular file")


def test_refuses_a_symlink(tmp_path, home):
    target = _source(tmp_path, {"connectors": []}, name="real.yaml")
    link = tmp_path / "link.yaml"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:  # Windows without developer mode
        pytest.skip(f"symlinks unavailable: {e}")
    _refused(link, home, "link or reparse point")


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_refuses_a_junction(tmp_path, home):
    import _winapi

    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "junction.yaml"
    _winapi.CreateJunction(str(target), str(link))
    _refused(link, home, "link or reparse point")


def test_refuses_a_hard_link(tmp_path, home):
    original = _source(tmp_path, {"connectors": []}, name="original.yaml")
    link = tmp_path / "second-name.yaml"
    try:
        os.link(original, link)
    except OSError as e:
        pytest.skip(f"hard links unavailable: {e}")
    _refused(link, home, "more than one hard link")
    _refused(original, home, "more than one hard link")


def test_refuses_a_file_replaced_while_reading(tmp_path, home, monkeypatch):
    source = _source(tmp_path, {"connectors": []})
    real_fstat = os.fstat

    class Swapped:
        def __init__(self, info):
            self.st_dev = info.st_dev
            self.st_ino = info.st_ino + 1

    monkeypatch.setattr(config_module.os, "fstat", lambda fd: Swapped(real_fstat(fd)))
    _refused(source, home, "replaced")


def test_missing_source(tmp_path, home):
    with pytest.raises(FileNotFoundError):
        import_config(tmp_path / "missing.yaml", home, database_path=SERVICE_DB)


def test_invalid_yaml_never_echoes_the_file(tmp_path, home):
    source = _source(tmp_path, f'llm:\n  api_key: "{TEST_API_KEY}\n  provider: [\n')
    with pytest.raises(ValueError, match="not valid YAML at line") as error:
        import_config(source, home, database_path=SERVICE_DB)
    assert TEST_API_KEY not in str(error.value)
    assert error.value.__cause__ is None


def test_invalid_config_never_echoes_values(tmp_path, home):
    source = _source(tmp_path, {"llm": TEST_API_KEY})
    with pytest.raises(ValueError, match="not a valid RootMind config: llm") as error:
        import_config(source, home, database_path=SERVICE_DB)
    assert TEST_API_KEY not in str(error.value)
    assert not (home / "config.yaml").exists()


@pytest.mark.parametrize("text", ["- a\n- b\n", "just text\n", "database: ./x.db\n"])
def test_refuses_non_config_content(tmp_path, home, text):
    with pytest.raises(ValueError):
        import_config(_source(tmp_path, text), home, database_path=SERVICE_DB)
    assert not (home / "config.yaml").exists()


def test_refuses_non_utf8(tmp_path, home):
    source = tmp_path / "latin1.yaml"
    source.write_bytes("connectors: []\n# Gr\xfc\xdfe\n".encode("latin-1"))
    _refused(source, home, "not UTF-8")


# --- config_file_path -----------------------------------------------------------------


def test_config_file_path_default():
    assert config_file_path() == config_module.app_home() / "config.yaml"


def test_config_file_path_follows_init_config():
    init_config("other.yaml")
    assert config_file_path() == config_module.app_home() / "other.yaml"


def test_config_file_path_honours_config_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "elsewhere.yaml"))
    assert config_file_path() == tmp_path / "elsewhere.yaml"
