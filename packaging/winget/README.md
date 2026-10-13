# winget manifests

These templates describe RootMind for the Windows Package Manager (winget): the package
`cgaeking.RootMind` with the moniker `rootmind`, installed by the per-user installer
`RootMind-Setup-x64.exe` from the GitHub release.

RootMind is not in the winget repository yet. The first submission is planned for when the
release binaries are code-signed. Until then, install RootMind from the
[GitHub releases](https://github.com/cgaeking/rootmind/releases).

## Files

| File | Content |
| --- | --- |
| `cgaeking.RootMind.yaml` | Version manifest |
| `cgaeking.RootMind.installer.yaml` | Installer manifest: NSIS (`nullsoft`), user scope, x64, the release URL and SHA-256 |
| `cgaeking.RootMind.locale.en-US.yaml` | Default locale: publisher, description, license, links |
| `schemas/` | The JSON schemas for `ManifestVersion` 1.12.0, from `microsoft/winget-cli` (see `schemas/README.md`) |

The templates use the placeholders `${VERSION}`, `${INSTALLER_SHA256}`, `${RELEASE_DATE}`,
`${PUBLISHER}` and `${COPYRIGHT}`. The publisher and the copyright come from
`desktop/src-tauri/tauri.conf.json` (`bundle.publisher`, `bundle.copyright`). The installer writes
`bundle.publisher` into the "Installed apps" entry, and winget recognises an installed package by
that entry, so the manifest and the installer must name the same publisher.

winget installs a `nullsoft` installer with `/S` and chooses a folder with `/D=<path>`. No extra
switches are needed. `ProductCode: RootMind` is the name of the uninstall key the installer
creates, and `%LocalAppData%\RootMind` is its default folder. With `UpgradeBehavior: install`,
`winget upgrade` runs the new installer over the installed version, which updates it in place
and keeps the app data; the installed version is not uninstalled first.

## Render and validate

Every run of `.github/workflows/release.yml` renders the manifests for the installer it built,
validates them against the schemas in `schemas/`, and uploads them as the artifact
`winget-manifests`. To do the same by hand:

```sh
python scripts/winget_manifests.py --version 1.2.0 --sha256 <SHA-256 of RootMind-Setup-x64.exe> --out build/winget
```

The script prints one line per manifest, `<manifest><TAB><schema>`. To check a manifest against its
schema:

```sh
uvx --from check-jsonschema==0.38.2 check-jsonschema \
  --schemafile packaging/winget/schemas/manifest.installer.1.12.0.json \
  build/winget/cgaeking.RootMind.installer.yaml
```

## Submit a release

Submit stable releases only, never prereleases. The manifests pin the installer's SHA-256, so the
release assets must never change after a submission; immutable releases on GitHub guarantee that.

### First submission

1. Download `RootMind-Setup-x64.exe` and `SHA256SUMS.txt` from the release and check the hash
   (`sha256sum --check --ignore-missing SHA256SUMS.txt`). Take the three manifests from the
   `winget-manifests` artifact of the release run.
2. Test them in Windows Sandbox, which keeps your own machine unchanged. The Sandbox has no winget;
   `Tools/SandboxTest.ps1` in a clone of `microsoft/winget-pkgs` installs it, validates a manifest
   folder and installs from it:

   ```powershell
   .\Tools\SandboxTest.ps1 <folder with the three manifests>
   ```

   On a machine that has winget, the manual steps are (from an administrator prompt):

   ```powershell
   winget settings --enable LocalManifestFiles
   winget validate --manifest <folder>
   winget install --manifest <folder>
   ```

   Start RootMind once, then remove it with `winget uninstall cgaeking.RootMind`.
3. Scan the installer and the installed backend (`%LOCALAPPDATA%\RootMind\backend`) with Microsoft
   Defender. The winget pipeline runs Defender on every submission. If a file is flagged, report
   it to Microsoft as a false positive before you submit.
4. Fork `microsoft/winget-pkgs`, copy the three files to
   `manifests/c/cgaeking/RootMind/<version>/`, and open a pull request. Fill in the checklist of
   the pull request template, and sign the Microsoft CLA when the bot asks for it.
5. Follow the validation labels on the pull request and fix what they report.

Once the first submission is merged, add `winget install cgaeking.RootMind` to the README.

### Later releases

After the first merge, `microsoft/winget-pkgs` holds the package's metadata. Update it with
[komac](https://github.com/russellbanks/Komac), which copies the previous version's manifests and
changes only the version, the URL, the hash and the date:

```sh
komac update cgaeking.RootMind --version <version> \
  --urls https://github.com/cgaeking/rootmind/releases/download/v<version>/RootMind-Setup-x64.exe \
  --submit
```

When you change the metadata in `microsoft/winget-pkgs`, make the same change to the templates
here, so the manifests that CI renders stay a faithful preview.

## Moving to a newer manifest schema

Use the `ManifestVersion` that new submissions to `microsoft/winget-pkgs` use (komac's current
output). Copy `manifest.version.<v>.json`, `manifest.installer.<v>.json` and
`manifest.defaultLocale.<v>.json` from `schemas/JSON/manifests/v<v>/` of a `microsoft/winget-cli`
release tag into `schemas/`, update `schemas/README.md`, and change `ManifestVersion` and the
`$schema` comment in all three templates. `scripts/winget_manifests.py` refuses to render when a
template names a schema version that is not in `schemas/`.
