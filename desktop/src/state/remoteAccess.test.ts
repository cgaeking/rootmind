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
    expect(publicEndpoint("https://a.ngrok.app")).toBe("https://a.ngrok.app/mcp");
    expect(publicEndpoint("https://a.ngrok.app/")).toBe("https://a.ngrok.app/mcp");
  });

  it("turns remote access on with only what changed", () => {
    expect(connectChange(info, "", null)).toEqual({ enabled: true });
    expect(connectChange(info, "  ", "saved.ngrok-free.app ")).toEqual({ enabled: true });
    expect(connectChange(info, " token ", null)).toEqual({ enabled: true, authtoken: "token" });
    expect(connectChange(info, "", "")).toEqual({ enabled: true, domain: "" });
    expect(connectChange(info, "t", "new.ngrok.app")).toEqual({
      enabled: true,
      authtoken: "t",
      domain: "new.ngrok.app",
    });
    expect(connectChange({ ...info, domain: null }, "", "")).toEqual({ enabled: true });
  });
});
