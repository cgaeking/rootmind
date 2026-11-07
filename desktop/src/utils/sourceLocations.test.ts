import { describe, expect, it } from "vitest";
import { formatLocations, groupSources } from "./sourceLocations";

const t = {
  page: (n: string) => `Page ${n}`,
  pages: (ns: string) => `Pages ${ns}`,
  slide: (n: string) => `Slide ${n}`,
  slides: (ns: string) => `Slides ${ns}`,
  sheet: (name: string) => `Sheet: ${name}`,
};
const de = {
  page: (n: string) => `Seite ${n}`,
  pages: (ns: string) => `Seiten ${ns}`,
  slide: (n: string) => `Folie ${n}`,
  slides: (ns: string) => `Folien ${ns}`,
  sheet: (name: string) => `Blatt: ${name}`,
};

describe("groupSources", () => {
  it("merges the entries of one file, in the order the files first appear", () => {
    const groups = groupSources([
      { file_name: "a.pdf", file_path: "C:/d/a.pdf", page: "Page 4" },
      { file_name: "b.pdf", file_path: "C:/d/b.pdf", page: null },
      { file_name: "a.pdf", file_path: "C:/d/a.pdf", page: "Page 3" },
      { file_name: "a.pdf", file_path: "C:/d/a.pdf", page: "Page 4" },
    ]);
    expect(groups.map((g) => [g.source.file_name, g.locations])).toEqual([
      ["a.pdf", ["Page 4", "Page 3"]],
      ["b.pdf", []],
    ]);
  });

  it("keeps files with the same name in different folders apart", () => {
    const groups = groupSources([
      { file_name: "x.pdf", file_path: "C:/1/x.pdf" },
      { file_name: "x.pdf", file_path: "C:/2/x.pdf" },
    ]);
    expect(groups).toHaveLength(2);
  });
});

describe("formatLocations", () => {
  it("lists pages and slides sorted, sheets by name, in the app's language", () => {
    expect(formatLocations([], t)).toBe("");
    expect(formatLocations(["Page 3"], t)).toBe("Page 3");
    expect(formatLocations(["Page 12", "Page 3", "Page 3"], t)).toBe("Pages 3, 12");
    expect(formatLocations(["Slide 2", "Slide 1"], de)).toBe("Folien 1, 2");
    expect(formatLocations(["Sheet: Costs", "Sheet: Plan"], de)).toBe("Blatt: Costs · Blatt: Plan");
    expect(formatLocations(["Page 1", "Chapter 2"], de)).toBe("Seite 1 · Chapter 2");
  });
});
