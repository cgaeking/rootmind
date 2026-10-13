import { describe, expect, it } from "vitest";
import { decideBackend } from "./bootstrap";
import type { BootstrapInputs } from "./bootstrap";
import { DEFAULT_BASE_URL } from "./store";

/** A release build on its first start, nothing stored. */
function inputs(over: Partial<BootstrapInputs> = {}): BootstrapInputs {
  return {
    dev: false,
    storedMode: null,
    storedBaseUrl: null,
    storedToken: null,
    ...over,
  };
}

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

  it("uses a stored custom backend in a debug build too", () => {
    expect(
      decideBackend(inputs({ dev: true, storedMode: "custom", storedBaseUrl: "http://x" })),
    ).toMatchObject({ action: "custom", url: "http://x" });
  });

  it("connects to the RootMind service on the first start", () => {
    expect(decideBackend(inputs())).toEqual({ action: "service" });
  });

  it("connects to the service in auto mode and ignores a stored URL without the custom mode", () => {
    for (const storedMode of [null, "auto"] as const) {
      expect(
        decideBackend(inputs({ storedMode, storedBaseUrl: "http://127.0.0.1:18200", storedToken: "t" })),
      ).toEqual({ action: "service" });
    }
  });

  it("asks for a URL in a debug build instead of using the installed service", () => {
    for (const storedMode of [null, "auto"] as const) {
      expect(decideBackend(inputs({ dev: true, storedMode }))).toEqual({
        action: "ask_url",
        url: DEFAULT_BASE_URL,
        token: null,
      });
    }
  });
});
