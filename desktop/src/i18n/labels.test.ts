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

describe("the Linux wording", () => {
  it("only replaces texts that exist, with the same parameters", () => {
    for (const labels of [en, de]) {
      const all = new Set(shape(labels).filter((p) => !p.startsWith("linux.")));
      const replaced = shape(labels.linux);
      expect(replaced.length).toBeGreaterThan(10);
      expect(replaced.filter((p) => !all.has(p))).toEqual([]);
    }
  });

  it("is used for Linux and nowhere else", async () => {
    const { labelsFor, overlay } = await import("./labels");
    expect(labelsFor("en", "windows").service.desc).toBe(en.service.desc);
    expect(labelsFor("en", "other").service.desc).toBe(en.service.desc);
    const linux = labelsFor("en", "linux");
    expect(linux.service.desc).toBe(en.linux.service.desc);
    expect(linux.service.desc).toMatch(/systemd user services/);
    // Untouched texts stay, deep inside a replaced section too.
    expect(linux.service.title).toBe(en.service.title);
    expect(linux.service.problems.portBusy).toEqual(en.service.problems.portBusy);
    expect(linux.service.codes.cancelled).toBe(en.service.codes.cancelled);
    expect(labelsFor("de", "linux").chat.openFolderTitle).toBe("Ordner im Dateimanager öffnen");
    expect(labelsFor("en", "linux")).toBe(linux); // built once
    expect(overlay({ a: 1, b: { c: 2, d: 3 } }, { b: { c: 9 }, x: 1 })).toEqual({ a: 1, b: { c: 9, d: 3 } });
  });

  it("no Linux text names Windows, UAC or administrators", () => {
    for (const labels of [en, de]) {
      const texts = JSON.stringify(labels.linux);
      expect(texts).not.toMatch(/Windows|UAC|Administrator|administrator|%ProgramData%/);
    }
  });
});
