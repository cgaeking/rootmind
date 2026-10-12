import { describe, expect, it } from "vitest";
import {
  isBlockedCode,
  changeUnderWay,
  isOlderVersion,
  n8nSnippet,
  serviceResultKey,
} from "./service";
import { labels as en } from "../i18n/labels.en";
import { labels as de } from "../i18n/labels.de";
import { serviceInfoFixture } from "../test/helpers";

function failed(code: number, cancelled = false) {
  return { ok: false as const, cancelled, code, backend: null };
}

describe("serviceResultKey", () => {
  it("maps codes 3 to 9 and a cancelled prompt to distinct messages", () => {
    const keys = [3, 4, 5, 6, 7, 8, 9].map((c) => serviceResultKey(failed(c)));
    keys.push(serviceResultKey(failed(1223, true)));
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toEqual([
      "foreignService",
      "portBusy",
      "notElevated",
      "dataFolderRefused",
      "healthCheckFailed",
      "accountMismatch",
      "accessContext",
      "cancelled",
    ]);
    // ...and to distinct texts in both languages
    for (const labels of [en, de]) {
      const texts = keys.map((k) => labels.settings.serviceCodes[k]);
      expect(new Set(texts).size).toBe(texts.length);
    }
  });

  it("treats 1223 as cancelled even without the flag", () => {
    expect(serviceResultKey(failed(1223))).toBe("cancelled");
  });

  it("maps the codes for a blocked program", () => {
    // 4556-4559 and 4580-4582: Smart App Control's reputation checks.
    for (const code of [225, 226, 1260, 4550, 4551, 4556, 4559, 4580, 4582, 4599]) {
      expect(isBlockedCode(code)).toBe(true);
      expect(serviceResultKey(failed(code))).toBe("blocked");
    }
    for (const code of [4549, 4600, 5]) expect(isBlockedCode(code)).toBe(false);
  });

  it("maps usage, unexpected and unknown codes", () => {
    expect(serviceResultKey(failed(2))).toBe("unsupported");
    expect(serviceResultKey(failed(1))).toBe("unexpected");
    expect(serviceResultKey(failed(42))).toBe("unexpected");
  });
});

describe("isOlderVersion", () => {
  it("compares release versions", () => {
    expect(isOlderVersion("1.1.0", "1.2.0")).toBe(true);
    expect(isOlderVersion("1.2.0", "1.2.0")).toBe(false);
    expect(isOlderVersion("1.10.0", "1.9.0")).toBe(false);
    expect(isOlderVersion("1.2", "1.2.1")).toBe(true);
    expect(isOlderVersion("2.0.0", "1.9.9")).toBe(false);
  });

  it("puts prereleases before their release", () => {
    expect(isOlderVersion("1.2.0rc1", "1.2.0")).toBe(true);
    expect(isOlderVersion("1.2.0-rc.1", "1.2.0")).toBe(true);
    expect(isOlderVersion("1.2.0", "1.2.0-rc.1")).toBe(false);
  });

  it("never reports an unparseable version as older", () => {
    expect(isOlderVersion("unknown", "1.2.0")).toBe(false);
    expect(isOlderVersion("1.2.0", "")).toBe(false);
  });
});

describe("changeUnderWay", () => {
  it("detects the step after the approval", () => {
    const off = serviceInfoFixture();
    const on = serviceInfoFixture({ installed: true, ours: true, state: "running" });
    expect(changeUnderWay("enable", off, off)).toBe(false);
    expect(changeUnderWay("enable", on, off)).toBe(true);
    expect(changeUnderWay("disable", on, on)).toBe(false);
    expect(changeUnderWay("disable", off, on)).toBe(true);
    expect(changeUnderWay("update", on, on)).toBe(false);
    expect(changeUnderWay("update", { ...on, state: "stop_pending" }, on)).toBe(true);
    // Repairing a stopped service: it starts again.
    const stopped = { ...on, state: "stopped" as const };
    expect(changeUnderWay("update", stopped, stopped)).toBe(false);
    expect(changeUnderWay("update", { ...on, state: "start_pending" }, stopped)).toBe(true);
    const mcp = { ...on, mcp: { installed: true, state: "running" as const, port: 3000, url: "u" } };
    expect(changeUnderWay("mcpOn", on, on)).toBe(false);
    expect(changeUnderWay("mcpOn", mcp, on)).toBe(true);
    expect(changeUnderWay("mcpOff", mcp, mcp)).toBe(false);
    expect(changeUnderWay("mcpOff", on, mcp)).toBe(true);
  });
});

describe("n8nSnippet", () => {
  it("names the endpoint and the Authorization header", () => {
    const text = n8nSnippet("http://127.0.0.1:3000/mcp", "tok");
    expect(text).toContain("http://127.0.0.1:3000/mcp");
    expect(text).toContain("Header Name: Authorization");
    expect(text).toContain("Header Value: Bearer tok");
  });
});
