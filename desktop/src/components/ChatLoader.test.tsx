import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useLanguageStore } from "../i18n/labels";
import { ChatLoader } from "./ChatLoader";

afterEach(() => {
  cleanup();
  useLanguageStore.getState().setPref("en");
});

// The default text used to be a German string literal, so the English UI
// showed "Denkt nach …" while the model was working.
describe("ChatLoader", () => {
  it("says what it is doing in English", () => {
    useLanguageStore.getState().setPref("en");
    render(<ChatLoader />);
    expect(screen.getByRole("status")).toHaveProperty("textContent", "Thinking …");
    expect(screen.queryByText("Denkt nach …")).toBeNull();
  });

  it("says what it is doing in German", () => {
    useLanguageStore.getState().setPref("de");
    render(<ChatLoader />);
    expect(screen.getByRole("status")).toHaveProperty("textContent", "Denkt nach …");
  });

  it("follows a language change between two renders", () => {
    useLanguageStore.getState().setPref("de");
    const first = render(<ChatLoader />);
    expect(first.container.textContent).toBe("Denkt nach …");
    first.unmount();

    useLanguageStore.getState().setPref("en");
    const second = render(<ChatLoader />);
    expect(second.container.textContent).toBe("Thinking …");
  });

  it("uses an explicit label as given", () => {
    render(<ChatLoader label="Loading" />);
    expect(screen.getByRole("status")).toHaveProperty("textContent", "Loading");
  });
});
