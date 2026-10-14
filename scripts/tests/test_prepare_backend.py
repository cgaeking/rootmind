"""scripts/prepare_backend.py: copy the onedir build into the app's resources, checked."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import prepare_backend
import pytest
from conftest import ROOT


def load_backend_tree_hash():
    path = ROOT / "backend" / "packaging" / "tree_hash.py"
    spec = importlib.util.spec_from_file_location("_backend_tree_hash", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_version(monkeypatch):
    """Answer `rootmind-backend.exe --version` without running anything; records the calls."""
    calls = []
    answer = {"stdout": f"rootmind-backend {prepare_backend.expected_version()}\n", "returncode": 0}

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, answer["returncode"], answer["stdout"], "boom")

    monkeypatch.setattr(prepare_backend.subprocess, "run", run)
    return calls, answer


def prepare(source: Path, dest: Path, *extra: str) -> int:
    return prepare_backend.main(
        ["--from", str(source), "--dest", str(dest), "--min-size-mb", "0", *extra]
    )


def test_tree_hash_vector(tmp_path):
    """The algorithm, pinned with the same vector as backend/tests/test_service_packaging.py."""
    root = tmp_path / "rootmind-backend"
    (root / "_internal" / "sub").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "rootmind-backend.exe").write_bytes(b"exe")
    (root / "_internal" / "a.dll").write_bytes(b"dll")
    (root / "_internal" / "sub" / "b.txt").write_bytes(b"")

    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    lines = (
        f"_internal/a.dll\t{sha(b'dll')}\n"
        f"_internal/sub/b.txt\t{sha(b'')}\n"
        f"rootmind-backend.exe\t{sha(b'exe')}\n"
    )
    assert prepare_backend.tree_sha256(root) == sha(lines.encode("utf-8"))


def test_tree_hash_equals_the_backend_packaging_helper(onedir):
    (onedir / "_internal" / "données-ä.txt").write_bytes("é".encode())
    backend = load_backend_tree_hash()
    assert prepare_backend.tree_lines(onedir) == backend.tree_lines(onedir)
    assert prepare_backend.tree_sha256(onedir) == backend.tree_sha256(onedir)


def test_tree_hash_cli(onedir, tmp_path, capsys):
    assert prepare_backend.main(["--tree-hash", str(onedir)]) == 0
    assert capsys.readouterr().out.strip() == prepare_backend.tree_sha256(onedir)
    assert prepare_backend.main(["--tree-hash", str(tmp_path / "missing")]) == 2


def test_copies_the_tree_and_checks_it(onedir, tmp_path, fake_version, capsys):
    calls, _ = fake_version
    dest = tmp_path / "binaries" / "rootmind-backend"
    expected = prepare_backend.tree_sha256(onedir)
    assert prepare(onedir, dest, "--expect-tree-sha256", expected.upper()) == 0
    assert prepare_backend.tree_sha256(dest) == expected
    # Files whose names start with a dot are part of the build.
    assert (
        dest / "_internal" / "docx" / "templates" / "default-docx-template" / "_rels" / ".rels"
    ).is_file()
    assert (dest / "_internal" / "anthropic" / "lib" / ".keep").is_file()
    # The copy is what runs, from its own folder.
    [(cmd, kwargs)] = calls
    assert cmd == [str(dest / "rootmind-backend.exe"), "--version"]
    assert kwargs["cwd"] == dest
    assert kwargs["timeout"] > 0
    out = capsys.readouterr().out
    assert f"Tree SHA-256: {expected}" in out
    assert "Copied 7 files" in out


def test_default_source_and_destination():
    assert ROOT / "backend" / "dist" / "rootmind-backend" == prepare_backend.DEFAULT_SOURCE
    assert (
        ROOT / "desktop" / "src-tauri" / "binaries" / "rootmind-backend"
        == prepare_backend.DEFAULT_DEST
    )
    release_overlay = (ROOT / "desktop" / "src-tauri" / "tauri.release.conf.json").read_text(
        encoding="utf-8"
    )
    assert '"binaries/rootmind-backend/"' in release_overlay


def test_wrong_tree_hash_changes_nothing(onedir, tmp_path, fake_version, capsys):
    dest = tmp_path / "dest"
    assert prepare(onedir, dest, "--expect-tree-sha256", "0" * 64) == 1
    assert "expected " + "0" * 64 in capsys.readouterr().err
    assert not dest.exists()


def test_placeholder_is_refused(onedir, tmp_path, fake_version, capsys):
    dest = tmp_path / "dest"
    code = prepare_backend.main(["--from", str(onedir), "--dest", str(dest), "--min-size-mb", "1"])
    assert code == 1
    assert "less than 1 MB" in capsys.readouterr().err
    assert not dest.exists()
    assert prepare_backend.DEFAULT_MIN_SIZE_MB >= 10


@pytest.mark.parametrize("missing", ["rootmind-backend.exe", "_internal"])
def test_not_a_onedir_build(onedir, tmp_path, fake_version, missing, capsys):
    target = onedir / missing
    shutil.rmtree(target) if target.is_dir() else target.unlink()
    assert prepare(onedir, tmp_path / "dest") == 1
    assert missing in capsys.readouterr().err


def test_missing_source(tmp_path, fake_version, capsys):
    assert prepare(tmp_path / "nothing", tmp_path / "dest") == 1
    assert "is not a folder" in capsys.readouterr().err


def test_replaces_a_previous_copy(onedir, tmp_path, fake_version):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "rootmind-backend.exe").write_bytes(b"old")
    (dest / "stale.dll").write_bytes(b"stale")
    assert prepare(onedir, dest) == 0
    assert not (dest / "stale.dll").exists()
    assert prepare_backend.tree_sha256(dest) == prepare_backend.tree_sha256(onedir)


def test_empty_destination_is_fine(onedir, tmp_path, fake_version):
    dest = tmp_path / "dest"
    dest.mkdir()
    assert prepare(onedir, dest) == 0


def test_refuses_to_replace_something_else(onedir, tmp_path, fake_version, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "notes.txt").write_text("keep me", encoding="utf-8")
    assert prepare(onedir, dest) == 1
    assert "refusing to replace" in capsys.readouterr().err
    assert (dest / "notes.txt").read_text(encoding="utf-8") == "keep me"
    other = tmp_path / "file"
    other.write_text("x", encoding="utf-8")
    assert prepare(onedir, other) == 1


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"cannot create symbolic links here: {error}")


def _directory_link(link: Path, target: Path) -> None:
    """A junction on Windows (no privilege needed), else a symbolic link."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        _symlink_or_skip(link, target, directory=True)


def test_link_in_the_source_is_refused(onedir, tmp_path, fake_version, capsys):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    _symlink_or_skip(onedir / "_internal" / "link.txt", outside, directory=False)
    assert prepare(onedir, tmp_path / "dest") == 1
    assert "is a link" in capsys.readouterr().err


def test_linked_folder_in_the_source_is_refused(onedir, tmp_path, fake_version, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _directory_link(onedir / "_internal" / "linked", outside)
    assert prepare(onedir, tmp_path / "dest") == 1
    assert "is a link" in capsys.readouterr().err
    assert not (tmp_path / "dest").exists()


def test_linked_destination_is_not_followed(onedir, tmp_path, fake_version, capsys):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "rootmind-backend.exe").write_bytes(b"other")
    dest = tmp_path / "dest"
    _directory_link(dest, target)
    assert prepare(onedir, dest) == 1
    assert "is a link" in capsys.readouterr().err
    assert (target / "rootmind-backend.exe").read_bytes() == b"other"


def test_version_mismatch(onedir, tmp_path, fake_version, capsys):
    _, answer = fake_version
    answer["stdout"] = "rootmind-backend 0.0.1\n"
    assert prepare(onedir, tmp_path / "dest") == 1
    assert "printed '0.0.1'" in capsys.readouterr().err


@pytest.mark.parametrize("stdout", ["", "garbage-without-version\n"])
def test_version_output_unusable(onedir, tmp_path, fake_version, stdout):
    _, answer = fake_version
    answer["stdout"] = stdout
    assert prepare(onedir, tmp_path / "dest") == 1


def test_version_exit_code(onedir, tmp_path, fake_version, capsys):
    _, answer = fake_version
    answer["returncode"] = 3
    assert prepare(onedir, tmp_path / "dest") == 1
    assert "exited with 3" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error", [OSError(1260, "blocked by policy"), subprocess.TimeoutExpired("exe", 1)]
)
def test_version_does_not_run(onedir, tmp_path, monkeypatch, error, capsys):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(prepare_backend.subprocess, "run", run)
    assert prepare(onedir, tmp_path / "dest") == 1
    assert "could not run" in capsys.readouterr().err


def test_prerelease_versions_compare_in_normal_form(onedir, tmp_path, monkeypatch):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "rootmind-backend 1.2.0rc1\n", "")

    monkeypatch.setattr(prepare_backend.subprocess, "run", run)
    tree = prepare_backend.prepare(
        onedir, tmp_path / "dest", expect_tree_sha256=None, min_size_mb=0, version="1.2.0rc1"
    )
    assert tree == prepare_backend.tree_sha256(onedir)


def test_expected_version_reads_pyproject(make_repo):
    assert prepare_backend.expected_version(make_repo(backend="1.2.0-rc.1")) == "1.2.0rc1"


def test_a_linux_build(onedir, tmp_path, fake_version):
    """A Linux onedir build has `rootmind-backend` without .exe; it is run under that name."""
    calls, _ = fake_version
    (onedir / "rootmind-backend.exe").rename(onedir / "rootmind-backend")
    dest = tmp_path / "dest"
    assert prepare(onedir, dest) == 0
    assert calls[0][0] == [str(dest / "rootmind-backend"), "--version"]
    # A previous Linux copy is replaced like a Windows one.
    assert prepare(onedir, dest) == 0
