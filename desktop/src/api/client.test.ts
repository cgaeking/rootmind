import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, requestHeaders } from "./client";
import { useStore } from "../state/store";
import { jsonResponse, resetStore } from "../test/helpers";

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  resetStore();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function sentHeaders(): Headers {
  const init = fetchMock.mock.calls[0][1];
  return new Headers(init?.headers);
}

describe("api", () => {
  it("sends no Authorization header without a token", async () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: null, conn: "online" });
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await api("/status");
    expect(fetchMock.mock.calls[0][0]).toBe("http://a/status");
    expect(sentHeaders().has("Authorization")).toBe(false);
    expect(sentHeaders().get("Content-Type")).toBe("application/json");
  });

  it("sends the bearer token when one is set", async () => {
    useStore.setState({ kind: "service", baseUrl: "http://b", token: "abc123", conn: "online" });
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await api("/config", { method: "POST", body: "{}" });
    expect(sentHeaders().get("Authorization")).toBe("Bearer abc123");
  });

  it("sets conn to unauthorized on a 401", async () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: "wrong", conn: "online" });
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Invalid token" }, 401));
    await expect(api("/status")).rejects.toMatchObject({ status: 401, message: "Invalid token" });
    expect(useStore.getState().conn).toBe("unauthorized");
  });

  it("ignores a 401 from a backend that is no longer in use", async () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: "t", conn: "online" });
    fetchMock.mockImplementation(async () => {
      // The user switched backends while the request was in flight.
      useStore.setState({ baseUrl: "http://b", conn: "checking" });
      return jsonResponse({ detail: "Invalid token" }, 401);
    });
    await expect(api("/status")).rejects.toBeInstanceOf(ApiError);
    expect(useStore.getState().conn).toBe("checking");
  });

  it("leaves conn alone on other errors", async () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: null, conn: "online" });
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Cannot use C:\\Windows" }, 400));
    await expect(api("/config", { method: "POST" })).rejects.toMatchObject({
      status: 400,
      message: "Cannot use C:\\Windows",
    });
    expect(useStore.getState().conn).toBe("online");
  });
});

describe("open routes", () => {
  it("never carry the token", async () => {
    const { getHealth, getRoot, checkAuth } = await import("./rlm");
    useStore.setState({ kind: "service", baseUrl: "http://b", token: "abc123", conn: "online" });
    fetchMock.mockImplementation(async () => jsonResponse({ ok: true }));
    await getHealth();
    await getRoot();
    await checkAuth();
    const sent = fetchMock.mock.calls.map(([url, init]) => [
      url,
      new Headers(init?.headers).get("Authorization"),
    ]);
    expect(sent).toEqual([
      ["http://b/health", null],
      ["http://b/", null],
      ["http://b/status", "Bearer abc123"],
    ]);
  });
});

describe("requestHeaders", () => {
  it("keeps the caller's headers and the JSON content type", () => {
    const h = requestHeaders(null, { "X-Test": "1" });
    expect(h.get("X-Test")).toBe("1");
    expect(h.get("Content-Type")).toBe("application/json");
    expect(h.has("Authorization")).toBe(false);
  });

  it("never forwards a caller's Authorization header without a token", () => {
    expect(requestHeaders(null, { Authorization: "Bearer x" }).has("Authorization")).toBe(false);
    expect(requestHeaders("t", { Authorization: "Bearer x" }).get("Authorization")).toBe("Bearer t");
  });
});
