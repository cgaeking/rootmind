import { describe, expect, it } from "vitest";
import type { RemoteAccessInfo } from "../types/api";
import { connectChange, publicEndpoint } from "./remoteAccess";

const info: RemoteAccessInfo = {
  enabled: false,
  domain: "saved.ngrok-free.app",
  authtoken_saved: true,
  sdk: { available: true, version: "1.7.0" },
  status: { state: "off", url: null, error: null, updated: null },
};

describe("remote access helpers", () => {
  it("builds the public MCP endpoint", () => {
    expect(publicEndpoint("https://a.ngrok.app")).toBe(
      "https://a.ngrok.app/mcp",
    );
    expect(publicEndpoint("https://a.ngrok.app/")).toBe(
      "https://a.ngrok.app/mcp",
    );
  });

  it("turns remote access on with only what changed", () => {
    expect(connectChange(info, "", null)).toEqual({ enabled: true });
    expect(connectChange(info, "  ", "saved.ngrok-free.app ")).toEqual({
      enabled: true,
    });
    expect(connectChange(info, " token ", null)).toEqual({
      enabled: true,
      authtoken: "token",
    });
    expect(connectChange(info, "", "")).toEqual({ enabled: true, domain: "" });
    expect(connectChange(info, "t", "new.ngrok.app")).toEqual({
      enabled: true,
      authtoken: "t",
      domain: "new.ngrok.app",
    });
    expect(connectChange({ ...info, domain: null }, "", "")).toEqual({
      enabled: true,
    });
  });
});

describe("ngrok Authtoken checks and errors", () => {
  const texts = {
    domainInUse: "In use.",
    badAuthtoken: "Bad.",
    agentLimit: "Limit.",
  };

  it("accepts what has the form of an ngrok Authtoken", async () => {
    const { looksLikeAuthtoken } = await import("./remoteAccess");
    expect(looksLikeAuthtoken(" 2abcDEFghiJKLmno_3pqrSTUvwxYZ ")).toBe(true);
    expect(looksLikeAuthtoken("short")).toBe(false);
    expect(looksLikeAuthtoken("with spaces in the middle of it")).toBe(false);
  });

  it("turns known ngrok codes into one sentence and keeps other errors", async () => {
    const { tunnelError } = await import("./remoteAccess");
    expect(
      tunnelError("The endpoint is already online. (ERR_NGROK_334)", texts),
    ).toBe("In use. (ERR_NGROK_334)");
    expect(tunnelError("bad token (ERR_NGROK_105)", texts)).toBe(
      "Bad. (ERR_NGROK_105)",
    );
    expect(tunnelError("limit (ERR_NGROK_108)", texts)).toBe(
      "Limit. (ERR_NGROK_108)",
    );
    expect(tunnelError("something else (ERR_NGROK_999)", texts)).toBe(
      "something else (ERR_NGROK_999)",
    );
    expect(tunnelError("network unreachable", texts)).toBe(
      "network unreachable",
    );
  });
});
