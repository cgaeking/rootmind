"""scripts/nsis_template.py and the installer template it maintains.

Offline: the committed template is checked as it is, and the patch logic runs
on a small stand-in for the upstream template. The header names the Tauri CLI
it was made for, so a Tauri CLI update (Dependabot) fails here until the
template is refreshed with ``python scripts/nsis_template.py --write``.
"""

from __future__ import annotations

import json
import re

import nsis_template
import pytest
from conftest import ROOT

DESKTOP = ROOT / "desktop"
TAURI_DIR = DESKTOP / "src-tauri"
TEMPLATE = ROOT / nsis_template.TEMPLATE
HOOKS = TAURI_DIR / "installer" / "hooks.nsh"


def template_text() -> str:
    return TEMPLATE.read_bytes().decode("ascii")


def lines() -> list[str]:
    return template_text().split("\r\n")


# --- the committed template -------------------------------------------------------------------


def test_template_is_made_for_the_locked_cli():
    header = nsis_template.HEADER_VERSIONS.search(template_text())
    assert header, (
        "installer.nsi has no RootMind header; run: python scripts/nsis_template.py --write"
    )
    locked = nsis_template.locked_cli_version(
        (DESKTOP / "pnpm-lock.yaml").read_text(encoding="utf-8")
    )
    assert header["cli"] == locked, (
        f"installer.nsi is the template for @tauri-apps/cli {header['cli']}, but {locked} is locked; "
        "run: python scripts/nsis_template.py --write"
    )


def test_template_is_ascii_with_crlf():
    data = TEMPLATE.read_bytes()
    assert data.isascii()
    assert data.count(b"\n") == data.count(b"\r\n")
    assert data.endswith(b"\r\n")


def test_template_has_no_reinstall_page():
    text = template_text()
    for marker in nsis_template.FORBIDDEN:
        assert marker not in text
    # Nothing else reads the installed version's uninstaller to run it: the
    # install writes the value, and the folder page only checks for it.
    uses = [line.strip() for line in lines() if "UninstallString" in line]
    assert uses == [
        'WriteRegStr SHCTX "${UNINSTKEY}" "UninstallString" "$\\"$INSTDIR\\uninstall.exe$\\""',
        'ReadRegStr $0 SHCTX "${UNINSTKEY}" "UninstallString"',
    ]
    assert "_?=" not in text


def test_an_update_skips_the_folder_page():
    found = lines()
    directory = found.index("!insertmacro MUI_PAGE_DIRECTORY")
    assert found[directory - 1] == "!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassiveOrInstalled"
    start = found.index("Function SkipIfPassiveOrInstalled")
    body = found[start : found.index("FunctionEnd", start)]
    assert "  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}" in body
    assert '  ReadRegStr $0 SHCTX "${UNINSTKEY}" "UninstallString"' in body


def test_template_keeps_the_hooks():
    text = template_text()
    for marker in nsis_template.REQUIRED:
        assert marker in text


def test_tauri_conf_uses_the_template_and_the_hooks():
    conf = json.loads((TAURI_DIR / "tauri.conf.json").read_text(encoding="utf-8"))
    nsis = conf["bundle"]["windows"]["nsis"]
    assert (TAURI_DIR / nsis["template"]).resolve() == TEMPLATE.resolve()
    assert (TAURI_DIR / nsis["installerHooks"]).resolve() == HOOKS.resolve()
    # Section EarlyChecks refuses a silent downgrade by a register only the
    # removed reinstall page set; with downgrades allowed it is not compiled.
    assert nsis.get("allowDowngrades", True) is True


def test_hooks_replace_the_backend_and_keep_the_service_on_update():
    text = HOOKS.read_bytes().decode("ascii")
    preinstall = text.split("!macro NSIS_HOOK_PREINSTALL", 1)[1].split("!macroend", 1)[0]
    assert "!insertmacro ROOTMIND_REMOVE_BACKEND" in preinstall
    assert 'RMDir /r "$INSTDIR\\backend"' in text
    preuninstall = text.split("!macro NSIS_HOOK_PREUNINSTALL", 1)[1].split("!macroend", 1)[0]
    assert "${If} $UpdateMode <> 1" in preuninstall
    assert "$EXEDIR" not in preuninstall


# --- the patch ----------------------------------------------------------------------------------

UPSTREAM = """\
Unicode true
{{#if installer_hooks}}
!include "{{installer_hooks}}"
{{/if}}
!define UNINSTKEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${PRODUCTNAME}"

; 3. Install mode (if it is set to `both`)

; 4. Custom page to ask user if he wants to reinstall/uninstall
;    only if a previous installation was detected
Var ReinstallPageCheck
Page custom PageReinstall PageLeaveReinstall
Function PageReinstall
FunctionEnd
Function PageLeaveReinstall
  ExecWait '$R1' $0
FunctionEnd

; 5. Choose install directory page
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!insertmacro MUI_PAGE_DIRECTORY

Section Install
  !insertmacro NSIS_HOOK_PREINSTALL
SectionEnd
Section Uninstall
  !insertmacro NSIS_HOOK_PREUNINSTALL
SectionEnd

Function SkipIfPassive
  ${IfThen} $PassiveMode = 1  ${|} Abort ${|}
FunctionEnd
Function un.SkipIfPassive
  ${IfThen} $PassiveMode = 1  ${|} Abort ${|}
FunctionEnd
"""


def test_render_applies_the_changes():
    out = nsis_template.render(UPSTREAM, cli="2.99.0", bundler="2.98.0")
    assert out.isascii()
    assert out.count("\n") == out.count("\r\n")
    text = out.replace("\r\n", "\n")
    header = nsis_template.HEADER_VERSIONS.search(text)
    assert header and (header["cli"], header["bundler"]) == ("2.99.0", "2.98.0")
    assert "tauri-bundler-v2.98.0/crates/tauri-bundler/" in text
    for marker in nsis_template.FORBIDDEN:
        assert marker not in text
    assert "ExecWait" not in text
    assert (
        "; RootMind: not shown when the app is installed (see SkipIfPassiveOrInstalled).\n"
        "!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassiveOrInstalled\n"
        "!insertmacro MUI_PAGE_DIRECTORY\n"
    ) in text
    assert nsis_template.SKIP_IF_PASSIVE_OR_INSTALLED + "Function SkipIfPassive\n" in text
    # Everything else is kept as it is.
    assert text.endswith(
        "Function un.SkipIfPassive\n  ${IfThen} $PassiveMode = 1  ${|} Abort ${|}\nFunctionEnd\n"
    )
    assert "; 3. Install mode (if it is set to `both`)\n\n; 4. RootMind: no reinstall page." in text


def test_render_accepts_crlf_input():
    lf = nsis_template.render(UPSTREAM, cli="2.99.0", bundler="2.98.0")
    crlf = nsis_template.render(UPSTREAM.replace("\n", "\r\n"), cli="2.99.0", bundler="2.98.0")
    assert lf == crlf


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # an anchor is gone
        ("; 4. Custom page to ask user if he wants to reinstall/uninstall\n", ""),
        ("; 5. Choose install directory page\n", ""),
        ("Function SkipIfPassive\n", "Function SkipIfPassiveMode\n"),
        # an anchor is no longer unique
        ("Section Install\n", "; 5. Choose install directory page\nSection Install\n"),
        # the page or the directory page changed shape
        ("Page custom PageReinstall PageLeaveReinstall\n", "Page custom PageReinstall\n"),
        ("  ExecWait '$R1' $0\nFunctionEnd\n\n", "  ExecWait '$R1' $0\nFunctionEnd\n"),
        (
            "!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive\n!insertmacro MUI_PAGE_DIRECTORY\n",
            "!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive\n!define X\n!insertmacro MUI_PAGE_DIRECTORY\n",
        ),
        # the reinstall page is still referenced elsewhere
        (
            "SectionEnd\n\nFunction SkipIfPassive",
            "SectionEnd\nCall PageReinstall\n\nFunction SkipIfPassive",
        ),
        # the hooks are gone
        ("  !insertmacro NSIS_HOOK_PREINSTALL\n", ""),
        # not ASCII
        ("Unicode true\n", "Unicode true ; ä\n"),
    ],
)
def test_render_refuses_a_changed_upstream(old, new):
    assert old in UPSTREAM
    with pytest.raises(nsis_template.TemplateError):
        nsis_template.render(UPSTREAM.replace(old, new, 1), cli="2.99.0", bundler="2.98.0")


# --- versions -----------------------------------------------------------------------------------

LOCKFILE = """\
lockfileVersion: '9.0'

importers:

  .:
    dependencies:
      '@tauri-apps/api':
        specifier: ^2.12.2
        version: 2.12.2
    devDependencies:
      '@tauri-apps/cli':
        specifier: ^2.12.1
        version: 2.12.1
      '@tauri-apps/cli-win32-x64-msvc':
        specifier: 2.0.0
        version: 2.0.0

packages:

  '@tauri-apps/cli@2.12.1':
    resolution: {integrity: sha512-x}
"""


def test_locked_cli_version():
    assert nsis_template.locked_cli_version(LOCKFILE) == "2.12.1"
    assert nsis_template.locked_cli_version(LOCKFILE.replace("\n", "\r\n")) == "2.12.1"


@pytest.mark.parametrize(
    "lockfile",
    [
        LOCKFILE.replace("'@tauri-apps/cli':", "'@tauri-apps/other':"),
        LOCKFILE.replace("version: 2.12.1", "version: link:../cli"),
        LOCKFILE + "  other:\n    devDependencies:\n      '@tauri-apps/cli':\n"
        "        specifier: ^2.13.0\n        version: 2.13.0\n",
    ],
)
def test_locked_cli_version_refuses_unclear_locks(lockfile):
    with pytest.raises(nsis_template.TemplateError):
        nsis_template.locked_cli_version(lockfile)


def test_the_locked_cli_is_the_one_in_package_json():
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    locked = nsis_template.locked_cli_version(
        (DESKTOP / "pnpm-lock.yaml").read_text(encoding="utf-8")
    )
    specifier = package["devDependencies"]["@tauri-apps/cli"]
    assert re.fullmatch(r"[\^~]?\d+\.\d+\.\d+", specifier)
    assert locked.split(".")[0] == specifier.lstrip("^~").split(".")[0]


WORKSPACE = '[workspace.dependencies]\ntauri-bundler = { version = "=2.10.1", path = "./crates/tauri-bundler" }\n'


@pytest.mark.parametrize(
    ("cli_manifest", "workspace", "expected"),
    [
        ("[dependencies]\ntauri-bundler = { workspace = true }\n", WORKSPACE, "2.10.1"),
        (
            '[dependencies]\ntauri-bundler = { version = "=2.11.0", default-features = false }\n',
            "",
            "2.11.0",
        ),
        ('[dependencies]\ntauri-bundler = "=2.11.0-rc.1"\n', "", "2.11.0-rc.1"),
    ],
)
def test_bundler_version(cli_manifest, workspace, expected):
    assert nsis_template.bundler_version(cli_manifest, lambda: workspace) == expected


@pytest.mark.parametrize(
    "cli_manifest",
    [
        '[dependencies]\ntauri-bundler = "2.10.1"\n',
        '[dependencies]\ntauri-bundler = { version = "^2.10" }\n',
        '[dependencies]\ntauri-bundler = { version = "=2.10.1/../x" }\n',
        '[dependencies]\nserde = "1"\n',
        "[dependencies]\ntauri-bundler = { workspace = true }\n",
    ],
)
def test_bundler_version_needs_one_pinned_version(cli_manifest):
    with pytest.raises(nsis_template.TemplateError):
        nsis_template.bundler_version(cli_manifest, lambda: "[workspace]\n")


# --- main ---------------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "desktop").mkdir(parents=True)
    (root / "desktop" / "pnpm-lock.yaml").write_text(LOCKFILE, encoding="utf-8")
    return root


def fake_get(requests: list[tuple[str, str]]):
    files = {
        (
            "tauri-cli-v2.12.1",
            nsis_template.CLI_MANIFEST,
        ): "[dependencies]\ntauri-bundler = { workspace = true }\n",
        ("tauri-cli-v2.12.1", nsis_template.WORKSPACE_MANIFEST): WORKSPACE,
        ("tauri-bundler-v2.10.1", nsis_template.UPSTREAM_TEMPLATE): UPSTREAM,
    }

    def get(ref: str, path: str) -> str:
        requests.append((ref, path))
        return files[(ref, path)]

    return get


def test_write_then_check(repo, capsys):
    requests: list[tuple[str, str]] = []
    get = fake_get(requests)
    assert nsis_template.main(["--check"], root=repo, get=get) == 1
    assert "--write" in capsys.readouterr().err

    assert nsis_template.main(["--write"], root=repo, get=get) == 0
    written = (repo / nsis_template.TEMPLATE).read_bytes()
    assert written == nsis_template.render(UPSTREAM, cli="2.12.1", bundler="2.10.1").encode("ascii")
    assert ("tauri-bundler-v2.10.1", nsis_template.UPSTREAM_TEMPLATE) in requests

    assert nsis_template.main(["--check"], root=repo, get=get) == 0
    assert "up to date" in capsys.readouterr().out

    # Line endings do not matter for --check; content does.
    (repo / nsis_template.TEMPLATE).write_bytes(written.replace(b"\r\n", b"\n"))
    assert nsis_template.main(["--check"], root=repo, get=get) == 0
    (repo / nsis_template.TEMPLATE).write_bytes(
        written.replace(b"SkipIfPassiveOrInstalled", b"SkipIfPassive")
    )
    assert nsis_template.main(["--check"], root=repo, get=get) == 1
    assert "-!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive" in capsys.readouterr().err


def test_errors_exit_2(repo, capsys):
    def offline(ref: str, path: str) -> str:
        raise OSError("network unreachable")

    assert nsis_template.main(["--check"], root=repo, get=offline) == 2
    assert "network unreachable" in capsys.readouterr().err

    def changed(ref: str, path: str) -> str:
        if path == nsis_template.UPSTREAM_TEMPLATE:
            return UPSTREAM.replace("; 5. Choose install directory page\n", "")
        return fake_get([])(ref, path)

    assert nsis_template.main(["--write"], root=repo, get=changed) == 2
    assert not (repo / nsis_template.TEMPLATE).exists()


def test_a_mode_is_required(repo):
    with pytest.raises(SystemExit):
        nsis_template.main([], root=repo, get=fake_get([]))
