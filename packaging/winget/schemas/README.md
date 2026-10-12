# winget manifest schemas

JSON schemas for winget manifests with `ManifestVersion` 1.12.0, used by the release workflow to
validate the rendered manifests without a network download.

| File | Source |
| --- | --- |
| `manifest.version.1.12.0.json` | `schemas/JSON/manifests/v1.12.0/manifest.version.1.12.0.json` |
| `manifest.installer.1.12.0.json` | `schemas/JSON/manifests/v1.12.0/manifest.installer.1.12.0.json` |
| `manifest.defaultLocale.1.12.0.json` | `schemas/JSON/manifests/v1.12.0/manifest.defaultLocale.1.12.0.json` |

Copied unchanged from [microsoft/winget-cli](https://github.com/microsoft/winget-cli) at the release
tag `v1.29.380` (commit `000f6b55151cb0f1afd2933bb54c62a4724b9ca8`). They are the files that
`https://aka.ms/winget-manifest.<type>.1.12.0.schema.json` points to. The schemas are
Copyright (c) Microsoft Corporation and licensed under the MIT License; see `LICENSE` in this
folder.
