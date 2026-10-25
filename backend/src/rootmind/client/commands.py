"""The client commands: ``status``, ``folders``, ``index``, ``search``, ``ask``,
``config llm``, ``mcp tokens`` and ``remote-access``.

::

    rootmind status
    rootmind folders list | add PATH [--name N] | remove NAME
    rootmind index [--full] [--folder N] [--wait] [--local]
    rootmind search QUERY [--limit N] [--type T] [--snippets]
    rootmind ask QUESTION
    rootmind config llm [--provider P] [--model M] [--base-url U] [--key-file FILE|-] [--remove-key]
    rootmind mcp tokens list | create NAME [--folders A,B] --out FILE | revoke NAME
    rootmind remote-access on|off|status [--authtoken-file FILE|-] [--domain D]

Every command takes ``--json`` (machine-readable output on stdout), ``--url``
and ``--token-file`` (another REST API instead of this account's service; see
:mod:`.discovery`). Secrets (the REST and MCP tokens, LLM API keys, the ngrok
authtoken) are read from files or stdin, never from the command line, and are
never printed: not even their masked form.

``index`` without ``--local`` goes to the service; it runs in this process,
as before 2.5, with ``--local``, a global ``--config``, ``RLM_HOME`` set, or
when this account has no RootMind service (see :func:`index_runs_locally`).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import ntpath
import os
import posixpath
import re
import stat
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .discovery import (
    MAX_TOKEN_BYTES,
    URL_ENV,
    ServiceProbe,
    Target,
    default_probe,
    discover,
    read_token_file,
)
from .rest import ASK_TIMEOUT_SECONDS, ClientError, ClientExit, RestClient

# Commands handled here (``index`` and ``mcp tokens`` are dispatched by main.py).
CLIENT_COMMANDS = ("status", "folders", "search", "ask", "config", "remote-access")

LLM_PROVIDERS = ("anthropic", "openai", "xai", "ollama")
MASK_PREFIX = "…"  # rootmind.config.MASK_PREFIX (not imported: no config side effects)
MAX_KEY_BYTES = MAX_TOKEN_BYTES
POLL_SECONDS = 1.0
_FOLDER_NAME = re.compile(r"[^A-Za-z0-9._-]+")

Output = Callable[[str], None]


# --- parser ------------------------------------------------------------------------


def client_options() -> argparse.ArgumentParser:
    """The options every client command shares (a parent parser)."""
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument("--json", action="store_true", help="Print JSON")
    options.add_argument(
        "--url",
        default=None,
        help=(
            f"REST API to use instead of this account's RootMind service (env {URL_ENV}); "
            "http:// only for 127.0.0.1 or localhost"
        ),
    )
    options.add_argument(
        "--token-file",
        metavar="FILE",
        default=None,
        help="File with the REST token (env RLM_TOKEN_FILE; default: the service's token file)",
    )
    return options


def add_parsers(subparsers: Any) -> None:
    """Add the client commands to ``rootmind``'s sub-command parser."""
    common = client_options()

    subparsers.add_parser(
        "status",
        parents=[common],
        help="Service, index, folders, MCP endpoint and remote access",
        description="Show the RootMind service of this account: its state, the index, the "
        "folders, the MCP endpoint and remote access.",
    )

    folders = subparsers.add_parser(
        "folders", help="List, add or remove indexed folders", description="Indexed folders."
    )
    folder_commands = folders.add_subparsers(
        dest="folders_command", metavar="{list,add,remove}", required=True
    )
    folder_commands.add_parser("list", parents=[common], help="List the indexed folders")
    add = folder_commands.add_parser(
        "add",
        parents=[common],
        help="Add a folder",
        description="Add a folder to the index. The service must be able to read it; on "
        "Windows it reads only what the account that set it up can open.",
    )
    add.add_argument("path", help="The folder (a relative path resolves against this folder)")
    add.add_argument("--name", help="Its name in RootMind (default: the folder's name)")
    add.add_argument(
        "--rescan-minutes",
        type=_minutes,
        default=None,
        metavar="N",
        help="Re-scan it every N minutes (for network shares, where changes made on other "
        "machines raise no event)",
    )
    remove = folder_commands.add_parser(
        "remove",
        parents=[common],
        help="Remove a folder",
        description="Remove a folder; its documents leave the index with the next sync.",
    )
    remove.add_argument("name", help="The folder's name (see: folders list)")

    search = subparsers.add_parser(
        "search", parents=[common], help="Full-text search", description="Full-text search."
    )
    search.add_argument("query")
    search.add_argument("--limit", type=_limit, default=10, help="Results (1-1000, default 10)")
    search.add_argument("--type", dest="file_type", help="Only this file type, e.g. pdf")
    search.add_argument("--snippets", action="store_true", help="Show a snippet per result")

    ask = subparsers.add_parser(
        "ask",
        parents=[common],
        help="Ask a question (uses the configured LLM)",
        description="Ask a question about the indexed documents. The question and the "
        "excerpts the model reads go to the configured LLM provider.",
    )
    ask.add_argument("question", help="The question; - reads it from stdin")

    config = subparsers.add_parser("config", help="Change settings", description="Settings.")
    config_commands = config.add_subparsers(dest="config_command", metavar="{llm}", required=True)
    llm = config_commands.add_parser(
        "llm",
        parents=[common],
        help="Show or change the LLM provider, model and API key",
        description="Without options: show the LLM settings (never the key). The API key "
        "comes from a file or stdin, never from the command line.",
    )
    llm.add_argument("--provider", choices=LLM_PROVIDERS)
    llm.add_argument("--model")
    llm.add_argument("--base-url", help="Ollama only: the server's address")
    key = llm.add_mutually_exclusive_group()
    key.add_argument(
        "--key-file",
        metavar="FILE",
        help="Read the provider's API key from FILE; - reads it from stdin",
    )
    key.add_argument(
        "--remove-key", action="store_true", help="Remove the provider's saved API key"
    )

    remote = subparsers.add_parser(
        "remote-access",
        parents=[common],
        help="The ngrok tunnel to MCP over HTTP: on, off or status",
        description="Remote access gives the MCP server a public HTTPS address through ngrok.",
    )
    remote.add_argument("action", choices=("on", "off", "status"))
    remote.add_argument(
        "--authtoken-file",
        metavar="FILE",
        help="Save the ngrok authtoken from FILE first; - reads it from stdin",
    )
    remote.add_argument("--domain", help="A reserved ngrok domain; '' removes it")


def add_index_parser(subparsers: Any) -> None:
    """``index``: through the service, or in this process (``--local``)."""
    index = subparsers.add_parser(
        "index",
        parents=[client_options()],
        help="Index the folders (through the service, or --local)",
        description=(
            "Start indexing in the RootMind service. With --local (also with --config, "
            "RLM_HOME set, or when this account has no RootMind service) the folders of "
            "config.yaml are indexed in this process instead, as before."
        ),
    )
    index.add_argument("--full", action="store_true", help="Full re-index")
    index.add_argument("--folder", "--connector", dest="connector", help="Index this folder only")
    index.add_argument(
        "--wait", action="store_true", help="Wait until indexing has finished (service)"
    )
    index.add_argument(
        "--local",
        action="store_true",
        help="Index config.yaml's folders in this process (RLM_HOME or the working folder)",
    )


def _minutes(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= 7 * 24 * 60:
        raise argparse.ArgumentTypeError(f"expected minutes from 1 to 10080, not {value!r}")
    return number


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError(f"expected a number from 1 to 1000, not {value!r}")
    return number


# --- running -----------------------------------------------------------------------


class Context:
    """What a command needs: its arguments, output and a client (connected on demand)."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        out: Output,
        err: Output,
        probe: ServiceProbe | None = None,
        client_factory: Callable[[Target], Any] | None = None,
        stdin: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.args = args
        self.out = out
        self.err = err
        self.json = bool(getattr(args, "json", False))
        self.probe = probe
        self._client_factory = client_factory or (
            lambda t: RestClient(t.url, t.token, verify=t.verify)
        )
        self.stdin = stdin if stdin is not None else sys.stdin
        self.sleep = sleep
        self.target: Target | None = None
        self._client: Any = None

    def connect(self, *, need_token: bool = True) -> Any:
        if self._client is None:
            self.target = discover(
                url=getattr(self.args, "url", None),
                token_file=getattr(self.args, "token_file", None),
                probe=self.probe,
                need_token=need_token,
            )
            self._client = self._client_factory(self.target)
        return self._client

    def emit(self, data: Any, text: str | list[str]) -> None:
        if self.json:
            self.out(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            self.out(text if isinstance(text, str) else "\n".join(text))


def run(
    args: argparse.Namespace,
    *,
    out: Output = print,
    err: Output | None = None,
    probe: ServiceProbe | None = None,
    client_factory: Callable[[Target], Any] | None = None,
    stdin: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run one client command; returns the exit code (:class:`ClientExit`)."""
    if err is None:

        def err(message: str) -> None:
            print(message, file=sys.stderr)

    ctx = Context(
        args,
        out=out,
        err=err,
        probe=probe,
        client_factory=client_factory,
        stdin=stdin,
        sleep=sleep,
    )
    handlers: dict[str, Callable[[Context], int]] = {
        "status": cmd_status,
        "folders": cmd_folders,
        "index": cmd_index,
        "search": cmd_search,
        "ask": cmd_ask,
        "config": cmd_config_llm,
        "remote-access": cmd_remote_access,
        "mcp-tokens": cmd_mcp_tokens,
    }
    try:
        return int(handlers[args.command](ctx))
    except ClientError as e:
        err(f"error: {e.message}")
        if ctx.json:
            out(json.dumps({"error": {"message": e.message, "code": int(e.code)}}, indent=2))
        return int(e.code)
    except KeyboardInterrupt:
        err("interrupted")
        return 130


# --- status ------------------------------------------------------------------------


def _service_summary(info: dict[str, Any] | None) -> dict[str, Any] | None:
    if info is None:
        return None
    keys = ("installed", "ours", "state", "port", "version", "token_file", "token_readable")
    summary = {k: info.get(k) for k in keys}
    summary["warnings"] = list(info.get("warnings") or [])
    return summary


def _mcp_endpoint(info: dict[str, Any] | None) -> dict[str, Any] | None:
    mcp = (info or {}).get("mcp_http") or {}
    if not mcp.get("installed") or not isinstance(mcp.get("port"), int):
        return None
    return {
        "url": f"http://127.0.0.1:{mcp['port']}/mcp",
        "state": mcp.get("state"),
        "token_file": mcp.get("token_file"),
    }


def cmd_status(ctx: Context) -> int:
    result: dict[str, Any] = {"service": None, "api": None}
    try:
        client = ctx.connect()
    except ClientError as e:
        result["service"] = _service_summary(e.service)
        result["error"] = {"message": e.message, "code": int(e.code)}
        ctx.err(f"error: {e.message}")
        if ctx.json:
            ctx.out(json.dumps(result, indent=2, ensure_ascii=False))
        return int(e.code)

    target = ctx.target
    info = target.service if target is not None else None
    root = client.get("/")
    stats = client.get("/statistics")
    index = client.get("/index/status")
    connectors = client.get("/connectors").get("connectors", [])
    try:
        remote = client.get("/remote-access")
    except ClientError:
        remote = None
    try:
        mcp_tokens = client.get("/mcp/tokens")
    except ClientError:
        mcp_tokens = None  # a service without named tokens
    folders = _folder_rows(connectors, index)
    result.update(
        service=_service_summary(info),
        api={"url": client.url, "version": (root or {}).get("version")},
        index={
            "documents": stats.get("indexed_documents"),
            "errors": stats.get("error_documents"),
            "total": stats.get("total_documents"),
            "text_chars": stats.get("total_content_chars"),
            "running": bool(index.get("is_running")),
        },
        folders=folders,
        mcp_http=_mcp_endpoint(info),
        mcp_network=(mcp_tokens or {}).get("network"),
        mcp_tokens=[
            {"name": t.get("name"), "folders": t.get("folders")}
            for t in (mcp_tokens or {}).get("tokens") or []
        ],
        remote_access=_remote_summary(remote),
    )
    lines = []
    if info is not None:
        version = (root or {}).get("version")
        lines.append(
            f"RootMind service: {info.get('state')}" + (f" (version {version})" if version else "")
        )
    else:
        version = (root or {}).get("version")
        lines.append("RootMind API" + (f" (version {version})" if version else ""))
    lines.append(f"  REST API:  {client.url}")
    lines.append(
        f"  Index:     {stats.get('indexed_documents', 0)} documents"
        + (f", {stats['error_documents']} with errors" if stats.get("error_documents") else "")
        + (" (indexing now)" if index.get("is_running") else "")
    )
    lines.append(f"  Folders:   {len(folders)}")
    for row in folders:
        lines.append(f"    {_folder_line(row)}")
    mcp = result["mcp_http"]
    if mcp:
        lines.append(f"  MCP:       {mcp['url']} ({mcp['state']})")
    elif info is not None:
        lines.append("  MCP:       off")
    network = result["mcp_network"] or {}
    if network.get("enabled"):
        lines.append(f"  Network:   {_network_line(network)}")
        lines.append("warning: MCP over HTTP is reachable from other machines")
    if len(result["mcp_tokens"]) > 1:
        lines.append(f"  Tokens:    {', '.join(t['name'] for t in result['mcp_tokens'])}")
    remote_summary = result["remote_access"]
    if remote_summary:
        lines.append(f"  Remote:    {_remote_line(remote_summary)}")
    for warning in (info or {}).get("warnings") or []:
        lines.append(f"warning: {warning}")
    ctx.emit(result, lines)
    return ClientExit.OK


# --- folders -----------------------------------------------------------------------


def _folder_rows(
    connectors: list[dict[str, Any]],
    index: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
) -> list[dict]:
    statuses = (index or {}).get("connectors") or {}
    rescan = {
        c.get("name"): c.get("rescan_minutes") for c in (config or {}).get("connectors") or []
    }
    rows = []
    for c in connectors:
        name = c.get("name")
        status = statuses.get(name) or {}
        rows.append(
            {
                "name": name,
                "path": c.get("root_path"),
                "available": c.get("available", c.get("exists", True)) is not False,
                "error": c.get("error"),
                "documents": status.get("documents_indexed"),
                "errors": status.get("documents_error"),
                "last_sync_at": status.get("last_sync_at"),
                "rescan_minutes": rescan.get(name),
            }
        )
    return rows


def _folder_line(row: dict[str, Any]) -> str:
    line = f"{row['name']}: {row['path']}"
    if not row["available"]:
        line += f"  [unavailable: {row['error'] or 'cannot be read'}]"
    elif row.get("documents") is not None:
        line += f"  ({row['documents']} documents"
        line += f", {row['errors']} errors)" if row.get("errors") else ")"
    if row.get("rescan_minutes"):
        line += f"  re-scan every {row['rescan_minutes']} min"
    return line


def _get_config(client: Any) -> dict[str, Any]:
    config = client.get("/config")
    if not isinstance(config, dict) or not isinstance(config.get("llm"), dict):
        raise ClientError("unexpected answer from GET /config")
    return config


def _save_config(client: Any, config: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """POST /config with the LLM settings as read (API keys masked: the service keeps them)."""
    body = {"llm": config["llm"], **changes}
    result = client.post("/config", body)
    return result if isinstance(result, dict) else {}


def _absolute_path(path: str, flavor: str) -> str:
    module = ntpath if flavor == "windows" else posixpath
    expanded = os.path.expanduser(path)
    if not module.isabs(expanded):
        expanded = module.join(os.getcwd(), expanded)
    return module.normpath(expanded)


def _default_name(path: str, existing: set[str]) -> str:
    base = (
        ntpath.basename(path.rstrip("\\/"))
        if "\\" in path
        else posixpath.basename(path.rstrip("/"))
    )
    name = _FOLDER_NAME.sub("-", base).strip("-.") or "folder"
    candidate, n = name, 2
    while candidate.lower() in existing:
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def cmd_folders(ctx: Context) -> int:
    client = ctx.connect()
    command = ctx.args.folders_command
    if command == "list":
        connectors = client.get("/connectors").get("connectors", [])
        rows = _folder_rows(connectors, client.get("/index/status"), _get_config(client))
        lines = [_folder_line(r) for r in rows] or [
            "No folders. Add one with: rootmind folders add PATH"
        ]
        ctx.emit({"folders": rows}, lines)
        return ClientExit.OK

    config = _get_config(client)
    connectors = list(config.get("connectors") or [])
    names = {str(c.get("name", "")).lower() for c in connectors}
    if command == "add":
        flavor = "windows" if sys.platform == "win32" else "posix"
        path = _absolute_path(ctx.args.path, flavor)
        if ctx.args.name is not None:
            name = ctx.args.name.strip()
            if not name or _FOLDER_NAME.search(name):
                raise ClientError(
                    "a folder name may contain only letters, digits, '.', '_' and '-'",
                    ClientExit.USAGE,
                )
            if name.lower() in names:
                raise ClientError(f"a folder named {name!r} exists already", ClientExit.USAGE)
        else:
            name = _default_name(path, names)
        same = os.path.normcase if flavor == "windows" else (lambda p: p)
        for c in connectors:
            if c.get("path") and same(os.path.normpath(c["path"])) == same(path):
                raise ClientError(
                    f"{path} is indexed already as {c.get('name')!r}", ClientExit.USAGE
                )
        entry: dict[str, Any] = {"name": name, "type": "local", "path": path}
        if ctx.args.rescan_minutes:
            entry["rescan_minutes"] = ctx.args.rescan_minutes
        connectors.append(entry)
        result = _save_config(client, config, connectors=connectors)
        lines = [f"Added {name}: {path}"]
        if ctx.args.rescan_minutes:
            lines.append(f"It is re-scanned every {ctx.args.rescan_minutes} minutes.")
        if result.get("restart_required"):
            lines.append("warning: saved, but the service must be restarted to use it")
        lines.append(f"Index it now with: rootmind index --folder {name}")
        ctx.emit({"added": {"name": name, "path": path}, **result}, lines)
        return ClientExit.OK

    name = ctx.args.name
    kept = [c for c in connectors if c.get("name") != name]
    if len(kept) == len(connectors):
        raise ClientError(
            f"no folder named {name!r} (see: rootmind folders list)", ClientExit.USAGE
        )
    result = _save_config(client, config, connectors=kept)
    lines = [f"Removed {name}; its documents leave the index with the next sync."]
    if result.get("restart_required"):
        lines.append("warning: saved, but the service must be restarted to apply it")
    ctx.emit({"removed": name, **result}, lines)
    return ClientExit.OK


# --- index -------------------------------------------------------------------------


def index_runs_locally(
    args: argparse.Namespace, probe_factory: Callable[[], ServiceProbe | None] | None = None
) -> tuple[bool, str | None]:
    """Whether ``index`` runs in this process, and a note to print when that was not asked for.

    In this process: ``--local`` or a global ``--config``; otherwise through
    the REST API with ``--url``/``RLM_URL``; otherwise in this process when
    ``RLM_HOME`` is set or this account has no RootMind service.
    """
    if getattr(args, "local", False) or getattr(args, "config", None) is not None:
        return True, None
    if getattr(args, "url", None) or os.environ.get(URL_ENV, "").strip():
        return False, None
    if os.environ.get("RLM_HOME"):
        return True, None
    probe = (probe_factory or default_probe)()
    if probe is None:
        return True, None
    try:
        installed = bool(probe.status().get("installed"))
    except Exception:
        installed = False
    if installed:
        return False, None
    return True, (
        "No RootMind service is set up for this account; indexing the folders of config.yaml "
        "in this process (--local skips this check)."
    )


def _index_summary(status: dict[str, Any]) -> list[str]:
    lines = []
    for name, c in sorted((status.get("connectors") or {}).items()):
        line = f"  {name}: {c.get('documents_indexed', 0)} documents"
        if c.get("documents_error"):
            line += f", {c['documents_error']} errors"
        if c.get("error_message"):
            line += f" ({c['error_message']})"
        lines.append(line)
    return lines


def _wait_idle(ctx: Context, client: Any) -> dict[str, Any]:
    while True:
        status = client.get("/index/status")
        if not status.get("is_running"):
            return status
        ctx.sleep(POLL_SECONDS)


def cmd_index(ctx: Context) -> int:
    client = ctx.connect()
    body = {"full": bool(ctx.args.full), "connector_name": ctx.args.connector}
    try:
        started = client.post("/index/refresh", body)
    except ClientError as e:
        if e.status == 404:
            raise ClientError(
                f"no folder named {ctx.args.connector!r} (see: rootmind folders list)",
                ClientExit.USAGE,
            ) from None
        if e.status != 409 or not ctx.args.wait:
            if e.status == 409:
                raise ClientError(
                    "indexing is running already; --wait waits for it and starts again",
                    ClientExit.FAILED,
                    409,
                ) from None
            raise
        if not ctx.json:
            ctx.err("Indexing is running already; waiting for it to finish...")
        _wait_idle(ctx, client)
        started = client.post("/index/refresh", body)
    kind = "full" if ctx.args.full else "incremental"
    what = ctx.args.connector or "all folders"
    if not ctx.args.wait:
        ctx.emit(
            {"started": started, "waited": False},
            f"Started {kind} indexing of {what}. 'rootmind status' shows the progress.",
        )
        return ClientExit.OK
    if not ctx.json:
        ctx.err(f"Indexing {what} ({kind})...")
    status = _wait_idle(ctx, client)
    ctx.emit(
        {"started": started, "waited": True, "status": status},
        [f"Indexing finished: {status.get('indexed_documents', 0)} documents."]
        + _index_summary(status),
    )
    return ClientExit.OK


# --- search and ask ----------------------------------------------------------------


def cmd_search(ctx: Context) -> int:
    client = ctx.connect()
    body = {
        "query": ctx.args.query,
        "limit": ctx.args.limit,
        "file_type": ctx.args.file_type,
        "with_snippets": bool(ctx.args.snippets),
    }
    result = client.post("/search", body)
    results = result.get("results") or []
    lines = []
    for n, hit in enumerate(results, 1):
        where = f" ({hit['page']})" if hit.get("page") else ""
        lines.append(f"{n}. {hit.get('file_name')}{where}  [{hit.get('doc_id')}]")
        lines.append(f"   {hit.get('file_path')}")
        if hit.get("snippet"):
            lines.append("   " + " ".join(str(hit["snippet"]).split()))
    ctx.emit(result, lines or [f"No results for {ctx.args.query!r}."])
    return ClientExit.OK


def cmd_ask(ctx: Context) -> int:
    question = ctx.args.question
    if question == "-":
        question = ctx.stdin.read()
    question = question.strip()
    if not question:
        raise ClientError("the question is empty", ClientExit.USAGE)
    client = ctx.connect()
    result = client.request(
        "POST", "/query", body={"question": question}, timeout=ASK_TIMEOUT_SECONDS
    )
    lines = [str(result.get("answer", "")).strip()]
    sources = result.get("sources") or []
    if sources:
        lines.append("")
        lines.append("Sources:")
        for s in sources:
            page = f", {s['page']}" if s.get("page") else ""
            lines.append(f"  - {s.get('file_name')}{page} ({s.get('file_path')})")
    ctx.emit(result, lines)
    return ClientExit.OK


# --- secrets from files or stdin -----------------------------------------------------


def read_secret(source: str, what: str, ctx: Context) -> str:
    """A secret from a file or stdin (``-``); one line, at most 4 KB, never echoed.

    On POSIX a file that other accounts can read gets a warning (it should be 0600).
    """
    if source == "-":
        if getattr(ctx.stdin, "isatty", lambda: False)():
            data = getpass.getpass(f"{what}: ")
        else:
            data = ctx.stdin.read(MAX_KEY_BYTES + 1)
        if len(data) > MAX_KEY_BYTES:
            raise ClientError(f"the {what} on stdin is longer than 4 KB", ClientExit.USAGE)
        value = data.strip()
    else:
        path = Path(source).expanduser()
        value = read_token_file(path, f"{what} file")
        if os.name == "posix":
            try:
                mode = path.stat().st_mode
            except OSError:
                mode = 0
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                ctx.err(f"warning: other accounts can read {path}; chmod 600 it")
    if not value or any(c.isspace() for c in value):
        raise ClientError(f"the {what} must be one line without spaces", ClientExit.USAGE)
    if value.startswith(MASK_PREFIX):
        raise ClientError(
            f"the {what} starts with '{MASK_PREFIX}': that is a masked value", ClientExit.USAGE
        )
    return value


# --- config llm --------------------------------------------------------------------


def _llm_view(llm: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(p for p, v in (llm.get("api_keys") or {}).items() if v)
    return {
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "base_url": llm.get("base_url"),
        "keys_saved": keys,
    }


def _llm_lines(view: dict[str, Any]) -> list[str]:
    lines = [f"Provider: {view['provider']}", f"Model:    {view['model']}"]
    if view["provider"] == "ollama":
        lines.append(f"Base URL: {view['base_url']}")
    saved = view["keys_saved"]
    lines.append(f"API keys: {', '.join(saved) if saved else 'none'} (never shown)")
    return lines


def cmd_config_llm(ctx: Context) -> int:
    args = ctx.args
    key = read_secret(args.key_file, "API key", ctx) if args.key_file else None
    client = ctx.connect()
    config = _get_config(client)
    llm = dict(config["llm"])
    if not any((args.provider, args.model, args.base_url, key, args.remove_key)):
        view = _llm_view(llm)
        ctx.emit(view, _llm_lines(view))
        return ClientExit.OK

    provider = args.provider or llm.get("provider") or "anthropic"
    if key is not None and provider == "ollama":
        raise ClientError("Ollama needs no API key", ClientExit.USAGE)
    if args.base_url is not None and provider != "ollama":
        raise ClientError("--base-url applies to the ollama provider only", ClientExit.USAGE)
    warnings = []
    if args.provider and args.provider != llm.get("provider") and not args.model:
        warnings.append(
            f"the model stays {llm.get('model')!r}; set one of {args.provider} with --model"
        )
    if args.provider:
        llm["provider"] = args.provider
    if args.model:
        llm["model"] = args.model.strip()
    if args.base_url is not None:
        llm["base_url"] = args.base_url.strip()
    # The masked keys from GET /config keep the stored keys; a new key replaces one.
    keys = dict(llm.get("api_keys") or {})
    if key is not None:
        keys[provider] = key
    if args.remove_key:
        keys[provider] = ""
    llm["api_keys"] = keys
    llm.pop("api_key", None)
    result = client.post("/config", {"llm": llm})
    view = _llm_view(_get_config(client)["llm"])
    lines = _llm_lines(view)
    if key is not None:
        lines.append(f"Saved the API key for {provider}.")
    if args.remove_key:
        lines.append(f"Removed the API key for {provider}.")
    if isinstance(result, dict) and result.get("restart_required"):
        warnings.append("saved, but the service must be restarted to use it")
    lines.extend(f"warning: {w}" for w in warnings)
    ctx.emit({**view, "saved": True, "warnings": warnings}, lines)
    return ClientExit.OK


# --- remote access -----------------------------------------------------------------


def _remote_summary(remote: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(remote, dict):
        return None
    status = remote.get("status") or {}
    return {
        "enabled": bool(remote.get("enabled")),
        "domain": remote.get("domain"),
        "authtoken_saved": bool(remote.get("authtoken_saved")),
        "state": status.get("state"),
        "url": status.get("url"),
        "error": status.get("error"),
    }


def _remote_line(summary: dict[str, Any]) -> str:
    if not summary["enabled"]:
        return "off"
    line = str(summary["state"] or "unknown")
    if summary.get("url"):
        line += f" {summary['url']}"
    if summary.get("error"):
        line += f" ({summary['error']})"
    return line


def cmd_remote_access(ctx: Context) -> int:
    args = ctx.args
    authtoken = (
        read_secret(args.authtoken_file, "ngrok authtoken", ctx) if args.authtoken_file else None
    )
    client = ctx.connect()
    body: dict[str, Any] = {}
    if authtoken is not None:
        body["authtoken"] = authtoken
    if args.domain is not None:
        body["domain"] = args.domain.strip() or None
    if args.action in ("on", "off"):
        body["enabled"] = args.action == "on"
    remote = client.post("/remote-access", body) if body else client.get("/remote-access")
    summary = _remote_summary(remote) or {}
    lines = [f"Remote access: {_remote_line(summary)}"]
    if summary.get("domain"):
        lines.append(f"  Domain:    {summary['domain']}")
    lines.append(f"  Authtoken: {'saved' if summary.get('authtoken_saved') else 'not saved'}")
    if args.action == "on" and summary.get("state") in ("off", "connecting", "unavailable"):
        lines.append(
            "The MCP service opens the tunnel within a few seconds; check with: "
            "rootmind remote-access status"
        )
    ctx.emit(summary, lines)
    return ClientExit.OK


# --- mcp tokens --------------------------------------------------------------------


def cmd_mcp_tokens(ctx: Context) -> int:
    """``mcp tokens list | create | revoke``: the MCP over HTTP tokens.

    Tokens are never printed: ``list`` shows names, folders and fingerprints
    (what the MCP service logs); ``create`` writes the new token to the file
    given with ``--out`` (owner-only), or to stdout only with ``--out -``.
    """
    command = getattr(ctx.args, "tokens_command", "list")
    if command == "create":
        return _mcp_token_create(ctx)
    if command == "revoke":
        return _mcp_token_revoke(ctx)
    client = ctx.connect()
    try:
        data = client.get("/mcp/tokens")
    except ClientError as e:
        if e.status != 404 or ctx.target is None or not ctx.target.discovered:
            raise
        return _mcp_tokens_from_status(ctx)  # a service older than named tokens
    endpoint = _mcp_endpoint(ctx.target.service if ctx.target else None)
    network = data.get("network") or {}
    lines = []
    if endpoint:
        lines.append(f"MCP endpoint: {endpoint['url']} ({endpoint['state']})")
    if network.get("enabled"):
        lines.append(f"Network:      {_network_line(network)}")
    for t in data.get("tokens") or []:
        folders = _scope_text(t.get("folders"))
        fingerprint = f"sha256:{t['fingerprint']}" if t.get("fingerprint") else "(not readable)"
        lines.append(f"  {t['name']:<16} {fingerprint:<16} {folders}")
    ctx.emit({**data, "mcp_http": endpoint}, lines)
    return ClientExit.OK


def _scope_text(folders: list[str] | None) -> str:
    """A token's folders as text: None is every folder, an empty list none at all."""
    if folders is None:
        return "every folder"
    return ", ".join(folders) if folders else "no folder (its folders were removed)"


def _network_line(network: dict[str, Any]) -> str:
    bind = network.get("bind") or ""
    shown = f"[{bind}]" if ":" in bind else bind
    line = f"{'https' if network.get('tls') else 'http'}://{shown}:{network.get('port')}/mcp"
    if not network.get("tls"):
        loopback = bind.strip("[]").lower() in ("127.0.0.1", "localhost", "::1")
        line += " (for a reverse proxy on this machine)" if loopback else " (PLAIN HTTP)"
    if network.get("problems"):
        line += " - not started: " + "; ".join(network["problems"])
    return line


def _write_new_token(path: Path, token: str) -> None:
    """Create ``path`` (it must not exist) with the token, readable by this account only.

    POSIX: mode 0600, no link followed. Windows: a protected access list with
    this account alone (the mode bits mean nothing there, and the entries the
    folder passes on could let other accounts read the file). A file that was
    created but could not be written is removed again.

    Raises:
        ClientError: the file exists or cannot be created or written.
    """
    data = (token + "\n").encode("utf-8")
    if sys.platform == "win32":
        _write_new_windows_file(path, data)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise ClientError(f"{path} exists; choose a new file", ClientExit.USAGE) from None
    except OSError as e:
        raise ClientError(f"cannot create {path}: {e.strerror or e}", ClientExit.USAGE) from None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except OSError as e:
        with contextlib.suppress(OSError):
            path.unlink()
        raise ClientError(f"cannot write {path}: {e.strerror or e}", ClientExit.USAGE) from None


def _write_new_windows_file(path: Path, data: bytes) -> None:
    """Create a new file whose access list names only this account, and write ``data``."""
    import pywintypes
    import win32con
    import win32file
    import win32security

    from ..winaccess import current_user_sid

    try:
        sddl = f"D:P(A;;FA;;;{current_user_sid()})"
        attributes = win32security.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = (
            win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                sddl, win32security.SDDL_REVISION_1
            )
        )
        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_WRITE,
            0,
            attributes,
            win32con.CREATE_NEW,  # never an existing file or link
            win32con.FILE_ATTRIBUTE_NORMAL,
            None,
        )
    except (pywintypes.error, OSError) as e:
        if getattr(e, "winerror", None) in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
            raise ClientError(f"{path} exists; choose a new file", ClientExit.USAGE) from None
        reason = getattr(e, "strerror", None) or e
        raise ClientError(f"cannot create {path}: {reason}", ClientExit.USAGE) from None
    try:
        try:
            win32file.WriteFile(handle, data)
        finally:
            handle.Close()
    except pywintypes.error as e:
        with contextlib.suppress(OSError):
            path.unlink()
        raise ClientError(f"cannot write {path}: {e.strerror}", ClientExit.USAGE) from None


def _mcp_token_create(ctx: Context) -> int:
    args = ctx.args
    out = args.out
    path = None if out == "-" else Path(out).expanduser()
    if path is not None and (path.exists() or path.is_symlink()):
        raise ClientError(f"{path} exists; choose a new file", ClientExit.USAGE)
    folders = None
    if args.folders is not None:
        folders = [f.strip() for part in args.folders for f in part.split(",") if f.strip()]
    client = ctx.connect()
    body: dict[str, Any] = {"name": args.name}
    if folders is not None:
        body["folders"] = folders
    created = client.post("/mcp/tokens", body)
    token = created.pop("token", None)
    if not isinstance(token, str) or not token:
        raise ClientError("the service sent no token")
    scope = ", ".join(created.get("folders") or []) or "every folder"
    if path is None:
        if ctx.json:
            ctx.out(json.dumps({**created, "token": token}, indent=2))
        else:
            ctx.out(token)
            ctx.err(
                f"Created MCP token {created['name']} (sha256:{created.get('fingerprint')}, "
                f"{scope}). It is shown only this once."
            )
        return ClientExit.OK
    try:
        _write_new_token(path, token)
    except Exception as e:
        # The token exists in the service but could not be saved: revoke it again.
        with contextlib.suppress(ClientError):
            client.delete(f"/mcp/tokens/{urllib.parse.quote(created['name'], safe='')}")
        message = e.message if isinstance(e, ClientError) else f"cannot write {path}: {e}"
        code = e.code if isinstance(e, ClientError) else ClientExit.FAILED
        raise ClientError(f"{message}; the token was revoked again", code) from None
    lines = [
        f"Created MCP token {created['name']} (sha256:{created.get('fingerprint')}), {scope}.",
        f"The token is in {path}"
        + (" (mode 0600)" if os.name == "posix" else " (readable by this account only)")
        + "; it is stored nowhere else. Hand it over privately, then delete the file. "
        "Clients send it as: Authorization: Bearer <token>",
    ]
    if folders is None:
        lines.append(
            "warning: this token sees every folder; limit it with --folders when it is for "
            "one person or one client"
        )
    ctx.emit({**created, "file": str(path)}, lines)
    return ClientExit.OK


def _mcp_token_revoke(ctx: Context) -> int:
    client = ctx.connect()
    name = ctx.args.name
    try:
        client.delete(f"/mcp/tokens/{urllib.parse.quote(name, safe='')}")
    except ClientError as e:
        if e.status == 404:
            raise ClientError(
                f"no token named {name!r} (see: rootmind mcp tokens list)", ClientExit.USAGE
            ) from None
        raise
    ctx.emit({"revoked": name}, f"Revoked {name}; the MCP server refuses it from now on.")
    return ClientExit.OK


def _mcp_tokens_from_status(ctx: Context) -> int:
    """``mcp tokens list`` for a service without ``/mcp/tokens``: the default token only."""
    from ..auth import token_fingerprint

    info = ctx.target.service if ctx.target else None
    endpoint = _mcp_endpoint(info)
    if endpoint is None:
        ctx.emit({"tokens": [], "mcp_http": None}, "MCP over HTTP is off")
        return ClientExit.OK
    path = endpoint["token_file"]
    token = {"name": "default", "kind": "file", "file": path, "folders": None, "fingerprint": None}
    if path:
        try:
            value = read_token_file(Path(path), "MCP token file")
        except ClientError:
            pass
        else:
            token["fingerprint"] = token_fingerprint(value)
    lines = [
        f"MCP endpoint: {endpoint['url']} ({endpoint['state']})",
        f"  default          {('sha256:' + token['fingerprint']) if token['fingerprint'] else '(not readable)'}"
        "  every folder",
    ]
    ctx.emit({"tokens": [token], "mcp_http": endpoint}, lines)
    return ClientExit.OK
