# MCP clients in detail

**English** | [Deutsch](MCP.de.md)

The [README](../README.md#use-rootmind-in-claude-and-other-tools) has the short version. This page has the details: the endpoint and token, how the Claude Desktop extension works, n8n in Docker, remote access and your own tunnel.

## Connect MCP clients

MCP clients get the same read-only tools as the app's agent and use their own
model. Connect them to the service's MCP over HTTP: Settings → **MCP**
shows the **MCP endpoint** and copies it and the MCP token (**Copy MCP
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
on (the default; Settings → MCP), and Claude Desktop must run
under the account that uses RootMind, since only that account can read the
token. On Linux it finds the service through
`~/.local/share/rootmind/service.json`, which names the program copy the
services run (no secret). When something is missing, the extension's error says
what to do, for example to install RootMind or to turn MCP over HTTP on;
once that is fixed, the next request works without reinstalling the
extension. After a repair it follows the service to a new port by itself. To
update it, install the `rootmind.mcpb` of a newer release. The bundle is not
signed yet, so Claude Desktop marks it as unsigned. Its source and how it
works: [packaging/mcpb](../packaging/mcpb/README.md).

The bundle is also listed in the official
[MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.cgaeking/rootmind`. Every release (not a prerelease) updates the
entry with its download URL and SHA-256, so clients that install from the
registry get the current bundle.

Claude Desktop starts only local (stdio) MCP servers; the extension is such
a server and forwards to the service's HTTP endpoint. The program in the
app's folder (`rootmind-backend.exe mcp`) cannot serve as a stdio server for
the installed app: it cannot open the service's index. The stdio server is
meant for a source or uv setup with its own `config.yaml` (see
[3. Use the tools from an MCP client](DEVELOPMENT.md#3-use-the-tools-from-an-mcp-client)).

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
   **ngrok Authtoken** from the dashboard (**Getting Started › Your
   Authtoken**). Under **Domains**, create the free static domain; without
   one, ngrok assigns an address that changes after every restart. **Get your
   free ngrok Authtoken and domain** in the app walks you through this.
2. In Settings → MCP, tick **Remote / Public access with ngrok**, paste the ngrok Authtoken
   and the domain, choose **Connect** and confirm. The ngrok Authtoken is
   never shown again: the field then only says **Saved** (**Replace** and
   **Remove** change it). The MCP token is refused there: it belongs in the
   client, not in this field.
3. The status changes to **Connected** and shows the **Public MCP endpoint**
   (`https://<domain>/mcp`) with **Copy** and **Copy MCP token**. **Use RootMind
   in Claude on the web and on your phone** shows the connector values ready
   to copy; see [In the assistant](#in-the-assistant).

Remote access needs **Serve MCP over HTTP**; turning that off turns remote
access off too. Changes need no administrator approval, take effect within a
few seconds and survive restarts and updates: the tunnel starts with the
service. Unticking **Remote** closes the public address at once. When the
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
| Authentication | header `Authorization: Bearer <token>` (Settings → MCP → **Copy MCP token**) |

Clients that only offer an OAuth sign-in and cannot send a bearer token cannot
connect this way.

**Claude on the web and on your phone.** On claude.ai (in the browser or in
Claude Desktop), open Customize → Connectors → **Add** → **Add custom
connector** and enter:

| Field | Value |
|---|---|
| Name | `RootMind` |
| URL | `https://<your-public-address>/mcp` |
| Authentication | **No sign in** |
| Request header | `Authorization` = `Bearer <token>` |

The connector is then also in the Claude app on your phone: turn it on in a
chat and ask about your documents. Claude connects from Anthropic's servers,
so your computer must be on and awake, and what Claude reads through RootMind
goes to Anthropic. In the app, **Use RootMind in Claude on the web and on your
phone** (under Remote) shows these values with Copy buttons.

Keep in mind:

- Anyone with the token can read every indexed document through the tunnel.
  Keep it secret; stopping the tunnel (or turning remote access off) closes
  the public access.
- What a cloud assistant reads through RootMind (search results and document
  text) goes to that assistant's provider.
- RootMind refuses requests that a web page sends from a browser (requests
  with a non-local `Origin` header); server-to-server requests from the
  assistant are not affected.
