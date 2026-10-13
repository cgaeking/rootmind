import { afterEach, describe, expect, it } from "vitest";
import { useLanguageStore } from "../i18n/labels";
import {
  canRepair,
  changeMessage,
  isBlockedCode,
  isOlderVersion,
  n8nSnippet,
  problemFromInfo,
  serviceProblem,
  serviceResultKey,
} from "./service";
import { labels as en } from "../i18n/labels.en";
import { labels as de } from "../i18n/labels.de";
import { serviceInfoFixture } from "../test/helpers";

// changeMessage reads the labels of the language in use.
afterEach(() => useLanguageStore.getState().setPref("en"));

function failed(code: number, cancelled = false) {
  return { ok: false as const, cancelled, code, backend: null };
}

describe("serviceResultKey", () => {
  it("maps codes 3 to 10 and a cancelled prompt to distinct messages", () => {
    const keys = [3, 4, 5, 6, 7, 8, 9, 10].map((c) => serviceResultKey(failed(c)));
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
      "programFolderUnsafe",
      "cancelled",
    ]);
    // ...and to distinct texts in both languages
    for (const labels of [en, de]) {
      const texts = keys.map((k) => labels.service.codes[k]);
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

const OURS = serviceInfoFixture({
  installed: true,
  ours: true,
  state: "running",
  port: 8000,
  url: "http://127.0.0.1:8000",
  tokenReadable: true,
});

describe("serviceProblem", () => {
  it("maps each service_connect failure", () => {
    expect(serviceProblem("not_installed", serviceInfoFixture())).toBe("notInstalled");
    expect(serviceProblem("not_running", { ...OURS, state: "stopped" })).toBe("notRunning");
    expect(serviceProblem("listener_mismatch", OURS)).toBe("portBusy");
    expect(serviceProblem("token_unreadable", { ...OURS, tokenReadable: false })).toBe("otherAccount");
    // Connected, but the REST API did not answer.
    expect(serviceProblem(null, OURS)).toBe("noAnswer");
  });

  it("uses service_info for another program's service and a non-Windows build", () => {
    const foreign = { ...OURS, ours: false, url: null, blockedReason: "foreign_service" as const };
    expect(serviceProblem("not_installed", foreign)).toBe("foreign");
    expect(serviceProblem("not_installed", serviceInfoFixture({ supported: false }))).toBe("unsupported");
  });

  it("falls back to the failure when service_info could not be read", () => {
    expect(serviceProblem("not_installed", null)).toBe("notInstalled");
    expect(serviceProblem("not_running", null)).toBe("notRunning");
  });

  it("has a title and a text for every problem in both languages", () => {
    for (const labels of [en, de]) {
      for (const p of Object.values(labels.service.problems)) {
        expect(p.title.length).toBeGreaterThan(0);
        expect(p.text.length).toBeGreaterThan(0);
      }
    }
  });
});

describe("problemFromInfo", () => {
  it("reads the problem from service_info alone", () => {
    expect(problemFromInfo(null, false)).toBeNull();
    expect(problemFromInfo(null, true)).toBe("unsupported");
    expect(problemFromInfo(OURS, false)).toBeNull();
    expect(problemFromInfo(serviceInfoFixture(), false)).toBe("notInstalled");
    expect(problemFromInfo({ ...OURS, ours: false }, false)).toBe("foreign");
    expect(problemFromInfo({ ...OURS, tokenReadable: false }, false)).toBe("otherAccount");
    expect(problemFromInfo({ ...OURS, state: "stopped" }, false)).toBe("notRunning");
    expect(problemFromInfo(serviceInfoFixture({ supported: false }), false)).toBe("unsupported");
  });
});

describe("canRepair", () => {
  it("offers Repair only where setting the service up again can help, and only to administrators", () => {
    for (const p of ["notRunning", "notInstalled", "noAnswer"] as const) {
      expect(canRepair(p, OURS)).toBe(true);
      expect(canRepair(p, null)).toBe(true);
      expect(canRepair(p, { ...OURS, canElevate: false })).toBe(false);
    }
    // Repair cannot free a port, remove a foreign service or take over another account's.
    for (const p of ["unsupported", "portBusy", "foreign", "otherAccount"] as const) {
      expect(canRepair(p, OURS)).toBe(false);
    }
  });
});

describe("changeMessage", () => {
  it("explains an unprotected program folder (code 10)", () => {
    expect(changeMessage({ kind: "result", result: failed(10) })).toEqual({
      tone: "err",
      text: "RootMind's program folder can be changed by accounts without administrator rights, so the service was not set up. Install RootMind with the official installer (it always uses Program Files).",
    });
  });

  it("says nothing after a success, 'nothing changed' after a cancel, else the error", () => {
    expect(changeMessage({ kind: "result", result: { ok: true, backend: { url: "u", token: "t" } } })).toBeNull();
    expect(changeMessage({ kind: "result", result: failed(1223, true) })).toEqual({
      tone: "info",
      text: "Nothing was changed.",
    });
    expect(changeMessage({ kind: "result", result: failed(4) })).toEqual({
      tone: "err",
      text: "The port of the RootMind service is in use by another program.",
    });
    expect(changeMessage({ kind: "error", message: "boom" })).toEqual({
      tone: "err",
      text: "The change failed: boom",
    });
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
