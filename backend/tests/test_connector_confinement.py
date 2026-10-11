"""File access stays inside the configured folder (LocalConnector and the REST path lookup)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from helpers import index_config

from rootmind.config import AppConfig, ConnectorConfig
from rootmind.connectors import LocalConnector


@pytest.fixture
def layout(tmp_path) -> Path:
    """``docs/`` (the root), a sibling ``docs-secret/`` and a file next to the root."""
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "readme.txt").write_text("inside", encoding="utf-8")
    (root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    (tmp_path / "docs-secret").mkdir()
    (tmp_path / "docs-secret" / "x.txt").write_text("secret sibling", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    return root


@pytest.fixture
def connector(layout) -> LocalConnector:
    return LocalConnector("docs", layout, include_patterns=["*.txt"])


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "../docs-secret/x.txt",  # shares the root's name as a string prefix (regression)
        "sub/../../outside.txt",
        "..",
    ],
)
def test_relative_escape_rejected(connector, path):
    with pytest.raises(ValueError, match="outside root"):
        connector.resolve_path(path)
    with pytest.raises(ValueError, match="outside root"):
        connector.read_file(path)
    with pytest.raises(ValueError, match="outside root"):
        connector.get_metadata(path)
    assert connector.file_exists(path) is False


def test_absolute_path_outside_rejected(connector, layout):
    outside = layout.parent / "docs-secret" / "x.txt"
    with pytest.raises(ValueError, match="outside root"):
        connector.resolve_path(str(outside))


def test_backslash_traversal(connector, layout):
    path = "..\\docs-secret\\x.txt"
    if sys.platform == "win32":
        with pytest.raises(ValueError, match="outside root"):
            connector.resolve_path(path)
    else:
        # On POSIX a backslash is an ordinary file-name character: no traversal.
        assert connector.resolve_path(path).is_relative_to(layout.resolve())


def test_paths_inside_the_root_resolve(connector, layout):
    assert connector.resolve_path("readme.txt") == (layout / "readme.txt").resolve()
    assert connector.resolve_path("sub/nested.txt") == (layout / "sub" / "nested.txt").resolve()
    assert connector.resolve_path("sub/../readme.txt") == (layout / "readme.txt").resolve()
    assert connector.read_file("sub/nested.txt") == b"nested"


def test_listing_never_leaves_the_root(connector):
    paths = sorted(Path(f.path).as_posix() for f in connector.list_files_recursive())
    assert paths == ["readme.txt", "sub/nested.txt"]


def test_listing_a_missing_root_raises(layout):
    """An unreachable folder must never look empty (the sync would drop its index)."""
    connector = LocalConnector("docs", layout, include_patterns=["*.txt"])
    layout.rename(layout.with_name("unplugged"))

    assert connector.is_available() is False
    with pytest.raises(FileNotFoundError):
        list(connector.list_files_recursive())


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_junction_pointing_outside_is_not_followed(tmp_path, layout):
    import _winapi

    _winapi.CreateJunction(str(tmp_path / "docs-secret"), str(layout / "j"))
    _winapi.CreateJunction(str(layout), str(layout / "sub" / "loop"))  # to an ancestor
    connector = LocalConnector("docs", layout, include_patterns=["*.txt"])

    paths = sorted(Path(f.path).as_posix() for f in connector.list_files_recursive())

    assert paths == ["readme.txt", "sub/nested.txt"]


def test_directory_symlink_pointing_outside_is_not_followed(tmp_path, layout):
    _symlink_or_skip(layout / "linked", tmp_path / "docs-secret")
    connector = LocalConnector("docs", layout, include_patterns=["*.txt"])

    paths = sorted(Path(f.path).as_posix() for f in connector.list_files_recursive())

    assert paths == ["readme.txt", "sub/nested.txt"]


def test_hidden_folders_follow_the_exclude_patterns(layout):
    (layout / ".notes").mkdir()
    (layout / ".notes" / "n.txt").write_text("hidden", encoding="utf-8")

    default = LocalConnector("docs", layout, ["*.txt"], exclude_patterns=[".*"])
    explicit = LocalConnector("docs", layout, ["*.txt"], exclude_patterns=["sub"])

    assert ".notes/n.txt" not in {Path(f.path).as_posix() for f in default.list_files_recursive()}
    assert {Path(f.path).as_posix() for f in explicit.list_files_recursive()} == {
        "readme.txt",
        ".notes/n.txt",
    }


def test_rootmind_data_folder_is_never_indexed(tmp_path, layout):
    data = layout / "data"
    data.mkdir()
    (data / "api_token.txt").write_text("not-a-real-token", encoding="utf-8")

    connector = LocalConnector("docs", layout, ["*.txt"], protected_dirs=[data])
    paths = {Path(f.path).as_posix() for f in connector.list_files_recursive()}

    assert "data/api_token.txt" not in paths
    with pytest.raises(ValueError, match="data folder"):
        LocalConnector("data", data, ["*.txt"], protected_dirs=[data])


def test_create_connectors_protects_the_app_data_folder(tmp_path):
    from rootmind.api.app import create_connectors
    from rootmind.config import app_home

    data = app_home() / "data"
    data.mkdir(parents=True)
    (data / "api_token.txt").write_text("not-a-real-token", encoding="utf-8")
    cfg = AppConfig(
        connectors=[
            ConnectorConfig(name="home", type="local", path=str(app_home())),
            ConnectorConfig(name="inside", type="local", path=str(data)),
        ]
    )
    cfg.database.path = str(data / "index.db")
    cfg.indexer.include = ["*.txt"]

    connectors = create_connectors(cfg)

    # RLM_HOME itself is protected in the tests (it is set), and so is its data folder.
    assert connectors == {}


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:  # Windows without developer mode
        pytest.skip(f"symlinks unavailable: {e}")


def test_symlink_pointing_outside_is_not_indexed(tmp_path, layout):
    _symlink_or_skip(layout / "link.txt", tmp_path / "docs-secret" / "x.txt")
    cfg = AppConfig(connectors=[ConnectorConfig(name="docs", type="local", path=str(layout))])
    cfg.database.path = str(tmp_path / "db" / "index.db")
    cfg.indexer.include = ["*.txt"]

    repo = index_config(cfg)

    indexed = {d["file_name"] for d in repo.list_documents()}
    assert indexed == {"readme.txt", "nested.txt"}
    assert repo.search_fulltext("sibling") == []
    # Indexing records the escape as an error; the cleanup pass may then drop the row.
    link = repo.get_document_by_path("docs", "link.txt")
    assert link is None or (link.status == "error" and link.content_text is None)
    repo.engine.dispose()


def test_document_path_endpoint_rejects_escaping_paths(api, tiny_corpus):
    from rootmind.api.app import app_state

    db = app_state.db
    good = db.upsert_document("docs", "handbook.txt", "handbook.txt", "txt", content_text="x")
    evil = db.upsert_document("docs", "../outside.txt", "outside.txt", "txt", content_text="x")
    orphan = db.upsert_document("gone", "a.txt", "a.txt", "txt", content_text="x")

    ok = api.get(f"/documents/{good.id}/path")
    assert ok.status_code == 200
    assert Path(ok.json()["abs_path"]) == (tiny_corpus / "handbook.txt").resolve()

    escaped = api.get(f"/documents/{evil.id}/path")
    assert escaped.status_code == 400
    assert "outside root" in escaped.json()["detail"]

    assert api.get(f"/documents/{orphan.id}/path").status_code == 400
    assert api.get("/documents/unknown/path").status_code == 404
