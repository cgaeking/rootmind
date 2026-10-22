<p align="center">
  <img src="desktop/src-tauri/icons/128x128@2x.png" alt="RootMind logo" width="128">
</p>

# RootMind

**English** | [Deutsch](README.de.md)

[![Latest release](https://img.shields.io/github/v/release/cgaeking/rootmind)](https://github.com/cgaeking/rootmind/releases/latest)
[![backend CI](https://github.com/cgaeking/rootmind/actions/workflows/backend.yml/badge.svg?branch=main)](https://github.com/cgaeking/rootmind/actions/workflows/backend.yml?query=branch%3Amain)
[![desktop CI](https://github.com/cgaeking/rootmind/actions/workflows/desktop.yml/badge.svg?branch=main)](https://github.com/cgaeking/rootmind/actions/workflows/desktop.yml?query=branch%3Amain)
[![License: MIT](https://img.shields.io/github/license/cgaeking/rootmind)](LICENSE)
[![Platform: Windows | Linux](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011%20%7C%20Linux-0078D6)](#install)
[![Downloads](https://img.shields.io/github/downloads/cgaeking/rootmind/total)](https://github.com/cgaeking/rootmind/releases)

[Install](#install) · [Connect MCP clients](#connect-mcp-clients) · [How it works](#how-it-works) · [Why a tool loop](#why-a-tool-loop-instead-of-embedding-rag) · [Security](#security-model-in-short) · [Limitations](#limitations)

**Ask your documents a question. Get the answer with the page it came from.**

RootMind indexes the folders you choose on your own computer (PDF, Word, Excel,
PowerPoint, CSV, Markdown and text files). When you ask a question, a language
model searches that index and reads the passages it asks for, then answers with
sources down to the page, slide or sheet. There is no embedding model and no
vector database: indexing is plain full-text search in SQLite, and the model
drives the retrieval.

https://github.com/user-attachments/assets/d3cc0249-a550-4de5-ae6b-55c1291a282f

- **Cited answers.** Every answer names its files and, where it can be
  resolved, the page, slide or sheet.
- **Your files stay on your computer.** Indexing and search are local and need
  no LLM and no API key. Answering a question sends the question and the
  excerpts the model reads to the provider you configure (Anthropic, OpenAI or
  xAI). With a local [Ollama](https://ollama.com) server as the provider,
  nothing leaves your computer (see [Local models with Ollama](#local-models-with-ollama)).
- **It reads, it does not stuff.** Instead of pasting whole documents into a
  chat, the model searches and reads targeted windows, and every step is a
  visible tool call. The cost is more model round trips than classic RAG; see
  [Why a tool loop instead of embedding RAG](#why-a-tool-loop-instead-of-embedding-rag).
- **Usable from your other tools.** The same read-only tools are served over
  MCP, for Claude Desktop, Claude Code, n8n and other agents.
- **Windows and Linux.** The desktop app is in English and German; the license
  is MIT.

### Try it in a few minutes

1. Download
   **[RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)**
   (Windows 10 or 11) or the [.deb, .rpm or AppImage](#install-on-linux)
   (Linux).
2. Start RootMind and add a folder with your documents under **Folders**.
3. Open **Chat**, tick **Search only** and search. This needs no API key. For
   answers, add a key under **Settings** and untick **Search only**.

The [Quickstart](docs/QUICKSTART.md) has the details.

## What it is

The repository has two parts:

- **Backend** (Python, FastAPI): indexer, full-text search, the agent loop, a
  localhost REST API, and MCP servers over stdio and HTTP.
- **Desktop app** (Tauri 2 with React and TypeScript, plus a Rust shell): a
  chat and search UI for Windows and Linux, in English and German. On Windows
  its installer sets up the backend as a Windows service; on Linux the app
  sets it up as systemd user services of your account. The app talks to that
  service.

> **Status:** this is a personal project. It is not commercially maintained and
> comes with no support commitment. Issues and pull requests are welcome and
> get a best-effort response.

## Install

Each [release](https://github.com/cgaeking/rootmind/releases/latest) has these
downloads for Windows (x64); the Linux packages are described in
[Install on Linux](#install-on-linux):

- **[RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)**,
  for most users, on Windows 10 or 11: the desktop app and its Windows
  service. Run it and approve the administrator prompt once; no Python is
  needed. It installs the app to `%ProgramFiles%\RootMind` and sets up the
  [RootMind service](#the-rootmind-service) for the account that is signed in
  to Windows.
- **[rootmind-backend-windows-x64.zip](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind-backend-windows-x64.zip)**,
  for the command line and servers: the backend alone, without the app (see
  [Install for developers and servers](#install-for-developers-and-servers)).
- **[rootmind.mcpb](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb)**,
  RootMind for Claude Desktop, to install after the app (see
  [Use RootMind in Claude Desktop](#use-rootmind-in-claude-desktop)).

Installing needs an administrator account. The service belongs to the account
that uses RootMind, so the installer sets it up only when it runs as the
account signed in to Windows. The install folder is always
`%ProgramFiles%\RootMind`, a folder that only administrators can change; the
installer refuses another one (for example with `/D=`, exit code 10).

**Silent and managed installs.** winget, Intune, Configuration Manager and
similar tools often run the installer silently (`/S`) as SYSTEM. Then, and
when an administrator approves the prompt while another account is signed in,
the installer installs the app but not the service, and exits with 0. The
service is set up when the account that uses RootMind first opens it: RootMind
says "The RootMind service is not set up yet", and **Set up** sets it up for
that account with one administrator approval. A standard account cannot set it
up; it needs an administrator account that uses RootMind itself. If the
installer could not set the service up for another reason, it also exits with
0 and RootMind shows the problem and **Repair**. The outcome is recorded in
the uninstall entry `HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\RootMind`
(`RootMindServiceSetup`: `installed`, `skipped` or `failed`, with
`RootMindServiceSetupReason` and, for a failure, `RootMindServiceSetupCode`).
Updates run as SYSTEM update an existing service in place.

The installer is not code-signed yet. Windows SmartScreen may show "Windows
protected your PC": choose **More info**, then **Run anyway**. If Smart App
Control is on, it may block the installer or RootMind's service program
without that choice. Smart App Control has no exception for a single app;
signed releases, which are planned, will avoid this (see the
[code signing policy](CODE_SIGNING.md)).

Then start RootMind and add a folder under **Folders**. Indexing starts within
about a minute, and search works without an API key once the folder is
indexed. To ask questions, add an API key for Anthropic, OpenAI or xAI under
**Settings** and save it, or choose **Ollama** to use a local model. The service keeps the index up to date while the
app is closed.

The app follows the Windows display language (English or German); Settings →
**Appearance** → **Language** changes it. Settings → **About** shows the app
and backend versions and links to releases and issues.

### Install on Linux

Each release also has the app for Linux on x86-64 with glibc 2.35 or later
(Ubuntu 22.04, Debian 12, Fedora 38, openSUSE Tumbleweed and newer). Pick
the package for your distribution:

| Distribution | Download | Install |
| --- | --- | --- |
| Ubuntu, Debian and their derivatives | [RootMind-amd64.deb](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-amd64.deb) | `sudo apt install ./RootMind-amd64.deb` |
| Fedora, openSUSE Tumbleweed | [RootMind-x86_64.rpm](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-x86_64.rpm) | `sudo dnf install ./RootMind-x86_64.rpm` (openSUSE: `sudo zypper install ./RootMind-x86_64.rpm`) |
| Any other distribution | [RootMind-x86_64.AppImage](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-x86_64.AppImage) | `chmod +x RootMind-x86_64.AppImage`, then run it |

The packages install the app and its backend to `/usr/lib/RootMind` and pull
in WebKitGTK 4.1; only that step needs root. The AppImage needs no
installation; it needs FUSE 2 (the package `libfuse2`, on Ubuntu 24.04
`libfuse2t64`). Check the
download with `sha256sum --check --ignore-missing SHA256SUMS.txt` (see
[Verify downloads](#verify-downloads)).

**Start** RootMind from your app menu, or in a terminal with:

```sh
rootmind-desktop
```

The .deb and .rpm print this command when they are installed. The AppImage
starts when you run the file.

**Windows Subsystem for Linux.** On Windows 11, RootMind runs in WSL too, and
its window opens on the Windows desktop (WSLg). WSL needs systemd for the
service: add the following to `/etc/wsl.conf`, then run `wsl --shutdown` in
Windows and open the distribution again. Without systemd, RootMind says
that it cannot set up its service.

```ini
[boot]
systemd=true
```

**What happens on the first start.** RootMind sets up its background service
for your account, without a question and without root: two systemd user
services, `rootmind-backend.service` (REST API and indexing) and
`rootmind-mcp-http.service` (MCP over HTTP), in `~/.config/systemd/user/`.
They run a copy of the backend in `~/.local/share/rootmind/runtime/`, so the
service keeps working when the AppImage is closed or moved, and keep their
data in `~/.local/share/rootmind`. When a newer version of the app starts, it
updates the service to its own version. If setting it up fails, RootMind says
why, and **Set up** (or **Repair**) tries again; Settings → **RootMind
service** → **Details** shows the service's state and the end of its
`logs/install.log`. Then add a folder under **Folders**, as on Windows.

The services start when you log in and stop when your last session ends. To
keep them running while nobody is logged in, for example for n8n on the same
computer, turn on lingering for your account once:

```sh
loginctl enable-linger "$USER"
```

**Update** by installing the package of the new version (as above, or
`sudo dnf upgrade ./RootMind-x86_64.rpm`) or by replacing the AppImage;
RootMind updates its service when it starts.

**Uninstall** the service first, then the app:

```sh
# the .deb and .rpm (from the AppImage: ~/.local/share/rootmind/runtime/*/rootmind-backend)
/usr/lib/RootMind/backend/rootmind-backend service uninstall
sudo apt remove root-mind        # or: sudo dnf remove root-mind
```

`service uninstall` stops and removes the two user services and the program
copy, and keeps your settings, API keys, index and MCP token in
`~/.local/share/rootmind`; `service uninstall --purge` deletes them as well.
Chats are in `~/.local/share/io.github.cgaeking.rootmind`.

### Upgrade from 1.x

RootMind 1.x was installed per account and, unless the background service was
turned on, ran its backend inside the app. The 2.0 installer removes a 1.x
installation of your account on its own. The folders, settings, API keys and
index of the 1.x built-in backend are not carried over: add your folders and
your API key again. If the background service was on in 1.x, it is updated
and keeps its folders, settings and index. Chats stay in both cases.

### Verify downloads

Each release lists the installer's size in its notes and ships `SHA256SUMS.txt`
and a build provenance attestation for every file. The hash must match the
file's line in `SHA256SUMS.txt`:

```powershell
(Get-FileHash .\RootMind-Setup-x64.exe -Algorithm SHA256).Hash.ToLower()
```

```sh
sha256sum --check --ignore-missing SHA256SUMS.txt
gh attestation verify RootMind-Setup-x64.exe --repo cgaeking/rootmind
```

### Update and uninstall

To update, run the installer of the new version and approve the prompt. It
replaces the installed version in place, without further questions, and
updates the service; ports, tokens, settings, index and chats are kept. If the
app is running, the installer asks to close it first.

Uninstall RootMind from Windows Settings → Apps. This removes the RootMind
services. Their data (settings, API keys, index, tokens and logs) stays in
`%ProgramData%\RootMind` unless you tick **Delete the application data**,
which deletes it and your chats as well. A silent uninstall (`/S`, as package
managers run it) also removes the services but never deletes data. To delete
the data later, remove `%ProgramData%\RootMind` from an elevated prompt.

### The RootMind service

RootMind's backend runs as the Windows service `RootMind-Backend`. It keeps
the index up to date while the app is closed and starts with Windows; the app
is a client of it. Settings → **RootMind service** shows its state, address
and version, offers **Set up** when the service is not set up yet (after a
silent or managed install) and **Repair** when it is stopped or does not
answer (one administrator approval each). When the service runs an older version than
the app, **Update service** brings it to the app's version. Both run the
backend installed with the app in `%ProgramFiles%\RootMind\backend`, which
only administrators can change.

- The service belongs to the account that installed RootMind. Other accounts
  on the same computer cannot use it; to move RootMind to another account,
  uninstall it and install it again from that account.
- It runs as LocalSystem, but it reads **only folders and files your account
  can open without administrator rights**. Network drives and shares, system
  folders and other users' profiles are not available to it, and the Folders
  screen marks a folder it cannot read.
- Its REST API requires a token. The token is in
  `%ProgramData%\RootMind\data\rest_token.txt`, which only your account,
  SYSTEM and administrators can read. The app uses it only after checking that
  the service's own process listens on the port.
- A second service, `RootMind-MCP-HTTP`, serves MCP over HTTP on 127.0.0.1
  with its own bearer token (see [Connect MCP clients](#connect-mcp-clients)).
  It is on by default; the checkbox **Serve MCP over HTTP** turns it off and
  on again (one administrator approval). Below it, **Remote access (ngrok)**
  gives it a public HTTPS address for cloud assistants (off by default; see
  [Remote access](#remote-access)).
- The services run from their own copy of the program under
  `%ProgramFiles%\RootMind Service\`, which only administrators can change,
  and keep their data in `%ProgramData%\RootMind`.

**On Linux** the same service is the two systemd user services of your
account (see [Install on Linux](#install-on-linux)), and nothing about it
needs root or an approval:

- It runs as your account and reads what your account can read, mounted
  network shares included. Other accounts on the computer get services of
  their own (on other ports) when they start RootMind.
- Its REST token is in `~/.local/share/rootmind/data/rest_token.txt`, which
  only your account can read (mode 0600, in folders of mode 0700). The app
  uses it only after checking that the unit's own process listens on the
  port.
- MCP over HTTP and remote access work as on Windows; **Serve MCP over HTTP**
  adds or removes `rootmind-mcp-http.service` without a prompt.
- `systemctl --user status rootmind-backend` and
  `journalctl --user -u rootmind-backend` show the unit and its systemd log;
  RootMind's own logs are in `~/.local/share/rootmind/logs`.

Settings → **Advanced** connects the app to a RootMind backend at another
address instead. [SECURITY.md](SECURITY.md#background-service) describes the
security model.

## Connect MCP clients

MCP clients get the same read-only tools as the app's agent and use their own
model. Connect them to the service's MCP over HTTP: Settings → RootMind
service shows the **MCP endpoint** and copies it and the token (**Copy
token**). For Claude Desktop there is a bundle that needs neither (see
[Use RootMind in Claude Desktop](#use-rootmind-in-claude-desktop)). Claude
Code:

```bash
claude mcp add --transport http rootmind http://127.0.0.1:<port>/mcp \
  --header "Authorization: Bearer <token>"
```

The port is 3000 when it is free, otherwise one from 18400–18499. The token
is kept across updates, and the server picks up folder changes without a
restart.

### Use RootMind in Claude Desktop

1. Download
   [rootmind.mcpb](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb)
   from the [latest release](https://github.com/cgaeking/rootmind/releases/latest).
2. Double-click it. Claude Desktop opens it, shows RootMind's tools and
   installs it once you confirm.

There is nothing to configure. The extension finds the RootMind service on
this computer, reads the MCP port and token from it and connects to MCP over
HTTP on 127.0.0.1. It needs RootMind installed with **Serve MCP over HTTP**
on (the default; Settings → RootMind service), and Claude Desktop must run
under the account that uses RootMind, since only that account can read the
token. On Linux it finds the service through
`~/.local/share/rootmind/service.json`, which names the program copy the
services run (no secret). When something is missing, the extension's error says
what to do, for example to install RootMind or to turn MCP over HTTP on;
once that is fixed, the next request works without reinstalling the
extension. After a repair it follows the service to a new port by itself. To
update it, install the `rootmind.mcpb` of a newer release. The bundle is not
signed yet, so Claude Desktop marks it as unsigned. Its source and how it
works: [packaging/mcpb](packaging/mcpb/README.md).

Claude Desktop starts only local (stdio) MCP servers; the extension is such
a server and forwards to the service's HTTP endpoint. The program in the
app's folder (`rootmind-backend.exe mcp`) cannot serve as a stdio server for
the installed app: it cannot open the service's index. The stdio server is
meant for a source or uv setup with its own `config.yaml` (see
[3. Use the tools from an MCP client](#3-use-the-tools-from-an-mcp-client)).

### Reach MCP from n8n

Use n8n's **MCP Client Tool** node with the values that Settings → RootMind
service shows; **Copy** next to **n8n (MCP Client Tool)** copies them with the
token filled in:

| Field | Value |
|---|---|
| Endpoint | `http://127.0.0.1:<port>/mcp` |
| Server Transport | HTTP Streamable |
| Authentication | Header Auth |
| Header Name | `Authorization` |
| Header Value | `Bearer <token>` |

- **n8n on the same machine:** use the endpoint as shown.
- **n8n in Docker on the same machine:** inside a container, `127.0.0.1` is
  the container itself. Use `http://host.docker.internal:<port>/mcp`. Docker
  Desktop forwards that name to the host. If the connection is refused, run a
  reverse proxy on the host that listens on an address the container can
  reach and forwards to `127.0.0.1:<port>`, and keep that address closed to
  other machines.
- **n8n on another machine:** only through a TLS-terminating reverse proxy or
  a tunnel (such as [Remote access](#remote-access)) in front of
  `127.0.0.1:<port>`. Never expose the plain HTTP port: the server speaks
  plain HTTP, and anyone with the token can read every indexed document. See
  [Reach RootMind from cloud assistants](#reach-rootmind-from-cloud-assistants).

### Reach RootMind from cloud assistants

Assistants that run on someone else's servers, such as Grok Bot, a cloud-hosted
n8n or other web agents, cannot reach `127.0.0.1` on your computer. They need a
public HTTPS address that forwards to the MCP endpoint, which a tunnel on your
computer provides: RootMind's built-in **Remote access** (ngrok), or a tunnel
you run yourself (Tailscale Funnel, the ngrok agent). Either way the tunnel
forwards to MCP over HTTP only, and the client sends the same token.

#### Remote access

RootMind can run the tunnel itself. The RootMind service's MCP server
(`RootMind-MCP-HTTP`) opens it with ngrok's open-source SDK, so no ngrok
program needs to be installed. It is off by default.

1. Create a free account at [ngrok.com](https://ngrok.com) and copy your
   authtoken from the dashboard (**Your Authtoken**). Under **Domains** you can
   reserve a free fixed domain; without one, ngrok assigns an address that can
   change.
2. In Settings → RootMind service, under **Remote access (ngrok)**, paste the
   authtoken and, if you have one, the domain, then choose **Save**. The
   authtoken is never shown again: the field then only says **Saved**
   (**Replace** and **Remove** change it).
3. Tick **Reach MCP from the internet through ngrok** and confirm. The status
   changes to **Connected** and shows the **Public MCP endpoint**
   (`https://<domain>/mcp`) with **Copy**. Use it with the MCP token (**Copy
   token**) in the assistant, see [In the assistant](#in-the-assistant).

Remote access needs **Serve MCP over HTTP**; turning that off turns remote
access off too. Changes need no administrator approval, take effect within a
few seconds and survive restarts and updates: the tunnel starts with the
service. Unticking the box closes the public address at once. When the
connection to ngrok drops, the service reconnects; a refused connection (a
wrong authtoken, a domain that is not yours or already in use) appears as the
status with ngrok's message and is retried with growing pauses (up to five
minutes).

- ngrok allows one endpoint per domain. If you already run your own ngrok
  tunnel on the same domain, stop it before you turn remote access on.
- The authtoken is stored in `%ProgramData%\RootMind\data\ngrok_authtoken.txt`,
  which only SYSTEM, administrators and the MCP service account can read; it
  is never written to `config.yaml`, never returned by the app's API and never
  logged. Uninstalling without **Delete the application data** keeps it.
- Free ngrok accounts have traffic limits; see ngrok's pricing page.

#### Your own tunnel

**With the ngrok agent** (free account, one fixed free domain):

1. Create an account at [ngrok.com](https://ngrok.com), then install the agent
   and add your authtoken:

   ```powershell
   winget install ngrok.ngrok
   ngrok config add-authtoken <your-ngrok-authtoken>
   ```

2. In the ngrok dashboard under **Domains**, note your free domain, then start
   the tunnel to the MCP port (3000 unless Settings shows another):

   ```powershell
   ngrok http 3000 --url https://<your-free-domain>
   ```

   To keep it running after a restart, define the tunnel in ngrok's
   configuration file and install it as a service (`ngrok service install`;
   see the ngrok documentation).

**With Tailscale Funnel** (free Tailscale account and app):

```powershell
tailscale funnel --bg 3000
```

The command prints the public address (`https://<computer>.<tailnet>.ts.net`)
and keeps the funnel running in the background, also after a restart. The first
time, it asks you to allow Funnel for your tailnet.

#### In the assistant

Add a custom MCP connector (or n8n's MCP Client Tool) with:

| Field | Value |
|---|---|
| URL | `https://<your-public-address>/mcp` |
| Transport | HTTP Streamable |
| Authentication | header `Authorization: Bearer <token>` (Settings → RootMind service → **Copy token**) |

Clients that only offer an OAuth sign-in and cannot send a bearer token cannot
connect this way.

Keep in mind:

- Anyone with the token can read every indexed document through the tunnel.
  Keep it secret; stopping the tunnel (or turning remote access off) closes
  the public access.
- What a cloud assistant reads through RootMind (search results and document
  text) goes to that assistant's provider.
- RootMind refuses requests that a web page sends from a browser (requests
  with a non-local `Origin` header); server-to-server requests from the
  assistant are not affected.

## How it works

```mermaid
flowchart LR
    desktop["Desktop app<br/>Tauri + React"]
    mcpclient["MCP client<br/>e.g. Claude Desktop, Claude Code, n8n"]

    subgraph backend["Backend (Python)"]
        rest["REST API<br/>127.0.0.1 + bearer token"]
        mcp["MCP servers<br/>stdio, HTTP + bearer token"]
        engine["Agent loop<br/>rlm_engine/engine.py"]
        tools["Tool layer<br/>tools.py"]
        repo[("SQLite<br/>FTS5 + trigrams")]
        indexer["Indexer + scheduler"]
    end

    folders[("Your folders")]
    llm(["LLM provider<br/>Anthropic, OpenAI or xAI"])

    desktop -- "/query, /search, /config" --> rest
    mcpclient --> mcp
    rest --> engine
    rest -- "search, documents" --> repo
    engine <--> llm
    engine --> tools
    mcp --> tools
    tools --> repo
    indexer -- "read files" --> folders
    indexer -- "extracted text" --> repo
```

1. **Indexing uses no LLM.** A connector walks each configured folder. Parsers
   extract text from PDF, DOCX, XLSX, PPTX, CSV, Markdown and other text files.
   The text goes into SQLite: a documents table, an FTS5 index and a trigram
   table for typo-tolerant search. PDF pages, slides and sheets keep markers in
   the text, so a hit can be cited by location. Unchanged files are skipped by
   content hash.
2. **The model chooses what to read.** For `POST /query`, the engine offers the
   model five read-only tools: `search_documents`, `search_fuzzy`,
   `list_documents`, `read_document` and `get_statistics`. A typical run is:
   search, read the window around a hit, perhaps search again with other words,
   then answer.
3. **The loop is bounded.** The loop makes at most 10 model calls that may
   request tools. If the model is still searching after that, one final call
   with tools disabled asks it to answer from what it has found. At most 8
   tool calls run per model turn, and the tool results of one question are
   capped at 200,000 characters; calls beyond that get an error result
   instead of running. Every LLM call has a timeout, a read returns at most
   50,000 characters, tool errors go back to the model as error results
   instead of ending the loop, and an answer cut off by the output token
   limit says so.
4. **Answers carry sources.** The response lists the documents the model read,
   with page, slide or sheet where it can be resolved, every tool call it
   made, and why each LLM call stopped.
5. **MCP clients bring their own model.** Over MCP, RootMind serves the same
   tool definitions and the client's model does the deciding. Search and
   indexing never need an API key; only questions (`POST /query`), chat titles
   and the model list in the settings call the provider.

The desktop app talks to the **RootMind service** on the same computer, or to
a **custom** backend at an address you enter under Settings → Advanced.
Development builds do not use the service and ask for a backend address.
[ARCHITECTURE.md](ARCHITECTURE.md#deployment-modes) has the details.

## Why a tool loop instead of embedding RAG

A typical RAG pipeline splits documents into chunks, embeds them, retrieves
the top matches by vector similarity and puts those chunks into the prompt.
The model sees only what that retrieval step picked.

RootMind keeps each document's full text in a full-text index and lets the
model drive retrieval. It can search, look at the snippets, read the text
around a hit, and search again with different terms when the first query
misses. This is sometimes called RLM-style or agentic retrieval. The engine
module is named `rlm_engine` after that idea; in this code base it is a bounded
tool-use loop, not a model that calls itself recursively.

| Works well | Works less well |
|---|---|
| Exact terms: invoice numbers, product codes, names, amounts | Synonyms and paraphrases: "vacation" does not match "paid time off" unless the model tries the other word |
| No embedding model or vector store; indexing only parses text and works offline | Questions in a different language than the documents |
| The model reads the context around a hit, such as the table header above a number, instead of an isolated chunk | More LLM round trips per question, so more latency and token cost |
| Every retrieval step is visible as a tool call | Answer quality depends on the model's search strategy |

Trigram fuzzy search covers typos, not synonyms. Eval case c09 (the synonym
trap) keeps that weakness visible.

## Install for developers and servers

**With uv** (any OS with Python 3.12 or newer; uv installs it if needed):

```bash
uv tool install "git+https://github.com/cgaeking/rootmind@v2.4.0#subdirectory=backend"
rootmind --help        # api, index, rebuild-trigrams, mcp, mcp-http, service
```

This installs the commands `rootmind`, `rootmind-mcp` and `rootmind-mcp-http`.
On Windows, `rootmind service status` and `rootmind service uninstall` work
from such an install, but `rootmind service install` is refused and prints the
download address of the release build. The service runs as SYSTEM, so it may
only run a program from a folder that administrators alone can change;
`service install` copies the release build into such a folder, while a uv or
pip environment lives in a folder your account can change.

**Windows server or a machine without the app.** The release also contains
the backend alone, `rootmind-backend-windows-x64.zip`. Download and check it
in PowerShell:

```powershell
$base = "https://github.com/cgaeking/rootmind/releases/latest/download"
Invoke-WebRequest "$base/rootmind-backend-windows-x64.zip" -OutFile rootmind-backend-windows-x64.zip -UseBasicParsing
Invoke-WebRequest "$base/SHA256SUMS.txt" -OutFile SHA256SUMS.txt -UseBasicParsing
$line = Select-String -Path SHA256SUMS.txt -SimpleMatch "rootmind-backend-windows-x64.zip"
$expected = ($line.Line -split '\s+')[0]
$actual = (Get-FileHash rootmind-backend-windows-x64.zip -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw "Checksum mismatch: $actual" }
```

Then, from an elevated PowerShell of the account whose folders RootMind should
index (it must be a local administrator), unzip it into a folder that only
administrators can change and install the service from there.
`service install` refuses, with exit code 10, a program in a folder that other
accounts can change, such as a download folder:

```powershell
$dir = Join-Path $env:ProgramFiles "RootMind Backend"
Expand-Archive rootmind-backend-windows-x64.zip -DestinationPath $dir -Force
$exe = Join-Path $dir "rootmind-backend\rootmind-backend.exe"
# install, or update an installed service
& $exe service install
# state, port, version and token files (works without elevation)
& $exe service status
# remove the service; --purge also deletes its data
& $exe service uninstall
```

`service install` copies the program to
`%ProgramFiles%\RootMind Service\<version>-<id>\`, so the unzipped folder is
not needed afterwards. It uses port 8000 and, for MCP over HTTP, port 3000;
`--port auto` and `--mcp-port auto` fall back to 18300–18399 and 18400–18499
when those are taken. MCP over HTTP is on for a new install; `--no-mcp-http`
leaves it out. Running `service install` again updates the service and keeps
its ports, MCP setting, tokens and data; `service install --help` lists every
option. The REST token is in `%ProgramData%\RootMind\data\rest_token.txt` and
the MCP token in `%ProgramData%\RootMind\data\api_token.txt`.

A new service starts without folders. Add them under `connectors` in
`%ProgramData%\RootMind\config.yaml` (as in
[`backend/config.example.yaml`](backend/config.example.yaml); the file can
only be changed from an elevated prompt) and restart the service with
`Restart-Service RootMind-Backend`, or send them to the service's REST API
with `POST /config` and the REST token.

The zip also runs without a service, for example
`rootmind-backend.exe api --require-token` or `rootmind-backend.exe mcp-http`.

**Linux server or a machine without the app.** The release contains the Linux
backend alone, `rootmind-backend-linux-x64.tar.gz` (x86-64, glibc 2.35 or
later). As the account whose folders RootMind should index (not root), unpack
it into a folder that only you can change and install the service from there:

```sh
base=https://github.com/cgaeking/rootmind/releases/latest/download
curl -L -O "$base/rootmind-backend-linux-x64.tar.gz" -O "$base/SHA256SUMS.txt"
sha256sum --check --ignore-missing SHA256SUMS.txt
mkdir -p ~/opt && tar -xzf rootmind-backend-linux-x64.tar.gz -C ~/opt
exe=~/opt/rootmind-backend/rootmind-backend
"$exe" service install      # install, or update the installed services
"$exe" service status       # state, port, version and token files
"$exe" service uninstall    # remove them; --purge also deletes the data
loginctl enable-linger "$USER"   # keep them running without a login
```

`service install` copies the program into
`~/.local/share/rootmind/runtime/<version>-<id>/` and writes the two units to
`~/.config/systemd/user/`; it needs a systemd user manager for the account,
which a desktop or SSH login starts (exit code 11 without one, for example
under `su` or `sudo`). The ports, options and tokens are those of Windows; the
data is in `~/.local/share/rootmind` (`config.yaml`, `data/rest_token.txt`,
`data/api_token.txt`). After editing `config.yaml`, restart the service with
`systemctl --user restart rootmind-backend`.

## Local models with Ollama

Choose **Ollama** under **Settings → LLM** (or `provider: ollama` under `llm`
in `config.yaml`) to run the question loop against a model on your own
machine. There is no API key; the base URL defaults to
`http://localhost:11434` and can point at another computer on your network.

```yaml
llm:
  provider: ollama
  model: qwen3:8b          # any model you pulled that supports tools
  base_url: http://localhost:11434
```

- **What stays local:** with Ollama on this computer, the question, the
  excerpts the model reads and the answer never leave it. With a base URL on
  another computer, they go to that computer, unencrypted over plain `http://`.
- **Tool support is required.** Pick a model Ollama lists as supporting tools
  (`ollama show <model>`). A model without it fails with a clear message.
- **Context length:** the loop reads document windows, and Ollama's default
  context is small. Raise it (for example `OLLAMA_CONTEXT_LENGTH=16384` for the
  server, or a Modelfile with `PARAMETER num_ctx 16384`), or long documents are
  cut off without an error.
- **Search first:** Ollama models are not forced to call a tool on the first
  turn, since not every model supports that. If the first answer never looked at
  the documents, RootMind asks once more with an explicit instruction.
- Small local models follow the tool loop less reliably than the hosted ones.
  Measure the one you use with `scripts/run_evals.py --mode live --provider
  ollama --model <name>` (see [`backend/evals`](backend/evals/README.md)).

## Run from source

You need [uv](https://docs.astral.sh/uv/); it installs Python 3.12 if
necessary. The desktop app also needs Node 24, pnpm 11, Rust 1.96 or newer and, on
Windows, the MSVC build tools and WebView2.

### 1. Index and search without an API key

```bash
cd backend
uv sync
cp config.example.yaml config.yaml   # indexes the fictional demo corpus in evals/corpus
uv run rootmind index                # builds data/index.db; no LLM involved
uv run rootmind api                  # REST API on http://127.0.0.1:8000, no token
```

In a second terminal:

```bash
curl "http://127.0.0.1:8000/search?q=total%20due&limit=3&snippets=true"
```

The first hit is `invoice_INV-2026-0142.pdf` with `"page": "Page 2"` and a
snippet around the match. Without `snippets=true` the search returns only the
ranked list, which is faster on large indexes.

The interactive API docs are at <http://127.0.0.1:8000/docs>. While the API
runs, it re-indexes about 30 seconds after start and then every 24 hours
(`indexer.sync_interval_hours`). To index your own files, add entries under
`connectors` in `config.yaml`. Use absolute paths: relative connector paths
resolve against the working directory.

`rootmind api` started this way requires no token. To require one,
set `RLM_API_TOKEN` or pass `--require-token`, which creates
`data/rest_token.txt`, and send `Authorization: Bearer <token>` with each
request. The docs pages are off while a token is required.

### 2. Ask a question

Set a key for one provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or
`XAI_API_KEY`; Ollama needs none, see
[Local models with Ollama](#local-models-with-ollama)) in the environment or in `backend/.env` (see
[`.env.example`](backend/.env.example)). Choose the provider and model under
`llm` in `config.yaml`, restart the API, then:

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the total amount due on invoice INV-2026-0142?"}'
```

The response contains `answer`, `sources` (with `page` where known),
`tool_calls` and `tokens_used`. The question, search snippets and document
excerpts go to the provider you configured; see [SECURITY.md](SECURITY.md).

### 3. Use the tools from an MCP client

The MCP servers read the same `config.yaml` and database as the REST API. They
only serve and never index, so keep the index current with `rootmind index` or
a running REST API.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "rootmind": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/rootmind/backend", "run", "rootmind-mcp"]
    }
  }
}
```

If Claude Desktop cannot find `uv`, put the absolute path of the `uv`
executable into `command`. On Windows, write the paths with forward slashes or
escaped backslashes (`C:\\...`).

Claude Code:

```bash
claude mcp add --transport stdio rootmind -- \
  uv --directory /absolute/path/to/rootmind/backend run rootmind-mcp
```

Over HTTP, for MCP clients that cannot start a local process:

```bash
cd backend
uv run rootmind-mcp-http             # http://127.0.0.1:3000/mcp
```

On first start the server generates a token and saves it to
`data/api_token.txt` (under `RLM_HOME`, or the working directory when that is
unset); alternatively, set `MCP_API_TOKEN`. The server prints where the token
came from and a short fingerprint of it, never the token itself. Then:

```bash
claude mcp add --transport http rootmind http://127.0.0.1:3000/mcp \
  --header "Authorization: Bearer <token from data/api_token.txt>"
```

The stdio server reads the folder list at startup, so restart it (the MCP
client) after adding or removing folders. The HTTP server re-reads it when
`config.yaml` changes. The HTTP server binds to 127.0.0.1 and speaks plain
HTTP; to reach it from another machine, put it behind a TLS-terminating proxy
or tunnel and never expose the plain port. `rootmind-mcp-http --index` also
runs the background indexer in that process. Use it only when no REST API
indexes the same database: every indexing process removes the documents of
folders that are missing from its own copy of the config.

### 4. Run the desktop app (development)

```bash
# terminal 1: backend, with the Vite dev origin allowed by CORS
cd backend
RLM_DEV=1 uv run rootmind api        # PowerShell: $env:RLM_DEV=1; uv run rootmind api

# terminal 2: desktop
cd desktop
pnpm install
pnpm tauri dev
```

A development build does not use the RootMind service, so the app asks for a
backend address: confirm `http://127.0.0.1:8000`. More in
[desktop/README.md](desktop/README.md).

### Build the installer yourself

On Windows, from the repository root:

```powershell
# 1. Backend: PyInstaller onedir build -> backend/dist/rootmind-backend/
cd backend
uv sync --locked --no-dev --group build
uv run --no-sync pyinstaller packaging/rootmind-backend.spec --noconfirm --clean --distpath dist
cd ..

# 2. Copy it to desktop/src-tauri/binaries/rootmind-backend/ and check it
python scripts/prepare_backend.py

# 3. Installer with the release overlay, which adds the backend
cd desktop
pnpm install
pnpm tauri build --config src-tauri/tauri.release.conf.json
```

The installer is written to `desktop/src-tauri/target/release/bundle/nsis/`.
Run `uv sync` in `backend/` afterwards to get the development tools back.
Without the overlay, `pnpm tauri build` builds an app without a backend.

## Repository layout

```
backend/
  src/rootmind/
    rlm_engine/engine.py   agent loop
    tools.py               tool registry, argument validation, result formatting
    mcp_stdio.py           MCP server over stdio
    mcp_http.py            MCP server over HTTP with a bearer token
    api/                   REST API (FastAPI)
    auth.py, access.py     REST token handling; folder access policy interface
    winaccess.py           folder access policy of the Windows service (AuthZ)
    service/               background service: install, uninstall, status, service host;
                           Windows services and Linux systemd user units (linux*.py, systemd.py)
    database/              SQLite models, FTS5 and trigram search
    indexer/               parsers, indexer, sync scheduler
    connectors/            local folder connector
  tests/                   offline test suite with fake LLM clients; e2e/ service test
  evals/                   fictional corpus and eval cases
  scripts/                 eval runner and corpus generator
  packaging/               PyInstaller spec, smoke test, tree hash
  deploy/windows/          run-backend.bat: the REST API from source in a console window
desktop/
  src/                     React + TypeScript UI
  src-tauri/               Rust shell: service connection and commands, chat history;
                           installer/ with the NSIS template and the service hooks;
                           linux/ and tauri.linux.conf.json for the Linux service and packages
  scripts/                 installer smoke test
packaging/winget/          winget manifest templates
packaging/mcpb/            Claude Desktop bundle (rootmind.mcpb): stdio-to-HTTP bridge, manifest, tests
scripts/                   version check, release notes, backend preparation, winget rendering,
                           NSIS template refresh
.github/workflows/         CI and release
```

Design decisions, guardrails and trade-offs are described in
[ARCHITECTURE.md](ARCHITECTURE.md), the threat model in [SECURITY.md](SECURITY.md),
and the changes per version in [CHANGELOG.md](CHANGELOG.md).

## Tests and evals

Both run offline and need no API key:

```bash
cd backend
uv sync
uv run pytest -q                      # fake LLM clients, generated test corpus
uv run python scripts/run_evals.py    # retrieval regression: scripted policy, no model
```

**Tests.** An autouse fixture replaces the Anthropic and OpenAI client
constructors with functions that raise and removes API keys from the
environment. Tests inject scripted fake clients
([`tests/fakes.py`](backend/tests/fakes.py)) through the engine's `client`
parameter. Every test gets its own `RLM_HOME` and working directory. Tests
that need documents get a small generated corpus with one PDF, DOCX, XLSX,
PPTX, CSV, Markdown and text file each. Tests of the Windows folder access
policy and of the service's folder permissions run on Windows only, those of
the Linux file modes on Linux only (the Linux service logic itself, with a
fake `systemctl`, runs everywhere); installing real services is left to the
CI end-to-end tests.

**Evals.** [`scripts/run_evals.py`](backend/scripts/run_evals.py) indexes the
fictional corpus in [`backend/evals/corpus/`](backend/evals/corpus/) with the
real indexer and runs 16 questions through the real engine.

- *Live mode* is the eval: it runs a real model several times per case and
  reports pass rate (expected source cited, required strings present,
  forbidden strings absent), tool-path variance, tool calls, tokens, latency
  and stop reasons. It reads keys only from environment variables and never
  runs in CI.
- *Dry mode* is a retrieval regression test, not an eval of the agent: a
  scripted policy replaces the model, searches with a query written by hand
  for each case (`dry_search` in the dataset), reads the suggested window and
  answers with the excerpt. It checks that the index and the tool layer
  still put the right document first (hit@1), among the first five hits
  (hit@5), and cite the right page, slide or sheet. With hand-picked queries
  1.0 on the 15 gated cases is the expected result; c09, the synonym trap,
  is the known miss. CI fails if hit@5 drops below 1.0. It says nothing
  about answer quality.

Details: [backend/evals/README.md](backend/evals/README.md).

**CI.** GitHub Actions workflows with actions pinned by commit SHA and no
secrets. Only the release job can write to the repository:

| Workflow | What it runs |
|---|---|
| [`backend.yml`](.github/workflows/backend.yml) | ruff check and format check; the version check across backend, desktop and the Claude Desktop bundle; the tests of the release scripts on Ubuntu and Windows; pytest with coverage on Ubuntu (Python 3.12 and 3.14) and Windows (Python 3.12); the dry retrieval regression |
| [`backend-exe.yml`](.github/workflows/backend-exe.yml) | The PyInstaller onedir builds on Windows and on Ubuntu 22.04, a smoke test of each executable (REST, MCP over HTTP and stdio, indexing of every file type) and their tree hashes; then end-to-end tests of the service: on the elevated Windows runner the Windows services (install, permissions, tokens, read confinement, update and uninstall), on Ubuntu the systemd user units of the runner's account (refusals, install, file modes, tokens, indexing, restarts, update, MCP off and on, uninstall). On pull requests the service tests run only when service-related code changed |
| [`desktop.yml`](.github/workflows/desktop.yml) | ESLint, TypeScript typecheck, vitest, Vite build, `cargo fmt`, `cargo clippy`, `cargo check`, `cargo test` on Windows; `cargo clippy` and `cargo test` on Ubuntu 22.04 |
| [`release.yml`](.github/workflows/release.yml) | For a version tag `v*`: the backend through `backend-exe.yml`; the per-machine installer with that backend, a check that it updates an installed version in place without a reinstall page, and a smoke test on the elevated runner that installs, updates, reinstalls and uninstalls RootMind with its Windows services, and installs it silently as SYSTEM (app only), sets the service up as the app does and updates it as SYSTEM; the winget manifests validated against their schemas; `rootmind.mcpb` for Claude Desktop, tested, validated and packed; the Linux packages (.deb, .rpm, AppImage) with the Linux backend, the .deb installed on the runner and both packaged backends started; then a GitHub Release with the installer, the Linux packages, the bundle, both backend archives, `SHA256SUMS.txt` and attestations. Manual runs and pull requests that change the packaging are dry runs that publish nothing |
| [`mcpb.yml`](.github/workflows/mcpb.yml) | The Claude Desktop bundle: the bridge's `node:test` tests on Ubuntu and Windows, then the manifest validated and the bundle packed with the mcpb CLI |
| [`secrets.yml`](.github/workflows/secrets.yml) | detect-secrets over all tracked files, against the audited baseline `.secrets.baseline` |

Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)) proposes
monthly dependency updates for all four ecosystems.

## Security model in short

- RootMind is built for one person on their own computer. The REST API binds
  to 127.0.0.1, refuses connections from other machines, rejects foreign
  `Host` headers and allows cross-origin requests only from the desktop app.
- The RootMind service requires a bearer token on its REST API. `rootmind api`
  from source runs without a token unless you ask for one; without a token,
  any local process can call it.
- The service runs as LocalSystem but reads only what the installing account
  can read without administrator rights. Installing and repairing it needs
  one administrator approval. On Linux it runs as systemd user services of
  your own account, without root; file modes keep its data and tokens
  private to the account.
- The MCP HTTP server is on by default; it requires a bearer token and binds
  to 127.0.0.1. Remote access (ngrok) is off by default; turned on, anyone
  with the MCP token can read every indexed document through the public
  address.
- Indexing stays on your machine. Answering a question sends the question,
  search snippets and document excerpts to the LLM provider you configured.
- Indexed documents are untrusted input. All tools are read-only, document
  text is marked as data, and a question's tool use is capped, but prompt
  injection is not a solved problem.

The full threat model is in [SECURITY.md](SECURITY.md).

## Limitations

- The desktop app and the RootMind service run on Windows and on Linux
  (x86-64); there is no macOS build. The backend is cross-platform Python;
  CI runs its tests on Ubuntu and Windows.
- The release binaries are not code-signed yet (see [Install](#install)).
  RootMind is not in the winget repository yet; the first submission is
  under way.
- No OCR: scanned PDFs without a text layer produce no text.
- Legacy `.doc` and `.xls` files go to the DOCX and XLSX parsers and usually
  fail to parse.
- No streaming: the desktop app waits for the complete answer.
- Full-text search has no synonym or cross-language matching (see the
  trade-off table above).
- On Windows, installing needs an administrator account, and the service
  serves one account per computer: the one that installed RootMind. On
  Linux every account that starts RootMind gets services of its own.
- The Windows service cannot index network drives or shares.
- On Linux the service runs only while you are logged in, unless lingering
  is on (`loginctl enable-linger`).
- Only one process per database should index. The MCP servers only serve by
  default; set `RLM_DISABLE_SCHEDULER=1` on any additional REST API that
  shares the database.
- The MCP stdio server reads the list of folders at startup; restart it after
  changing folders.
- An incremental sync hashes every file in every configured folder, which is
  slow for very large folders.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, checks,
conventions and the release steps. Please report security issues privately as
described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE), copyright (c) 2026 Christian Gäking.

The packaged backend and the installer bundle third-party dependencies, which
keep their own licenses.

## Code signing policy

Releases are not code-signed yet; each release ships checksums and build
provenance attestations instead (see [Verify downloads](#verify-downloads)).
[CODE_SIGNING.md](CODE_SIGNING.md) describes what will be signed, the team
roles and the privacy policy.
