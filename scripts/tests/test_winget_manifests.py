"""scripts/winget_manifests.py and the templates in packaging/winget/."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import winget_manifests
from conftest import ROOT

SHA = "ab" * 32
TEMPLATES = ROOT / "packaging" / "winget"


@pytest.fixture
def tauri_conf(tmp_path) -> Path:
    path = tmp_path / "tauri.conf.json"
    path.write_text(
        json.dumps(
            {
                "bundle": {
                    "publisher": "Christian Gäking",
                    "copyright": "Copyright (c) 2026 Christian Gäking",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def render(tmp_path, tauri_conf, *argv: str, templates: Path = TEMPLATES) -> tuple[int, Path]:
    out = tmp_path / "out"
    code = winget_manifests.main(
        [
            "--sha256",
            SHA,
            "--out",
            str(out),
            "--tauri-conf",
            str(tauri_conf),
            "--templates",
            str(templates),
            *argv,
        ]
    )
    return code, out


def read(out: Path, name: str) -> str:
    return (out / name).read_text(encoding="utf-8")


def test_renders_the_three_manifests(tmp_path, tauri_conf, capsys):
    code, out = render(tmp_path, tauri_conf, "--version", "1.2.0", "--date", "2026-10-20")
    assert code == 0
    installer = read(out, "cgaeking.RootMind.installer.yaml")
    locale = read(out, "cgaeking.RootMind.locale.en-US.yaml")
    version = read(out, "cgaeking.RootMind.yaml")
    for text in (installer, locale, version):
        assert "PackageIdentifier: cgaeking.RootMind\n" in text
        assert "PackageVersion: 1.2.0\n" in text
        assert "${" not in text
    assert (
        "InstallerUrl: https://github.com/cgaeking/rootmind/releases/download/v1.2.0/RootMind-Setup-x64.exe\n"
        in installer
    )
    assert f"InstallerSha256: {SHA.upper()}\n" in installer
    assert "ReleaseDate: 2026-10-20\n" in installer
    assert "InstallerType: nullsoft\n" in installer
    assert "Scope: user\n" in installer
    assert "ProductCode: RootMind\n" in installer
    assert "  Publisher: Christian Gäking\n" in installer
    assert "Publisher: Christian Gäking\n" in locale
    assert "Copyright: Copyright (c) 2026 Christian Gäking\n" in locale
    assert "ReleaseNotesUrl: https://github.com/cgaeking/rootmind/releases/tag/v1.2.0\n" in locale
    # UTF-8 without a byte order mark, LF line endings.
    raw = (out / "cgaeking.RootMind.locale.en-US.yaml").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert "Gäking".encode() in raw

    pairs = capsys.readouterr().out
    assert "\r" not in pairs
    lines = [line.split("\t") for line in pairs.splitlines()]
    assert [Path(m).name for m, _ in lines] == list(winget_manifests.TEMPLATE_FILES)
    assert [Path(s).name for _, s in lines] == [
        "manifest.version.1.12.0.json",
        "manifest.installer.1.12.0.json",
        "manifest.defaultLocale.1.12.0.json",
    ]
    assert all(Path(m).is_file() and Path(s).is_file() for m, s in lines)


def test_this_repository_renders(tmp_path, capsys):
    """With the real tauri.conf.json and package.json, as the release workflow runs it."""
    out = tmp_path / "out"
    assert winget_manifests.main(["--sha256", SHA, "--out", str(out)]) == 0
    conf = json.loads(
        (ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    locale = read(out, "cgaeking.RootMind.locale.en-US.yaml")
    assert f"Publisher: {winget_manifests.yaml_scalar(conf['bundle']['publisher'])}\n" in locale
    assert f"PackageVersion: {package['version']}\n" in locale


def _load_yaml(text: str):
    yaml = pytest.importorskip("yaml")

    class Loader(yaml.SafeLoader):
        """YAML as winget reads it: a date stays a string."""

    Loader.add_constructor("tag:yaml.org,2002:timestamp", Loader.construct_yaml_str)
    return yaml.load(text, Loader=Loader)


@pytest.mark.parametrize("version", ["1.2.0", "1.2.0-rc.1"])
def test_rendered_manifests_satisfy_the_vendored_schemas(tmp_path, tauri_conf, capsys, version):
    jsonschema = pytest.importorskip("jsonschema")
    code, _ = render(tmp_path, tauri_conf, "--version", version)
    assert code == 0
    for line in capsys.readouterr().out.splitlines():
        manifest, schema = line.split("\t")
        document = _load_yaml(Path(manifest).read_text(encoding="utf-8"))
        schema_json = json.loads(Path(schema).read_text(encoding="utf-8"))
        validator = jsonschema.Draft7Validator(
            schema_json, format_checker=jsonschema.FormatChecker()
        )
        errors = [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(document)]
        assert errors == [], f"{Path(manifest).name}: {errors}"


def test_templates_and_schemas_belong_together():
    names = {p.name for p in TEMPLATES.glob("*.yaml")}
    assert names == set(winget_manifests.TEMPLATE_FILES)
    for name in winget_manifests.TEMPLATE_FILES:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert set(re.findall(r"\$\{([A-Z_0-9]+)\}", text)) <= {
            "VERSION",
            "INSTALLER_SHA256",
            "RELEASE_DATE",
            "PUBLISHER",
            "COPYRIGHT",
        }
        kind = re.search(r"^ManifestType: (\S+)$", text, re.M)[1]
        manifest_version = re.search(r"^ManifestVersion: (\S+)$", text, re.M)[1]
        comment = re.search(r"^# yaml-language-server: \$schema=(\S+)$", text, re.M)[1]
        assert comment == f"https://aka.ms/winget-manifest.{kind}.{manifest_version}.schema.json"
        schema = json.loads(
            (TEMPLATES / "schemas" / f"manifest.{kind}.{manifest_version}.json").read_text(
                encoding="utf-8"
            )
        )
        assert schema["$id"].lower() == comment.lower()
    assert (TEMPLATES / "schemas" / "LICENSE").is_file()


def test_tag_sets_or_checks_the_version(tmp_path, tauri_conf, capsys):
    code, out = render(tmp_path, tauri_conf, "--tag", "v1.3.0")
    assert code == 0
    assert "PackageVersion: 1.3.0\n" in read(out, "cgaeking.RootMind.yaml")
    assert render(tmp_path, tauri_conf, "--tag", "v1.3.0", "--version", "1.3.0")[0] == 0
    capsys.readouterr()
    # The URL names the tag, so the spelling must match exactly.
    assert render(tmp_path, tauri_conf, "--tag", "v1.3.0rc1", "--version", "1.3.0-rc.1")[0] == 1
    assert "does not match the tag" in capsys.readouterr().err
    assert render(tmp_path, tauri_conf, "--tag", "1.3.0")[0] == 1


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--version", "1.2"], "not an app version"),
        (["--version", "1.2.0+build.1"], "not an app version"),
        (["--version", "1.2.0", "--date", "20.10.2026"], "--date"),
        (["--version", "1.2.0", "--date", "2026-02-30"], "--date"),
    ],
)
def test_rejects_bad_input(tmp_path, tauri_conf, capsys, argv, message):
    code, out = render(tmp_path, tauri_conf, *argv)
    assert code == 1
    assert message in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("sha", ["ab" * 31, "zz" * 32, ""])
def test_rejects_a_bad_hash(tmp_path, tauri_conf, capsys, sha):
    code = winget_manifests.main(
        [
            "--sha256",
            sha,
            "--out",
            str(tmp_path / "out"),
            "--tauri-conf",
            str(tauri_conf),
            "--version",
            "1.2.0",
        ]
    )
    assert code == 1
    assert "--sha256" in capsys.readouterr().err


def test_publisher_is_required(tmp_path, capsys):
    conf = tmp_path / "tauri.conf.json"
    conf.write_text(json.dumps({"bundle": {"copyright": "c"}}), encoding="utf-8")
    assert render(tmp_path, conf, "--version", "1.2.0")[0] == 1
    assert "bundle.publisher" in capsys.readouterr().err


def test_publisher_needing_quotes(tmp_path, capsys):
    yaml = pytest.importorskip("yaml")
    conf = tmp_path / "tauri.conf.json"
    publisher = "ACME: Labs #1"
    conf.write_text(
        json.dumps({"bundle": {"publisher": publisher, "copyright": "c"}}), encoding="utf-8"
    )
    code, out = render(tmp_path, conf, "--version", "1.2.0")
    assert code == 0
    assert (
        yaml.safe_load(read(out, "cgaeking.RootMind.locale.en-US.yaml"))["Publisher"] == publisher
    )
    installer = yaml.safe_load(read(out, "cgaeking.RootMind.installer.yaml"))
    assert installer["AppsAndFeaturesEntries"][0]["Publisher"] == publisher


@pytest.mark.parametrize(
    "value",
    [
        "Christian Gäking",
        "ACME: Labs",
        "#1",
        "yes",
        "Null",
        "- dash",
        "trailing ",
        "'quoted'",
        'say "hi"',
        "a\\b",
    ],
)
def test_yaml_scalar_round_trips(value):
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(f"key: {winget_manifests.yaml_scalar(value)}\n")["key"] == value


def test_hardcoded_publisher_in_a_template_is_caught(tmp_path, tauri_conf, capsys):
    templates = tmp_path / "templates"
    shutil.copytree(TEMPLATES, templates)
    path = templates / "cgaeking.RootMind.locale.en-US.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Publisher: ${PUBLISHER}", "Publisher: Someone Else"
        ),
        encoding="utf-8",
    )
    code, out = render(tmp_path, tauri_conf, "--version", "1.2.0", templates=templates)
    assert code == 1
    assert "expected bundle.publisher" in capsys.readouterr().err
    assert not out.exists()


def test_unknown_placeholder_is_caught(tmp_path, tauri_conf, capsys):
    templates = tmp_path / "templates"
    shutil.copytree(TEMPLATES, templates)
    path = templates / "cgaeking.RootMind.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "Extra: ${MISSING}\n", encoding="utf-8")
    assert render(tmp_path, tauri_conf, "--version", "1.2.0", templates=templates)[0] == 1
    assert "${MISSING}" in capsys.readouterr().err


def test_missing_schema_version_is_caught(tmp_path, tauri_conf, capsys):
    templates = tmp_path / "templates"
    shutil.copytree(TEMPLATES, templates)
    for name in winget_manifests.TEMPLATE_FILES:
        path = templates / name
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "ManifestVersion: 1.12.0", "ManifestVersion: 9.9.9"
            ),
            encoding="utf-8",
        )
    assert render(tmp_path, tauri_conf, "--version", "1.2.0", templates=templates)[0] == 1
    assert "no vendored schema" in capsys.readouterr().err
