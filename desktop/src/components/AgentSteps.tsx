import { useEffect, useState } from "react";
import { labels } from "../i18n/labels";
import type { AgentStep } from "../types/chat";
import { countCalls } from "../state/agentSteps";
import styles from "./AgentSteps.module.css";

/** "< 0.1 s", "4.2 s" below 10 s, "12 s" below a minute, then "1:07". */
export function formatDuration(ms: number): string {
  if (ms < 50) return labels.chat.steps.instant;
  if (ms < 60_000) {
    return labels.chat.steps.seconds(ms < 10_000 ? Math.round(ms / 100) / 10 : Math.round(ms / 1000));
  }
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function arg(step: AgentStep, key: string): string | undefined {
  const args = step.arguments;
  if (!args || typeof args === "string") return undefined;
  const value = args[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

/** What a step does, in words: "Searching “Nadine Schulz” (pdf only)". */
export function stepTitle(step: AgentStep, maxIterations: number): string {
  const t = labels.chat.steps;
  if (step.kind === "note") return t.notes[step.note ?? ""] ?? step.note ?? "";
  if (step.kind === "model") {
    if (step.purpose === "condense") return t.modelCondense;
    if (step.purpose === "fallback") return t.modelFallback;
    return t.modelLoop(step.iteration ?? 1, maxIterations);
  }
  const query = arg(step, "query") ?? "";
  switch (step.name) {
    case "search_documents": {
      const type = arg(step, "file_type");
      return t.search(query) + (type ? t.searchType(type) : "");
    }
    case "search_fuzzy":
      return t.fuzzy(query);
    case "list_documents": {
      const name = arg(step, "search_filename");
      return name ? t.listName(name) : t.list;
    }
    case "read_document": {
      const name = step.reads?.[0]?.file_name;
      return name ? t.read(name) : t.readDocument;
    }
    case "get_statistics":
      return t.statistics;
    default:
      return t.tool(step.name ?? "");
  }
}

/** What came back, or how long the step has been running: "3 hits: a.pdf, b.pdf · 0.4 s". */
export function stepDetail(step: AgentStep, now: number): string {
  const t = labels.chat.steps;
  if (step.kind === "note") return "";
  if (step.durationMs === undefined) return t.running(formatDuration(Math.max(0, now - step.startedAt)));
  const parts: string[] = [];
  if (step.interrupted) parts.push(t.interrupted);
  else if (step.skipped) parts.push(t.skipped);
  else if (step.error !== undefined) parts.push(step.error ? `${t.failed}: ${step.error}` : t.failed);
  else if (step.kind === "tool" && step.name === "read_document") {
    const read = step.reads?.[0];
    if (read?.location) parts.push(read.location);
    else if (read && read.start !== null && read.end !== null) parts.push(t.readChars(read.start, read.end));
  } else if (step.kind === "tool" && step.hits !== undefined && step.name !== "get_statistics") {
    const files = step.files ?? [];
    const shown = files.slice(0, 3).join(", ");
    parts.push(t.hits(step.hits) + (shown ? `: ${shown}${files.length > 3 || step.hits > 3 ? " …" : ""}` : ""));
  }
  parts.push(formatDuration(step.durationMs));
  return parts.join(" · ");
}

interface Props {
  steps: AgentStep[];
  maxIterations: number;
  /** While the question runs: show the list open, with a running timer. */
  live: boolean;
  /** After the answer: the question's duration, for the collapsed summary. */
  elapsedMs?: number;
}

/**
 * The agent's steps for one question, as a list that opens and closes with a
 * click on its summary. It starts closed; while the question runs, closed it
 * still shows the current step with its timer.
 */
export function AgentSteps({ steps, maxIterations, live, elapsedMs }: Props) {
  const [now, setNow] = useState(() => Date.now());
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live]);

  const item = (step: AgentStep) => {
    const running = step.durationMs === undefined;
    const failed = !running && (step.error !== undefined || !!step.skipped || !!step.interrupted);
    const state = step.kind === "note" ? styles.note : running ? styles.running : failed ? styles.failed : styles.done;
    const title = stepTitle(step, maxIterations);
    const detail = stepDetail(step, now);
    return (
      <li key={step.step} className={`${styles.item} ${state}`}>
        <span className={styles.icon} aria-hidden="true">
          {step.kind === "note" ? "•" : running ? "" : failed ? "✕" : "✓"}
        </span>
        <span className={styles.text} title={detail ? `${title} · ${detail}` : title}>
          <span className={styles.title}>{title}</span>
          {detail && <span className={styles.detail}> · {detail}</span>}
        </span>
      </li>
    );
  };

  const t = labels.chat.steps;
  const summary = live
    ? t.liveSummary(countCalls(steps), formatDuration(Math.max(0, now - (steps[0]?.startedAt ?? now))))
    : t.summary(countCalls(steps), formatDuration(elapsedMs ?? 0));
  const current = steps[steps.length - 1];

  const details = (
    <details
      className={`${styles.steps} ${live ? "" : styles.finished}`}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>{summary}</summary>
      <ol className={styles.list}>{steps.map(item)}</ol>
    </details>
  );
  if (!live) return details;
  return (
    <div className={styles.live} role="status" aria-live="polite">
      {details}
      {/* Closed, the list still shows what is happening right now. */}
      {!open && current && <ol className={styles.list}>{item(current)}</ol>}
    </div>
  );
}
