"""The example end to end, without a model or network: a real RootMind MCP server over stdio
on a small temporary index, its tools loaded through langchain-mcp-adapters, and a fake
chat model that searches and answers.

Run from this folder: ``uv run pytest``. Needs ``uv`` on PATH and a synced backend
environment (``uv sync`` in ``backend/``); skips otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

import rootmind_agent

TOOL_NAMES = {
    "search_documents",
    "search_fuzzy",
    "list_documents",
    "read_document",
    "get_statistics",
}

UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(
    UV is None or not (rootmind_agent.BACKEND / ".venv").is_dir(),
    reason="needs uv and a synced backend environment",
)


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    """A config.yaml with one indexed Markdown file (indexed by RootMind's own CLI)."""
    root = tmp_path_factory.mktemp("rootmind")
    docs = root / "docs"
    docs.mkdir()
    (docs / "tent_specs.md").write_text(
        "# Ridgeline 3P tent\n\nPacked weight: 2.4 kg. Warranty: 7 years.\n", encoding="utf-8"
    )
    home = root / "home"  # RootMind's data folder; an indexed folder must be outside it
    home.mkdir()
    config = home / "config.yaml"
    config.write_text(
        f"database:\n  path: {(home / 'index.db').as_posix()}\n"
        f"connectors:\n  - name: docs\n    type: local\n    path: {docs.as_posix()}\n",
        encoding="utf-8",
    )
    indexed = subprocess.run(
        [UV, "--directory", str(rootmind_agent.BACKEND), "run", "rootmind",
         "--config", str(config), "index", "--full"],
        env={**os.environ, "RLM_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=300,
    )  # fmt: skip
    output = indexed.stdout + indexed.stderr
    assert indexed.returncode == 0 and "No connectors" not in output, output
    return config


async def test_rootmind_tools_load_and_run(config):
    client = MultiServerMCPClient(rootmind_agent.server_connections(config=config))
    async with client.session(rootmind_agent.SERVER) as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}
        assert set(tools) == TOOL_NAMES
        result = await tools["search_documents"].ainvoke({"query": "warranty"})

    assert "tent_specs.md" in str(result)


async def test_agent_answers_with_rootmind_tools(config):
    model = FakeToolModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "search_documents", "args": {"query": "warranty"}, "id": "c1"}
                    ],
                ),
                AIMessage(content="The Ridgeline 3P has a 7-year warranty (tent_specs.md)."),
            ]
        )
    )
    client = MultiServerMCPClient(rootmind_agent.server_connections(config=config))
    async with client.session(rootmind_agent.SERVER) as session:
        agent = rootmind_agent.build_agent(model, await load_mcp_tools(session))
        state = await agent.ainvoke({"messages": [("user", "What is the tent's warranty?")]})

    [tool_message] = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert "tent_specs.md" in str(tool_message.content)
    assert rootmind_agent.final_text(state).startswith("The Ridgeline 3P has a 7-year warranty")
