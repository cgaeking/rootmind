import { create } from "zustand";
import { labels } from "../i18n/labels";
import type { Conversation, ConversationMeta, StoredMessage } from "../types/chat";
import {
  deleteConversation,
  listConversations,
  loadConversation,
  saveConversation,
} from "./conversationsStorage";

function uid(): string {
  return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function deriveTitle(messages: StoredMessage[]): string {
  const firstUser = messages.find((m) => m.role === "user");
  const t = (firstUser?.text ?? "").trim().replace(/\s+/g, " ");
  if (!t) return labels.common.newConversation;
  return t.length > 60 ? t.slice(0, 57) + "…" : t;
}

function toMeta(c: Conversation): ConversationMeta {
  return {
    id: c.id,
    title: c.title,
    createdAt: c.createdAt,
    updatedAt: c.updatedAt,
    messageCount: c.messages.length,
  };
}

// Read-modify-write steps on stored conversations run one at a time, so two
// updates that finish close together (an answer and a "load more") cannot
// overwrite each other's changes.
let writeQueue: Promise<unknown> = Promise.resolve();
function serialized<T>(task: () => Promise<T>): Promise<T> {
  const run = writeQueue.then(task, task);
  writeQueue = run.catch(() => undefined);
  return run;
}

interface ChatStore {
  list: ConversationMeta[];
  active: Conversation | null;

  refresh: () => Promise<void>;
  newChat: () => void;
  open: (id: string) => Promise<void>;
  /** Create a new conversation with `messages`, make it the active one and persist it. */
  startConversation: (messages: StoredMessage[]) => Promise<Conversation>;
  /**
   * Change the messages of conversation `id`, whether or not it is still the
   * active one, and persist it. A request that started in one conversation
   * writes its result there even if the user has opened another meanwhile.
   * Returns null when the conversation was deleted in the meantime.
   */
  updateConversation: (
    id: string,
    fn: (messages: StoredMessage[]) => StoredMessage[],
  ) => Promise<Conversation | null>;
  setTitle: (id: string, title: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
}

export const useChatStore = create<ChatStore>((set, get) => ({
  list: [],
  active: null,

  refresh: async () => {
    set({ list: await listConversations() });
  },

  newChat: () => set({ active: null }),

  open: async (id) => {
    const conv = await loadConversation(id);
    if (conv) set({ active: conv });
  },

  startConversation: (messages) =>
    serialized(async () => {
      const now = Date.now();
      const conv: Conversation = {
        id: uid(),
        title: deriveTitle(messages),
        createdAt: now,
        updatedAt: now,
        messages,
      };
      await saveConversation(conv);
      set({ active: conv, list: [toMeta(conv), ...get().list] });
      return conv;
    }),

  updateConversation: (id, fn) =>
    serialized(async () => {
      const current = get().active;
      const base = current && current.id === id ? current : await loadConversation(id);
      if (!base) return null;
      const conv: Conversation = { ...base, messages: fn(base.messages), updatedAt: Date.now() };

      await saveConversation(conv);
      set({
        // Only the conversation on screen is replaced; another one stays open.
        active: get().active?.id === id ? conv : get().active,
        list: [toMeta(conv), ...get().list.filter((m) => m.id !== id)],
      });
      return conv;
    }),

  setTitle: (id, title) =>
    serialized(async () => {
      const a = get().active;
      const base = a && a.id === id ? a : await loadConversation(id);
      if (!base) return;
      const conv = { ...base, title };
      await saveConversation(conv);
      set({
        active: get().active?.id === id ? conv : get().active,
        list: get().list.map((m) => (m.id === id ? { ...m, title } : m)),
      });
    }),

  remove: (id) =>
    serialized(async () => {
      await deleteConversation(id);
      set({
        list: get().list.filter((m) => m.id !== id),
        active: get().active?.id === id ? null : get().active,
      });
    }),
}));
