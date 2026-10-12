"""Folders and permissions of the service: SDDL, data-tree validation, program copy.

The permission tests run on Windows, unelevated, against crafted temporary
trees. An unelevated process cannot make Administrators the owner, so the
tests pass the current account as an accepted owner and writer; everything
else is checked exactly as in production.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
from pathlib import Path

import pytest

from rootmind.service import layout
from rootmind.winaccess import service_sid

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows permissions")

USER = "S-1-5-21-1-2-3-1001"
MCP = "S-1-5-80-298245856-1475029704-2427701759-2504033446-509022668"


# --- SIDs and SDDL (every platform) -------------------------------------------------------


def test_mcp_account_sid_known_vector():
    # `sc.exe showsid RootMind-MCP-HTTP`
    assert layout.mcp_sid() == MCP == service_sid("RootMind-MCP-HTTP")


def test_root_sddl_without_mcp():
    assert layout.root_sddl(USER, None) == (
        "O:BAD:P(A;OICI;0x1f01ff;;;S-1-5-18)(A;OICI;0x1f01ff;;;S-1-5-32-544)"
        f"(A;OICI;0x1200a9;;;{USER})"
    )


def test_root_sddl_with_mcp():
    assert layout.root_sddl(USER, MCP).endswith(
        f"(A;OICI;0x1200a9;;;{USER})(A;OINP;0x1200a9;;;{MCP})"
    )


def test_tmp_and_program_sddl():
    assert layout.tmp_sddl() == (
        "O:BAD:P(A;OICI;0x1f01ff;;;S-1-5-18)(A;OICI;0x1f01ff;;;S-1-5-32-544)"
    )
    assert layout.program_sddl() == (
        "O:BAD:P(A;OICI;0x1f01ff;;;S-1-5-18)(A;OICI;0x1f01ff;;;S-1-5-32-544)"
        "(A;OICI;0x1200a9;;;S-1-5-32-545)"
    )


def test_data_layout_gives_mcp_only_its_entries():
    with_mcp = layout.data_layout(USER, MCP)
    assert with_mcp["data/db"].dacl_sddl() == f"D:(A;OICI;0x1301bf;;;{MCP})"
    assert with_mcp["logs/mcp-http"].dacl_sddl() == f"D:(A;OICI;0x1301bf;;;{MCP})"
    assert with_mcp["data/api_token.txt"].dacl_sddl() == f"D:(A;;0x120089;;;{MCP})"
    for name in ("data", "logs", "logs/backend"):
        assert with_mcp[name].dacl_sddl() == "D:"
    assert "data/rest_token.txt" not in with_mcp  # inherits only: no M entry
    without = layout.data_layout(USER, None)
    assert all(MCP not in entry.dacl_sddl() for entry in without.values())
    assert without["tmp"].protected and without[""].protected


def test_write_grants_ignores_deny_and_read_entries():
    security = layout.Security(
        owner=layout.ADMINISTRATORS,
        protected=True,
        aces=[
            (0, 0, layout.READ_EXECUTE, layout.USERS),  # allow read
            (1, 0, layout.FULL_CONTROL, "S-1-1-0"),  # deny
            (0, 0, layout.FULL_CONTROL, layout.SYSTEM),
            (0, 0, 0x2, "S-1-5-11"),  # allow write data
            (5, 0, 0x40000000, "S-1-5-4"),  # allow-object, generic write
            (0x11, 0, 0x10000000, "S-1-1-0"),  # unknown type: counts (fail closed)
        ],
    )
    assert layout.write_grants(security, layout.ADMIN_SIDS) == ["S-1-5-11", "S-1-5-4", "S-1-1-0"]
    assert layout.write_grants(layout.Security("x", False, None), layout.ADMIN_SIDS)


# --- files (every platform) ------------------------------------------------------------------


def make_tree(root: Path) -> None:
    (root / "_internal" / "sub").mkdir(parents=True)
    (root / "rootmind-backend.exe").write_bytes(b"MZ" + os.urandom(1000))
    (root / "_internal" / "a.dll").write_bytes(os.urandom(5000))
    (root / "_internal" / "sub" / "b.txt").write_bytes(b"")


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_copy_tree_verified(tmp_path):
    source = tmp_path / "source"
    make_tree(source)
    target = tmp_path / "target"
    target.mkdir()
    assert layout.copy_tree_verified(source, target) == 3
    assert tree_digest(target) == tree_digest(source)


def test_copy_refuses_hard_links(tmp_path):
    source = tmp_path / "source"
    make_tree(source)
    os.link(source / "_internal" / "a.dll", tmp_path / "elsewhere.dll")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(layout.LayoutError, match="hard link"):
        layout.copy_tree_verified(source, target)


def make_link(link: Path, target: Path) -> None:
    """A junction on Windows (no privilege needed), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_copy_refuses_links(tmp_path):
    source = tmp_path / "source"
    make_tree(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    make_link(source / "_internal" / "link", outside)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(layout.LayoutError, match="link or reparse point"):
        layout.copy_tree_verified(source, target)


def test_remove_tree_never_follows_links(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    tree = tmp_path / "tree"
    make_tree(tree)
    make_link(tree / "link", victim)
    assert layout.remove_tree(tree) == []
    assert not tree.exists()
    assert (victim / "keep.txt").read_text() == "keep"


def test_remove_version_folders_keeps_one(tmp_path):
    base = tmp_path / "RootMind Service"
    for name in ("1.1.0-aaaaaaaa", "1.2.0-bbbbbbbb", "1.2.0-cccccccc"):
        make_tree(base / name)
    assert layout.remove_version_folders(base, keep=base / "1.2.0-CCCCCCCC".lower()) == []
    assert [p.name for p in base.iterdir()] == ["1.2.0-cccccccc"]
    assert layout.remove_program_base(base) == []
    assert not base.exists()
    assert layout.remove_program_base(base) == []


def test_find_reparse_points(tmp_path):
    make_tree(tmp_path / "tree")
    assert layout.find_reparse_points(tmp_path / "tree") == []
    (tmp_path / "x").mkdir()
    make_link(tmp_path / "tree" / "_internal" / "link", tmp_path / "x")
    assert layout.find_reparse_points(tmp_path / "tree") == ["_internal/link"]


def test_safe_append(tmp_path):
    log = tmp_path / "logs" / "install.log"
    log.parent.mkdir()
    assert layout.safe_append(log, "one\n")
    assert layout.safe_append(log, "two\n")
    assert log.read_text() == "one\ntwo\n"


def test_safe_append_refuses_hard_links_and_linked_folders(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    logs = tmp_path / "logs"
    logs.mkdir()
    os.link(victim, logs / "install.log")
    assert not layout.safe_append(logs / "install.log", "x")
    assert victim.read_text() == "original"
    other = tmp_path / "other"
    other.mkdir()
    make_link(tmp_path / "linked", other)
    assert not layout.safe_append(tmp_path / "linked" / "install.log", "x")
    assert not (other / "install.log").exists()
    assert not layout.safe_append(tmp_path / "missing" / "install.log", "x")


# --- Windows permissions ---------------------------------------------------------------------


@pytest.fixture
def me():
    from rootmind.winaccess import current_user_sid

    return current_user_sid()


def set_dacl(path: Path, dacl_sddl: str, *, protected: bool) -> None:
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        dacl_sddl, win32security.SDDL_REVISION_1
    )
    info = win32security.DACL_SECURITY_INFORMATION | (
        win32security.PROTECTED_DACL_SECURITY_INFORMATION
        if protected
        else win32security.UNPROTECTED_DACL_SECURITY_INFORMATION
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        info,
        None,
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def full_control(sid: str) -> str:
    return f"(A;OICI;0x1f01ff;;;{sid})"


@pytest.fixture
def owned_root(tmp_path, me):
    """``RootMind`` with a protected DACL: SYSTEM, Administrators and the account (full)."""
    root = tmp_path / "RootMind"
    root.mkdir()
    set_dacl(
        root, "D:" + full_control("SY") + full_control("BA") + full_control(me), protected=True
    )
    for sub in layout.DATA_SUBFOLDERS:
        (root / sub).mkdir()
    (root / "config.yaml").write_text("database:\n  path: ./data/db/index.db\n")
    (root / "data" / "rest_token.txt").write_text("rest")
    (root / "data" / "api_token.txt").write_text("mcp")
    (root / "data" / "db" / "index.db").write_bytes(b"db")
    yield root
    # The account owns everything, so it can always restore its own access.
    for path in [root, *root.rglob("*")] if root.exists() else []:
        with contextlib.suppress(Exception):
            set_dacl(path, "D:" + full_control(me), protected=False)


def validate(root: Path, me: str) -> list[str]:
    return layout.validate_data_tree(
        root,
        mcp=MCP,
        owners=frozenset({layout.SYSTEM, layout.ADMINISTRATORS, me}),
        writers=layout.ADMIN_SIDS | {me},
    )


@windows
def test_known_folders():
    assert layout.program_files().name == "Program Files"
    assert layout.program_data().is_dir()
    assert layout.data_root() == layout.program_data() / "RootMind"


@windows
def test_create_directory_with_a_security_descriptor(tmp_path, me):
    folder = tmp_path / "created"
    # Without an owner part: an unelevated process cannot make Administrators the owner.
    layout.create_directory(
        folder, "D:P" + full_control("SY") + full_control(me) + "(A;OICI;0x1200a9;;;BU)"
    )
    security = layout.read_security(folder)
    assert security.protected
    assert {(ace[3], ace[2]) for ace in security.aces} == {
        (layout.SYSTEM, layout.FULL_CONTROL),
        (me, layout.FULL_CONTROL),
        (layout.USERS, layout.READ_EXECUTE),
    }
    with pytest.raises(FileExistsError):
        layout.create_directory(folder, "D:P" + full_control(me))


@windows
def test_a_clean_tree_is_accepted(owned_root, me):
    assert validate(owned_root, me) == []


@windows
def test_the_owner_must_be_an_administrator_sid(owned_root):
    problems = layout.validate_data_tree(owned_root, mcp=MCP)
    assert problems and all("owned by" in p or "may change it" in p for p in problems)


@windows
def test_a_write_entry_for_users_is_refused(owned_root, me):
    set_dacl(owned_root / "data", "D:(A;OICI;0x116;;;BU)", protected=False)
    problems = validate(owned_root, me)
    assert any(p.startswith("data: S-1-5-32-545 may change it") for p in problems)
    # inherited by everything below
    assert any(p.startswith("data/rest_token.txt: S-1-5-32-545") for p in problems)


@windows
def test_mcp_may_write_only_in_its_folders(owned_root, me):
    modify = f"D:(A;OICI;0x1301bf;;;{MCP})"
    set_dacl(owned_root / "data" / "db", modify, protected=False)
    set_dacl(owned_root / "logs" / "mcp-http", modify, protected=False)
    assert validate(owned_root, me) == []
    set_dacl(owned_root / "data", modify, protected=False)
    problems = validate(owned_root, me)
    assert f"data: {MCP} may change it" in problems
    assert f"data/rest_token.txt: {MCP} may change it" in problems


@windows
def test_an_inherited_create_right_is_refused(tmp_path, me):
    """A folder made by hand below ProgramData inherits "Users: create" and is refused."""
    parent = tmp_path / "ProgramData"
    parent.mkdir()
    set_dacl(
        parent,
        "D:" + full_control("SY") + full_control("BA") + full_control(me) + "(A;CI;0x116;;;BU)",
        protected=True,
    )
    root = parent / "RootMind"
    root.mkdir()
    assert any("S-1-5-32-545 may change it" in p for p in validate(root, me))


@windows
def test_links_and_hard_links_are_refused(owned_root, me, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_link(owned_root / "logs" / "backend" / "link", outside)
    os.link(owned_root / "config.yaml", tmp_path / "config-link.yaml")
    problems = validate(owned_root, me)
    assert "logs/backend/link: is a link or reparse point" in problems
    assert "config.yaml: has 2 hard links" in problems


@windows
def test_normalize_applies_the_layout(owned_root, me):
    set_dacl(owned_root / "data", "D:(A;OICI;0x1200a9;;;BU)", protected=False)  # stray entry
    layout.normalize_data_tree(owned_root, me, MCP, set_owner=False)

    def explicit(path: Path) -> set[tuple[int, int, str]]:
        return {
            (flags & 0x0F, mask, sid)
            for _type, flags, mask, sid in layout.read_security(path).aces
            if not flags & 0x10  # INHERITED_ACE
        }

    def inherited_sids(path: Path) -> set[str]:
        return {sid for _t, flags, _m, sid in layout.read_security(path).aces if flags & 0x10}

    root_security = layout.read_security(owned_root)
    assert root_security.protected
    assert explicit(owned_root) == {
        (0x3, layout.FULL_CONTROL, layout.SYSTEM),
        (0x3, layout.FULL_CONTROL, layout.ADMINISTRATORS),
        (0x3, layout.READ_EXECUTE, me),
        (0x5, layout.READ_EXECUTE, MCP),  # OI + NP
    }
    assert explicit(owned_root / "data") == set()
    assert MCP not in inherited_sids(owned_root / "data")
    assert layout.USERS not in inherited_sids(owned_root / "data")
    assert explicit(owned_root / "data" / "db") == {(0x3, layout.MODIFY, MCP)}
    assert MCP in inherited_sids(owned_root / "data" / "db" / "index.db")
    assert explicit(owned_root / "data" / "api_token.txt") == {(0x0, layout.READ, MCP)}
    rest = layout.read_security(owned_root / "data" / "rest_token.txt")
    assert MCP not in {sid for *_rest, sid in rest.aces}
    assert MCP in inherited_sids(owned_root / "config.yaml")  # M reads the config
    assert explicit(owned_root / "logs" / "mcp-http") == {(0x3, layout.MODIFY, MCP)}
    assert validate(owned_root, me) == []

    # MCP off: every entry of M goes away.
    layout.normalize_data_tree(owned_root, me, None, set_owner=False)
    for path in [owned_root, *owned_root.rglob("*")]:
        assert MCP not in {sid for *_rest, sid in layout.read_security(path).aces}, path


@windows
def test_permissions_are_never_written_through_a_link(owned_root, me, tmp_path):
    """A folder swapped for a link after the walk: its target keeps its permissions."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    before = layout.read_security(target)
    swapped = owned_root / "logs" / "mcp-http"
    swapped.rmdir()
    make_link(swapped, target)

    with pytest.raises(layout.LayoutError, match="link or reparse point"):
        layout._set_dacl(swapped, layout.data_layout(me, MCP)["logs/mcp-http"])

    assert layout.read_security(target) == before
    swapped.rmdir()  # removes the junction only


@windows
def test_check_admin_only(tmp_path, me):
    folder = tmp_path / "1.2.0-0a1b2c3d"
    folder.mkdir()
    (folder / "rootmind-backend.exe").write_bytes(b"MZ")
    set_dacl(
        folder,
        "D:"
        + full_control("SY")
        + full_control("BA")
        + full_control(me)
        + "(A;OICI;0x1200a9;;;BU)"
        + "(A;OICIIO;GA;;;CO)",  # CREATOR OWNER template: grants nothing here
        protected=True,
    )
    owners = layout.ADMIN_SIDS | {me}
    assert layout.check_admin_only(folder, recursive=True, owners=owners, writers=owners) == []
    assert layout.check_admin_only(folder, recursive=False)  # the account is not an admin SID
    set_dacl(folder / "rootmind-backend.exe", "D:(A;;0x116;;;AU)", protected=False)
    problems = layout.check_admin_only(folder, recursive=True, owners=owners, writers=owners)
    assert problems == ["rootmind-backend.exe: S-1-5-11 may change it"]


# --- program folder (every platform, with the Windows calls replaced) ----------------------------


@pytest.fixture
def plain_folders(monkeypatch):
    """create_directory without security descriptors, and a recorder for check_admin_only."""
    created: list[str] = []

    def create_directory(path, sddl):
        assert sddl == layout.program_sddl()
        os.mkdir(path)  # FileExistsError when it exists, like CreateDirectoryW
        created.append(Path(path).name)

    def set_dacl(path, entry, *, owner_admins=False):
        assert entry.protected and owner_admins  # re-protecting an existing base folder
        created.append(f"protected {Path(path).name}")

    monkeypatch.setattr(layout, "create_directory", create_directory)
    monkeypatch.setattr(layout, "_set_dacl", set_dacl)
    monkeypatch.setattr(layout, "check_admin_only", lambda *args, **kwargs: [])
    return created


def test_version_folders_are_unique(tmp_path, plain_folders, monkeypatch):
    base = tmp_path / "RootMind Service"
    base.mkdir()
    (base / "1.2.0-00000000").mkdir()
    tokens = iter(["00000000", "0a1b2c3d"])
    monkeypatch.setattr(layout.secrets, "token_hex", lambda n: next(tokens))
    folder = layout.new_version_folder(base, "1.2.0")
    assert folder.name == "1.2.0-0a1b2c3d"
    assert sorted(p.name for p in base.iterdir()) == ["1.2.0-00000000", "1.2.0-0a1b2c3d"]


def test_version_folder_names_are_safe(tmp_path, plain_folders):
    base = tmp_path / "base"
    base.mkdir()
    name = layout.new_version_folder(base, r"1.2.0rc1+local/..\x").name
    assert name.startswith("1.2.0rc1+local_.._x-")
    assert len(name.rsplit("-", 1)[1]) == 8


def test_install_program_copies_into_a_new_folder(tmp_path, plain_folders):
    source = tmp_path / "rootmind-backend"
    make_tree(source)
    base = tmp_path / "Program Files" / "RootMind Service"
    base.parent.mkdir()
    first = layout.install_program(source, base, "1.2.0")
    second = layout.install_program(source, base, "1.2.0")
    assert first != second and first.parent == second.parent == base
    assert tree_digest(first) == tree_digest(source) == tree_digest(second)
    assert plain_folders[0] == "RootMind Service"
    assert "protected RootMind Service" in plain_folders  # the second install
    assert layout.remove_version_folders(base, keep=second) == []
    assert [p.name for p in base.iterdir()] == [second.name]


def test_install_program_removes_an_unprotected_copy(tmp_path, plain_folders, monkeypatch):
    source = tmp_path / "rootmind-backend"
    make_tree(source)
    base = tmp_path / "RootMind Service"
    monkeypatch.setattr(layout, "check_admin_only", lambda *args, **kwargs: ["x: S-1-1-0"])
    with pytest.raises(layout.LayoutError, match="not protected after the copy"):
        layout.install_program(source, base, "1.2.0")
    assert list(base.iterdir()) == []
