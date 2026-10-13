"""Print the release notes for a tag: its CHANGELOG.md section plus a fixed footer.

``CHANGELOG.md`` follows Keep a Changelog: one ``## [X.Y.Z] - YYYY-MM-DD``
section per release. The notes are the body of the section the tag names
(versions are compared in their PEP 440 normal form, so ``v1.2.0-rc.1`` finds
``## [1.2.0-rc.1]``), followed by a footer that lists the release assets and
explains how to verify them while the binaries are unsigned.

Exits 1 when the changelog has no section for the version, or an empty one,
and 2 on a malformed tag. Standard library only::

    python scripts/release_notes.py v1.2.0 > notes.md
    python scripts/release_notes.py v1.2.0 --changelog CHANGELOG.md --installer RootMind-Setup-x64.exe
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from check_versions import ROOT, VersionError, normalize_version, tag_version

REPOSITORY = "cgaeking/rootmind"
INSTALLER = "RootMind-Setup-x64.exe"
BACKEND_ZIP = "rootmind-backend-windows-x64.zip"
MCP_BUNDLE = "rootmind.mcpb"
CHECKSUMS = "SHA256SUMS.txt"

_HEADING = re.compile(r"^##\s+\[(?P<label>[^\]]+)\]")
# Keep a Changelog ends with link definitions such as "[1.2.0]: https://...".
_VERSION_LINK = re.compile(r"^\[(?:unreleased|v?\d[^\]]*)\]:\s*\S", re.IGNORECASE)


class NotesError(Exception):
    """The changelog has no usable section for the requested version."""


def section(changelog: str, version: str) -> str:
    """The body of the changelog section for ``version``, without its heading."""
    wanted = normalize_version(version)
    body: list[str] | None = None
    fenced = False
    for line in changelog.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if (
            not fenced
            and body is not None
            and (line.startswith("## ") or _VERSION_LINK.match(line))
        ):
            break
        if not fenced and body is None:
            match = _HEADING.match(line)
            if match and _names_version(match["label"], wanted):
                body = []
            continue
        if body is not None:
            body.append(line.rstrip())
    if body is None:
        raise NotesError(
            f"the changelog has no section for {version} (expected '## [{version}] - <date>')"
        )
    text = "\n".join(body).strip("\n")
    if not text.strip():
        raise NotesError(f"the changelog section for {version} is empty")
    return text


def _names_version(label: str, wanted: str) -> bool:
    try:
        return normalize_version(label) == wanted
    except VersionError:
        return False  # "[Unreleased]" and other labels


def footer(installer_size: int | None = None) -> str:
    """Assets and verification steps; the same for every release.

    Each paragraph is one line: GitHub shows a line break inside a paragraph
    of release notes as a break.
    """
    size = f" ({installer_size / 1_000_000:.1f} MB)" if installer_size else ""
    return f"""---

### Downloads

| File | What it is |
| --- | --- |
| `{INSTALLER}` | The Windows app with its background service{size}. Installs for all users of the computer and sets up the service for your account; needs one administrator approval, no Python. |
| `{MCP_BUNDLE}` | RootMind for Claude Desktop: double-click it and Claude Desktop installs it. Needs RootMind installed with **Serve MCP over HTTP** on (the default). |
| `{BACKEND_ZIP}` | The backend on its own, for the command line and servers. Not needed for the app. |
| `{CHECKSUMS}` | SHA-256 checksums of these files. |

### Verify the download

The binaries are not code-signed yet. On the first start, Windows SmartScreen may show "Windows protected your PC" (choose More info, then Run anyway), and Smart App Control, when it is on, may block them; it has no exception for a single app. Check the files before you run them. The hash must match the line for the file in `{CHECKSUMS}`:

```powershell
(Get-FileHash .\\{INSTALLER} -Algorithm SHA256).Hash.ToLower()
```

```sh
sha256sum --check --ignore-missing {CHECKSUMS}
gh attestation verify {INSTALLER} --repo {REPOSITORY}
```

Every file has a build provenance attestation that ties it to this repository and to the workflow run that built it.

### Code signing policy

See [CODE_SIGNING.md](https://github.com/{REPOSITORY}/blob/main/CODE_SIGNING.md): free code signing provided by SignPath.io, certificate by SignPath Foundation; what is signed, team roles and the privacy policy.
"""


def notes(changelog: str, tag: str, installer_size: int | None = None) -> str:
    """The complete release notes for ``tag``."""
    return f"{section(changelog, tag_version(tag))}\n\n{footer(installer_size)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the release notes for a tag.")
    parser.add_argument("tag", help="release tag, like v1.2.0")
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    parser.add_argument("--installer", type=Path, help="the installer, to state its size")
    args = parser.parse_args(argv)

    try:
        tag_version(args.tag)
        changelog = args.changelog.read_text(encoding="utf-8")
        size = args.installer.stat().st_size if args.installer else None
        text = notes(changelog, args.tag, size)
    except VersionError as error:
        print(f"release_notes: {error}", file=sys.stderr)
        return 2
    except (NotesError, OSError) as error:
        print(f"release_notes: {error}", file=sys.stderr)
        return 1
    # UTF-8 with LF line endings on every platform, whatever the console uses.
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
