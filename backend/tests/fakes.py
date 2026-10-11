"""Scripted fake LLM clients for offline tests.

Each fake mimics just enough of the SDK surface the engine uses. A *script* is
a list consumed one item per call; an item is a response object, an exception
instance (raised), or a callable ``(kwargs) -> response`` for routing by input.
Every call's keyword arguments are deep-copied into ``.calls`` for assertions.
"""

from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace
from typing import Any

# --- Anthropic Messages API --------------------------------------------------------


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def tool_use(id: str, name: str, input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def anthropic_response(
    *blocks: SimpleNamespace,
    stop_reason: str | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> SimpleNamespace:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _Scripted:
    def __init__(self, script: list[Any], sleep: float = 0.0):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.sleep = sleep
        self.closed = False

    def _next(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        if self.sleep:
            time.sleep(self.sleep)
        if not self.script:
            raise AssertionError("fake LLM script exhausted")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(kwargs)
        return item

    def close(self) -> None:
        self.closed = True


class FakeAnthropic(_Scripted):
    """Stands in for ``anthropic.Anthropic``: ``.messages.create(**kw)``."""

    def __init__(self, script: list[Any], sleep: float = 0.0):
        super().__init__(script, sleep)
        self.messages = SimpleNamespace(create=self._next_kw)

    def _next_kw(self, **kwargs: Any) -> Any:
        return self._next(kwargs)


# --- OpenAI Chat Completions -------------------------------------------------------


def openai_tool_call(id: str, name: str, arguments: dict[str, Any] | str) -> SimpleNamespace:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(
        id=id, type="function", function=SimpleNamespace(name=name, arguments=args)
    )


def openai_response(
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    total_tokens: int = 15,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    if finish_reason is None:
        finish_reason = "tool_calls" if tool_calls else "stop"
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(total_tokens=total_tokens),
    )


class FakeOpenAI(_Scripted):
    """Stands in for ``openai.OpenAI``: ``.chat.completions.create(**kw)``."""

    def __init__(self, script: list[Any], sleep: float = 0.0):
        super().__init__(script, sleep)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._next_kw))

    def _next_kw(self, **kwargs: Any) -> Any:
        return self._next(kwargs)
