# Code signing policy

**Status:** RootMind releases are not code-signed yet. Every release ships
`SHA256SUMS.txt` and a build provenance attestation for every file, so you can
check that a download is unchanged and was built from this repository (see the
README, "Verify downloads"). Once a signing service is set up, this page will
name it and the first signed version.

## What will be signed

Only files that the [release workflow](.github/workflows/release.yml) builds from
the source in this repository, on GitHub Actions, for a version tag:

- `RootMind-Setup-x64.exe`, the Windows installer,
- `rootmind-desktop.exe`, the desktop app inside the installer,
- `rootmind-backend.exe`, the backend inside the installer and in
  `rootmind-backend-windows-x64.zip`.

Third-party components bundled with these files (the Python runtime and the
libraries listed in [`backend/uv.lock`](backend/uv.lock) and
[`desktop/src-tauri/Cargo.lock`](desktop/src-tauri/Cargo.lock)) are included as
their maintainers publish them and are not signed by this project.

Every signing request will be created by the release workflow and approved by
hand before anything is signed.

## Team roles

- Committers and reviewers: [Christian Gäking](https://github.com/cgaeking)
- Approvers: [Christian Gäking](https://github.com/cgaeking)

All team members use multi-factor authentication for GitHub (and for the
signing service, once one is used).
Changes from anyone else arrive as pull requests and are reviewed before they
are merged; build scripts and workflows are reviewed like any other code.

## Privacy policy

This program will not transfer any information to other networked systems
unless specifically requested by the user or the person installing or
operating it.

In detail:

- Indexing, search and reading documents stay on your computer.
- When you ask a question, RootMind sends the question and the document
  excerpts the model asks for to the LLM provider you configured in Settings
  (Anthropic, OpenAI or xAI), using your API key. Saving a key sends it to that
  provider to load the model list. Their privacy policies apply to this data:
  [Anthropic](https://www.anthropic.com/legal/privacy),
  [OpenAI](https://openai.com/policies/privacy-policy/),
  [xAI](https://x.ai/legal/privacy-policy).
- MCP over HTTP is on by default and listens on 127.0.0.1 only; it serves
  search results and document text to clients that present its token. Settings
  → RootMind service turns it off.
- RootMind has no telemetry and no update check.

[SECURITY.md](SECURITY.md#what-leaves-the-machine) lists every request in detail.
