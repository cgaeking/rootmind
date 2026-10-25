"""The agent's tool layer: one registry shared by the LLM loop and both MCP transports.

Every tool is read-only. This module owns three things:

- **Schemas** (``TOOLS``, Anthropic format). The OpenAI and MCP formats are
  derived from it, so all three surfaces always expose identical tools.
- **Validation and clamping** of model-supplied arguments. Models send strings
  for integers, negative offsets, huge limits or malformed JSON; none of that
  may reach the repository unchecked, and every problem comes back as a tool
  error the model can read and correct, never as an exception.
- **Formatting** of results as plain text. Document text is wrapped in
  ``<document>`` / ``<snippet>`` tags so the model can tell file content
  (data) apart from instructions. Text inside cannot open or close those
  tags, in any letter case, and file names cannot add lines or tags to the
  result headers.

Error results always start with ``Error:``. The Anthropic loop also sets
``is_error``; Chat Completions has no such flag, so the prefix is what tells
an OpenAI or xAI model that a call failed.

Entry point: :func:`execute`.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .database.repository import DocumentRepository

logger = logging.getLogger(__name__)

# Guardrails
MAX_READ_CHARS = 50_000
SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX = 10, 1, 50
LIST_LIMIT_DEFAULT, LIST_LIMIT_MIN, LIST_LIMIT_MAX = 50, 1, 200
SIMILARITY_DEFAULT, SIMILARITY_MIN, SIMILARITY_MAX = 0.3, 0.1, 1.0
# How far before / after a search hit the suggested read window reaches.
# Table headers (the usual missing context for a bare number) sit before the hit.
HINT_LOOKBEHIND, HINT_LOOKAHEAD = 3_000, 1_000

_FILE_TYPE_DESCRIPTION = "Optional: filter by file type, e.g. pdf, docx, xlsx, pptx, md, txt, csv"

# Server instructions for MCP clients (sent with the initialize result).
MAX_FOLDER_LABELS = 10
_LABEL_MAX_CHARS = 60
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def folder_labels(connectors: Any) -> list[str]:
    """Short names of the configured folders (the last path part), for the MCP instructions.

    Only the folder names are used, not the full paths, so the user name in a
    path such as ``C:/Users/<name>/Documents`` is not sent to the MCP client.
    """
    labels: list[str] = []
    for connector in connectors or []:
        path = str(getattr(connector, "path", "") or "").replace("\\", "/").rstrip("/")
        label = path.rsplit("/", 1)[-1] if path else ""
        label = _CONTROL_CHARS.sub(" ", label or str(getattr(connector, "name", "") or ""))
        label = label.strip()[:_LABEL_MAX_CHARS]
        if label and label not in labels:
            labels.append(label)
        if len(labels) == MAX_FOLDER_LABELS:
            break
    return labels


def server_instructions(labels: list[str] | None = None) -> str:
    """Tell MCP clients what RootMind is, so they pick it for the user's local files.

    Without this, a client with several document tools (for example a cloud
    drive connector) cannot tell that RootMind holds the files on this computer.
    """
    where = f" ({', '.join(labels)})" if labels else ""
    return (
        "RootMind gives read-only access to the user's own files on this computer: "
        f"the local folders indexed by RootMind{where}. When the user asks about their "
        "documents or files on this computer or PC, or about invoices, contracts, letters, "
        "offers, reports or similar documents without naming another source, search "
        "RootMind first with search_documents, then read the relevant hits with "
        "read_document. Cite the document name and the page, slide or sheet."
    )


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_documents",
        "description": (
            "Search the user's own files on this computer (the local folders indexed by "
            "RootMind). Fast full-text search (SQLite FTS5) over all indexed documents. Matches exact "
            "words and word prefixes. USE THIS FIRST: it is fast and cheap. Returns matching "
            "documents with text snippets, the location (page/slide/sheet) when known, and a "
            "suggested read_document call around each hit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search term(s). Matches exact words and prefixes "
                        "(e.g. 'invoice' also finds 'invoices')."
                    ),
                },
                "file_type": {"type": "string", "description": _FILE_TYPE_DESCRIPTION},
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Maximum number of results ({SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX}, "
                        f"default {SEARCH_LIMIT_DEFAULT})."
                    ),
                    "default": SEARCH_LIMIT_DEFAULT,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_fuzzy",
        "description": (
            "Trigram-based fuzzy search that tolerates typos. USE THIS ONLY when "
            "search_documents finds nothing or the search term may be misspelled "
            "(e.g. 'lantren' finds 'lantern')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term, typos allowed. Finds similar words.",
                },
                "file_type": {"type": "string", "description": _FILE_TYPE_DESCRIPTION},
                "min_similarity": {
                    "type": "number",
                    "description": (
                        f"Minimum similarity {SIMILARITY_MIN}-{SIMILARITY_MAX} "
                        f"(default {SIMILARITY_DEFAULT}). Higher means fewer but closer matches."
                    ),
                    "default": SIMILARITY_DEFAULT,
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Maximum number of results ({SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX}, "
                        f"default {SEARCH_LIMIT_DEFAULT})."
                    ),
                    "default": SEARCH_LIMIT_DEFAULT,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_documents",
        "description": (
            "List indexed documents with metadata (no content). Use it for an overview of "
            "the available files or to find a file by name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_type": {"type": "string", "description": _FILE_TYPE_DESCRIPTION},
                "search_filename": {
                    "type": "string",
                    "description": "Optional: substring to look for in file names.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Maximum number of documents ({LIST_LIMIT_MIN}-{LIST_LIMIT_MAX}, "
                        f"default {LIST_LIMIT_DEFAULT})."
                    ),
                    "default": LIST_LIMIT_DEFAULT,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_document",
        "description": (
            "Read the text of a document, or a character window of it. At most "
            f"{MAX_READ_CHARS:,} characters are returned per call; for large documents read "
            "the window around a known hit instead of paging through everything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "Document ID (from search_documents or list_documents).",
                },
                "start_char": {
                    "type": "integer",
                    "description": "Optional: start position (character offset, default 0).",
                },
                "end_char": {
                    "type": "integer",
                    "description": (
                        "Optional: end position (exclusive). Capped at start_char + "
                        f"{MAX_READ_CHARS:,}."
                    ),
                },
            },
            "required": ["doc_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_statistics",
        "description": (
            "Show statistics about the document collection (number of documents, file "
            "types, total size)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in TOOLS)


def openai_tools() -> list[dict[str, Any]]:
    """The registry in OpenAI function-calling format (also used for xAI)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


def mcp_tools() -> list[dict[str, Any]]:
    """The registry in MCP ``tools/list`` format."""
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
        for t in TOOLS
    ]


@dataclass(frozen=True)
class ToolResult:
    """Text handed back to the model; ``is_error`` marks invalid calls and failures."""

    text: str
    is_error: bool = False


@dataclass
class ToolContext:
    """Per-query state. One instance per question, never shared between requests.

    Attributes:
        allowed_connectors: Connector names whose documents the tools may
            return. None disables the filter. Callers pass the names from the
            current config, so documents of a removed folder are never surfaced
            even before the next sync prunes them.
        referenced: Documents surfaced by search/list as ``{"doc_id", "page",
            "position"}`` (fallback sources; the hit position and page let a
            later read of the same document cite the right location).
        tool_calls: Every call as ``{"tool", "input"}``, in order.
        read_windows: ``(doc_id, start_char as given, end)`` of every successful
            read, where ``end`` is the exclusive end of the returned window.
    """

    allowed_connectors: frozenset[str] | None = None
    referenced: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    read_windows: list[tuple[str, int | None, int]] = field(default_factory=list)


class ToolInputError(ValueError):
    """Raised for invalid tool arguments; reported to the model as a tool error."""


# --- argument coercion -----------------------------------------------------------


def _int_arg(args: dict[str, Any], name: str, default: int | None = None) -> int | None:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ToolInputError(f"'{name}' must be an integer, got {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ToolInputError(f"'{name}' must be an integer, got {value!r}")
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise ToolInputError(f"'{name}' must be an integer, got {value!r}") from None
    raise ToolInputError(f"'{name}' must be an integer, got {type(value).__name__}")


def _float_arg(args: dict[str, Any], name: str, default: float) -> float:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ToolInputError(f"'{name}' must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"'{name}' must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ToolInputError(f"'{name}' must be a finite number, got {value!r}")
    return number


def _str_arg(args: dict[str, Any], name: str, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ToolInputError(f"missing required argument '{name}'")
        return None
    if not isinstance(value, str):
        raise ToolInputError(f"'{name}' must be a string, got {type(value).__name__}")
    return value.strip()


def _clamp(value: int | float, low: int | float, high: int | float):
    return max(low, min(high, value))


# --- formatting ------------------------------------------------------------------


# An opening or closing <document> / <snippet> tag in any letter case, also
# with whitespace after "<" or "/" (models do not apply XML's strict rules).
_WRAPPER_TAG_RE = re.compile(r"<(?=\s*/?\s*(?:document|snippet)\b)", re.IGNORECASE)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _attr(value: Any) -> str:
    """A value for a tag attribute: one line, HTML-escaped."""
    return html.escape(_CONTROL_CHARS_RE.sub(" ", str(value)), quote=True)


def _neutralize(text: str) -> str:
    """Escape every wrapper-like tag in file content, so it cannot open or close a wrapper."""
    return _WRAPPER_TAG_RE.sub("&lt;", text)


def _safe_name(name: str | None) -> str:
    """A file name for a result header: one line, no tags.

    File names come from the indexed folders. On Linux and macOS they may hold
    newlines or ``<``, which could otherwise add lines or tags outside the
    data framing.
    """
    one_line = _CONTROL_CHARS_RE.sub(" ", name or "")
    return one_line.replace("<", "&lt;").replace(">", "&gt;")


def _location_line(page: str | None) -> str:
    return f"Location: {page}\n" if page else ""


def _position_hint(position: int | None, doc_id: str) -> str:
    """Suggest a read_document window around a search hit.

    Snippets are short and often show raw numbers or table rows without their
    header (a column label several lines above the match). Pointing directly at
    the surrounding text lets the model verify what a number means instead of
    guessing an offset in a huge document or giving up.
    """
    if position is None:
        return ""
    start = max(0, position - HINT_LOOKBEHIND)
    end = position + HINT_LOOKAHEAD
    return (
        f"Position: ~char {position:,}. For full context (e.g. table headers cut off in "
        f'the snippet) call read_document(doc_id="{doc_id}", start_char={start}, '
        f"end_char={end})\n"
    )


def _format_hits(results: list[dict[str, Any]], header: str, fuzzy: bool) -> str:
    out = [header, ""]
    for i, r in enumerate(results, 1):
        title = f"--- Result {i}" + (f" (similarity: {r['similarity']})" if fuzzy else "") + " ---"
        snippet = _neutralize(r.get("snippet") or "")
        out.append(
            f"{title}\n"
            f"Document: {_safe_name(r['file_name'])}\n"
            f"ID: {r['doc_id']}\n"
            f"Type: {r['file_type']}\n"
            f"Size: {r['content_length'] or 0:,} chars\n"
            f"{_location_line(r.get('page'))}"
            f"<snippet>{snippet}</snippet>\n"
            f"{_position_hint(r.get('position'), r['doc_id'])}"
            "---\n"
        )
    return "\n".join(out)


# --- tool implementations --------------------------------------------------------


def _reference(hit: dict[str, Any]) -> dict[str, Any]:
    return {"doc_id": hit["doc_id"], "page": hit.get("page"), "position": hit.get("position")}


def _search_documents(repo: DocumentRepository, args: dict, ctx: ToolContext) -> ToolResult:
    query = _str_arg(args, "query", required=True)
    file_type = _str_arg(args, "file_type")
    limit = _clamp(
        _int_arg(args, "limit", SEARCH_LIMIT_DEFAULT), SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX
    )

    results = repo.search_fulltext(
        query=query,
        limit=limit,
        file_type=file_type,
        connector_names=ctx.allowed_connectors,
    )
    ctx.referenced.extend(_reference(r) for r in results)

    if not results:
        return ToolResult(f"No results for '{query}'.")
    return ToolResult(
        _format_hits(results, f"Found {len(results)} result(s) for '{query}'", fuzzy=False)
    )


def _search_fuzzy(repo: DocumentRepository, args: dict, ctx: ToolContext) -> ToolResult:
    query = _str_arg(args, "query", required=True)
    file_type = _str_arg(args, "file_type")
    min_similarity = _clamp(
        _float_arg(args, "min_similarity", SIMILARITY_DEFAULT), SIMILARITY_MIN, SIMILARITY_MAX
    )
    limit = _clamp(
        _int_arg(args, "limit", SEARCH_LIMIT_DEFAULT), SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX
    )

    results = repo.search_fuzzy(
        query=query,
        limit=limit,
        file_type=file_type,
        min_similarity=min_similarity,
        connector_names=ctx.allowed_connectors,
    )
    ctx.referenced.extend(_reference(r) for r in results)

    if not results:
        return ToolResult(
            f"No fuzzy results for '{query}' (similarity >= {min_similarity * 100:.0f}%)."
        )
    return ToolResult(
        _format_hits(results, f"Fuzzy search: {len(results)} result(s) for '{query}'", fuzzy=True)
    )


def _list_documents(repo: DocumentRepository, args: dict, ctx: ToolContext) -> ToolResult:
    file_type = _str_arg(args, "file_type")
    search_filename = _str_arg(args, "search_filename")
    limit = _clamp(_int_arg(args, "limit", LIST_LIMIT_DEFAULT), LIST_LIMIT_MIN, LIST_LIMIT_MAX)

    docs = repo.list_documents(
        file_type=file_type,
        search_filename=search_filename,
        limit=limit,
        connector_names=ctx.allowed_connectors,
    )
    # Only count as sources when the list was filtered (a targeted lookup),
    # not a full unfiltered dump of the whole index.
    if file_type or search_filename:
        ctx.referenced.extend({"doc_id": d["doc_id"], "page": None, "position": None} for d in docs)

    if not docs:
        return ToolResult("No documents found.")

    lines = [f"Documents: {len(docs)} found", ""]
    for doc in docs:
        size_kb = (doc["size_bytes"] or 0) / 1024
        content_k = (doc["content_length"] or 0) / 1000
        lines.append(
            f"- {_safe_name(doc['file_name'])} ({doc['file_type']}, {size_kb:.1f} KB, "
            f"{content_k:.0f}k chars) [ID: {doc['doc_id']}]"
        )
    return ToolResult("\n".join(lines) + "\n")


def _read_document(repo: DocumentRepository, args: dict, ctx: ToolContext) -> ToolResult:
    doc_id = _str_arg(args, "doc_id", required=True)
    start_given = _int_arg(args, "start_char")
    end_given = _int_arg(args, "end_char")

    if start_given is not None and start_given < 0:
        raise ToolInputError("'start_char' must be >= 0")
    if end_given is not None and end_given < 0:
        raise ToolInputError("'end_char' must be >= 0")
    start = start_given or 0
    if end_given is not None and end_given <= start:
        raise ToolInputError("'end_char' must be greater than 'start_char'")

    meta = repo.get_document_meta(doc_id)
    if (
        not meta
        or meta["status"] != "indexed"
        or (
            ctx.allowed_connectors is not None
            and meta["connector_name"] not in ctx.allowed_connectors
        )
    ):
        return ToolResult(f"Error: document not found: {doc_id}", is_error=True)

    total = meta["content_length"] or 0
    if total and start >= total:
        raise ToolInputError(
            f"'start_char' {start:,} is beyond the end of the document ({total:,} chars)"
        )

    # The window is capped in every case, including when only start_char is given.
    end = min(start + MAX_READ_CHARS, total)
    if end_given is not None:
        end = min(end, end_given)

    result = repo.get_document_content(doc_id, start, end)
    if not result:
        return ToolResult(f"Error: document not found: {doc_id}", is_error=True)
    ctx.read_windows.append((doc_id, start_given, end))

    location = repo.resolve_page(doc_id, start) if start > 0 else None
    attrs = f'id="{_attr(doc_id)}" name="{_attr(result["file_name"])}"'
    if location:
        attrs += f' location="{_attr(location)}"'

    header = (
        f"Document: {_safe_name(result['file_name'])}\n"
        f"Type: {result['file_type']}\n"
        f"Total length: {total:,} chars\n"
        f"Showing chars {start:,}-{end:,}\n"
    )
    footer = ""
    if end < total:
        footer = (
            f"\n[Document continues. Call read_document with start_char={end} for the next "
            "part, or read a window around a search hit.]"
        )
    body = _neutralize(result["content"])
    return ToolResult(f"{header}\n<document {attrs}>\n{body}\n</document>{footer}")


def _get_statistics(repo: DocumentRepository, args: dict, ctx: ToolContext) -> ToolResult:
    # Only the folders this request may see (a scoped MCP token sees no other counts).
    stats = repo.get_statistics(connector_names=ctx.allowed_connectors)
    lines = [
        "Document collection statistics:",
        "",
        f"Documents total: {stats['total_documents']:,}",
        f"Indexed: {stats['indexed_documents']:,}",
        f"With errors: {stats['error_documents']:,}",
        "",
        f"Total file size: {stats['total_size_bytes'] / 1024 / 1024:.1f} MB",
        f"Total text: {stats['total_content_chars']:,} chars",
        "",
        "By file type:",
    ]
    for ft, count in sorted(stats["file_types"].items(), key=lambda x: -x[1]):
        lines.append(f"  - {ft}: {count:,}")
    return ToolResult("\n".join(lines) + "\n")


_EXECUTORS = {
    "search_documents": _search_documents,
    "search_fuzzy": _search_fuzzy,
    "list_documents": _list_documents,
    "read_document": _read_document,
    "get_statistics": _get_statistics,
}
if set(_EXECUTORS) != set(TOOL_NAMES):  # pragma: no cover - guards edits to the registry
    raise RuntimeError("every tool schema needs exactly one executor")


def parse_arguments(raw_args: Any) -> dict[str, Any]:
    """Turn tool arguments (dict, JSON string or None) into a dict, or raise ToolInputError."""
    if raw_args is None or raw_args == "":
        return {}
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError as e:
            raise ToolInputError(f"arguments are not valid JSON ({e.msg})") from None
        if not isinstance(parsed, dict):
            raise ToolInputError("arguments must be a JSON object")
        return parsed
    raise ToolInputError(f"arguments must be an object, got {type(raw_args).__name__}")


def execute(
    name: str,
    raw_args: Any,
    repo: DocumentRepository,
    ctx: ToolContext | None = None,
) -> ToolResult:
    """Validate and run one tool call. Never raises; failures become error results.

    Args:
        name: Tool name as sent by the model.
        raw_args: Arguments as a dict or a JSON string (OpenAI-style).
        repo: Document repository to run against.
        ctx: Per-query context; records the call, reads and surfaced documents.
    """
    ctx = ctx if ctx is not None else ToolContext()
    try:
        args = parse_arguments(raw_args)
    except ToolInputError as e:
        # Recorded as sent; anything but a string is serialized, so the call log
        # stays "an object or a string" for the API's typed response.
        shown = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, default=str)
        ctx.tool_calls.append({"tool": name, "input": shown})
        return ToolResult(f"Error: invalid arguments for '{name}': {e}", is_error=True)

    ctx.tool_calls.append({"tool": name, "input": args})

    executor = _EXECUTORS.get(name)
    if executor is None:
        return ToolResult(
            f"Error: unknown tool '{name}'. Available tools: {', '.join(TOOL_NAMES)}.",
            is_error=True,
        )

    try:
        result = executor(repo, args, ctx)
    except ToolInputError as e:
        return ToolResult(f"Error: invalid arguments for '{name}': {e}", is_error=True)
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return ToolResult(f"Error: tool '{name}' failed: {e}", is_error=True)
    if result.is_error and not result.text.startswith("Error:"):
        result = ToolResult(f"Error: {result.text}", is_error=True)
    return result
