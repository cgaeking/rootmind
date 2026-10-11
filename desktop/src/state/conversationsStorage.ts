import type { Conversation, ConversationMeta } from "../types/chat";

// Persistence for chat history. In the packaged app, conversations are JSON
// files written by Rust commands (under the app-data dir). In the dev browser
// preview there is no Tauri bridge, so we fall back to localStorage.

const isTauri = () => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(cmd, args);
}

const LS_KEY = "rlm.conversations";

function lsAll(): Record<string, Conversation> {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || "{}");
  } catch {
    return {};
  }
}

function lsWrite(map: Record<string, Conversation>) {
  localStorage.setItem(LS_KEY, JSON.stringify(map));
}

export async function listConversations(): Promise<ConversationMeta[]> {
  if (isTauri()) {
    const rows = await invoke<string[]>("list_conversations");
    return rows.map((r) => JSON.parse(r) as ConversationMeta);
  }
  return Object.values(lsAll())
    .map((c) => ({
      id: c.id,
      title: c.title,
      createdAt: c.createdAt,
      updatedAt: c.updatedAt,
      messageCount: c.messages.length,
    }))
    .sort((a, b) => b.updatedAt - a.updatedAt);
}

export async function loadConversation(id: string): Promise<Conversation | null> {
  if (isTauri()) {
    const json = await invoke<string | null>("load_conversation", { id });
    return json ? (JSON.parse(json) as Conversation) : null;
  }
  return lsAll()[id] ?? null;
}

export async function saveConversation(conv: Conversation): Promise<void> {
  if (isTauri()) {
    await invoke("save_conversation", { id: conv.id, json: JSON.stringify(conv) });
    return;
  }
  const map = lsAll();
  map[conv.id] = conv;
  lsWrite(map);
}

export async function deleteConversation(id: string): Promise<void> {
  if (isTauri()) {
    await invoke("delete_conversation", { id });
    return;
  }
  const map = lsAll();
  delete map[id];
  lsWrite(map);
}
