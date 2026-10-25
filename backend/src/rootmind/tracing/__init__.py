"""Optional tracing of the agent loop (off by default).

See :mod:`.base` for the hook the engine calls, :mod:`.local` for the local
trace file and :mod:`.langfuse_export` for the optional Langfuse exporter.
This package is imported only where tracing is set up (the eval runner, and
the backend when ``tracing`` is turned on in ``config.yaml``); the engine
itself never imports it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .base import MultiTracer, QuestionTrace, Span, Tracer

if TYPE_CHECKING:
    from ..config import TracingConfig

logger = logging.getLogger(__name__)

__all__ = ["MultiTracer", "QuestionTrace", "Span", "Tracer", "tracer_from_config"]


def tracer_from_config(settings: TracingConfig, logs_dir: Path) -> Tracer | None:
    """The tracer that ``config.yaml``'s ``tracing`` section asks for, or None (the default).

    ``tracing.local`` writes under ``logs_dir/traces``; ``tracing.langfuse``
    exports to Langfuse. A backend that cannot be set up (for example Langfuse
    without the extra or without keys) is logged and left out; the backend
    keeps answering questions.
    """
    tracers: list[Tracer] = []
    if settings.local:
        try:
            from .local import LocalTracer

            tracer = LocalTracer(logs_dir / "traces")
            tracers.append(tracer)
            logger.warning(
                "Local tracing is on: questions and document text are written to %s",
                tracer.jsonl_path,
            )
        except OSError as e:
            logger.error("Local tracing is on but its folder cannot be created: %s", e)
    if settings.langfuse:
        from .langfuse_export import LangfuseTracer, TracingConfigError

        try:
            tracers.append(LangfuseTracer())
            logger.warning("Langfuse tracing is on: traces are sent to the Langfuse server")
        except TracingConfigError as e:
            logger.error("Langfuse tracing is on but cannot start: %s", e)
    if not tracers:
        return None
    return tracers[0] if len(tracers) == 1 else MultiTracer(tracers)
