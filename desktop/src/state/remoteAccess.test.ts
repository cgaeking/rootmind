import { describe, expect, it } from "vitest";
import type { RemoteAccessInfo } from "../types/api";
import { canTurnOn, publicEndpoint, settingsChange } from "./remoteAccess";

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

  it("sends only what changed", () => {
    expect(settingsChange(info, "", null)).toBeNull();
    expect(settingsChange(info, "  ", "saved.ngrok-free.app ")).toBeNull();
    expect(settingsChange(info, " token ", null)).toEqual({ authtoken: "token" });
    expect(settingsChange(info, "", "")).toEqual({ domain: "" });
    expect(settingsChange(info, "t", "new.ngrok.app")).toEqual({ authtoken: "t", domain: "new.ngrok.app" });
    expect(settingsChange({ ...info, domain: null }, "", "")).toBeNull();
  });

  it("can be turned on only with MCP over HTTP, an authtoken and the SDK", () => {
    expect(canTurnOn(info, true)).toBe(true);
    expect(canTurnOn(info, false)).toBe(false);
    expect(canTurnOn({ ...info, authtoken_saved: false }, true)).toBe(false);
    expect(canTurnOn({ ...info, sdk: { available: false, version: null } }, true)).toBe(false);
  });
});
