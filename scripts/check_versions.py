"""Check that the backend, the desktop app and the MCP Bundle carry the same version.

The version lives in four files: ``backend/pyproject.toml``,
``desktop/package.json`` (``tauri.conf.json`` reads it from there),
``desktop/src-tauri/Cargo.toml`` and ``packaging/mcpb/manifest.json`` (the
Claude Desktop bundle). They are compared in their PEP 440 normal form, so the
npm, Cargo and bundle spelling ``1.2.0-rc.1`` matches the Python spelling
``1.2.0rc1``. With ``--tag``, the release tag (``v`` plus the version) must
name the same version as well.

It also checks ``packaging/mcpb/server.json``, the template of the bundle's
entry in the MCP Registry (``scripts/registry_server_json.py`` renders it for
a release). The template carries no version of its own: ``version`` and the
download URL must use the ``${VERSION}`` placeholder and ``fileSha256`` the
``${FILE_SHA256}`` placeholder, so it cannot fall behind. Its ``name`` must be
``io.github.<owner>/<bundle name>`` for the repository that
``manifest.json`` names, and its ``$schema`` must be the URL of a schema
vendored in ``packaging/mcpb/schemas/``.

Exits 1 and names the files when the versions differ or the template does not
match, 2 when a version or the tag cannot be read. Standard library only, so it runs with any Python 3.11+::

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


MCPB_MANIFEST = "packaging/mcpb/manifest.json"
REGISTRY_TEMPLATE = "packaging/mcpb/server.json"
REGISTRY_SCHEMAS = "packaging/mcpb/schemas"
# The bundle's file name in a GitHub release.
MCPB_ASSET = "rootmind.mcpb"
_SCHEMA_URL = re.compile(
    r"^https://static\.modelcontextprotocol\.io/schemas/(?P<date>\d{4}-\d{2}-\d{2})/server\.schema\.json$"
)
_GITHUB_REPO = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


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
    bundle = json.loads((root / MCPB_MANIFEST).read_text(encoding="utf-8"))
    return {
        "backend/pyproject.toml": pyproject["project"]["version"],
        "desktop/package.json": package["version"],
        "desktop/src-tauri/Cargo.toml": cargo["package"]["version"],
        MCPB_MANIFEST: bundle["version"],
    }


def schema_path(schema_url: str, root: Path = ROOT) -> Path | None:
    """The vendored copy of a server.json schema URL, or None for a URL of another form."""
    match = _SCHEMA_URL.match(schema_url)
    if not match:
        return None
    return root / REGISTRY_SCHEMAS / f"server.schema.{match['date']}.json"


def registry_expectations(root: Path = ROOT) -> dict[str, str]:
    """What the registry entry must say, derived from the bundle's manifest.json.

    Keys: ``name`` (``io.github.<owner>/<bundle name>``), ``repository`` (the
    repository URL without ``.git``) and ``identifier`` (the release download
    URL of the bundle with the ``${VERSION}`` placeholder).
    """
    manifest = json.loads((root / MCPB_MANIFEST).read_text(encoding="utf-8"))
    url = (manifest.get("repository") or {}).get("url", "")
    match = _GITHUB_REPO.match(url)
    if not match:
        raise ValueError(f"{MCPB_MANIFEST}: repository.url is not a GitHub repository: {url!r}")
    repository = f"https://github.com/{match['owner']}/{match['repo']}"
    return {
        "name": f"io.github.{match['owner'].lower()}/{manifest['name']}",
        "repository": repository,
        "identifier": f"{repository}/releases/download/v${{VERSION}}/{MCPB_ASSET}",
    }


def registry_template_problems(root: Path = ROOT) -> list[str]:
    """What is wrong with the MCP Registry template; an empty list when it is consistent."""
    path = root / REGISTRY_TEMPLATE
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
        expected = registry_expectations(root)
    except (OSError, ValueError, KeyError) as error:
        return [f"{REGISTRY_TEMPLATE}: {error}"]
    if not isinstance(template, dict):
        return [f"{REGISTRY_TEMPLATE}: not a JSON object"]

    problems = []
    if template.get("name") != expected["name"]:
        problems.append(f"name is {template.get('name')!r}, expected {expected['name']!r}")
    schema_url = template.get("$schema", "")
    schema = schema_path(schema_url, root) if isinstance(schema_url, str) else None
    if schema is None:
        problems.append(f"$schema is not a server.json schema URL: {schema_url!r}")
    elif not schema.is_file():
        problems.append(
            f"$schema names a schema that is not vendored as {schema.relative_to(root).as_posix()}"
        )
    else:
        try:
            schema_id = json.loads(schema.read_text(encoding="utf-8")).get("$id")
        except ValueError as error:
            problems.append(f"{schema.name}: {error}")
        else:
            if schema_id != schema_url:
                problems.append(f"{schema.name} has $id {schema_id!r}, not {schema_url!r}")
    if (template.get("repository") or {}).get("url") != expected["repository"]:
        problems.append(f"repository.url must be {expected['repository']!r}")
    if template.get("version") != "${VERSION}":
        problems.append(
            f"version must be the placeholder ${{VERSION}}, not {template.get('version')!r}"
        )
    packages = template.get("packages")
    if not isinstance(packages, list) or len(packages) != 1 or not isinstance(packages[0], dict):
        problems.append("packages must hold exactly one package, the bundle")
    else:
        package = packages[0]
        if package.get("registryType") != "mcpb":
            problems.append(
                f"the package's registryType is {package.get('registryType')!r}, not 'mcpb'"
            )
        if package.get("identifier") != expected["identifier"]:
            problems.append(f"the package's identifier must be {expected['identifier']!r}")
        if package.get("fileSha256") != "${FILE_SHA256}":
            problems.append("the package's fileSha256 must be the placeholder ${FILE_SHA256}")
        if "version" in package and package["version"] != "${VERSION}":
            problems.append("the package's version must be the placeholder ${VERSION} or left out")
    return [f"{REGISTRY_TEMPLATE}: {problem}" for problem in problems]


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

    problems = registry_template_problems(root)
    if problems:
        print("The MCP Registry template does not match:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    spellings = sorted(set(found.values()) - {version})
    written = f" (written as {', '.join(spellings)})" if spellings else ""
    tag = f" and the tag {args.tag}" if args.tag is not None else ""
    print(f"All versions{tag} match: {version}{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
