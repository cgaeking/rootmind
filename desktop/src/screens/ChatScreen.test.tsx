import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/rlm", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rlm")>()),
  askQuestionStream: vi.fn(),
  generateChatTitle: vi.fn(),
}));

import * as rlm from "../api/rlm";
import type { AskStreamOptions } from "../api/rlm";
import { AbortedError } from "../api/client";
import { useLanguageStore } from "../i18n/labels";
import { useChatStore } from "../state/chatStore";
import { runRequest, useRuns } from "../state/runs";
import { useStore } from "../state/store";
import { resetStore } from "../test/helpers";
import type { QueryResponse } from "../types/api";
import { ChatScreen } from "./ChatScreen";

let pending: { opts: AskStreamOptions; answer: (text: string) => void } | null = null;

beforeEach(() => {
  resetStore();
  useStore.setState({ kind: "service", conn: "online" });
  useLanguageStore.getState().setPref("en");
  useChatStore.setState({ list: [], active: null });
  useRuns.setState({ runs: {}, unread: {} });
  pending = null;
  vi.mocked(rlm.generateChatTitle).mockResolvedValue({ title: "" });
  vi.mocked(rlm.askQuestionStream).mockImplementation(
    (_question, opts = {}) =>
      new Promise<QueryResponse>((resolve, reject) => {
        opts.signal?.addEventListener("abort", () => reject(new AbortedError()));
        pending = {
          opts,
          answer: (answer) =>
            resolve({ answer, sources: [], tokens_used: 1, tool_calls: [], stop_reasons: ["end_turn"] }),
        };
      }),
  );
});

afterEach(() => cleanup());

function renderChat() {
  return render(
    <MemoryRouter>
      <ChatScreen />
    </MemoryRouter>,
  );
}

describe("ChatScreen", () => {
  it("shows a question that keeps running after leaving the screen and coming back", async () => {
    const conv = await useChatStore.getState().startConversation([{ id: 1, role: "user", text: "Q?" }]);
    let done!: Promise<void>;
    act(() => {
      done = runRequest(conv, "Q?", true);
    });
    await vi.waitFor(() => expect(pending).not.toBeNull());
    act(() =>
      pending!.opts.onStep?.({
        type: "tool_start",
        step: 1,
        iteration: 1,
        name: "search_documents",
        arguments: { query: "x" },
      }),
    );

    // Open the screen, leave it (Settings or another chat), come back.
    renderChat().unmount();
    renderChat();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(document.body.textContent).toContain("Working: 1 step");
    expect(document.body.textContent).toContain("Searching “x”");

    await act(async () => {
      pending!.answer("The answer.");
      await done;
    });
    expect(screen.getByText("The answer.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });

  it("can ask in another conversation while one runs", async () => {
    const first = await useChatStore.getState().startConversation([{ id: 1, role: "user", text: "Q1?" }]);
    act(() => void runRequest(first, "Q1?", true));
    await vi.waitFor(() => expect(pending).not.toBeNull());

    act(() => useChatStore.getState().newChat());
    renderChat();
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Q2?" } });
    const send = screen.getByRole("button", { name: "Send" });
    expect(send.hasAttribute("disabled")).toBe(false);
    await act(async () => {
      fireEvent.click(send);
    });
    await vi.waitFor(() => expect(Object.keys(useRuns.getState().runs)).toHaveLength(2));
    for (const run of Object.values(useRuns.getState().runs)) run.abort.abort();
  });
});
