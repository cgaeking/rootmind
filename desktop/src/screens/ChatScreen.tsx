import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, UIEvent } from "react";
import { Link } from "react-router-dom";
import { labels, useLanguage } from "../i18n/labels";
import { useBackend } from "../hooks/useBackend";
import { useLiveBackend } from "../hooks/useLiveBackend";
import { useChatStore } from "../state/chatStore";
import { search, getConfig, getDocumentPath, getIndexStatus } from "../api/rlm";
import { ApiError } from "../api/client";
import type { QuerySource } from "../types/api";
import type { StoredMessage } from "../types/chat";
import { Banner, Button } from "../components/ui";
import { displayPath } from "../utils/paths";
import { ConnBanner } from "../components/ConnBanner";
import { Markdown } from "../components/Markdown";
import { ChatLoader } from "../components/ChatLoader";
import { AgentSteps } from "../components/AgentSteps";
import {
  DEFAULT_MAX_ITERATIONS,
  SEARCH_PAGE,
  cancelRun,
  runRequest,
  toSources,
  useRuns,
} from "../state/runs";
import styles from "./ChatScreen.module.css";

const SEARCH_MAX = 1000;
// While the first index is being built, re-check this often.
const BUILDING_POLL_MS = 5_000;

interface Hints {
  noFolders: boolean;
  noKey: boolean;
  building: boolean;
}

/** First-run guidance for the empty chat: no folders, no API key, index being built. */
function EmptyChat() {
  const live = useLiveBackend();
  const [hints, setHints] = useState<Hints | null>(null);

  useEffect(() => {
    setHints(null);
    if (!live) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const [config, index] = await Promise.all([getConfig(), getIndexStatus()]);
        if (cancelled) return;
        const building = index.is_running && index.indexed_documents === 0;
        setHints({
          noFolders: config.connectors.length === 0,
          noKey: !(config.llm.api_keys ?? {})[config.llm.provider],
          building,
        });
        if (building) timer = setTimeout(() => void load(), BUILDING_POLL_MS);
      } catch {
        /* the hints are optional */
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [live]);

  return (
    <div className={styles.empty}>
      <p>{labels.chat.emptyPrompt}</p>
      {hints?.noFolders && (
        <p>
          <Link to="/folders">{labels.chat.hintNoFolders}</Link>
        </p>
      )}
      {hints?.building && <p>{labels.chat.hintBuilding}</p>}
      {hints?.noKey && (
        <p>
          {labels.chat.hintNoKey} <Link to="/settings">{labels.chat.hintNoKeyLink}</Link>
        </p>
      )}
    </div>
  );
}

// Stable empty list, so the scroll effect below does not rerun on every render.
const NO_MESSAGES: StoredMessage[] = [];

function nextId(messages: StoredMessage[]): number {
  return messages.length > 0 ? messages[messages.length - 1].id + 1 : 1;
}

/** Message ids repeat across conversations; this key does not. */
function messageKey(convId: string, messageId: number): string {
  return `${convId}:${messageId}`;
}

function SourceList({
  sources,
  onOpen,
  onReveal,
  collapse = true,
  title = labels.chat.sourcesTitle,
  showHeader = true,
}: {
  sources: QuerySource[];
  onOpen: (s: QuerySource) => void;
  onReveal: (s: QuerySource) => void;
  collapse?: boolean;
  title?: string;
  showHeader?: boolean;
}) {
  const collapsible = collapse && sources.length > 3;
  const [open, setOpen] = useState(!collapsible);
  const showList = !collapsible || open;

  return (
    <div className={styles.sources}>
      {showHeader &&
        (collapsible ? (
          <button
            type="button"
            className={styles.sourcesToggle}
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
          >
            <span className={`${styles.chevron} ${open ? styles.chevronOpen : ""}`}>›</span>
            {title} ({sources.length})
          </button>
        ) : (
          <strong>{title}</strong>
        ))}
      {showList &&
        sources.map((s, i) => (
          <div key={i} className={styles.sourceItem}>
            <div className={styles.sourceRow}>
              <button
                type="button"
                className={styles.sourceIcon}
                onClick={() => onReveal(s)}
                title={labels.chat.openFolderTitle}
                aria-label={labels.chat.openFolderTitle}
              >
                📁
              </button>
              <button
                type="button"
                className={styles.source}
                onClick={() => onOpen(s)}
                title={
                  s.file_path
                    ? labels.chat.openFileTitle(displayPath(s.file_path))
                    : labels.chat.openFileDefaultTitle
                }
              >
                📄 {s.file_name}
              </button>
            </div>
            {s.snippet && <div className={styles.sourceSnippet}>{s.snippet}</div>}
          </div>
        ))}
    </div>
  );
}

export function ChatScreen() {
  useLanguage();
  const { conn } = useBackend();
  const active = useChatStore((s) => s.active);
  const startConversation = useChatStore((s) => s.startConversation);
  const updateConversation = useChatStore((s) => s.updateConversation);

  const [input, setInput] = useState("");
  const [onlySearch, setOnlySearch] = useState(false);
  const [srcError, setSrcError] = useState<string | null>(null);
  const [loadingMoreKey, setLoadingMoreKey] = useState<string | null>(null);
  const [loadingDescKey, setLoadingDescKey] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const messages = active?.messages ?? NO_MESSAGES;
  const activeId = active?.id;
  // The request in flight in this conversation, if any (state/runs.ts). It
  // keeps running while another conversation or screen is open; other
  // conversations can ask meanwhile.
  const run = useRuns((s) => (activeId ? s.runs[activeId] : undefined));
  const markRead = useRuns((s) => s.markRead);
  const loadingHere = run !== undefined;

  useEffect(() => {
    if (activeId) markRead(activeId);
  }, [activeId, markRead]);

  function cancel() {
    if (activeId) cancelRun(activeId);
  }

  // Whether the conversation is scrolled to (near) its end: new steps of the
  // question in flight keep it there, but do not pull a reader back down.
  const atEnd = useRef(true);
  function onScroll(e: UIEvent<HTMLDivElement>) {
    const box = e.currentTarget;
    atEnd.current = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  }

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
    atEnd.current = true;
  }, [messages, loadingHere]);

  const liveSteps = run?.steps;
  useEffect(() => {
    if (atEnd.current) endRef.current?.scrollIntoView({ block: "end" });
  }, [liveSteps]);

  async function send() {
    const q = input.trim();
    if (!q || loadingHere) return;
    setInput("");
    const isQuestion = !onlySearch;

    // Add the question to the conversation it was typed in (a new one when
    // none is open); the run writes the answer there (state/runs.ts).
    let userId = 1;
    const conv = active
      ? await updateConversation(active.id, (msgs) => {
          userId = nextId(msgs);
          return [...msgs, { id: userId, role: "user", text: q }];
        })
      : await startConversation([{ id: userId, role: "user", text: q }]);
    if (!conv) return; // deleted in the meantime
    await runRequest(conv, q, isQuestion);
  }

  /** Update one message of conversation `convId` (not necessarily the one on screen). */
  function updateMessage(convId: string, id: number, fn: (m: StoredMessage) => StoredMessage) {
    return updateConversation(convId, (msgs) => msgs.map((x) => (x.id === id ? fn(x) : x)));
  }

  // Lazy-load: fetch the next page of hits (up to SEARCH_MAX) for a search result.
  async function loadMore(m: StoredMessage) {
    if (!active || !m.searchQuery) return;
    const convId = active.id;
    const key = messageKey(convId, m.id);
    if (loadingMoreKey === key) return;
    const newLimit = Math.min((m.searchLimit ?? SEARCH_PAGE) + SEARCH_PAGE, SEARCH_MAX);
    setLoadingMoreKey(key);
    try {
      const res = await search(m.searchQuery, {
        limit: newLimit,
        with_snippets: m.descriptionsLoaded,
      });
      await updateMessage(convId, m.id, (x) => ({
        ...x,
        sources: toSources(res.results),
        searchLimit: newLimit,
      }));
    } catch (e) {
      setSrcError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoadingMoreKey(null);
    }
  }

  // Load content snippets ("descriptions") for the currently shown results.
  async function loadDescriptions(m: StoredMessage) {
    if (!active || !m.searchQuery) return;
    const convId = active.id;
    const key = messageKey(convId, m.id);
    if (loadingDescKey === key) return;
    setLoadingDescKey(key);
    try {
      const res = await search(m.searchQuery, {
        limit: m.searchLimit ?? SEARCH_PAGE,
        with_snippets: true,
      });
      await updateMessage(convId, m.id, (x) => ({
        ...x,
        sources: toSources(res.results),
        descriptionsLoaded: true,
      }));
    } catch (e) {
      setSrcError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoadingDescKey(null);
    }
  }

  // Resolve a source to its absolute path on disk (validated within the
  // connector root by the backend).
  async function resolveSourcePath(s: QuerySource): Promise<string> {
    if (s.id) return (await getDocumentPath(s.id)).abs_path;
    return s.file_path;
  }

  async function openSource(s: QuerySource) {
    setSrcError(null);
    try {
      const path = await resolveSourcePath(s);
      const { openPath } = await import("@tauri-apps/plugin-opener");
      await openPath(path);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setSrcError(labels.chat.openFileFailed(msg));
    }
  }

  async function revealSource(s: QuerySource) {
    setSrcError(null);
    try {
      const path = await resolveSourcePath(s);
      const { revealItemInDir } = await import("@tauri-apps/plugin-opener");
      await revealItemInDir(path);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setSrcError(labels.chat.openFolderFailed(msg));
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <div className={styles.main}>
        <div className={styles.messages} onScroll={onScroll}>
          <ConnBanner />

          {messages.length === 0 && conn === "online" && <EmptyChat />}

          {messages.map((m) => {
            const key = active ? messageKey(active.id, m.id) : String(m.id);
            return (
              <div
                key={m.id}
                className={[
                  styles.msg,
                  m.role === "user" ? styles.user : styles.assistant,
                  m.error ? styles.assistantError : "",
                ].join(" ")}
              >
                {m.md ? <Markdown>{m.text}</Markdown> : m.text}
                {m.searchMode && m.sources && m.sources.length > 0 ? (
                  <div className={styles.searchBlock}>
                    <div className={styles.searchHeader}>
                      <strong>{labels.chat.hitsCount(m.sources.length)}</strong>
                      {!m.descriptionsLoaded && (
                        <button
                          type="button"
                          className={styles.descLink}
                          onClick={() => void loadDescriptions(m)}
                          disabled={loadingDescKey === key}
                        >
                          {loadingDescKey === key ? labels.chat.loadingShort : labels.chat.loadDescriptions}
                        </button>
                      )}
                    </div>
                    <SourceList
                      sources={m.sources}
                      onOpen={openSource}
                      onReveal={revealSource}
                      collapse={false}
                      showHeader={false}
                    />
                    {(m.searchLimit ?? 0) <= m.sources.length && (m.searchLimit ?? 0) < SEARCH_MAX && (
                      <button
                        type="button"
                        className={styles.loadMore}
                        onClick={() => void loadMore(m)}
                        disabled={loadingMoreKey === key}
                      >
                        {loadingMoreKey === key ? labels.chat.loadingShort : labels.chat.loadMore(SEARCH_PAGE)}
                      </button>
                    )}
                  </div>
                ) : (
                  m.sources &&
                  m.sources.length > 0 && (
                    <SourceList sources={m.sources} onOpen={openSource} onReveal={revealSource} />
                  )
                )}
                {m.steps && m.steps.length > 0 && (
                  <AgentSteps
                    steps={m.steps}
                    maxIterations={m.maxIterations ?? DEFAULT_MAX_ITERATIONS}
                    live={false}
                    elapsedMs={m.elapsedMs}
                  />
                )}
              </div>
            );
          })}

          {loadingHere && <ChatLoader />}
          {run?.question && run.steps.length > 0 && (
            <AgentSteps
              key={run.startedAt}
              steps={run.steps}
              maxIterations={run.maxIterations}
              live
            />
          )}
          {srcError && <Banner tone="err">{srcError}</Banner>}
          <div ref={endRef} />
        </div>

        <div className={styles.inputBar}>
          <textarea
            className={styles.textarea}
            value={input}
            onChange={(e) => setInput(e.currentTarget.value)}
            onKeyDown={onKeyDown}
            placeholder={labels.chat.inputPlaceholder}
            disabled={conn !== "online"}
            rows={1}
          />
          <div className={styles.controls}>
            <label className={styles.toggle}>
              <input
                type="checkbox"
                checked={onlySearch}
                onChange={(e) => setOnlySearch(e.currentTarget.checked)}
              />
              {labels.chat.searchOnly}
            </label>
            {run?.question ? (
              <Button variant="primary" onClick={cancel}>
                {labels.chat.stop}
              </Button>
            ) : (
              <Button
                variant="primary"
                onClick={() => void send()}
                disabled={loadingHere || !input.trim() || conn !== "online"}
              >
                {labels.chat.send}
              </Button>
            )}
          </div>
        </div>
      </div>
  );
}
