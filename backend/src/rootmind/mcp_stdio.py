"""MCP server over stdio, for local MCP clients such as Claude Desktop.

Exposes the same read-only tools as the agent loop (see :mod:`rootmind.tools`),
so an external agent can search and read the index directly.

Usage::

    rootmind-mcp [--config config.yaml]

Configuration and database are resolved like the REST API: ``config.yaml``
under ``RLM_HOME`` (or the working directory). This process only serves; it
never indexes. Run the REST API (or ``rootmind index``) to keep the index
fresh. The folders whose documents the tools may return are read from the
config at startup; restart the server (the MCP client) after changing them.
Example Claude Desktop entry::

    {
      "mcpServers": {
        "rootmind": {
          "command": "uv",
          "args": ["--directory", "/path/to/rootmind/backend", "run", "rootmind-mcp"]
        }
      }
    }
"""

import argparse
import asyncio
import logging
import sys
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import __version__, tools
from .config import AppConfig, init_config
from .database.repository import DocumentRepository

logger = logging.getLogger("rootmind.mcp_stdio")

SERVER_NAME = "rootmind"


def list_tools_result() -> types.ListToolsResult:
    """The tool registry as an MCP ``tools/list`` result."""
    return types.ListToolsResult(
        tools=[
            types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
            for t in tools.mcp_tools()
        ]
    )


async def call_tool_result(
    repo: DocumentRepository,
    name: str,
    arguments: dict[str, Any] | None,
    allowed_connectors: frozenset[str] | None,
) -> types.CallToolResult:
    """Run one tool call (in a worker thread) and wrap it as an MCP result."""
    ctx = tools.ToolContext(allowed_connectors=allowed_connectors)
    result = await asyncio.to_thread(tools.execute, name, arguments or {}, repo, ctx)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result.text)],
        is_error=result.is_error,
    )


def create_server(repo: DocumentRepository, config: AppConfig) -> Server:
    """Build the MCP server for ``repo``; tools see only the configured connectors."""
    allowed = frozenset(c.name for c in config.connectors)

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return list_tools_result()

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        logger.info("Tool call: %s", params.name)
        return await call_tool_result(repo, params.name, params.arguments, allowed)

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=tools.server_instructions(tools.folder_labels(config.connectors)),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def open_repository(config: AppConfig) -> DocumentRepository:
    """Open the index database named in the config and log its size."""
    logger.info("Opening database: %s", config.database.path)
    repo = DocumentRepository(config.database.path)
    stats = repo.get_statistics()
    logger.info(
        "Database ready: %d documents, %s characters",
        stats["indexed_documents"],
        f"{stats['total_content_chars']:,}",
    )
    return repo


async def serve(config: AppConfig) -> None:
    """Run the stdio transport until the client disconnects."""
    repo = open_repository(config)
    server = create_server(repo, config)
    async with stdio_server() as (read_stream, write_stream):
        logger.info("MCP server ready on stdio")
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``rootmind-mcp``."""
    parser = argparse.ArgumentParser(prog="rootmind-mcp", description="RootMind MCP server (stdio)")
    parser.add_argument("--config", "-c", default=None, help="Path to config.yaml")
    args = parser.parse_args(argv)

    # Log to stderr: stdout carries the MCP protocol.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    config = init_config(args.config)
    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
