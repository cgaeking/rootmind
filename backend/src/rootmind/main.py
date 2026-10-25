"""Command-line entry point of the RootMind backend.

::

    rootmind [--config C] [--debug] [--version]
      api [--host H] [--port P] [--require-token]
      index [--full] [--folder N] [--wait] [--local]
      rebuild-trigrams
      mcp ...          MCP over stdio (rootmind.mcp_stdio)
      mcp tokens ...   the MCP over HTTP tokens of the service (rootmind.client)
      mcp-http ...     MCP over HTTP with a bearer token (rootmind.mcp_http)
      service ...      background service (rootmind.service.cli)
      status | folders | search | ask | config | remote-access
                       client of the running service (rootmind.client)

Without a command it runs ``api``. The client commands talk to the running
service through its REST API (:mod:`rootmind.client`); ``index`` does so too,
unless it runs in this process (``--local``, see
:func:`rootmind.client.commands.index_runs_locally`). The packaged executable
(``rootmind-backend.exe``) runs the same entry point. The desktop app does not
start ``api``: it talks to the Windows service (``service run backend``, see
:mod:`rootmind.service.host`). ``api`` is for development and other setups.

``mcp``, ``mcp-http`` and ``service`` are handed to their own entry points
before logging or the config is set up, with the rest of the command line
unchanged. A ``--config`` given before them is passed on to ``mcp`` and
``mcp-http``.
"""

import argparse
import importlib
import json
import logging
import os
import socket
import sys
from typing import Any

from . import __version__
from .config import AppConfig, get_config, init_config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_CONFIG_FILE = "config.yaml"

# Commands handed to their own entry points (see the module docstring).
FORWARDED_COMMANDS = ("mcp", "mcp-http", "service")

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

logger = logging.getLogger(__name__)


class UsageError(Exception):
    """Invalid input from the command line or the environment (exit code 2)."""


def setup_logging(debug: bool = False, stream: Any = None):
    """Configure logging (to stdout unless ``stream`` is given)."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(stream or sys.stdout)],
    )


# --- bind address and token -------------------------------------------------------


def _parse_port(value: str, what: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise UsageError(f"{what} must be a port number from 0 to 65535, not {value!r}")
    return port


def _port_argument(value: str) -> int:
    try:
        return _parse_port(value, "--port")
    except UsageError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def resolve_bind(host: str | None, port: int | None, config: AppConfig) -> tuple[str, int]:
    """The REST API's bind address.

    Each part resolves on its own: the command-line flag, then ``RLM_HOST`` /
    ``RLM_PORT``, then ``api.host`` / ``api.port`` in ``config.yaml``, then
    127.0.0.1:8000. Port 0 lets the system pick a free port.

    Raises:
        UsageError: ``RLM_PORT`` is not a port number.
    """
    if host is None:
        host = os.environ.get("RLM_HOST", "").strip() or None
    if host is None:
        host = config.api.host or DEFAULT_HOST
    if port is None:
        env_port = os.environ.get("RLM_PORT", "").strip()
        if env_port:
            port = _parse_port(env_port, "RLM_PORT")
    if port is None:
        port = config.api.port if config.api.port is not None else DEFAULT_PORT
    return host, port


def resolve_api_token(require_token: bool) -> str | None:
    """The REST API token, or None for no authentication.

    1. ``RLM_API_TOKEN`` when it is set.
    2. With ``require_token``: ``<RLM_HOME>/data/rest_token.txt``, created if missing.
    3. Otherwise None, and a warning is logged.

    ``RLM_API_TOKEN`` is removed from the environment afterwards. Only the
    token's source and fingerprint are logged.

    Raises:
        UsageError: the variable or the file is empty, or the file cannot be
            read or created.
    """
    from .auth import REST_TOKEN_ENV, TokenError, resolve_token, rest_token_path, token_fingerprint

    try:
        if os.environ.get(REST_TOKEN_ENV) is None and not require_token:
            logger.warning(
                "REST API authentication is off. Set %s or pass --require-token to require "
                "a bearer token.",
                REST_TOKEN_ENV,
            )
            return None
        path = rest_token_path()
        try:
            token, source = resolve_token(REST_TOKEN_ENV, path, generate=True)
        except TokenError as e:
            raise UsageError(str(e)) from None
        except FileExistsError:
            # Another process created the file at the same moment: use its token.
            try:
                token, source = resolve_token(REST_TOKEN_ENV, path, generate=False)
            except (TokenError, OSError) as e:
                raise UsageError(f"Cannot read {path}: {e}") from None
        except OSError as e:
            raise UsageError(f"Cannot create {path}: {e.strerror or e}") from None
    finally:
        os.environ.pop(REST_TOKEN_ENV, None)
    logger.info(
        "REST API authentication: bearer token from %s (fingerprint sha256:%s)",
        source,
        token_fingerprint(token),
    )
    return token


# --- serving ---------------------------------------------------------------------


def bind_socket(host: str, port: int) -> socket.socket:
    """Create the API's socket, bound to ``host:port`` (port 0: any free port).

    On Windows the socket is exclusive (``SO_EXCLUSIVEADDRUSE``), so no other
    process can bind the same address while it is open.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except BaseException:
        sock.close()
        raise
    return sock


def serve(app: Any, sock: socket.socket) -> None:
    """Serve ``app`` on the bound socket until stopped."""
    import uvicorn

    host, port = sock.getsockname()[:2]
    shown = f"[{host}]" if ":" in host else host
    # The socket is bound already, so this is the real port (also for --port 0).
    logger.info("RootMind API listening on http://%s:%d", shown, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()


def run_api(
    config: AppConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    require_token: bool = False,
) -> None:
    """Run the REST API server.

    1. Resolve the token (``RLM_API_TOKEN``, or ``--require-token``) and
       remove ``RLM_API_TOKEN`` from the environment.
    2. Resolve the bind address (:func:`resolve_bind`), bind the socket,
       and serve.

    Raises:
        UsageError: invalid token or port settings.
    """
    from .api.app import create_app

    token = resolve_api_token(require_token)
    host, port = resolve_bind(host, port, config)

    if host not in _LOOPBACK_HOSTS:
        logger.warning(
            "Binding the REST API to %s. It refuses connections from other machines, but do "
            "not expose it on a network; use the MCP HTTP server (bearer token) behind TLS "
            "for remote access.",
            host,
        )
    app = create_app(api_token=token)
    try:
        sock = bind_socket(host, port)
    except OSError as e:
        logger.error("Cannot listen on %s:%d: %s", host, port, e)
        sys.exit(1)
    serve(app, sock)


# --- command line ------------------------------------------------------------------


def _prog() -> str:
    return "rootmind-backend" if getattr(sys, "frozen", False) else "rootmind"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description="RootMind: local document search with an agentic LLM tool loop",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help=(
            f"Path to the configuration file (default: {DEFAULT_CONFIG_FILE}; relative paths "
            "resolve against RLM_HOME or the working directory)"
        ),
    )
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Used when no command is given (the API with default options).
    parser.set_defaults(host=None, port=None, require_token=False)

    subparsers = parser.add_subparsers(
        dest="command", metavar="COMMAND", help="Command to run (default: api)"
    )

    api_parser = subparsers.add_parser("api", help="Run the REST API server")
    api_parser.add_argument(
        "--host",
        default=None,
        help="Host to bind to (default: RLM_HOST, then api.host in the config, then 127.0.0.1)",
    )
    api_parser.add_argument(
        "--port",
        type=_port_argument,
        default=None,
        help=(
            "Port to bind to; 0 picks a free port, which the log names (default: RLM_PORT, "
            "then api.port in the config, then 8000)"
        ),
    )
    api_parser.add_argument(
        "--require-token",
        action="store_true",
        help=(
            "Require a bearer token on every request except / and /health: RLM_API_TOKEN, "
            "or <RLM_HOME>/data/rest_token.txt (created if missing)"
        ),
    )

    from .client import commands as client_commands

    client_commands.add_index_parser(subparsers)

    subparsers.add_parser("rebuild-trigrams", help="Rebuild the trigram index for fuzzy search")
    client_commands.add_parsers(subparsers)

    for name, help_text in (
        ("mcp", "MCP server over stdio (see: mcp --help); mcp tokens list: the MCP tokens"),
        ("mcp-http", "MCP server over HTTP with a bearer token (see: mcp-http --help)"),
        (
            "service",
            "Background service (Windows services or systemd user units): "
            "install, uninstall, status",
        ),
    ):
        forwarded = subparsers.add_parser(name, help=help_text, add_help=False)
        forwarded.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def _find_forwarded(argv: list[str]) -> tuple[str, list[str], str | None] | None:
    """``(command, rest, config)`` when the first command in ``argv`` is forwarded.

    Only the global options ``--config``/``-c`` and ``--debug``/``-d`` may come
    before it; ``config`` is the ``--config`` value when one was given.
    """
    config = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--config", "-c"):
            if i + 1 >= len(argv):
                return None
            config = argv[i + 1]
            i += 2
        elif arg.startswith("--config="):
            config = arg.split("=", 1)[1]
            i += 1
        elif arg.startswith("-c") and not arg.startswith("--") and len(arg) > 2:
            config = arg[2:]
            i += 1
        elif arg in ("--debug", "-d"):
            i += 1
        else:
            break
    if i < len(argv) and argv[i] in FORWARDED_COMMANDS:
        return argv[i], argv[i + 1 :], config
    return None


def _reconfigure_output() -> None:
    """Never fail on characters the console cannot show (Windows code pages)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def _client_logging(debug: bool) -> None:
    # Client output goes to stdout (JSON with --json); log only problems, to stderr.
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _run_client(args: argparse.Namespace) -> None:
    from .client import commands as client_commands

    _reconfigure_output()
    _client_logging(getattr(args, "debug", False))
    code = client_commands.run(args)
    if code:
        sys.exit(code)


def _mcp_tokens_parser() -> argparse.ArgumentParser:
    from .client import commands as client_commands

    parser = argparse.ArgumentParser(
        prog=f"{_prog()} mcp tokens",
        description="The bearer tokens of MCP over HTTP. Tokens are never printed.",
    )
    actions = parser.add_subparsers(dest="tokens_command", metavar="{list}", required=True)
    actions.add_parser(
        "list",
        parents=[client_commands.client_options()],
        help="List the tokens (name and fingerprint)",
    )
    return parser


def _run_forwarded(command: str, rest: list[str], config: str | None) -> None:
    if command == "mcp" and rest[:1] == ["tokens"]:
        args = _mcp_tokens_parser().parse_args(rest[1:])
        args.command = "mcp-tokens"
        _run_client(args)
        return
    if command == "service":
        if config is not None:
            print("error: --config does not apply to the service commands", file=sys.stderr)
            sys.exit(2)
        try:
            cli = importlib.import_module("rootmind.service.cli")
        except ModuleNotFoundError as e:
            if e.name not in ("rootmind.service", "rootmind.service.cli"):
                raise
            print("error: the service commands are not part of this installation", file=sys.stderr)
            sys.exit(2)
        code = cli.main(rest)
        if code:
            sys.exit(code)
        return

    if config is not None:
        rest = ["--config", config, *rest]
    if command == "mcp":
        from . import mcp_stdio

        mcp_stdio.main(rest)
    else:
        from . import mcp_http

        mcp_http.main(rest)


def main(argv: list[str] | None = None):
    """Main entry point."""
    if argv is None:
        argv = sys.argv[1:]
    forwarded = _find_forwarded(argv)
    if forwarded is not None:
        _run_forwarded(*forwarded)
        return

    args = build_parser().parse_args(argv)
    if args.command in FORWARDED_COMMANDS:
        # Only reached with unusual option spellings before the command.
        _run_forwarded(args.command, args.rest, args.config)
        return

    from .client import commands as client_commands

    if args.command in client_commands.CLIENT_COMMANDS:
        _run_client(args)
        return
    if args.command == "index":
        local, note = client_commands.index_runs_locally(args)
        if not local:
            _run_client(args)
            return
        if note:
            print(f"note: {note}", file=sys.stderr)

    # With index --json, stdout carries only the JSON result.
    setup_logging(args.debug, sys.stderr if getattr(args, "json", False) else None)
    # --config defaults to config.yaml here (CONFIG_PATH does not apply), as before.
    init_config(args.config if args.config is not None else DEFAULT_CONFIG_FILE)
    config = get_config()

    try:
        if args.command == "index":
            run_index(config, args.full, args.connector, as_json=args.json)
        elif args.command == "rebuild-trigrams":
            run_rebuild_trigrams(config)
        else:
            # "api", or no command at all.
            run_api(
                config,
                host=args.host,
                port=args.port,
                require_token=args.require_token,
            )
    except UsageError as e:
        logger.error("%s", e)
        sys.exit(2)


def run_rebuild_trigrams(config: AppConfig):
    """Rebuild the trigram index for fuzzy search."""
    from .database import DocumentRepository

    db = DocumentRepository(config.database.path)

    stats = db.get_statistics()
    total_docs = stats["indexed_documents"]
    print(f"Rebuilding trigram index for {total_docs} documents...")

    def progress(current, total):
        pct = (current / total * 100) if total > 0 else 100
        bar = "=" * int(pct / 2) + "-" * (50 - int(pct / 2))
        print(f"\r[{bar}] {pct:.0f}% ({current}/{total})", end="", flush=True)

    count = db.rebuild_trigram_index(progress_callback=progress)
    print(f"\n\nDone! {count} documents indexed for fuzzy search.")


def run_index(config: AppConfig, full: bool, connector_name: str | None, as_json: bool = False):
    """Run an indexing pass in this process (``index --local``)."""
    from .api.app import create_connectors
    from .database import DocumentRepository
    from .indexer import Indexer, SyncManager

    db = DocumentRepository(config.database.path)
    connectors = create_connectors(config)

    if not connectors:
        if as_json:
            print(json.dumps({"error": {"message": "no folders configured"}}))
        else:
            print("No connectors configured!")
        return

    indexer = Indexer(db, connectors, config)
    sync_manager = SyncManager(db, connectors, config, indexer)

    if not as_json:
        print(f"Starting {'full' if full else 'incremental'} index...")

    if full:
        results = sync_manager.full_sync(connector_name)
    else:
        results = sync_manager.incremental_sync(connector_name)
    if as_json:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"\nIndexing completed in {results['duration_seconds']:.2f}s")
    for name, counts in results["results"].items():
        if isinstance(counts, dict) and "error" not in counts:
            print(
                f"  {name}: {counts['indexed']} indexed, {counts['skipped']} skipped, "
                f"{counts['errors']} errors, {counts.get('removed', 0)} removed"
            )
        else:
            print(f"  {name}: not indexed ({counts.get('error', counts)}); its index was kept")


if __name__ == "__main__":
    main()
