"""Check that the backend, the desktop app and the MCP Bundle carry the same version.

The version lives in four files: ``backend/pyproject.toml``,
``desktop/package.json`` (``tauri.conf.json`` reads it from there),
``desktop/src-tauri/Cargo.toml`` and ``packaging/mcpb/manifest.json`` (the
Claude Desktop bundle). They are compared in their PEP 440 normal form, so the
npm, Cargo and bundle spelling ``1.2.0-rc.1`` matches the Python spelling
``1.2.0rc1``. With ``--tag``, the release tag (``v`` plus the version) must
name the same version as well.

Exits 1 and names the files when the versions differ, 2 when a version or the
tag cannot be read. Standard library only, so it runs with any Python 3.11+::

    python scripts/check_versions.py
    python scripts/check_versions.py --tag v1.2.0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The version syntax of PEP 440 (its appendix B), including the spellings it
# accepts and normalises, such as "-rc.1" or "-beta.2".
_PEP440 = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>
        [-_.]?
        (?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)
        [-_.]?
        (?P<pre_n>[0-9]+)?
    )?
    (?P<post>
        (?:-(?P<post_n1>[0-9]+))
        |
        (?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)
    )?
    (?P<dev>[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)
_PRE_LABELS = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}


class VersionError(ValueError):
    """A version string that is not a valid PEP 440 version, or a malformed tag."""


def normalize_version(version: str) -> str:
    """The PEP 440 normal form of ``version``: ``1.2.0-rc.1`` becomes ``1.2.0rc1``."""
    match = _PEP440.match(version)
    if not match:
        raise VersionError(f"not a valid version: {version!r}")
    parts = []
    if match["epoch"] and int(match["epoch"]):
        parts.append(f"{int(match['epoch'])}!")
    parts.append(".".join(str(int(n)) for n in match["release"].split(".")))
    if match["pre"]:
        parts.append(f"{_PRE_LABELS[match['pre_l'].lower()]}{int(match['pre_n'] or 0)}")
    if match["post"]:
        parts.append(f".post{int(match['post_n1'] or match['post_n2'] or 0)}")
    if match["dev"]:
        parts.append(f".dev{int(match['dev_n'] or 0)}")
    if match["local"]:
        local = re.split(r"[-_.]", match["local"].lower())
        parts.append("+" + ".".join(str(int(p)) if p.isdigit() else p for p in local))
    return "".join(parts)


def tag_version(tag: str) -> str:
    """The version a release tag names: ``v1.2.0`` names ``1.2.0``."""
    if not tag.startswith("v") or len(tag) < 2:
        raise VersionError(f"a release tag is 'v' plus the version, like v1.2.0, not {tag!r}")
    return tag[1:]


def versions(root: Path = ROOT) -> dict[str, str]:
    """The version declared in each file, keyed by its path relative to the repository."""
    pyproject = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((root / "desktop/package.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
    bundle = json.loads((root / "packaging/mcpb/manifest.json").read_text(encoding="utf-8"))
    return {
        "backend/pyproject.toml": pyproject["project"]["version"],
        "desktop/package.json": package["version"],
        "desktop/src-tauri/Cargo.toml": cargo["package"]["version"],
        "packaging/mcpb/manifest.json": bundle["version"],
    }


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the backend, the desktop app and the MCP Bundle carry the same version."
    )
    parser.add_argument("--tag", help="release tag that must name the same version, like v1.2.0")
    args = parser.parse_args(argv)

    found = versions(root)
    try:
        normalized = {path: normalize_version(version) for path, version in found.items()}
        expected = normalize_version(tag_version(args.tag)) if args.tag is not None else None
    except VersionError as error:
        print(f"check_versions: {error}", file=sys.stderr)
        return 2

    if len(set(normalized.values())) != 1:
        print("Version mismatch:", file=sys.stderr)
        for path, version in found.items():
            print(f"  {path}: {version}", file=sys.stderr)
        return 1
    version = next(iter(normalized.values()))
    if expected is not None and version != expected:
        print(f"The tag {args.tag} does not name the version in the files:", file=sys.stderr)
        for path, declared in found.items():
            print(f"  {path}: {declared}", file=sys.stderr)
        return 1

    spellings = sorted(set(found.values()) - {version})
    written = f" (written as {', '.join(spellings)})" if spellings else ""
    tag = f" and the tag {args.tag}" if args.tag is not None else ""
    print(f"All versions{tag} match: {version}{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
