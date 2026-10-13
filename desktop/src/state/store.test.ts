import { beforeEach, describe, expect, it } from "vitest";
import {
  STORAGE_KEYS,
  connAfterSwitch,
  normalizeUrl,
  readStoredBackend,
  useStore,
} from "./store";
import { resetStore } from "../test/helpers";

beforeEach(resetStore);

describe("setBackend", () => {
  it("checks a new custom or service backend first", () => {
    useStore.setState({ kind: "service", baseUrl: "http://127.0.0.1:8000", conn: "online" });
    useStore.getState().setBackend({ kind: "custom", url: "http://a", token: null });
    expect(useStore.getState().conn).toBe("checking");

    useStore.getState().setConn("online");
    useStore.getState().setBackend({ kind: "service", url: "http://127.0.0.1:8000", token: "t" });
    expect(useStore.getState().conn).toBe("checking");
  });

  it("keeps the state when the backend stays the same", () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", conn: "online", version: "1.2.0" });
    useStore.getState().setBackend({ kind: "custom", url: "http://a/", token: "new" });
    const s = useStore.getState();
    expect(s.conn).toBe("online");
    expect(s.version).toBe("1.2.0");
    expect(s.token).toBe("new");
  });

  it("treats a different kind on the same URL as a switch", () => {
    useStore.setState({ kind: "custom", baseUrl: "http://127.0.0.1:8000", conn: "online" });
    useStore.getState().setBackend({ kind: "service", url: "http://127.0.0.1:8000", token: "t" });
    expect(useStore.getState().conn).toBe("checking");
    expect(useStore.getState().version).toBeNull();
  });

  it("stores URL and token for a custom backend only", () => {
    useStore.getState().setBackend({ kind: "custom", url: " http://a:9000/ ", token: "secret" });
    expect(localStorage.getItem(STORAGE_KEYS.mode)).toBe("custom");
    expect(localStorage.getItem(STORAGE_KEYS.baseUrl)).toBe("http://a:9000");
    expect(localStorage.getItem(STORAGE_KEYS.token)).toBe("secret");
    expect(useStore.getState().baseUrl).toBe("http://a:9000");
  });

  it("stores only the auto mode for the service, never its URL or token", () => {
    useStore.getState().setBackend({ kind: "custom", url: "http://a", token: "secret" });
    useStore.getState().setBackend({ kind: "service", url: "http://127.0.0.1:8000", token: "t2" });
    expect(localStorage.getItem(STORAGE_KEYS.mode)).toBe("auto");
    expect(localStorage.getItem(STORAGE_KEYS.baseUrl)).toBeNull();
    expect(localStorage.getItem(STORAGE_KEYS.token)).toBeNull();
  });

  it("does not store anything with persist: false", () => {
    useStore
      .getState()
      .setBackend({ kind: "custom", url: "http://a", token: "t" }, { persist: false });
    expect(localStorage.length).toBe(0);
    expect(useStore.getState().baseUrl).toBe("http://a");
  });

  it("does not store an empty custom token", () => {
    useStore.getState().setBackend({ kind: "custom", url: "http://a", token: "" });
    expect(localStorage.getItem(STORAGE_KEYS.token)).toBeNull();
    expect(useStore.getState().token).toBeNull();
  });
});

describe("readStoredBackend", () => {
  it("reads the stored choice", () => {
    localStorage.setItem(STORAGE_KEYS.mode, "custom");
    localStorage.setItem(STORAGE_KEYS.baseUrl, "http://a");
    localStorage.setItem(STORAGE_KEYS.token, "t");
    expect(readStoredBackend()).toEqual({ mode: "custom", baseUrl: "http://a", token: "t" });
  });

  it("treats a missing or unknown mode as the first start", () => {
    expect(readStoredBackend()).toEqual({ mode: null, baseUrl: null, token: null });
    localStorage.setItem(STORAGE_KEYS.mode, "service");
    expect(readStoredBackend().mode).toBeNull();
  });
});

describe("helpers", () => {
  it("normalizes typed URLs", () => {
    expect(normalizeUrl("  http://127.0.0.1:8000//  ")).toBe("http://127.0.0.1:8000");
  });

  it("computes the state after a switch", () => {
    const prev = { kind: "custom" as const, baseUrl: "http://a", conn: "offline" as const };
    expect(connAfterSwitch(prev, { kind: "custom", url: "http://a", token: null })).toBe("offline");
    expect(connAfterSwitch(prev, { kind: "custom", url: "http://b", token: null })).toBe("checking");
    expect(connAfterSwitch(prev, { kind: "service", url: "", token: null })).toBe("checking");
  });
});
