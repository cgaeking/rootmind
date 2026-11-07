# Install in detail

**English** | [Deutsch](INSTALL.de.md)

The [README](../README.md#install) has the short version. This page has everything else: the downloads, silent and managed installs, Linux and WSL, upgrading from 1.x and the RootMind service.

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
  [Install for developers and servers](DEVELOPMENT.md#install-for-developers-and-servers)).
- **[rootmind.mcpb](https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb)**,
  RootMind for Claude Desktop, to install after the app (see
  [Use RootMind in Claude Desktop](../README.md#use-rootmind-in-claude-desktop)).

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
[code signing policy](../CODE_SIGNING.md)).

Then start RootMind and add a folder under **Folders**. Indexing starts within
about a minute, and search works without an API key once the folder is
indexed. To ask questions, add an API key for Anthropic, OpenAI or xAI under
**Settings** and save it, or choose **Ollama** to use a local model. The service keeps the index up to date while the
app is closed.

The app follows the Windows display language (English or German); Settings →
**Appearance** → **Language** changes it. **About** in the menu shows the app
and server versions and links to releases and issues.

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
`libfuse2t64`). Text recognition (OCR, per folder) uses the distribution's
Tesseract: the packages recommend it, so `apt` installs it along; with the
AppImage, install `tesseract-ocr` and `tesseract-ocr-deu` (Fedora:
`tesseract`, `tesseract-langpack-deu`) yourself. The Windows installer
brings its own. Check the
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
is a client of it. Settings → **RootMind Service** shows its state, offers **Set up** when the service is not set up yet (after a
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
  SYSTEM and administrators can read. The app uses it only after the service
  on the port proved that it knows the token (without the token being sent).
- A second service, `RootMind-MCP-HTTP`, serves MCP over HTTP on 127.0.0.1
  with its own bearer token (see [Connect MCP clients](MCP.md#connect-mcp-clients)).
  It is on by default; the checkbox **Serve MCP over HTTP** turns it off and
  on again (one administrator approval). It is in Settings → **MCP**, with
  **Local**, the endpoint for programs on this computer with the MCP token,
  and the **Remote / Public access with ngrok** checkbox, which adds a public
  HTTPS address for Claude on the web and on your phone and other cloud
  assistants (see
  [Remote access](MCP.md#remote-access)).
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
  uses it only after the service on the port proved that it knows the token.
- MCP over HTTP and remote access work as on Windows; **Serve MCP over HTTP**
  adds or removes `rootmind-mcp-http.service` without a prompt.
- `systemctl --user status rootmind-backend` and
  `journalctl --user -u rootmind-backend` show the unit and its systemd log;
  RootMind's own logs are in `~/.local/share/rootmind/logs`.

Settings → **RootMind Service** chooses the server the app uses: **Local**
(this service, with its address) or **Remote**, a RootMind server at another
address with an optional token (**Apply**, **Check**); the service's own
address typed there stays the service. [SECURITY.md](../SECURITY.md#background-service) describes the
security model.
