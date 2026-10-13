import { afterEach, describe, expect, it, vi } from "vitest";
import { displayPath, isWindows, samePath, storedPath } from "./paths";

const WEBVIEW2_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("paths", () => {
  it("detects Windows from the webview's user agent", () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(WEBVIEW2_UA);
    expect(isWindows()).toBe(true);
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (X11; Linux x86_64)");
    expect(isWindows()).toBe(false);
  });

  it("shows backslashes on Windows", () => {
    expect(displayPath("C:/Users/anna/Documents", true)).toBe("C:\\Users\\anna\\Documents");
    expect(displayPath("C:\\Users\\anna/Documents/a.pdf", true)).toBe(
      "C:\\Users\\anna\\Documents\\a.pdf",
    );
    expect(displayPath("//server/share/x", true)).toBe("\\\\server\\share\\x");
  });

  it("leaves paths alone elsewhere", () => {
    expect(displayPath("/home/anna/docs", false)).toBe("/home/anna/docs");
  });

  it("follows the platform by default", () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(WEBVIEW2_UA);
    expect(displayPath("C:/a/b")).toBe("C:\\a\\b");
  });

  it("accepts both separators as input and stores forward slashes on Windows", () => {
    expect(storedPath("  C:\\Users\\anna\\Documents ", true)).toBe("C:/Users/anna/Documents");
    expect(storedPath("C:/Users/anna/Documents", true)).toBe("C:/Users/anna/Documents");
    expect(storedPath(" /home/a\\b ", false)).toBe("/home/a\\b");
  });

  it("round-trips: a displayed path is stored as before", () => {
    const stored = "C:/Users/anna/Documents";
    expect(storedPath(displayPath(stored, true), true)).toBe(stored);
  });

  it("compares paths regardless of the separator on Windows", () => {
    expect(samePath("C:\\a\\b", "C:/a/b", true)).toBe(true);
    expect(samePath("C:/a/b", "C:/a/c", true)).toBe(false);
    expect(samePath("/a\\b", "/a/b", false)).toBe(false);
  });
});
