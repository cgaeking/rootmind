"""Packaging helpers: the tree hash shared with the release scripts, and the exe's entry point."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"_packaging_{name}", PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tree_hash_algorithm(tmp_path):
    tree_hash = load("tree_hash")
    root = tmp_path / "rootmind-backend"
    (root / "_internal" / "sub").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "rootmind-backend.exe").write_bytes(b"exe")
    (root / "_internal" / "a.dll").write_bytes(b"dll")
    (root / "_internal" / "sub" / "b.txt").write_bytes(b"")

    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # Sorted "relpath\tsha256\n" lines, "/" separators, UTF-8; folders do not count.
    lines = (
        f"_internal/a.dll\t{sha(b'dll')}\n"
        f"_internal/sub/b.txt\t{sha(b'')}\n"
        f"rootmind-backend.exe\t{sha(b'exe')}\n"
    )
    assert tree_hash.tree_lines(root) == lines.splitlines(keepends=True)
    assert tree_hash.tree_sha256(root) == sha(lines.encode("utf-8"))


def test_tree_hash_changes_with_content_and_names(tmp_path):
    tree_hash = load("tree_hash")
    root = tmp_path / "t"
    root.mkdir()
    (root / "a").write_bytes(b"1")
    first = tree_hash.tree_sha256(root)
    (root / "a").write_bytes(b"2")
    second = tree_hash.tree_sha256(root)
    (root / "a").rename(root / "b")
    third = tree_hash.tree_sha256(root)
    assert len({first, second, third}) == 3


def test_tree_hash_cli(tmp_path, capsys):
    tree_hash = load("tree_hash")
    (tmp_path / "f").write_bytes(b"x")
    assert tree_hash.main([str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == tree_hash.tree_sha256(tmp_path)
    assert tree_hash.main([]) == 2
    assert tree_hash.main([str(tmp_path / "missing")]) == 2


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["rootmind-backend.exe"], ["rootmind-backend.exe", "api"]),
        (["rootmind-backend.exe", "--version"], ["rootmind-backend.exe", "--version"]),
        (
            ["rootmind-backend.exe", "service", "status"],
            ["rootmind-backend.exe", "service", "status"],
        ),
    ],
)
def test_entry_runs_api_without_arguments(monkeypatch, argv, expected):
    """No arguments means plain `api`: the bind then follows flags, env and config.yaml."""
    import rootmind.main

    seen = {}
    monkeypatch.setattr(rootmind.main, "main", lambda: seen.setdefault("argv", list(sys.argv)))
    monkeypatch.setattr(sys, "argv", list(argv))
    entry = load("entry")
    entry.run()
    assert seen["argv"] == expected
