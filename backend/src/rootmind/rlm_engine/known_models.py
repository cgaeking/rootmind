"""Models RootMind knows already: what each one needs, so it works from the first question.

``KNOWN_MODELS[provider][model]`` holds the same quirks the engine learns at run
time (see ``engine._MODEL_QUIRKS``):

- ``checked``: the model works with the question loop; it is not checked again
  when it is chosen.
- ``no_forced_tool``: it refuses a forced tool call (``tool_choice`` any/required).
- ``efforts``: the reasoning effort levels it supports, cheapest first; missing
  or empty for a model without them.
- ``reasoning_effort``, ``max_completion_tokens``, ``max_cap``: OpenAI request
  adjustments.
- ``unusable``: the provider's reason why the model cannot be used; it is left
  out of the model list.

``rootmind check-models --write`` checks every model the configured keys can
use and writes this file again (run it before a release when new models
appear, then run ``ruff format`` on it). The Anthropic entries below come from Anthropic's model documentation
(forced tool use and effort levels per model).
"""

from __future__ import annotations

from typing import Any

_EFFORT_ALL = ["low", "medium", "high", "xhigh", "max"]

KNOWN_MODELS: dict[str, dict[str, dict[str, Any]]] = {
    "anthropic": {
        "claude-fable-5-1": {"checked": True, "no_forced_tool": True, "efforts": _EFFORT_ALL},
        "claude-fable-5": {"checked": True, "efforts": _EFFORT_ALL},
        "claude-opus-5-5": {"checked": True, "no_forced_tool": True, "efforts": _EFFORT_ALL},
        "claude-opus-5": {"checked": True, "efforts": _EFFORT_ALL},
        "claude-opus-4-8": {"checked": True, "efforts": _EFFORT_ALL},
        "claude-opus-4-7": {"checked": True, "efforts": _EFFORT_ALL},
        "claude-opus-4-6": {"checked": True, "efforts": ["low", "medium", "high", "max"]},
        "claude-sonnet-5-5": {"checked": True, "no_forced_tool": True, "efforts": _EFFORT_ALL},
        "claude-sonnet-5": {"checked": True, "efforts": _EFFORT_ALL},
        "claude-sonnet-4-6": {"checked": True, "efforts": ["low", "medium", "high", "max"]},
        "claude-haiku-5-5": {"checked": True, "efforts": _EFFORT_ALL},
        "claude-haiku-4-5": {"checked": True, "efforts": []},
    },
    "openai": {},
    "xai": {},
}
