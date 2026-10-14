"""scripts/release_notes.py: the changelog section for a tag plus the fixed footer."""

from __future__ import annotations

import release_notes
from conftest import FIXTURES

CHANGELOG = FIXTURES / "CHANGELOG.md"


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = release_notes.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_prints_the_section_and_the_footer(capsys):
    code, out, _ = run(capsys, "v1.2.0", "--changelog", str(CHANGELOG))
    assert code == 0
    assert out.startswith("### Added\n\n- A one-step Windows installer")
    assert "`rootmind api` now uses `api.host`" in out
    # Only this section: not the heading, nor the neighbouring sections.
    assert "## [1.2.0]" not in out
    assert "not released yet" not in out
    assert "release candidate" not in out
    footer = out.split("\n---\n", 1)[1]
    assert "`RootMind-Setup-x64.exe`" in footer
    assert "`rootmind-backend-windows-x64.zip`" in footer
    assert "Not needed for the app" in footer
    assert "`rootmind.mcpb`" in footer
    assert "Claude Desktop" in footer
    # The downloads table lists the installer, the Linux packages, the bundle,
    # the backend archives and the checksums.
    rows = [line for line in footer.splitlines() if line.startswith("| `")]
    assert [row.split("`")[1] for row in rows] == [
        "RootMind-Setup-x64.exe",
        "RootMind-amd64.deb",
        "RootMind-x86_64.rpm",
        "RootMind-x86_64.AppImage",
        "rootmind.mcpb",
        "rootmind-backend-windows-x64.zip",
        "rootmind-backend-linux-x64.tar.gz",
        "SHA256SUMS.txt",
    ]
    assert "sudo apt install ./RootMind-amd64.deb" in footer
    assert "sudo dnf install ./RootMind-x86_64.rpm" in footer
    assert "chmod +x RootMind-x86_64.AppImage" in footer
    assert "systemd user services" in footer
    assert "not code-signed" in footer
    assert "SmartScreen" in footer
    assert "sha256sum --check --ignore-missing SHA256SUMS.txt" in footer
    assert "gh attestation verify RootMind-Setup-x64.exe --repo cgaeking/rootmind" in footer
    assert "\r" not in out


def test_missing_section_exits_1(capsys):
    code, out, err = run(capsys, "v1.3.0", "--changelog", str(CHANGELOG))
    assert code == 1
    assert out == ""
    assert "no section for 1.3.0" in err


def test_empty_section_exits_1(capsys):
    code, _, err = run(capsys, "v1.1.1", "--changelog", str(CHANGELOG))
    assert code == 1
    assert "empty" in err


def test_unreleased_is_never_a_release(capsys):
    code, _, _ = run(capsys, "vUnreleased", "--changelog", str(CHANGELOG))
    assert code == 2


def test_prerelease_section_keeps_code_blocks(capsys):
    code, out, _ = run(capsys, "v1.2.0-rc.1", "--changelog", str(CHANGELOG))
    assert code == 0
    assert "A release candidate of the installer." in out
    # The heading-like line inside the code block belongs to the section.
    assert "## [9.9.9] - a heading inside a code block" in out
    assert "1.1.1" not in out.split("\n---\n", 1)[0]


def test_a_heading_inside_a_code_block_is_not_a_section(capsys):
    code, _, _ = run(capsys, "v9.9.9", "--changelog", str(CHANGELOG))
    assert code == 1


def test_link_definitions_after_the_last_section_are_dropped(capsys):
    code, out, _ = run(capsys, "v1.1.0", "--changelog", str(CHANGELOG))
    assert code == 0
    notes = out.split("\n---\n", 1)[0]
    assert notes.strip() == "### Added\n\n- The first tagged version."


def test_versions_compare_in_normal_form(tmp_path):
    changelog = "## [1.2.0rc1] - 2026-10-15\n\n- Candidate.\n"
    assert release_notes.section(changelog, "1.2.0-rc.1") == "- Candidate."


def test_installer_size(tmp_path, capsys):
    installer = tmp_path / "RootMind-Setup-x64.exe"
    installer.write_bytes(b"\0" * 31_400_000)
    code, out, _ = run(
        capsys, "v1.2.0", "--changelog", str(CHANGELOG), "--installer", str(installer)
    )
    assert code == 0
    assert "with its background service (31.4 MB)" in out


def test_malformed_tag_and_missing_files(tmp_path, capsys):
    assert run(capsys, "1.2.0", "--changelog", str(CHANGELOG))[0] == 2
    code, _, err = run(capsys, "v1.2.0", "--changelog", str(tmp_path / "missing.md"))
    assert code == 1
    assert "missing.md" in err
    code, _, _ = run(
        capsys,
        "v1.2.0",
        "--changelog",
        str(CHANGELOG),
        "--installer",
        str(tmp_path / "missing.exe"),
    )
    assert code == 1


def test_crlf_changelog(tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(CHANGELOG.read_bytes().replace(b"\n", b"\r\n"))
    code, out, _ = run(capsys, "v1.2.0", "--changelog", str(changelog))
    assert code == 0
    assert out.startswith("### Added\n\n- A one-step")
    assert "\r" not in out
