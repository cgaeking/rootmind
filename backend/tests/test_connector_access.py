"""Folder access policy: the protocol, the local connector, create_connectors and the REST routes."""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import pytest
import yaml
from helpers import TEST_API_KEY, TINY_INCLUDE, index_config, rest_client

from rootmind.access import UNRESTRICTED, FolderNotAllowed, Unrestricted
from rootmind.api.app import app_state, create_app, create_connectors
from rootmind.config import AppConfig, ConnectorConfig
from rootmind.connectors import LocalConnector

REFUSED_REASON = "network drives and shares are not available to the background service"


class RecordingAccess(Unrestricted):
    """Unrestricted access that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._lock = threading.Lock()

    def _record(self, name: str, path, root=None) -> None:
        with self._lock:
            self.calls.append((name, Path(path).name, None if root is None else Path(root).name))

    def check_root(self, path):
        self._record("check_root", path)
        return super().check_root(path)

    def may_descend(self, directory, root):
        self._record("may_descend", directory, root)
        return super().may_descend(directory, root)

    def open_file(self, path, root):
        self._record("open_file", path, root)
        return super().open_file(path, root)


class DenyingAccess(Unrestricted):
    """Refuses folders named ``share*``, never enters ``private``, never opens ``locked*`` files."""

    def __init__(self) -> None:
        self.checked: list[str] = []

    def check_root(self, path):
        self.checked.append(str(path))
        resolved = Path(path).resolve()
        if resolved.name.startswith("share"):
            raise FolderNotAllowed(REFUSED_REASON)
        if not resolved.exists():
            raise FileNotFoundError(f"Folder not found: {resolved}")
        return resolved

    def may_descend(self, directory, root):
        return directory.name != "private"

    def open_file(self, path, root):
        if path.name.startswith("locked"):
            raise PermissionError(f"Access denied: {path.name}")
        return super().open_file(path, root)


@pytest.fixture
def tree(tmp_path) -> Path:
    """``docs/`` with a readable file, a ``private/`` subfolder and a locked file; ``share/``."""
    root = tmp_path / "docs"
    (root / "private").mkdir(parents=True)
    (root / "public").mkdir()
    (root / "notes.txt").write_text("the copper kettle", encoding="utf-8")
    (root / "public" / "plan.txt").write_text("the silver spoon", encoding="utf-8")
    (root / "private" / "diary.txt").write_text("the hidden attic", encoding="utf-8")
    (root / "locked.txt").write_text("the sealed vault", encoding="utf-8")
    (tmp_path / "share").mkdir()
    return root


# --- Unrestricted ---------------------------------------------------------------------------


def test_unrestricted_is_todays_behaviour(tree):
    assert UNRESTRICTED.check_root(str(tree / "public" / "..")) == tree.resolve()
    assert UNRESTRICTED.may_descend(tree / "private", tree) is True
    with UNRESTRICTED.open_file(tree / "notes.txt", tree) as f:
        assert f.read() == b"the copper kettle"
    assert repr(UNRESTRICTED) == "UNRESTRICTED"


def test_unrestricted_is_the_default(tree):
    connector = LocalConnector("docs", tree, include_patterns=["*.txt"])
    assert connector.access is UNRESTRICTED
    paths = sorted(Path(f.path).as_posix() for f in connector.list_files_recursive())
    assert paths == ["locked.txt", "notes.txt", "private/diary.txt", "public/plan.txt"]
    assert connector.read_file("locked.txt") == b"the sealed vault"


def test_folder_not_allowed_is_a_value_error():
    assert issubclass(FolderNotAllowed, ValueError)
    assert str(FolderNotAllowed(REFUSED_REASON)) == REFUSED_REASON


# --- the connector uses the policy ------------------------------------------------------


def test_connector_asks_the_policy(tree):
    access = RecordingAccess()
    connector = LocalConnector("docs", tree, include_patterns=["*.txt"], access=access)
    assert access.calls == [("check_root", "docs", None)]
    assert connector.root_path == tree.resolve()

    access.calls.clear()
    list(connector.list_files_recursive())
    assert sorted(access.calls) == [
        ("may_descend", "private", "docs"),
        ("may_descend", "public", "docs"),
    ]

    access.calls.clear()
    assert connector.read_file("public/plan.txt") == b"the silver spoon"
    assert access.calls == [("open_file", "plan.txt", "docs")]

    access.calls.clear()
    metadata = connector.get_metadata("notes.txt")
    assert metadata.content_hash is not None
    assert access.calls == [("open_file", "notes.txt", "docs")]


LINK_MODE = stat.S_IFLNK | 0o777


def spy_following(monkeypatch, is_watched) -> list[str]:
    """Record every call that would open a watched path and follow a link there.

    ``Path.resolve``, ``Path.stat``, ``Path.exists``, ``Path.is_file``,
    ``Path.is_dir`` and ``os.stat`` (following links) all open the path.
    """
    followed: list[str] = []
    for name in ("resolve", "stat", "exists", "is_file", "is_dir"):
        real = getattr(Path, name)

        def spy(self, *args, _name=name, _real=real, **kwargs):
            if is_watched(self):
                followed.append(f"Path.{_name}")
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, spy)
    real_stat = os.stat

    def spy_stat(path, *args, follow_symlinks=True, **kwargs):
        if follow_symlinks and is_watched(Path(path)):
            followed.append("os.stat")
        return real_stat(path, *args, follow_symlinks=follow_symlinks, **kwargs)

    monkeypatch.setattr(os, "stat", spy_stat)
    return followed


def test_file_links_are_never_followed_under_a_policy(tree, monkeypatch):
    """Under a policy a file link is skipped, and never opened, by every connector method."""
    from rootmind.connectors import local

    (tree / "link.txt").write_text("the paper lantern", encoding="utf-8")
    link_info = os.stat_result((LINK_MODE, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    real_lstat = os.lstat
    real_entry_lstat = local._entry_lstat

    def fake_lstat(path, *args, **kwargs):
        return link_info if Path(path).name == "link.txt" else real_lstat(path, *args, **kwargs)

    def fake_entry_lstat(entry):
        return link_info if entry.name == "link.txt" else real_entry_lstat(entry)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(local, "_entry_lstat", fake_entry_lstat)
    restricted = LocalConnector("docs", tree, ["*.txt"], access=RecordingAccess())
    followed = spy_following(monkeypatch, lambda p: p.name == "link.txt")

    paths = {Path(f.path).as_posix() for f in restricted.list_files_recursive()}
    assert "link.txt" not in paths
    assert "notes.txt" in paths
    assert restricted.file_exists("link.txt") is False
    with pytest.raises(PermissionError, match="links are not followed"):
        restricted.read_file("link.txt")
    with pytest.raises(PermissionError, match="links are not followed"):
        restricted.get_metadata("link.txt")
    assert restricted.resolve_path("link.txt") == tree.resolve() / "link.txt"
    assert followed == []


def test_without_a_policy_a_file_link_inside_the_root_is_listed(tree, monkeypatch):
    (tree / "link.txt").write_text("the paper lantern", encoding="utf-8")
    real_islink = os.path.islink
    monkeypatch.setattr(
        os.path, "islink", lambda path: Path(path).name == "link.txt" or real_islink(path)
    )
    unrestricted = LocalConnector("docs", tree, ["*.txt"])
    paths = {Path(f.path).as_posix() for f in unrestricted.list_files_recursive()}
    assert "link.txt" in paths


def make_folder_link(link: Path, target: Path) -> None:
    """A junction on Windows (no privilege needed), a symbolic link elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_folder_links_are_never_followed_under_a_policy(tree, tmp_path, monkeypatch):
    """A folder link inside the root: not listed, not entered, and never opened."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("the far island", encoding="utf-8")
    make_folder_link(tree / "linked", outside)
    access = RecordingAccess()
    restricted = LocalConnector("docs", tree, ["*.txt"], access=access)
    followed = spy_following(monkeypatch, lambda p: "linked" in p.parts)

    paths = {Path(f.path).as_posix() for f in restricted.list_files_recursive()}
    assert paths == {"locked.txt", "notes.txt", "private/diary.txt", "public/plan.txt"}
    assert not any(name == "linked" for _call, name, _root in access.calls)
    assert restricted.file_exists("linked/secret.txt") is False
    with pytest.raises(PermissionError, match="links are not followed"):
        restricted.read_file("linked/secret.txt")
    with pytest.raises(PermissionError, match="links are not followed"):
        restricted.get_metadata("linked/secret.txt")
    assert followed == []
    assert not any(c == ("open_file", "secret.txt", "docs") for c in access.calls)


def test_paths_are_resolved_lexically_under_a_policy(tree, monkeypatch):
    restricted = LocalConnector("docs", tree, ["*.txt"], access=RecordingAccess())
    followed = spy_following(monkeypatch, lambda p: True)
    root = restricted.root_path

    assert restricted.resolve_path("public/../notes.txt") == root / "notes.txt"
    assert restricted.resolve_path("") == root
    with pytest.raises(ValueError, match="outside root"):
        restricted.resolve_path("../docs-secret/x.txt")
    assert restricted.file_exists("../share") is False
    assert restricted.file_exists("public") is False  # a folder is not a file
    assert restricted.file_exists("public/plan.txt") is True
    assert restricted.file_exists("public/gone.txt") is False
    assert restricted.file_exists("notes.txt/inner.txt") is False
    assert [c for c in followed if c == "Path.resolve"] == []
    with pytest.raises(FileNotFoundError, match="File not found: gone.txt"):
        restricted.read_file("gone.txt")
    with pytest.raises(ValueError, match="Not a file"):
        restricted.read_file("public")


def test_policy_root_is_used_as_the_root(tree, tmp_path):
    class Canonical(Unrestricted):
        def check_root(self, path):
            return tree.resolve()

    connector = LocalConnector("docs", tmp_path / "alias", ["*.txt"], access=Canonical())
    assert connector.root_path == tree.resolve()
    assert connector.read_file("notes.txt") == b"the copper kettle"


def test_refused_root_raises(tree, tmp_path):
    with pytest.raises(FolderNotAllowed, match="not available to the background service"):
        LocalConnector("share", tmp_path / "share", ["*.txt"], access=DenyingAccess())


def test_refused_subfolders_and_files(tree):
    connector = LocalConnector("docs", tree, ["*.txt"], access=DenyingAccess())

    paths = sorted(Path(f.path).as_posix() for f in connector.list_files_recursive())
    assert paths == ["locked.txt", "notes.txt", "public/plan.txt"]
    with pytest.raises(PermissionError):
        connector.read_file("locked.txt")
    with pytest.raises(PermissionError):
        connector.get_metadata("locked.txt")


def test_indexing_skips_what_the_policy_refuses(tree, tmp_path, monkeypatch):
    import rootmind.api.app as app_module

    cfg = AppConfig(connectors=[ConnectorConfig(name="docs", type="local", path=str(tree))])
    cfg.database.path = str(tmp_path / "db" / "index.db")
    cfg.indexer.include = ["*.txt"]
    real = app_module.create_connectors
    monkeypatch.setattr(
        app_module,
        "create_connectors",
        lambda config, access=UNRESTRICTED, errors=None: real(config, DenyingAccess(), errors),
    )

    repo = index_config(cfg)

    assert repo.search_fulltext("kettle")
    assert repo.search_fulltext("spoon")
    assert repo.search_fulltext("attic") == []
    assert repo.search_fulltext("vault") == []
    locked = repo.get_document_by_path("docs", "locked.txt")
    assert locked is not None and locked.status == "error" and locked.content_text is None
    repo.engine.dispose()


# --- create_connectors ---------------------------------------------------------------------


def test_create_connectors_reports_unusable_folders(tree, tmp_path):
    cfg = AppConfig(
        connectors=[
            ConnectorConfig(name="docs", type="local", path=str(tree)),
            ConnectorConfig(name="share", type="local", path=str(tmp_path / "share")),
            ConnectorConfig(name="gone", type="local", path=str(tmp_path / "gone")),
        ]
    )
    errors: dict[str, str] = {}

    connectors = create_connectors(cfg, access=DenyingAccess(), errors=errors)

    assert list(connectors) == ["docs"]
    assert connectors["docs"].access.__class__ is DenyingAccess
    assert errors["share"] == REFUSED_REASON
    assert "not found" in errors["gone"]


def test_create_connectors_defaults_are_unchanged(tree, tmp_path):
    cfg = AppConfig(
        connectors=[
            ConnectorConfig(name="docs", type="local", path=str(tree)),
            ConnectorConfig(name="gone", type="local", path=str(tmp_path / "gone")),
        ]
    )
    connectors = create_connectors(cfg)
    assert list(connectors) == ["docs"]
    assert connectors["docs"].access is UNRESTRICTED


# --- REST routes ------------------------------------------------------------------------


@pytest.fixture
def home_with_share(rlm_home, tiny_corpus, tmp_path) -> Path:
    """RLM_HOME's config with the tiny corpus (``docs``) and a refused folder (``share``)."""
    share = tmp_path / "share"
    share.mkdir()
    data = {
        "llm": {"provider": "anthropic", "api_keys": {"anthropic": TEST_API_KEY}},
        "connectors": [
            {"name": "docs", "type": "local", "path": str(tiny_corpus)},
            {"name": "share", "type": "local", "path": str(share)},
        ],
        "indexer": {"include": list(TINY_INCLUDE)},
    }
    (rlm_home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return rlm_home


def _saved_paths(home: Path) -> list[str]:
    data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    return [c["path"] for c in data["connectors"]]


def test_connectors_lists_refused_folders(home_with_share, tmp_path):
    with rest_client(create_app(folder_access=DenyingAccess())) as client:
        listed = client.get("/connectors").json()["connectors"]

    assert [c["name"] for c in listed] == ["docs", "share"]
    assert listed[0]["exists"] is True
    assert listed[1] == {
        "name": "share",
        "type": "LocalConnector",
        "root_path": str(tmp_path / "share"),
        "exists": False,
        "available": False,
        "error": REFUSED_REASON,
    }
    assert app_state.connector_errors == {"share": REFUSED_REASON}


def test_connectors_without_errors_unchanged(api):
    listed = api.get("/connectors").json()["connectors"]
    assert [c["name"] for c in listed] == ["docs"]
    assert "available" not in listed[0]


def test_post_config_refuses_a_folder_before_saving(rlm_home, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    before = (rlm_home / "config.yaml").read_text(encoding="utf-8")
    with rest_client(create_app(folder_access=DenyingAccess())) as client:
        body = client.get("/config").json()
        body["connectors"].append({"name": "share", "type": "local", "path": str(share)})

        response = client.post("/config", json=body)

        assert response.status_code == 400
        assert response.json()["detail"] == f"Cannot use {share}: {REFUSED_REASON}"
        assert (rlm_home / "config.yaml").read_text(encoding="utf-8") == before


def test_post_config_refuses_a_new_missing_folder(rlm_home, tmp_path):
    with rest_client(create_app(folder_access=DenyingAccess())) as client:
        body = client.get("/config").json()
        body["connectors"].append({"name": "gone", "type": "local", "path": str(tmp_path / "x")})
        response = client.post("/config", json=body)
    assert response.status_code == 400
    assert "folder not found" in response.json()["detail"]


def test_post_config_keeps_a_configured_folder_that_is_unavailable(rlm_home, tiny_corpus):
    with rest_client(create_app(folder_access=DenyingAccess())) as client:
        body = client.get("/config").json()
        tiny_corpus.rename(tiny_corpus.with_name("unplugged"))
        body["llm"]["provider"] = "openai"
        response = client.post("/config", json=body)
    assert response.status_code == 200
    assert _saved_paths(rlm_home) == [str(tiny_corpus)]


def test_post_config_keeps_a_configured_folder_that_is_refused(home_with_share, tmp_path):
    """A refused folder already in the config does not block saving other settings."""
    with rest_client(create_app(folder_access=DenyingAccess())) as client:
        body = client.get("/config").json()
        body["llm"]["provider"] = "openai"
        response = client.post("/config", json=body)

        assert response.status_code == 200, response.json()
        listed = {c["name"]: c for c in client.get("/connectors").json()["connectors"]}
    assert listed["share"]["available"] is False
    assert listed["share"]["error"] == REFUSED_REASON
    assert str(tmp_path / "share") in _saved_paths(home_with_share)
    saved = yaml.safe_load((home_with_share / "config.yaml").read_text(encoding="utf-8"))
    assert saved["llm"]["provider"] == "openai"


def test_post_config_accepts_allowed_folders(rlm_home, tree):
    access = DenyingAccess()
    with rest_client(create_app(folder_access=access)) as client:
        body = client.get("/config").json()
        body["connectors"].append({"name": "docs2", "type": "local", "path": str(tree)})

        response = client.post("/config", json=body)

        assert response.status_code == 200
        assert str(tree) in access.checked
        assert app_state.connectors["docs2"].access is access
    assert str(tree) in _saved_paths(rlm_home)


def test_post_config_without_connectors_checks_nothing(rlm_home, tiny_corpus):
    access = DenyingAccess()
    with rest_client(create_app(folder_access=access)) as client:
        access.checked.clear()
        response = client.post("/config", json={"llm": {"provider": "openai"}})
    assert response.status_code == 200
    # Only the reload after saving creates the connector again; no check before saving.
    assert access.checked == [str(tiny_corpus)]


def test_post_config_unrestricted_accepts_a_missing_folder(api, rlm_home, tmp_path):
    """As before: without a policy a missing folder is saved and reported as unavailable."""
    body = api.get("/config").json()
    body["connectors"].append({"name": "gone", "type": "local", "path": str(tmp_path / "x")})

    assert api.post("/config", json=body).status_code == 200

    assert str(tmp_path / "x") in _saved_paths(rlm_home)
    listed = {c["name"]: c for c in api.get("/connectors").json()["connectors"]}
    assert listed["gone"]["available"] is False
    assert "does not exist" in listed["gone"]["error"]
