import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useLanguageStore } from "../i18n/labels";
import { applyStepEvent, countCalls, finishOpenSteps } from "../state/agentSteps";
import type { QueryStepEvent } from "../types/api";
import type { AgentStep } from "../types/chat";
import { AgentSteps, formatDuration, stepDetail, stepTitle } from "./AgentSteps";

beforeEach(() => {
  useLanguageStore.getState().setPref("en");
});

afterEach(() => {
  cleanup();
});

/** The steps of a question that searched, read a document and answered. */
function finishedSteps(): AgentStep[] {
  let steps: AgentStep[] = [];
  const events: QueryStepEvent[] = [
    { type: "model_start", step: 1, purpose: "loop", iteration: 1 },
    { type: "model_end", step: 1, duration_ms: 3200, stop_reason: "tool_use", tokens: 10, tool_requests: 1 },
    { type: "tool_start", step: 2, iteration: 1, name: "search_documents", arguments: { query: "Nadine Schulz", file_type: "pdf" } },
    {
      type: "tool_end",
      step: 2,
      duration_ms: 400,
      is_error: false,
      hits: 4,
      files: ["a.pdf", "b.pdf", "c.pdf", "d.pdf"],
      reads: [],
      result_chars: 900,
    },
    { type: "tool_start", step: 3, iteration: 2, name: "read_document", arguments: { doc_id: "x" } },
    {
      type: "tool_end",
      step: 3,
      duration_ms: 50,
      is_error: false,
      hits: 0,
      files: [],
      reads: [{ file_name: "Bewerbung.pdf", location: "Page 2", start: 0, end: 2500 }],
      result_chars: 2500,
    },
    { type: "note", kind: "iteration_limit" },
  ];
  for (const event of events) steps = applyStepEvent(steps, event, 1000);
  return steps;
}

describe("applyStepEvent", () => {
  it("adds a step on start and completes it on end", () => {
    const steps = finishedSteps();
    expect(steps.map((s) => [s.step, s.kind, s.durationMs])).toEqual([
      [1, "model", 3200],
      [2, "tool", 400],
      [3, "tool", 50],
      [-4, "note", 0],
    ]);
    expect(steps[1]).toMatchObject({ name: "search_documents", hits: 4, startedAt: 1000 });
    expect(countCalls(steps)).toBe(3);
  });

  it("marks steps still running when the question ends", () => {
    const running = applyStepEvent([], { type: "tool_start", step: 1, iteration: 1, name: "search_documents", arguments: {} }, 1000);
    expect(finishOpenSteps(running, 61_000)[0]).toMatchObject({ durationMs: 60_000, interrupted: true });
  });
});

describe("step texts", () => {
  it("says what each step does and what came back", () => {
    const [model, search, read, note] = finishedSteps();
    expect(stepTitle(model, 10)).toBe("Thinking (step 1 of max. 10)");
    expect(stepTitle(search, 10)).toBe("Searching “Nadine Schulz” (pdf only)");
    expect(stepDetail(search, 0)).toBe("4 hits: a.pdf, b.pdf, c.pdf … · 0.4 s");
    expect(stepTitle(read, 10)).toBe("Reading Bewerbung.pdf");
    expect(stepDetail(read, 0)).toBe("Page 2 · 0.1 s");
    expect(stepTitle(note, 10)).toBe("Step limit reached: answering from what was found");
  });

  it("shows how long a running step has been running", () => {
    const [step] = applyStepEvent([], { type: "tool_start", step: 1, iteration: 1, name: "search_documents", arguments: { query: "x" } }, 0);
    expect(stepDetail(step, 75_000)).toBe("running for 1:15");
  });

  it("is German with the German interface", () => {
    useLanguageStore.getState().setPref("de");
    const [, search] = finishedSteps();
    expect(stepTitle(search, 10)).toBe("Sucht „Nadine Schulz“ (nur pdf)");
    expect(stepDetail(search, 0)).toBe("4 Treffer: a.pdf, b.pdf, c.pdf … · 0,4 s");
  });

  it("formats durations", () => {
    expect(formatDuration(12)).toBe("< 0.1 s");
    expect(formatDuration(4230)).toBe("4.2 s");
    expect(formatDuration(42_300)).toBe("42 s");
    expect(formatDuration(62_000)).toBe("1:02");
  });
});

describe("AgentSteps", () => {
  it("shows the steps open while the question runs", () => {
    render(<AgentSteps steps={finishedSteps()} maxIterations={10} live />);
    expect(screen.getByRole("status").textContent).toContain("Searching “Nadine Schulz” (pdf only)");
  });

  it("shows a collapsed summary after the answer that opens to the steps", () => {
    const { container } = render(<AgentSteps steps={finishedSteps()} maxIterations={10} live={false} elapsedMs={62_000} />);
    const summary = screen.getByText("How it searched: 3 steps, 1:02");
    const details = container.querySelector("details");
    expect(details?.open).toBe(false);
    fireEvent.click(summary);
    expect(details?.textContent).toContain("Reading Bewerbung.pdf");
  });
});
