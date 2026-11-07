import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../state/store";
import { resetStore } from "../test/helpers";

// The conversation list is not under test here.
vi.mock("../state/chatStore", async (importOriginal) => {
  const real = await importOriginal<typeof import("../state/chatStore")>();
  real.useChatStore.setState({ refresh: async () => {} });
  return real;
});

vi.mock("../utils/openExternal", () => ({ openExternal: vi.fn(async () => {}) }));

const { openExternal } = await import("../utils/openExternal");
const { Sidebar } = await import("./Sidebar");

beforeEach(() => {
  resetStore();
  useStore.setState({
    version: "1.1.0",
    appMode: { version: "1.2.0", dev: false, platform: "windows" },
  });
});

afterEach(cleanup);

describe("About", () => {
  it("opens from the menu with the app and server versions, and closes", () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "About" }));

    const dialog = screen.getByRole("dialog", { name: "About" });
    expect(dialog.textContent).toContain("RootMind 1.2.0");
    expect(dialog.textContent).toContain("1.1.0");
    expect(dialog.textContent).toContain("Christian Gäking");
    expect(dialog.textContent).toContain("MIT");
    for (const [name, href] of [
      ["Repository", "https://github.com/cgaeking/rootmind"],
      ["Releases and changelog", "https://github.com/cgaeking/rootmind/releases"],
      ["Issues", "https://github.com/cgaeking/rootmind/issues"],
    ]) {
      expect(screen.getByRole("link", { name }).getAttribute("href")).toBe(href);
    }

    expect(dialog.querySelector('img[src="/rootmind.svg"]')).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "⭐ Star us on GitHub" }));
    expect(openExternal).toHaveBeenCalledWith("https://github.com/cgaeking/rootmind");

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
