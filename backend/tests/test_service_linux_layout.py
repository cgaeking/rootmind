# ruff: noqa: S103 (the tests set lax modes on purpose)
"""The Linux data folder and program copy: paths everywhere, file modes on POSIX."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from rootmind.service import linux_layout

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX owners and modes")


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# --- paths (every OS) --------------------------------------------------------------------


def test_xdg_folders():
    env = {"HOME": "/home/me"}
    assert linux_layout.data_root(env).as_posix() == "/home/me/.local/share/rootmind"
    assert linux_layout.unit_dir(env).as_posix() == "/home/me/.config/systemd/user"
    env = {"HOME": "/home/me", "XDG_DATA_HOME": "/data/x", "XDG_CONFIG_HOME": "/cfg"}
    assert linux_layout.data_root(env).as_posix() == "/data/x/rootmind"
    assert linux_layout.unit_dir(env).as_posix() == "/cfg/systemd/user"
    # A relative value is invalid (XDG Base Directory spec) and ignored.
    env = {"HOME": "/home/me", "XDG_DATA_HOME": "rel", "XDG_CONFIG_HOME": ""}
    assert linux_layout.data_root(env).as_posix() == "/home/me/.local/share/rootmind"
    assert linux_layout.unit_dir(env).as_posix() == "/home/me/.config/systemd/user"


def test_exe_is_ours():
    root = Path("/home/me/.local/share/rootmind")
    ok = "/home/me/.local/share/rootmind/runtime/2.3.0-0a1b2c3d/rootmind-backend"
    assert linux_layout.exe_is_ours(ok, root)
    for exe in (
        "/home/me/.local/share/rootmind/runtime/rootmind-backend",
        "/home/me/.local/share/rootmind/runtime/a/b/rootmind-backend",
        "/home/me/.local/share/rootmind/runtime/../runtime/x/rootmind-backend",
        "/home/me/.local/share/rootmind/runtime/x/other",
        "/home/me/.local/share/rootmind/runtime//rootmind-backend",
        "/usr/lib/RootMind/backend/rootmind-backend",
        "/home/me/.local/share/rootmindx/runtime/a/rootmind-backend",
    ):
        assert not linux_layout.exe_is_ours(exe, root), exe


def test_home_of_program():
    exe = "/home/me/.local/share/rootmind/runtime/2.3.0-0a1b2c3d/rootmind-backend"
    assert linux_layout.home_of_program(exe).as_posix() == "/home/me/.local/share/rootmind"
    assert linux_layout.home_of_program("/usr/lib/RootMind/backend/rootmind-backend") is None
    assert linux_layout.home_of_program("/usr/bin/python3") is None


# --- the data folder (POSIX) ------------------------------------------------------------------


@posix
def test_a_new_data_folder_is_private(tmp_path):
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "share" / "rootmind"
    old = os.umask(0)  # even a lax umask gives no group or other bits
    try:
        assert linux_layout.prepare_data_tree(root, os.getuid()) is True
    finally:
        os.umask(old)
    assert mode(root) == 0o700
    for sub in (*linux_layout.DATA_SUBFOLDERS, "runtime"):
        assert mode(root / sub) == 0o700, sub
    assert linux_layout.validate_data_tree(root, os.getuid()) == []


@posix
def test_an_existing_folder_is_validated_and_normalized(tmp_path):
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "rootmind"
    (root / "data" / "db").mkdir(parents=True)
    os.chmod(root, 0o755)
    os.chmod(root / "data", 0o755)
    # Below data/, write bits for the group (umask 002) are fixed, not refused.
    os.chmod(root / "data" / "db", 0o775)
    token = root / "data" / "rest_token.txt"
    token.write_text("t")
    os.chmod(token, 0o644)
    config = root / "config.yaml"
    config.write_text("x")
    os.chmod(config, 0o640)
    assert linux_layout.prepare_data_tree(root, os.getuid()) is False
    assert mode(root) == 0o700
    assert mode(root / "data" / "db") == 0o700
    assert mode(token) == 0o600
    assert mode(config) == 0o600


@posix
@pytest.mark.parametrize(
    "bad", ["group_writable", "secret_writable", "symlink", "hardlink", "other_uid"]
)
def test_an_unprotected_folder_is_refused(tmp_path, bad):
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "rootmind"
    (root / "data").mkdir(parents=True)
    os.chmod(root, 0o700)
    uid = os.getuid()
    os.chmod(root / "data", 0o700)
    if bad == "group_writable":
        os.chmod(root / "data", 0o777)
    elif bad == "secret_writable":
        (root / "data" / "api_token.txt").write_text("t")
        os.chmod(root / "data" / "api_token.txt", 0o666)
    elif bad == "symlink":
        os.symlink(tmp_path, root / "data" / "link")
    elif bad == "hardlink":
        (root / "data" / "a").write_text("x")
        os.link(root / "data" / "a", root / "data" / "b")
    else:
        uid = uid + 1
    with pytest.raises(linux_layout.LayoutError) as raised:
        linux_layout.prepare_data_tree(root, uid)
    assert raised.value.problems
    assert linux_layout.validate_data_tree(root, uid)


@posix
def test_the_private_group_of_the_account_is_its_own(tmp_path):
    """Ubuntu's umask 002 makes folders group-writable for the account's own group."""
    if not linux_layout.group_is_private(os.getegid(), os.getuid()):
        pytest.skip("this account has no user private group")
    parent = tmp_path / "share"
    parent.mkdir()
    os.chmod(tmp_path, 0o755)
    os.chmod(parent, 0o775)
    assert linux_layout.prepare_data_tree(parent / "rootmind", os.getuid()) is True
    os.chmod(parent / "rootmind" / "data", 0o770)
    assert linux_layout.validate_data_tree(parent / "rootmind", os.getuid()) == []


def test_group_is_private_without_the_account_databases(monkeypatch):
    assert linux_layout.group_is_private(-1, -1) is False


@posix
def test_a_world_writable_parent_without_sticky_bit_is_refused(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    os.chmod(tmp_path, 0o755)
    os.chmod(parent, 0o777)
    with pytest.raises(linux_layout.LayoutError) as raised:
        linux_layout.prepare_data_tree(parent / "rootmind", os.getuid())
    assert any("sticky" in p for p in raised.value.problems)
    os.chmod(parent, 0o1777)  # like /tmp
    assert linux_layout.prepare_data_tree(parent / "rootmind", os.getuid()) is True


@posix
def test_the_start_check(tmp_path):
    os.chmod(tmp_path, 0o755)
    root = tmp_path / "rootmind"
    linux_layout.prepare_data_tree(root, os.getuid())
    token = root / "data" / "api_token.txt"
    linux_layout.write_private(token, b"t")
    assert linux_layout.check_service_home(root, os.getuid()) == []
    os.chmod(token, 0o640)
    assert linux_layout.check_service_home(root, os.getuid()) == [
        "data/api_token.txt: readable by the group or others"
    ]
    os.chmod(token, 0o600)
    os.chmod(root, 0o777)
    assert linux_layout.check_service_home(root, os.getuid())
    os.chmod(root, 0o700)
    assert linux_layout.check_service_home(root, os.getuid() + 1)


@posix
def test_write_private_is_0600_from_the_start(tmp_path):
    old = os.umask(0)
    try:
        path = tmp_path / "secret.txt"
        linux_layout.write_private(path, b"one")
        assert mode(path) == 0o600 and path.read_bytes() == b"one"
        linux_layout.write_private(path, b"two")
        assert mode(path) == 0o600 and path.read_bytes() == b"two"
    finally:
        os.umask(old)
    assert not [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]


@posix
def test_shared_writers_are_owner_only(tmp_path):
    """The token, config and status writers of the service create 0600 files."""
    from rootmind.auth import write_private_file
    from rootmind.config import _write_private_atomic
    from rootmind.remote_access import PrivateFiles
    from rootmind.service import common

    old = os.umask(0o022)
    try:
        write_private_file(tmp_path / "a", "x")
        _write_private_atomic(tmp_path / "b", "x")
        PrivateFiles().write_secret(tmp_path / "data" / "ngrok_authtoken.txt", "x" * 20)
        common.write_atomic(tmp_path / "c", "x")
        common.ensure_status_file(tmp_path / "d", 0o600)
        common.safe_append(tmp_path / "e.log", "x")
    finally:
        os.umask(old)
    for name in ("a", "b", "data/ngrok_authtoken.txt", "c", "d", "e.log"):
        assert mode(tmp_path / name) == 0o600, name


# --- the program copy (POSIX) -------------------------------------------------------------------


def make_build(folder: Path) -> Path:
    (folder / "_internal" / "lib").mkdir(parents=True)
    for path in (folder, folder / "_internal", folder / "_internal" / "lib"):
        os.chmod(path, 0o755)  # whatever the umask (Ubuntu gives accounts 002)
    exe = folder / "rootmind-backend"
    exe.write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(exe, 0o755)
    (folder / "_internal" / "lib" / "a.so").write_bytes(b"so")
    os.chmod(folder / "_internal" / "lib" / "a.so", 0o644)
    return folder


@posix
def test_install_program_copies_with_owner_only_modes(tmp_path):
    os.chmod(tmp_path, 0o755)
    source = make_build(tmp_path / "src")
    base = tmp_path / "rootmind" / "runtime"
    folder = linux_layout.install_program(source, base, "2.3.0")
    assert folder.parent == base and folder.name.startswith("2.3.0-")
    assert mode(folder) == 0o700
    assert mode(folder / "rootmind-backend") == 0o700
    assert mode(folder / "_internal" / "lib" / "a.so") == 0o600
    assert (folder / "_internal" / "lib" / "a.so").read_bytes() == b"so"
    second = linux_layout.install_program(source, base, "2.3.0")
    assert linux_layout.remove_version_folders(base, keep=second) == []
    assert [p.name for p in base.iterdir()] == [second.name]
    assert linux_layout.remove_program_base(base) == []
    assert not base.exists()


@posix
def test_install_program_refuses_links(tmp_path):
    source = make_build(tmp_path / "src")
    os.symlink("lib/a.so", source / "_internal" / "b.so")
    base = tmp_path / "runtime"
    with pytest.raises(linux_layout.LayoutError):
        linux_layout.install_program(source, base, "2.3.0")
    assert list(base.iterdir()) == []


@posix
def test_program_source_problems(tmp_path):
    os.chmod(tmp_path, 0o755)
    source = make_build(tmp_path / "src")
    os.chmod(source, 0o755)
    assert linux_layout.program_source_problems(source, os.getuid()) == []
    os.chmod(source / "_internal", 0o777)
    assert linux_layout.program_source_problems(source, os.getuid())
    os.chmod(source / "_internal", 0o755)
    if os.getuid() != 0:  # files of root are fine for everyone
        assert linux_layout.program_source_problems(source, os.getuid() + 1)
    os.symlink("x", source / "link")
    assert any(
        "symbolic link" in p for p in linux_layout.program_source_problems(source, os.getuid())
    )


@posix
def test_remove_tree_never_follows_links(tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "file").write_text("x")
    tree = tmp_path / "tree"
    tree.mkdir()
    os.symlink(keep, tree / "link")
    linux_layout.remove_tree(tree)
    assert not tree.exists() and (keep / "file").exists()
