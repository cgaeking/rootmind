import { describe, expect, it } from "vitest";
import { labels as en } from "./labels.en";
import { labels as de } from "./labels.de";

/** Every key path, with "()" and the parameter count for label functions. */
function shape(value: unknown, path = ""): string[] {
  if (typeof value === "function") return [`${path}(${value.length})`];
  if (typeof value === "object" && value !== null) {
    return Object.entries(value).flatMap(([k, v]) => shape(v, path ? `${path}.${k}` : k));
  }
  return [path];
}

describe("labels", () => {
  it("has the same keys in English and German", () => {
    expect(shape(de).sort()).toEqual(shape(en).sort());
  });

  it("has no empty texts", () => {
    for (const labels of [en, de]) {
      const empty = shape(labels).filter((p) => {
        const v = p
          .replace(/\(\d+\)$/, "")
          .split(".")
          .reduce<unknown>((o, k) => (o as Record<string, unknown>)[k], labels);
        return typeof v === "string" && v.trim() === "";
      });
      expect(empty).toEqual([]);
    }
  });
});
