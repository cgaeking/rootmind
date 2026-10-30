// Turns POST /query/stream's step events into the list of steps the chat shows.

import type { QueryStepEvent } from "../types/api";
import type { AgentStep } from "../types/chat";

/**
 * The step list after `event`: a start adds a step, an end completes the step
 * with the same number, a note adds a line. `now` is the client time, used
 * for the running timer of a step that has not ended yet.
 */
export function applyStepEvent(
  steps: AgentStep[],
  event: QueryStepEvent,
  now: number,
): AgentStep[] {
  switch (event.type) {
    case "model_start":
      return [
        ...steps,
        { step: event.step, kind: "model", purpose: event.purpose, iteration: event.iteration, startedAt: now },
      ];
    case "tool_start":
      return [
        ...steps,
        {
          step: event.step,
          kind: "tool",
          name: event.name,
          arguments: event.arguments,
          iteration: event.iteration,
          startedAt: now,
        },
      ];
    case "model_end":
      return steps.map((s) =>
        s.step === event.step && s.kind === "model"
          ? { ...s, durationMs: event.duration_ms, error: event.error }
          : s,
      );
    case "tool_end":
      return steps.map((s) =>
        s.step === event.step && s.kind === "tool"
          ? {
              ...s,
              durationMs: event.duration_ms,
              hits: event.hits,
              files: event.files,
              reads: event.reads,
              skipped: event.skipped,
              error: event.is_error ? (event.error ?? "") : undefined,
            }
          : s,
      );
    case "note":
      return [...steps, { step: -steps.length - 1, kind: "note", note: event.kind, startedAt: now, durationMs: 0 }];
    default:
      return steps;
  }
}

/** `steps` with every step still running ended at `now` and marked as interrupted. */
export function finishOpenSteps(steps: AgentStep[], now: number): AgentStep[] {
  return steps.map((s) =>
    s.durationMs === undefined
      ? { ...s, durationMs: Math.max(0, now - s.startedAt), interrupted: true }
      : s,
  );
}

/** Model and tool calls among `steps` (notes are not counted). */
export function countCalls(steps: AgentStep[]): number {
  return steps.filter((s) => s.kind !== "note").length;
}
