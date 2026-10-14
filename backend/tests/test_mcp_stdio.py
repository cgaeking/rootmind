"""MCP stdio server, driven in-process by the official MCP client; tool parity across surfaces."""

from __future__ import annotations

from mcp import Client

from rootmind import mcp_http, mcp_stdio, tools
from rootmind.rlm_engine import engine

# mode="legacy" runs the initialize handshake and JSON-RPC over in-memory streams,
# the same exchange a stdio client such as Claude Desktop performs.


async def test_list_tools_through_the_protocol(app_config, indexed_repo):
    server = mcp_stdio.create_server(indexed_repo, app_config)

    async with Client(server, mode="legacy") as client:
        listed = await client.list_tools()

    assert server.name == "rootmind"
    assert {t.name: t.input_schema for t in listed.tools} == {
        t["name"]: t["input_schema"] for t in tools.TOOLS
    }


async def test_call_tool_through_the_protocol(app_config, indexed_repo):
    server = mcp_stdio.create_server(indexed_repo, app_config)

    async with Client(server, mode="legacy") as client:
        found = await client.call_tool("search_documents", {"query": "lantern"})
        missing = await client.call_tool("read_document", {"doc_id": "no-such-doc"})
        unknown = await client.call_tool("rm_rf", {})

    assert found.is_error is False
    assert "camp_notes.md" in found.content[0].text
    assert missing.is_error is True
    assert "Error: document not found" in missing.content[0].text
    assert unknown.is_error is True


async def test_tools_only_see_configured_connectors(app_config, indexed_repo):
    app_config.connectors = []
    result = await mcp_stdio.call_tool_result(
        indexed_repo, "list_documents", {}, frozenset(c.name for c in app_config.connectors)
    )
    assert result.content[0].text == "No documents found."


def test_tool_parity():
    """The agent loop, MCP stdio and MCP HTTP expose identical tools (one registry)."""
    anthropic_format = {t["name"]: t["input_schema"] for t in engine.tools.TOOLS}
    openai_format = {
        t["function"]["name"]: t["function"]["parameters"] for t in engine.tools.openai_tools()
    }
    stdio = {t.name: t.input_schema for t in mcp_stdio.list_tools_result().tools}
    http = {t["name"]: t["inputSchema"] for t in mcp_http.tools.mcp_tools()}

    assert anthropic_format == openai_format == stdio == http
    assert len(stdio) == 5
    descriptions = {t.name: t.description for t in mcp_stdio.list_tools_result().tools}
    assert descriptions == {t["name"]: t["description"] for t in tools.TOOLS}


async def test_initialize_sends_instructions_with_folder_names(app_config, indexed_repo):
    server = mcp_stdio.create_server(indexed_repo, app_config)

    async with Client(server, mode="legacy") as client:
        instructions = client.instructions

    assert "user's own files on this computer" in instructions
    for label in tools.folder_labels(app_config.connectors):
        assert label in instructions
