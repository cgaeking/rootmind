# Quickstart

**English** | [Deutsch](QUICKSTART.de.md)

From download to a first search result in a few minutes, on Windows 10 or 11
(x64) with an administrator account. The [README](../README.md) has the full
documentation.

1. **Download**
   [RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)
   from the [latest release](https://github.com/cgaeking/rootmind/releases/latest).
2. **Install.** Run the installer and approve the administrator prompt once;
   no Python is needed. It installs the app and sets up the RootMind service,
   which keeps your index up to date and starts with Windows. The installer is
   not code-signed yet, so Windows SmartScreen may show "Windows protected
   your PC": choose **More info**, then **Run anyway**.
3. **Add a folder.** Start RootMind, open **Folders** and add a folder with
   your documents; the change is saved right away. Indexing starts within
   about a minute; **Status** shows how far it is.
4. **Search.** Open **Chat**, tick **Search only**, type a word or phrase from
   your documents and press Enter. Search needs no API key.
5. **Ask questions** (optional). Under **Settings**, paste an API key from
   Anthropic, OpenAI or xAI and click **Save** next to it. RootMind usually
   recognizes the provider from the key (otherwise choose it under
   **Provider** first), checks the key and loads the model list; a model is
   selected for you. Then untick **Search only** and ask in Chat; answers
   list their sources. The question and document excerpts go to that
   provider.

The app follows the Windows display language (English or German); Settings →
**Appearance** → **Language** changes it. Coming from RootMind 1.x? The
installer replaces it, but you add your folders and API key again (see
[Upgrade from 1.x](../README.md#upgrade-from-1x)).

## Use RootMind from other tools (MCP)

MCP clients get the same read-only search tools and use their own model, so
RootMind needs no API key for them. The RootMind service serves MCP over HTTP
on 127.0.0.1 by default: Settings → **RootMind service** shows the **MCP
endpoint** and copies it and the bearer token (**Copy token**).

- **Claude Code:**
  `claude mcp add --transport http rootmind <endpoint> --header "Authorization: Bearer <token>"`.
  Claude Desktop needs a stdio-to-HTTP bridge, which RootMind does not include
  (see [Connect MCP clients](../README.md#connect-mcp-clients)).
- **n8n and other remote clients:** **Copy** next to **n8n (MCP Client Tool)**
  copies the values for n8n's MCP Client Tool node. See
  [Reach MCP from n8n](../README.md#reach-mcp-from-n8n).
