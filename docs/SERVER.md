# Run RootMind on a server

RootMind can run on a Linux server without a desktop: it indexes folders the
server already holds (project folders, mounted Samba/NFS shares), and people
connect their MCP clients (Claude Code, Claude Desktop, n8n, other agents) to
the server's MCP endpoint, each with a token of their own that sees only some
folders. Everything is set up from the command line.

Read [the threat model for server use](../SECURITY.md#server-use) first: one
index serves every token, and the **folder scope of each token is the only
access control**. RootMind does not check the permissions of a share per
user.

## 1. Choose the account

RootMind runs as systemd **user** services of one account, the same way it
does on a desktop. On a server, use a dedicated account that owns nothing
else and turn on lingering, so the services run without anybody logged in:

```sh
sudo useradd --create-home --shell /bin/bash rootmind
sudo loginctl enable-linger rootmind
```

Work as that account in a session with its own systemd user manager. Plain
`sudo -u` or `su` has none, and `service install` stops with exit code 11:

```sh
sudo machinectl shell rootmind@      # or: ssh rootmind@server
```

(Without `machinectl`, `sudo -iu rootmind` followed by
`export XDG_RUNTIME_DIR=/run/user/$(id -u)` works as well once lingering is on.)

**Why not a system service?** RootMind's service is built around one account:
its data folder (`~/.local/share/rootmind`, mode 0700) holds the index, the
config with the LLM key and the tokens, and the commands find the service and
its REST token through that account's unit files and permissions. A dedicated
account with lingering gives the same separation as a system service with a
`rootmind` user, without running anything as root and without a second set of
install and update code to keep secure. A `service install --system` with
`/etc/rootmind` and `/var/lib/rootmind` may follow if this proves too
limiting.

The account must be able to **read** the folders you want to index, and
nothing more: add it to the group that owns a share, or mount the share with
`uid=`/`gid=` or `file_mode`/`dir_mode` options that let it read. The index
can only contain what this account can read; the tokens then decide who sees
which part of it.

## 2. Install the backend

Either the backend alone (no GUI, no WebKitGTK):

```sh
base=https://github.com/cgaeking/rootmind/releases/latest/download
curl -L -O "$base/rootmind-backend-linux-x64.tar.gz" -O "$base/SHA256SUMS.txt"
sha256sum --check --ignore-missing SHA256SUMS.txt
mkdir -p ~/opt && tar -xzf rootmind-backend-linux-x64.tar.gz -C ~/opt
alias rootmind=~/opt/rootmind-backend/rootmind-backend    # or a symlink in ~/bin
rootmind service install
```

or the .deb/.rpm (`sudo apt install ./RootMind-amd64.deb`), then, as the
`rootmind` account, `/usr/lib/RootMind/backend/rootmind-backend service install`.

`service install` copies the program into `~/.local/share/rootmind/runtime/`,
writes the two user units and starts them: `rootmind-backend.service` (REST
API on 127.0.0.1 and indexing) and `rootmind-mcp-http.service` (MCP over HTTP
on 127.0.0.1:3000). `rootmind status` shows both.

## 3. Folders and the LLM key

```sh
rootmind folders add /srv/shares/projects --name projects --rescan-minutes 10
rootmind folders add /srv/shares/marketing --name marketing --rescan-minutes 10
rootmind config llm --provider anthropic --model claude-sonnet-5-5 --key-file ~/anthropic.key
shred -u ~/anthropic.key     # the key now lives in config.yaml (mode 0600)
rootmind index --wait
rootmind search "framework agreement"
rootmind ask "What is the notice period in the framework agreement?"
```

**Network shares.** A change made on another machine raises no event on the
server, so a mounted share is only seen to change when it is scanned.
`--rescan-minutes N` (or `rescan_minutes: N` under the folder in
`config.yaml`) re-scans that folder every N minutes, in addition to the daily
full schedule (`indexer.sync_interval_hours`). An incremental scan reads every
file's metadata and hashes the files whose size or time changed, so choose N
by the size of the share: minutes for a few thousand files, longer for very
large shares. One sync runs at a time: a re-scan is skipped while another sync
runs, and the scheduled sync of every folder, when it finds a re-scan running,
retries every minute and holds the re-scans back until it has started.

`--key-file -` reads the key from stdin instead (without echo in a terminal).
For Ollama on another machine: `rootmind config llm --provider ollama --model
qwen3:8b --base-url http://gpu-box:11434` (the questions and excerpts then
travel unencrypted to that machine).

## 4. One token per person or client

```sh
rootmind mcp tokens create alice --folders projects,marketing --out alice.token
rootmind mcp tokens create n8n --folders projects --out n8n.token
rootmind mcp tokens list
rootmind mcp tokens revoke alice
```

- The token is written to the `--out` file (created with mode 0600, on
  Windows with an access list for your account alone; it must not exist yet) and stored nowhere else: `config.yaml` keeps only its
  SHA-256. Hand the file over through a channel you trust, then delete it.
- `--folders` limits what the token's searches, listings, reads and
  statistics can see. **Without `--folders` a token sees every folder**; the
  command warns about that. A folder added later is not visible to scoped
  tokens until you create a new token for it. A removed folder also leaves
  the scopes (a token whose folders are all removed sees nothing), so a
  folder added later under the same name is not visible to the old tokens.
- `revoke` takes effect with the token's next request; the other tokens keep
  working. To change a token's folders, create a new one and revoke the old.
- The `default` token in `~/.local/share/rootmind/data/api_token.txt` sees
  every folder. It is the service's own token (used by `service install`, the
  app and the Claude Desktop bundle) and is **not accepted on the network
  listener** unless you set `allow_default_token: true`. It cannot be revoked
  with `mcp tokens revoke`; to replace it, delete the file and run
  `rootmind service install` again.

## 5. Serve MCP to the network

The MCP service keeps listening on 127.0.0.1:3000 as before. A second
listener for other machines is configured in `~/.local/share/rootmind/config.yaml`
and needs a restart of the MCP service
(`systemctl --user restart rootmind-mcp-http`). Choose one of two ways.

### A. TLS in RootMind

```yaml
mcp_http:
  allowed_hosts: [rootmind.example.lan]     # the names clients use, without port
  network:
    bind: 0.0.0.0
    port: 3443
    tls_cert: /home/rootmind/tls/fullchain.pem
    tls_key: /home/rootmind/tls/privkey.pem
```

The certificate must be valid for the names in `allowed_hosts` and trusted by
the clients (your internal CA, or a public certificate). Keep the key file
readable only by the `rootmind` account (`chmod 600`). Open the port in the
firewall, ideally only for your network (`sudo ufw allow from 10.0.0.0/8 to
any port 3443 proto tcp`).

### B. A reverse proxy (Caddy, nginx) in front

Let the proxy terminate TLS and forward to a loopback listener:

```yaml
mcp_http:
  allowed_hosts: [rootmind.example.com]
  network:
    bind: 127.0.0.1
    port: 3443
```

```caddyfile
rootmind.example.com {
    reverse_proxy 127.0.0.1:3443 {
        flush_interval -1        # the legacy SSE transport streams
    }
}
```

```nginx
location / {
    proxy_pass http://127.0.0.1:3443;
    proxy_set_header Host $host;                       # checked against allowed_hosts
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;                               # SSE
    proxy_read_timeout 1h;
}
```

On a loopback address the listener trusts `X-Forwarded-Proto` and
`X-Forwarded-For` from 127.0.0.1 only; nothing in RootMind decides anything
by the client's address. Do **not** point the proxy at port 3000: that
listener accepts the default token and checks no `Host`.

Both ways apply the same rules to every request on the network listener: the
`Host` header must be in `allowed_hosts` (400 otherwise), only named tokens
are accepted (unless `allow_default_token`), and each token sees only its
folders. Browser `Origin`s other than loopback are refused unless listed in
`mcp_http.allowed_origins`.

**Plain HTTP** on a non-loopback address needs `insecure_http: true`: every
request then carries its token unencrypted. Use it only on a network you
trust completely, or when a TLS proxy on another machine forwards to this one.
The service refuses to start the MCP service when `tls_cert`/`tls_key` and
`insecure_http` are both missing, when `allowed_hosts` is empty, or when the
certificate cannot be loaded, and says why in
`~/.local/share/rootmind/logs/mcp-http/` and in
`journalctl --user -u rootmind-mcp-http`. A started network listener is
logged as a warning, and `rootmind status` shows it.

## 6. Connect clients

Each user gets the endpoint and their token:

- **Claude Code:**
  `claude mcp add --transport http rootmind https://rootmind.example.lan:3443/mcp --header "Authorization: Bearer $(cat alice.token)"`
- **n8n** (MCP Client Tool): endpoint `https://rootmind.example.lan:3443/mcp`,
  transport HTTP Streamable, authentication *Bearer*, the token as the value.
- **Claude Desktop** and other clients that start a local process: a bridge
  such as `npx mcp-remote https://rootmind.example.lan:3443/mcp --header "Authorization: Bearer ${ROOTMIND_TOKEN}"`
  with `ROOTMIND_TOKEN` set in the server entry's `env`.

`curl -H "Authorization: Bearer $(cat alice.token)" https://rootmind.example.lan:3443/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -H "Content-Type: application/json"`
checks the connection.

## 7. Operate

- `rootmind status` shows the services, the index, the folders, the MCP
  endpoints and the tokens; every command takes `--json`.
- Logs: `~/.local/share/rootmind/logs/` and `journalctl --user -u rootmind-backend`
  / `-u rootmind-mcp-http`. Tool calls are logged with the token's name.
- Update: unpack the new tar.gz (or install the new package) and run
  `rootmind service install` again; ports, tokens, folders and the index stay.
- Back up `~/.local/share/rootmind/config.yaml` (it holds the LLM key and the
  token hashes) and, if re-indexing takes long, `data/db/index.db`.

## Windows servers

The same commands work against the Windows service (`rootmind-backend.exe
folders add ...`, from the account that installed the service; elevation is
not needed). The network listener works there too, but the MCP service runs as
`NT SERVICE\RootMind-MCP-HTTP` and must be able to read the certificate and
key, and the Windows service reads only folders the installing account can
open without elevation, which excludes network shares. A reverse proxy on the
same machine (way B) avoids the certificate permissions; Linux remains the
recommended server platform.
