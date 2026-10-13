# Quickstart

**English** | [Deutsch](QUICKSTART.de.md)

From download to a first search result in a few minutes, on Windows 10 or 11
(x64). The [README](../README.md) has the full documentation.

1. **Download**
   [RootMind-Setup-x64.exe](https://github.com/cgaeking/rootmind/releases/latest/download/RootMind-Setup-x64.exe)
   from the [latest release](https://github.com/cgaeking/rootmind/releases/latest).
2. **Install.** Run the installer. It installs RootMind for your account, with
   no administrator rights and no Python needed. The installer is not
   code-signed yet, so Windows SmartScreen may show "Windows protected your
   PC": choose **More info**, then **Run anyway**.
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
**Appearance** → **Language** changes it.

## Use RootMind from other tools (MCP)

MCP clients get the same read-only search tools and use their own model, so
RootMind needs no API key for them.

- **Claude Desktop and Claude Code** start the bundled backend as an MCP
  server over stdio (`rootmind-backend.exe mcp`). The configuration for both
  is in [Connect MCP clients](../README.md#connect-mcp-clients).
- **n8n and other remote clients** connect over HTTP. Turn on Settings →
  **Background service** (needs an administrator account) with **Serve MCP
  over HTTP**; Settings then shows the endpoint and a bearer token to copy.
  See [Reach MCP from n8n](../README.md#reach-mcp-from-n8n).
