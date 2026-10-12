import { beforeEach, describe, expect, it } from "vitest";
import { isCurrentBackend } from "./useLiveBackend";
import { useStore } from "../state/store";
import { resetStore } from "../test/helpers";

beforeEach(resetStore);

describe("isCurrentBackend (the save guard)", () => {
  it("allows saving to the backend the data came from", () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", conn: "online" });
    expect(isCurrentBackend("http://a")).toBe(true);
  });

  it("refuses once another backend is in use", () => {
    useStore.setState({ kind: "custom", baseUrl: "http://b", conn: "online" });
    expect(isCurrentBackend("http://a")).toBe(false);
  });

  it("refuses when nothing was loaded", () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", conn: "online" });
    expect(isCurrentBackend(null)).toBe(false);
  });
});
