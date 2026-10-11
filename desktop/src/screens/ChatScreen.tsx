import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { labels } from "../i18n/labels";
import { useBackend } from "../hooks/useBackend";
import { useChatStore } from "../state/chatStore";
import { useStore } from "../state/store";
import { askQuestion, search, getDocumentPath, generateChatTitle } from "../api/rlm";
import { ApiError, AbortedError } from "../api/client";
import type { QuerySource } from "../types/api";
import type { StoredMessage } from "../types/chat";
import { Banner, Button } from "../components/ui";
import { Markdown } from "../components/Markdown";
import { ChatLoader } from "../components/ChatLoader";
import type { SearchResult } from "../types/api";
import styles from "./ChatScreen.module.css";

const SEARCH_PAGE = 100;
const SEARCH_MAX = 1000;
// How many recent turns to send for multi-turn context (the backend caps it too).
const HISTORY_TURNS = 6;
// Stable empty list, so the scroll effect below does not rerun on every render.
const NO_MESSAGES: StoredMessage[] = [];

function nextId(messages: StoredMessage[]): number {
  return messages.length > 0 ? messages[messages.length - 1].id + 1 : 1;
}

/** Message ids repeat across conversations; this key does not. */
function messageKey(convId: string, messageId: number): string {
  return `${convId}:${messageId}`;
}

/** The request in flight: which conversation it belongs to and whether it can be stopped. */
interface Pending {
  convId: string;
  question: boolean;
}

function toSources(results: SearchResult[]): QuerySource[] {
  return results.map((r) => ({
    id: r.doc_id,
    file_name: r.file_name,
    file_path: r.file_path,
    // Strip FTS highlight markers (**match**); undefined when no snippet was loaded.
    snippet: r.snippet ? r.snippet.replace(/\*\*/g, "").trim() : undefined,
  }));
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
                title={s.file_path ? labels.chat.openFileTitle(s.file_path) : labels.chat.openFileDefaultTitle}
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
  const { conn } = useBackend();
  const active = useChatStore((s) => s.active);
  const startConversation = useChatStore((s) => s.startConversation);
  const updateConversation = useChatStore((s) => s.updateConversation);
  const setTitle = useChatStore((s) => s.setTitle);
  const setBusy = useStore((s) => s.setBusy);

  const [input, setInput] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [onlySearch, setOnlySearch] = useState(false);
  const [srcError, setSrcError] = useState<string | null>(null);
  const [loadingMoreKey, setLoadingMoreKey] = useState<string | null>(null);
  const [loadingDescKey, setLoadingDescKey] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  // The question in flight, so the user can cancel one that hangs.
  const queryAbort = useRef<AbortController | null>(null);

  const messages = active?.messages ?? NO_MESSAGES;
  // One request at a time. Its loader and Stop button show only in the
  // conversation it belongs to; the answer is written there even if the user
  // opens another conversation meanwhile.
  const loading = pending !== null;
  const loadingHere = pending !== null && pending.convId === active?.id;

  function cancel() {
    queryAbort.current?.abort();
  }

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loadingHere]);

  async function send() {
    const q = input.trim();
    if (!q || loading) return;
    setInput("");
    const isQuestion = !onlySearch;

    // Add the question to the conversation it was typed in (a new one when
    // none is open), and remember that conversation for the answer.
    let userId = 1;
    const conv = active
      ? await updateConversation(active.id, (msgs) => {
          userId = nextId(msgs);
          return [...msgs, { id: userId, role: "user", text: q }];
        })
      : await startConversation([{ id: userId, role: "user", text: q }]);
    if (!conv) return; // deleted in the meantime
    const assistantId = userId + 1;
    const earlier = conv.messages.slice(0, -1);
    const isFirst = earlier.length === 0;

    setPending({ convId: conv.id, question: isQuestion });
    let assistant: StoredMessage;
    try {
      if (!isQuestion) {
        const res = await search(q, { limit: SEARCH_PAGE });
        if (res.results.length === 0) {
          assistant = { id: assistantId, role: "assistant", text: labels.chat.noResults };
        } else {
          // Fast result list (no snippets); descriptions + more pages load on demand.
          assistant = {
            id: assistantId,
            role: "assistant",
            text: "",
            sources: toSources(res.results),
            searchMode: true,
            searchQuery: q,
            searchLimit: SEARCH_PAGE,
          };
        }
      } else {
        queryAbort.current = new AbortController();
        setBusy(true); // pause the heartbeat so a long question isn't read as offline
        // Send recent turns for multi-turn context: text only (drop the bulky
        // source lists and any error bubbles). The backend caps and condenses.
        const history = earlier
          .filter((m) => m.text.trim() && !m.error)
          .slice(-HISTORY_TURNS)
          .map((m) => ({ role: m.role, text: m.text }));
        const res = await askQuestion(q, { history, signal: queryAbort.current.signal });
        assistant = {
          id: assistantId,
          role: "assistant",
          text: res.answer,
          sources: res.sources,
          md: true,
        };
      }
    } catch (e) {
      assistant =
        e instanceof AbortedError
          ? { id: assistantId, role: "assistant", text: labels.chat.cancelled, error: true }
          : {
              id: assistantId,
              role: "assistant",
              text: labels.chat.errorPrefix(e instanceof ApiError ? e.message : String(e)),
              error: true,
            };
    } finally {
      queryAbort.current = null;
      setBusy(false);
    }
    try {
      await updateConversation(conv.id, (msgs) => [...msgs, assistant]);
    } finally {
      setPending(null);
    }
    // Refine the auto-title via the LLM after the first real exchange.
    if (isFirst && isQuestion && !assistant.error) {
      void refineTitle(conv.id, q, assistant.text);
    }
  }

  async function refineTitle(id: string, question: string, answer: string) {
    try {
      const { title } = await generateChatTitle(question, answer);
      if (title) await setTitle(id, title);
    } catch {
      /* keep the derived title */
    }
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
        <div className={styles.messages}>
          {conn !== "online" && <Banner tone="err">{labels.common.backendUnreachable}</Banner>}

          {messages.length === 0 && conn === "online" && (
            <div className={styles.empty}>
              <p>{labels.chat.emptyPrompt}</p>
            </div>
          )}

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
              </div>
            );
          })}

          {loadingHere && <ChatLoader />}
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
            {loadingHere && pending?.question ? (
              <Button variant="primary" onClick={cancel}>
                {labels.chat.stop}
              </Button>
            ) : (
              <Button
                variant="primary"
                onClick={() => void send()}
                disabled={loading || !input.trim() || conn !== "online"}
              >
                {labels.chat.send}
              </Button>
            )}
          </div>
        </div>
      </div>
  );
}
