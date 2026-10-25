"""scripts/registry_server_json.py and the template packaging/mcpb/server.json."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import registry_server_json
from conftest import ROOT

URL = "https://github.com/cgaeking/rootmind/releases/download/v{}/rootmind.mcpb"

# The entry published by hand for 2.4.0 (GET /v0.1/servers/io.github.cgaeking%2Frootmind/versions/2.4.0),
# without its fileSha256. The template must keep rendering the same entry.
PUBLISHED_2_4_0 = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.github.cgaeking/rootmind",
    "description": "Search your local documents with page citations. Needs the RootMind app installed.",
    "title": "RootMind",
    "repository": {"url": "https://github.com/cgaeking/rootmind", "source": "github"},
    "version": "2.4.0",
    "websiteUrl": "https://github.com/cgaeking/rootmind",
    "packages": [
        {
            "registryType": "mcpb",
            "identifier": "https://github.com/cgaeking/rootmind/releases/download/v2.4.0/rootmind.mcpb",
            "transport": {"type": "stdio"},
        }
    ],
}


def make_bundle(path: Path, version: str = "1.2.0", name: str = "rootmind") -> Path:
    """A small stand-in for rootmind.mcpb: a zip with a manifest.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"name": name, "version": version}))
        bundle.writestr("server/index.mjs", "// bridge\n")
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(tmp_path: Path, *argv: str, bundle: Path | None = None) -> tuple[int, Path]:
    bundle = bundle or make_bundle(tmp_path / "bundle" / "rootmind.mcpb")
    out = tmp_path / "out"
    code = registry_server_json.main(["--mcpb", str(bundle), "--out", str(out), *argv])
    return code, out / "server.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_renders_version_url_and_hash(tmp_path, capsys):
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb")
    code, out = render(tmp_path, "--version", "1.2.0", bundle=bundle)
    assert code == 0
    entry = read(out)
    assert entry["name"] == "io.github.cgaeking/rootmind"
    assert entry["version"] == "1.2.0"
    (package,) = entry["packages"]
    assert package["registryType"] == "mcpb"
    assert package["identifier"] == URL.format("1.2.0")
    assert package["fileSha256"] == sha256(bundle)
    assert package["transport"] == {"type": "stdio"}
    assert "${" not in out.read_text(encoding="utf-8")
    # UTF-8 without a byte order mark, LF line endings.
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw

    printed = capsys.readouterr().out
    assert "\r" not in printed
    (line,) = printed.splitlines()
    rendered, schema = line.split("\t")
    assert Path(rendered) == out
    assert Path(schema).name == "server.schema.2025-12-11.json"
    assert Path(schema).is_file()


def test_template_renders_the_published_entry(tmp_path):
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb", version="2.4.0")
    code, out = render(tmp_path, "--tag", "v2.4.0", bundle=bundle)
    assert code == 0
    entry = read(out)
    assert entry["packages"][0].pop("fileSha256") == sha256(bundle)
    assert entry == PUBLISHED_2_4_0


@pytest.mark.parametrize("version", ["1.2.0", "1.2.0-rc.1"])
def test_rendered_entry_satisfies_the_vendored_schema(tmp_path, capsys, version):
    jsonschema = pytest.importorskip("jsonschema")
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb", version=version)
    code, _ = render(tmp_path, "--version", version, bundle=bundle)
    assert code == 0
    rendered, schema = capsys.readouterr().out.strip().split("\t")
    schema_json = json.loads(Path(schema).read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema_json, format_checker=jsonschema.FormatChecker())
    errors = [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(read(Path(rendered)))]
    assert errors == []


def test_this_repository_renders(tmp_path):
    """With the real package.json, as a dry run of the release workflow runs it."""
    version = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))["version"]
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb", version=version)
    code, out = render(tmp_path, bundle=bundle)
    assert code == 0
    assert read(out)["version"] == version


def test_sums_must_list_the_same_hash(tmp_path, capsys):
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb")
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(
        f"{'ab' * 32}  RootMind-Setup-x64.exe\n{sha256(bundle)}  rootmind.mcpb\n", encoding="utf-8"
    )
    assert render(tmp_path, "--version", "1.2.0", "--sums", str(sums), bundle=bundle)[0] == 0
    # sha256sum's binary marker and upper-case hex are read too.
    sums.write_text(f"{sha256(bundle).upper()} *rootmind.mcpb\r\n", encoding="utf-8")
    assert render(tmp_path, "--version", "1.2.0", "--sums", str(sums), bundle=bundle)[0] == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    ("sums_text", "message"),
    [
        ("{other}  rootmind.mcpb\n", "lists rootmind.mcpb with SHA-256"),
        ("{hash}  RootMind-Setup-x64.exe\n", "lists rootmind.mcpb 0 times"),
        ("{hash}  rootmind.mcpb\n{hash}  rootmind.mcpb\n", "lists rootmind.mcpb 2 times"),
        ("", "lists rootmind.mcpb 0 times"),
        ("{short}  rootmind.mcpb\n", "lists rootmind.mcpb 0 times"),
    ],
)
def test_wrong_or_missing_hash_in_sums_fails(tmp_path, capsys, sums_text, message):
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb")
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(
        sums_text.format(hash=sha256(bundle), other="cd" * 32, short="ab" * 31), encoding="utf-8"
    )
    code, out = render(tmp_path, "--version", "1.2.0", "--sums", str(sums), bundle=bundle)
    assert code == 1
    assert message in capsys.readouterr().err
    assert not out.exists()


def test_missing_sums_file_fails(tmp_path, capsys):
    code, out = render(tmp_path, "--version", "1.2.0", "--sums", str(tmp_path / "missing.txt"))
    assert code == 1
    assert "cannot read" in capsys.readouterr().err
    assert not out.exists()


def test_missing_bundle_fails(tmp_path, capsys):
    code, out = render(tmp_path, "--version", "1.2.0", bundle=tmp_path / "missing.mcpb")
    assert code == 1
    assert "does not exist" in capsys.readouterr().err
    assert not out.exists()


def test_bundle_must_be_an_mcp_bundle(tmp_path, capsys):
    bundle = tmp_path / "rootmind.mcpb"
    bundle.write_bytes(b"not a zip")
    assert render(tmp_path, "--version", "1.2.0", bundle=bundle)[0] == 1
    assert "not an MCP bundle" in capsys.readouterr().err
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("server/index.mjs", "")
    assert render(tmp_path, "--version", "1.2.0", bundle=bundle)[0] == 1
    assert "manifest.json" in capsys.readouterr().err


def test_bundle_must_carry_the_version_and_name(tmp_path, capsys):
    old = make_bundle(tmp_path / "old" / "rootmind.mcpb", version="1.1.0")
    code, out = render(tmp_path, "--version", "1.2.0", bundle=old)
    assert code == 1
    assert "the bundle of version '1.1.0', expected 1.2.0" in capsys.readouterr().err
    assert not out.exists()
    other = make_bundle(tmp_path / "other" / "rootmind.mcpb", name="something-else")
    assert render(tmp_path, "--version", "1.2.0", bundle=other)[0] == 1
    assert "expected 'rootmind'" in capsys.readouterr().err


def test_tag_sets_or_checks_the_version(tmp_path, capsys):
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb", version="1.3.0")
    code, out = render(tmp_path, "--tag", "v1.3.0", bundle=bundle)
    assert code == 0
    assert read(out)["packages"][0]["identifier"] == URL.format("1.3.0")
    assert render(tmp_path, "--tag", "v1.3.0", "--version", "1.3.0", bundle=bundle)[0] == 0
    capsys.readouterr()
    # The URL names the tag, so the spelling must match exactly.
    assert render(tmp_path, "--tag", "v1.3.0rc1", "--version", "1.3.0-rc.1", bundle=bundle)[0] == 1
    assert "does not match the tag" in capsys.readouterr().err
    assert render(tmp_path, "--tag", "1.3.0", bundle=bundle)[0] == 1


@pytest.mark.parametrize("version", ["1.2", "1.2.0+build.1", "1.2.0rc1", "latest"])
def test_rejects_a_bad_version(tmp_path, capsys, version):
    bundle = make_bundle(tmp_path / "bundle" / "rootmind.mcpb", version=version)
    code, out = render(tmp_path, "--version", version, bundle=bundle)
    assert code == 1
    assert "not an app version" in capsys.readouterr().err
    assert not out.exists()


def test_a_broken_template_is_not_rendered(tmp_path, make_repo, capsys):
    root = make_repo()
    path = root / "packaging" / "mcpb" / "server.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"${VERSION}"', '"1.2.0"'), encoding="utf-8"
    )
    code, out = render(tmp_path, "--version", "1.2.0", "--root", str(root))
    assert code == 1
    assert "version must be the placeholder" in capsys.readouterr().err
    assert not out.exists()


def test_unknown_placeholder_is_caught(tmp_path, make_repo, capsys):
    root = make_repo()
    path = root / "packaging" / "mcpb" / "server.json"
    template = json.loads(path.read_text(encoding="utf-8"))
    template["websiteUrl"] = "https://github.com/cgaeking/rootmind/tree/${BRANCH}"
    path.write_text(json.dumps(template), encoding="utf-8")
    code, out = render(tmp_path, "--version", "1.2.0", "--root", str(root))
    assert code == 1
    assert "${BRANCH}" in capsys.readouterr().err
    assert not out.exists()


def test_too_long_description_is_caught(tmp_path, make_repo, capsys):
    root = make_repo()
    path = root / "packaging" / "mcpb" / "server.json"
    template = json.loads(path.read_text(encoding="utf-8"))
    template["description"] = "x" * 101
    path.write_text(json.dumps(template), encoding="utf-8")
    assert render(tmp_path, "--version", "1.2.0", "--root", str(root))[0] == 1
    assert "description must have 1 to 100 characters" in capsys.readouterr().err
