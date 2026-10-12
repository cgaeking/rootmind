import { describe, expect, it } from "vitest";
import { PROBE_URL, classifyProbe, decideBackend } from "./bootstrap";
import type { BootstrapInputs } from "./bootstrap";
import { DEFAULT_BASE_URL } from "./store";
import { serviceInfoFixture } from "../test/helpers";

/** A release build on its first start, nothing stored, no service. */
function inputs(over: Partial<BootstrapInputs> = {}): BootstrapInputs {
  return {
    dev: false,
    builtinAvailable: true,
    storedMode: null,
    storedBaseUrl: null,
    storedToken: null,
    service: serviceInfoFixture(),
    ...over,
  };
}

const serviceOurs = serviceInfoFixture({
  installed: true,
  ours: true,
  state: "running",
  port: 8000,
  url: "http://127.0.0.1:8000",
  tokenReadable: true,
});

describe("decideBackend", () => {
  it("uses a stored custom backend with its URL and token", () => {
    expect(
      decideBackend(
        inputs({ storedMode: "custom", storedBaseUrl: "http://10.0.0.5:8000", storedToken: "t" }),
      ),
    ).toEqual({ action: "custom", url: "http://10.0.0.5:8000", token: "t" });
  });

  it("uses the default URL for a custom backend without a stored URL", () => {
    expect(decideBackend(inputs({ storedMode: "custom" }))).toEqual({
      action: "custom",
      url: DEFAULT_BASE_URL,
      token: null,
    });
  });

  it("prefers a stored custom backend over the service", () => {
    expect(
      decideBackend(inputs({ storedMode: "custom", storedBaseUrl: "http://x", service: serviceOurs })),
    ).toMatchObject({ action: "custom", url: "http://x" });
  });

  it("starts the bundled backend in a dev build that has one", () => {
    expect(decideBackend(inputs({ dev: true, builtinAvailable: true }))).toEqual({
      action: "builtin",
    });
  });

  it("asks for a URL in a dev build without a bundled backend", () => {
    expect(decideBackend(inputs({ dev: true, builtinAvailable: false }))).toEqual({
      action: "ask_url",
      url: DEFAULT_BASE_URL,
      token: null,
    });
  });

  it("never probes 8000 or picks the service in a dev build", () => {
    for (const builtinAvailable of [true, false]) {
      const d = decideBackend(
        inputs({ dev: true, builtinAvailable, storedMode: null, service: serviceOurs }),
      );
      expect(d.action).not.toBe("probe");
      expect(d.action).not.toBe("service");
    }
  });

  it("connects to our service when its token is readable", () => {
    expect(decideBackend(inputs({ storedMode: "auto", service: serviceOurs }))).toEqual({
      action: "service",
    });
    // Also on the first start: the service wins over the probe.
    expect(decideBackend(inputs({ storedMode: null, service: serviceOurs }))).toEqual({
      action: "service",
    });
  });

  it("runs built-in when our service belongs to another account (token unreadable)", () => {
    const service = { ...serviceOurs, tokenReadable: false, blockedReason: "other_account" as const };
    expect(decideBackend(inputs({ storedMode: "auto", service }))).toEqual({ action: "builtin" });
    expect(decideBackend(inputs({ storedMode: null, service }))).toEqual({ action: "builtin" });
  });

  it("ignores a service that is not ours", () => {
    const service = serviceInfoFixture({ installed: true, ours: false, blockedReason: "foreign_service" });
    expect(decideBackend(inputs({ storedMode: "auto", service }))).toEqual({ action: "builtin" });
  });

  it("keeps a URL typed into version 1.1 on the first start", () => {
    expect(
      decideBackend(inputs({ storedMode: null, storedBaseUrl: "http://127.0.0.1:9000" })),
    ).toEqual({ action: "custom", url: "http://127.0.0.1:9000", token: null });
  });

  it("probes 8000 on the first start without a stored URL", () => {
    expect(decideBackend(inputs({ storedMode: null }))).toEqual({ action: "probe" });
  });

  it("asks once when an open RootMind server answers on 8000", () => {
    expect(decideBackend(inputs({ storedMode: null, probe8000: "rootmind_open" }))).toEqual({
      action: "ask_choice",
      url: PROBE_URL,
    });
  });

  it("uses the found server when the user picks it", () => {
    expect(
      decideBackend(
        inputs({ storedMode: null, probe8000: "rootmind_open", firstRunChoice: "custom" }),
      ),
    ).toEqual({ action: "custom", url: PROBE_URL, token: null });
  });

  it("uses the built-in backend when the user picks it", () => {
    expect(
      decideBackend(
        inputs({ storedMode: null, probe8000: "rootmind_open", firstRunChoice: "builtin" }),
      ),
    ).toEqual({ action: "builtin" });
  });

  it("starts built-in when something else (or nothing) answers on 8000", () => {
    expect(decideBackend(inputs({ storedMode: null, probe8000: "other" }))).toEqual({
      action: "builtin",
    });
  });

  it("starts built-in in auto mode", () => {
    expect(decideBackend(inputs({ storedMode: "auto" }))).toEqual({ action: "builtin" });
    // auto never probes, even with a leftover URL
    expect(decideBackend(inputs({ storedMode: "auto", storedBaseUrl: "http://x" }))).toEqual({
      action: "builtin",
    });
  });

  it("starts built-in when service_info could not be read", () => {
    expect(decideBackend(inputs({ storedMode: "auto", service: null }))).toEqual({
      action: "builtin",
    });
  });
});

describe("classifyProbe", () => {
  it("accepts an unauthenticated RootMind API", () => {
    expect(classifyProbe({ name: "RootMind API", version: "1.1.0" })).toBe("rootmind_open");
    expect(classifyProbe({ name: "RootMind API", auth: "none" })).toBe("rootmind_open");
  });

  it("rejects a RootMind API that needs a token, and anything else", () => {
    expect(classifyProbe({ name: "RootMind API", auth: "bearer" })).toBe("other");
    expect(classifyProbe({ name: "Something else" })).toBe("other");
    expect(classifyProbe(null)).toBe("other");
    expect(classifyProbe("RootMind API")).toBe("other");
  });
});
