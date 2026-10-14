"""scripts/check_versions.py: the four versions agree with each other and with the tag."""

from __future__ import annotations

import json

import check_versions
import pytest
from conftest import ROOT


@pytest.mark.parametrize(
    ("version", "normal"),
    [
        ("1.2.0", "1.2.0"),
        ("v1.2.0", "1.2.0"),
        ("01.02.00", "1.2.0"),
        ("1.2", "1.2"),
        ("1.2.0-rc.1", "1.2.0rc1"),
        ("1.2.0rc1", "1.2.0rc1"),
        ("1.2.0-RC.1", "1.2.0rc1"),
        ("1.2.0c1", "1.2.0rc1"),
        ("1.2.0-preview.3", "1.2.0rc3"),
        ("1.2.0-beta.2", "1.2.0b2"),
        ("1.2.0-alpha", "1.2.0a0"),
        ("1.2.0a1", "1.2.0a1"),
        ("1.2.0-1", "1.2.0.post1"),
        ("1.2.0.post2", "1.2.0.post2"),
        ("1.2.0-rev3", "1.2.0.post3"),
        ("1.2.0.dev4", "1.2.0.dev4"),
        ("1.2.0-rc.1.dev2", "1.2.0rc1.dev2"),
        ("1.2.0+Build.007", "1.2.0+build.7"),
        ("1!2.0", "1!2.0"),
        ("0!2.0", "2.0"),
    ],
)
def test_normalize_version(version, normal):
    assert check_versions.normalize_version(version) == normal


@pytest.mark.parametrize(
    "version", ["", "abc", "1.2.0-rc.1.2", "1.2.0-nightly", "1..2", "v", "1.2.0 rc1"]
)
def test_normalize_rejects_invalid_versions(version):
    with pytest.raises(check_versions.VersionError):
        check_versions.normalize_version(version)


def test_tag_version():
    assert check_versions.tag_version("v1.2.0") == "1.2.0"
    for tag in ("1.2.0", "v", "", "release-1.2.0"):
        with pytest.raises(check_versions.VersionError):
            check_versions.tag_version(tag)


def test_matching_versions(make_repo, capsys):
    root = make_repo()
    assert check_versions.main([], root=root) == 0
    assert capsys.readouterr().out.strip() == "All versions match: 1.2.0"


def test_mismatch_names_every_file(make_repo, capsys):
    root = make_repo(cargo="1.1.0")
    assert check_versions.main([], root=root) == 1
    err = capsys.readouterr().err
    assert "Version mismatch" in err
    assert "backend/pyproject.toml: 1.2.0" in err
    assert "desktop/package.json: 1.2.0" in err
    assert "desktop/src-tauri/Cargo.toml: 1.1.0" in err
    assert "packaging/mcpb/manifest.json: 1.2.0" in err


def test_bundle_version_is_checked(make_repo, capsys):
    root = make_repo(mcpb="1.1.0")
    assert check_versions.main([], root=root) == 1
    assert "packaging/mcpb/manifest.json: 1.1.0" in capsys.readouterr().err


def test_prerelease_spellings_match(make_repo, capsys):
    root = make_repo(
        backend="1.2.0rc1", package="1.2.0-rc.1", cargo="1.2.0-rc.1", mcpb="1.2.0-rc.1"
    )
    assert check_versions.main(["--tag", "v1.2.0-rc.1"], root=root) == 0
    out = capsys.readouterr().out
    assert "match: 1.2.0rc1" in out
    assert "1.2.0-rc.1" in out


def test_tag_must_name_the_version(make_repo, capsys):
    root = make_repo()
    assert check_versions.main(["--tag", "v1.2.0"], root=root) == 0
    assert "the tag v1.2.0 match" in capsys.readouterr().out
    assert check_versions.main(["--tag", "v1.2.1"], root=root) == 1
    assert "does not name the version" in capsys.readouterr().err
    assert check_versions.main(["--tag", "v1.2.0-rc.1"], root=root) == 1


def test_malformed_tag_or_version_exits_2(make_repo, capsys):
    root = make_repo()
    assert check_versions.main(["--tag", "1.2.0"], root=root) == 2
    assert check_versions.main(["--tag", "vnext"], root=root) == 2
    root = make_repo(package="next")
    assert check_versions.main([], root=root) == 2
    assert "not a valid version" in capsys.readouterr().err


def test_this_repository():
    """The acceptance check: the repository's own versions agree, and only their tag matches."""
    version = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))["version"]
    assert check_versions.main([]) == 0
    assert check_versions.main(["--tag", f"v{version}"]) == 0
    assert check_versions.main(["--tag", "v9.9.9"]) == 1
