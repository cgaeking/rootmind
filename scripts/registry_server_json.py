"""Render the MCP Registry entry (server.json) of a release from packaging/mcpb/server.json.

The template holds everything that does not change between releases. This
script fills in the version, the bundle's download URL
(``https://github.com/cgaeking/rootmind/releases/download/v<version>/rootmind.mcpb``)
and ``fileSha256``, the SHA-256 of the built ``rootmind.mcpb``. MCP clients
check that hash before they install the bundle, and the registry does not, so
the hash is always computed from the file itself, never passed in:

- the bundle must exist, be a zip with a ``manifest.json``, and carry the
  release version and the bundle name;
- with ``--sums``, the release's ``SHA256SUMS.txt`` must list ``rootmind.mcpb``
  with the same hash (a missing entry or another hash is an error).

Before rendering, the template itself is checked as ``scripts/check_versions.py``
checks it. After rendering, every placeholder must be filled and the entry
must name the version, URL and hash. On success the script prints one line,
``<server.json><TAB><schema>``, for a schema validator. Standard library only::

    python scripts/registry_server_json.py --mcpb rootmind.mcpb --out DIR [--sums SHA256SUMS.txt] [--version 1.2.0 | --tag v1.2.0]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

from check_versions import (
    MCPB_ASSET,
    REGISTRY_TEMPLATE,
    ROOT,
    registry_expectations,
    registry_template_problems,
    schema_path,
)

PACKAGE_JSON = ROOT / "desktop" / "package.json"

# The app's version as package.json spells it (SemVer without build metadata),
# the same rule as scripts/winget_manifests.py: the download URL names the tag.
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
# A line of sha256sum's output: the hash, a space, " " (text) or "*" (binary), the name.
_SUMS_LINE = re.compile(r"^(?P<hash>[0-9A-Fa-f]{64}) [ *](?P<name>.+)$")


class RegistryError(Exception):
    """The input or the rendered entry is not what the registry needs."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def bundle_sha256(path: Path, *, version: str, name: str) -> str:
    """SHA-256 of the bundle after checking that it is the bundle for ``version``."""
    if not path.is_file():
        raise RegistryError(f"the bundle {path} does not exist")
    try:
        with zipfile.ZipFile(path) as bundle:
            manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise RegistryError(f"{path} is not an MCP bundle with a manifest.json: {error}") from None
    if manifest.get("version") != version:
        raise RegistryError(
            f"{path} is the bundle of version {manifest.get('version')!r}, expected {version}"
        )
    if manifest.get("name") != name:
        raise RegistryError(f"{path} is the bundle {manifest.get('name')!r}, expected {name!r}")
    return sha256_file(path)


def listed_sha256(sums: Path, asset: str = MCPB_ASSET) -> str:
    """The hash that a SHA256SUMS.txt lists for ``asset``; an error unless exactly one line does."""
    try:
        lines = sums.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RegistryError(f"cannot read {sums}: {error}") from None
    found = []
    for line in lines:
        match = _SUMS_LINE.match(line.rstrip("\r"))
        if match and Path(match["name"].strip()).name == asset:
            found.append(match["hash"].lower())
    if len(found) != 1:
        raise RegistryError(f"{sums} lists {asset} {len(found)} times, expected once")
    return found[0]


def render(template: object, values: dict[str, str]) -> object:
    """Replace every ``${NAME}`` in the template's strings; an unknown name is an error."""
    if isinstance(template, dict):
        return {key: render(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [render(value, values) for value in template]
    if isinstance(template, str):

        def substitute(match: re.Match[str]) -> str:
            if match[1] not in values:
                raise RegistryError(f"unknown placeholder ${{{match[1]}}}")
            return values[match[1]]

        text = _PLACEHOLDER.sub(substitute, template)
        if "${" in text:
            raise RegistryError(f"a placeholder is left after rendering: {text!r}")
        return text
    return template


def check_rendered(entry: dict, *, version: str, sha256: str, root: Path = ROOT) -> Path:
    """Check the rendered entry against the inputs; returns the vendored schema."""
    expected = registry_expectations(root)
    url = expected["identifier"].replace("${VERSION}", version)
    if entry.get("name") != expected["name"]:
        raise RegistryError(f"name is {entry.get('name')!r}, expected {expected['name']!r}")
    if entry.get("version") != version:
        raise RegistryError(f"version is {entry.get('version')!r}, expected {version}")
    for key in ("description", "title"):
        if not 1 <= len(entry.get(key) or "") <= 100:
            raise RegistryError(f"{key} must have 1 to 100 characters")
    (package,) = entry["packages"]
    if package.get("identifier") != url:
        raise RegistryError(f"the package URL is {package.get('identifier')!r}, expected {url}")
    if package.get("fileSha256") != sha256 or not _SHA256.match(sha256):
        raise RegistryError("the package's fileSha256 is not the bundle's SHA-256")
    if (package.get("transport") or {}).get("type") != "stdio":
        raise RegistryError("the bundle's transport must be stdio")
    schema = schema_path(entry.get("$schema", ""), root)
    if schema is None or not schema.is_file():
        raise RegistryError(f"no vendored schema for $schema {entry.get('$schema')!r}")
    return schema


def parse_version(args: argparse.Namespace) -> str:
    """The validated release version from --version, --tag or desktop/package.json."""
    version = args.version
    if args.tag is not None:
        if not args.tag.startswith("v"):
            raise RegistryError(f"a release tag is 'v' plus the version, not {args.tag!r}")
        if version is None:
            version = args.tag[1:]
        elif version != args.tag[1:]:
            raise RegistryError(f"--version {version} does not match the tag {args.tag}")
    if version is None:
        version = json.loads(args.package_json.read_text(encoding="utf-8"))["version"]
    if not _VERSION.match(version):
        raise RegistryError(f"not an app version (X.Y.Z or X.Y.Z-pre): {version!r}")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the MCP Registry entry of a release.")
    parser.add_argument("--mcpb", required=True, type=Path, help="the built rootmind.mcpb")
    parser.add_argument("--out", required=True, type=Path, help="folder for server.json")
    parser.add_argument("--sums", type=Path, help="the release's SHA256SUMS.txt to check against")
    parser.add_argument("--version", help="the app version (default: desktop/package.json)")
    parser.add_argument("--tag", help="the release tag; must name the same version")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--package-json", type=Path, default=PACKAGE_JSON, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    out = args.out / "server.json"
    try:
        problems = registry_template_problems(args.root)
        if problems:
            raise RegistryError("; ".join(problems))
        version = parse_version(args)
        expected = registry_expectations(args.root)
        bundle_name = expected["name"].rsplit("/", 1)[1]
        sha256 = bundle_sha256(args.mcpb, version=version, name=bundle_name)
        if args.sums is not None:
            listed = listed_sha256(args.sums)
            if listed != sha256:
                raise RegistryError(
                    f"{args.sums} lists {MCPB_ASSET} with SHA-256 {listed}, "
                    f"but {args.mcpb} has {sha256}"
                )
        template = json.loads((args.root / REGISTRY_TEMPLATE).read_text(encoding="utf-8"))
        entry = render(template, {"VERSION": version, "FILE_SHA256": sha256})
        schema = check_rendered(entry, version=version, sha256=sha256, root=args.root)
        args.out.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
    except (RegistryError, OSError, ValueError, KeyError) as error:
        print(f"registry_server_json: {error}", file=sys.stderr)
        return 1

    sys.stdout.flush()
    sys.stdout.buffer.write(f"{out}\t{schema}\n".encode())
    sys.stdout.buffer.flush()
    print(f"Rendered the MCP Registry entry for {version} into {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
