"""RootMind: local document search with an agentic LLM tool loop, exposed via REST and MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rootmind-backend")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0+unknown"
