import { describe, expect, it } from "vitest";
import { decideBackend } from "./bootstrap";
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

  it("never picks the service in a dev build", () => {
    for (const builtinAvailable of [true, false]) {
      for (const storedMode of [null, "auto"] as const) {
        const d = decideBackend(
          inputs({ dev: true, builtinAvailable, storedMode, service: serviceOurs }),
        );
        expect(d.action).not.toBe("service");
      }
    }
  });

  it("starts the built-in backend on the first start", () => {
    expect(decideBackend(inputs({ storedMode: null }))).toEqual({ action: "builtin" });
  });

  it("ignores a stored URL without the custom mode", () => {
    for (const storedMode of [null, "auto"] as const) {
      expect(
        decideBackend(inputs({ storedMode, storedBaseUrl: "http://127.0.0.1:18200", storedToken: "t" })),
      ).toEqual({ action: "builtin" });
    }
  });

  it("connects to our service when its token is readable", () => {
    for (const storedMode of [null, "auto"] as const) {
      expect(decideBackend(inputs({ storedMode, service: serviceOurs }))).toEqual({
        action: "service",
      });
    }
  });

  it("runs built-in when our service belongs to another account (token unreadable)", () => {
    const service = { ...serviceOurs, tokenReadable: false, blockedReason: "other_account" as const };
    expect(decideBackend(inputs({ storedMode: "auto", service }))).toEqual({ action: "builtin" });
    expect(decideBackend(inputs({ storedMode: null, service }))).toEqual({ action: "builtin" });
  });

  it("ignores a service that is not ours", () => {
    const service = serviceInfoFixture({ installed: true, ours: false, blockedReason: "foreign_service" });
    expect(decideBackend(inputs({ storedMode: "auto", service }))).toEqual({ action: "builtin" });
    expect(decideBackend(inputs({ storedMode: null, service }))).toEqual({ action: "builtin" });
  });

  it("starts built-in in auto mode", () => {
    expect(decideBackend(inputs({ storedMode: "auto" }))).toEqual({ action: "builtin" });
  });

  it("starts built-in when service_info could not be read", () => {
    for (const storedMode of [null, "auto"] as const) {
      expect(decideBackend(inputs({ storedMode, service: null }))).toEqual({ action: "builtin" });
    }
  });
});
