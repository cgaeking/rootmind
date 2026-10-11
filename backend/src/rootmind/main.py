"""Command-line entry point: ``rootmind [api|index|rebuild-trigrams]``."""

import argparse
import logging
import sys

from .config import AppConfig, get_config, init_config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def setup_logging(debug: bool = False):
    """Configure logging."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run_api(host: str, port: int):
    """Run the REST API server."""
    import uvicorn

    from .api.app import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger(__name__).warning(
            "Binding the REST API to %s. The API has no authentication. It refuses "
            "connections from other machines, but do not expose it on a network; use the "
            "MCP HTTP server (bearer token) for remote access.",
            host,
        )
    app = create_app()
    uvicorn.run(app, host=host, port=port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rootmind",
        description="RootMind: local document search with an agentic LLM tool loop",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default="config.yaml",
        help="Path to the configuration file (relative paths resolve against RLM_HOME or cwd)",
    )
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")

    subparsers = parser.add_subparsers(dest="command", help="Command to run (default: api)")

    api_parser = subparsers.add_parser("api", help="Run the REST API server")
    api_parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind to")
    api_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind to")

    index_parser = subparsers.add_parser("index", help="Index the configured folders")
    index_parser.add_argument("--full", action="store_true", help="Full re-index")
    index_parser.add_argument("--connector", help="Index a specific connector only")

    subparsers.add_parser("rebuild-trigrams", help="Rebuild the trigram index for fuzzy search")
    return parser


def main(argv: list[str] | None = None):
    """Main entry point."""
    args = build_parser().parse_args(argv)

    setup_logging(args.debug)
    init_config(args.config)
    config = get_config()

    if args.command == "api":
        run_api(args.host, args.port)
    elif args.command == "index":
        run_index(config, args.full, args.connector)
    elif args.command == "rebuild-trigrams":
        run_rebuild_trigrams(config)
    else:
        # Default: run the API server with the host/port from the config.
        print(f"Starting RootMind API on http://{config.api.host}:{config.api.port}")
        run_api(config.api.host, config.api.port)


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


def run_index(config: AppConfig, full: bool, connector_name: str | None):
    """Run an indexing pass."""
    from .api.app import create_connectors
    from .database import DocumentRepository
    from .indexer import Indexer, SyncManager

    db = DocumentRepository(config.database.path)
    connectors = create_connectors(config)

    if not connectors:
        print("No connectors configured!")
        return

    indexer = Indexer(db, connectors, config)
    sync_manager = SyncManager(db, connectors, config, indexer)

    print(f"Starting {'full' if full else 'incremental'} index...")

    if full:
        results = sync_manager.full_sync(connector_name)
    else:
        results = sync_manager.incremental_sync(connector_name)

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
