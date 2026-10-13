"""Render the winget manifests of a release from the templates in packaging/winget/.

The templates hold everything that does not change between releases. This
script fills in the version, the installer's SHA-256, the release date, and
the publisher and copyright from ``desktop/src-tauri/tauri.conf.json``
(winget matches the installed app by the publisher the installer writes, so
both must come from the same place). The installer URL is
``https://github.com/cgaeking/rootmind/releases/download/v<version>/RootMind-Setup-x64.exe``.

The rendered files are checked before they are written: every placeholder is
filled, all three name the same package, version and manifest version, the
publisher is ``bundle.publisher``, and a vendored JSON schema exists for each
one. On success the script prints one line per file, ``<manifest><TAB><schema>``,
for a schema validator. Standard library only::

    python scripts/winget_manifests.py --sha256 HASH --out DIR [--version 1.2.0 | --tag v1.2.0] [--date 2026-10-20]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from check_versions import ROOT

TEMPLATES = ROOT / "packaging" / "winget"
SCHEMAS = TEMPLATES / "schemas"
TAURI_CONF = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
PACKAGE_JSON = ROOT / "desktop" / "package.json"
TEMPLATE_FILES = (
    "cgaeking.RootMind.yaml",
    "cgaeking.RootMind.installer.yaml",
    "cgaeking.RootMind.locale.en-US.yaml",
)
MANIFEST_TYPES = {"version", "installer", "defaultLocale"}

# The app's version as package.json and Cargo.toml spell it (SemVer without
# build metadata). Three numbers never read as a YAML number.
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_TOP_LEVEL = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9]*):\s*(?P<value>.*?)\s*$")
_PLAIN_SCALAR = re.compile(r"^[^\W\d_][\w .,()'&+/-]*$")
_YAML_WORDS = {"y", "n", "yes", "no", "true", "false", "on", "off", "null"}


class ManifestError(Exception):
    """The input or a rendered manifest is not what a submission needs."""


def yaml_scalar(value: str) -> str:
    """``value`` as a YAML scalar: plain when that is unambiguous, otherwise double-quoted."""
    if (
        _PLAIN_SCALAR.match(value)
        and not value.endswith(" ")
        and value.lower() not in _YAML_WORDS
        and not re.search(r":\s|\s#", value)
    ):
        return value
    # A JSON string is a valid YAML double-quoted scalar.
    return json.dumps(value, ensure_ascii=False)


def render(template: str, values: dict[str, str]) -> str:
    """Replace every ``${NAME}`` in ``template``; an unknown name is an error."""

    def substitute(match: re.Match[str]) -> str:
        name = match[1]
        if name not in values:
            raise ManifestError(f"unknown placeholder ${{{name}}}")
        return values[name]

    text = _PLACEHOLDER.sub(substitute, template)
    if "${" in text:
        raise ManifestError("a placeholder is left after rendering")
    return text


def top_level(text: str) -> dict[str, str]:
    """The top-level ``Key: value`` pairs of a manifest (nested lines are skipped)."""
    pairs = {}
    for line in text.splitlines():
        match = _TOP_LEVEL.match(line)
        if match:
            pairs[match["key"]] = match["value"]
    return pairs


def check_rendered(
    rendered: dict[str, str], *, version: str, publisher: str, sha256: str
) -> dict[str, Path]:
    """Check the rendered manifests against each other and the inputs; returns name -> schema."""
    fields = {name: top_level(text) for name, text in rendered.items()}
    for key in ("PackageIdentifier", "ManifestVersion"):
        found = {f.get(key) for f in fields.values()}
        if len(found) != 1 or None in found:
            raise ManifestError(f"the manifests disagree on {key}: {sorted(map(str, found))}")
    for name, f in fields.items():
        if f.get("PackageVersion") != version:
            raise ManifestError(
                f"{name}: PackageVersion is {f.get('PackageVersion')!r}, expected {version}"
            )
    types = {name: f.get("ManifestType") for name, f in fields.items()}
    if sorted(map(str, types.values())) != sorted(MANIFEST_TYPES):
        raise ManifestError(
            f"expected one manifest of each type {sorted(MANIFEST_TYPES)}, got {types}"
        )

    by_type = {types[name]: name for name in rendered}
    expected_publisher = yaml_scalar(publisher)
    locale = fields[by_type["defaultLocale"]]
    if locale.get("Publisher") != expected_publisher:
        raise ManifestError(
            f"Publisher is {locale.get('Publisher')!r}, expected bundle.publisher {publisher!r}"
        )
    installer = rendered[by_type["installer"]]
    if f"Publisher: {expected_publisher}\n" not in installer:
        raise ManifestError(
            "the installer manifest's AppsAndFeaturesEntries name another publisher"
        )
    if f"/releases/download/v{version}/RootMind-Setup-x64.exe" not in installer:
        raise ManifestError(f"the installer URL does not point at the v{version} release")
    if f"InstallerSha256: {sha256}\n" not in installer:
        raise ManifestError("the installer manifest does not carry the installer's SHA-256")

    manifest_version = next(iter(fields.values()))["ManifestVersion"]
    schemas = {}
    for name, kind in types.items():
        schema = SCHEMAS / f"manifest.{kind}.{manifest_version}.json"
        if not schema.is_file():
            raise ManifestError(
                f"no vendored schema {schema.name} for {name} (ManifestVersion {manifest_version})"
            )
        schemas[name] = schema
    return schemas


def parse_inputs(args: argparse.Namespace) -> tuple[str, str, str]:
    """Validated (version, sha256, date) from the command line."""
    version = args.version
    if args.tag is not None:
        if not args.tag.startswith("v"):
            raise ManifestError(f"a release tag is 'v' plus the version, not {args.tag!r}")
        if version is None:
            version = args.tag[1:]
        elif version != args.tag[1:]:
            raise ManifestError(f"--version {version} does not match the tag {args.tag}")
    if version is None:
        version = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["version"]
    if not _VERSION.match(version):
        raise ManifestError(f"not an app version (X.Y.Z or X.Y.Z-pre): {version!r}")
    if not _SHA256.match(args.sha256):
        raise ManifestError("--sha256 must be 64 hexadecimal digits")
    date = args.date or dt.datetime.now(dt.UTC).date().isoformat()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError
        dt.date.fromisoformat(date)
    except ValueError:
        raise ManifestError(f"--date must be YYYY-MM-DD, not {date!r}") from None
    return version, args.sha256.upper(), date


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the winget manifests of a release.")
    parser.add_argument("--sha256", required=True, help="SHA-256 of RootMind-Setup-x64.exe")
    parser.add_argument("--out", required=True, type=Path, help="folder for the three manifests")
    parser.add_argument("--version", help="the app version (default: desktop/package.json)")
    parser.add_argument("--tag", help="the release tag; must name the same version")
    parser.add_argument("--date", help="release date, YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--templates", type=Path, default=TEMPLATES, help=argparse.SUPPRESS)
    parser.add_argument("--tauri-conf", type=Path, default=TAURI_CONF, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        version, sha256, date = parse_inputs(args)
        bundle = json.loads(args.tauri_conf.read_text(encoding="utf-8")).get("bundle", {})
        publisher, copyright_ = bundle.get("publisher"), bundle.get("copyright")
        if not publisher or not copyright_:
            raise ManifestError(f"{args.tauri_conf} needs bundle.publisher and bundle.copyright")
        values = {
            "VERSION": version,
            "INSTALLER_SHA256": sha256,
            "RELEASE_DATE": date,
            "PUBLISHER": yaml_scalar(publisher),
            "COPYRIGHT": yaml_scalar(copyright_),
        }
        rendered = {
            name: render((args.templates / name).read_text(encoding="utf-8"), values)
            for name in TEMPLATE_FILES
        }
        schemas = check_rendered(rendered, version=version, publisher=publisher, sha256=sha256)
        args.out.mkdir(parents=True, exist_ok=True)
        for name, text in rendered.items():
            (args.out / name).write_text(text, encoding="utf-8", newline="\n")
    except (ManifestError, OSError, ValueError, KeyError) as error:
        print(f"winget_manifests: {error}", file=sys.stderr)
        return 1

    # LF line endings on every platform, so a shell loop can read the pairs.
    pairs = "".join(f"{args.out / name}\t{schemas[name]}\n" for name in TEMPLATE_FILES)
    sys.stdout.flush()
    sys.stdout.buffer.write(pairs.encode("utf-8"))
    sys.stdout.buffer.flush()
    print(f"Rendered the winget manifests for {version} into {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
