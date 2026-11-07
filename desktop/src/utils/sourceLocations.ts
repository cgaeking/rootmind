import type { QuerySource } from "../types/api";

/** The labels for locations (labels.chat.locations). */
export interface LocationLabels {
  page: (n: string) => string;
  pages: (ns: string) => string;
  slide: (n: string) => string;
  slides: (ns: string) => string;
  sheet: (name: string) => string;
}

/** A file of an answer's sources with every location it was cited at. */
export interface GroupedSource {
  source: QuerySource;
  locations: string[];
}

/**
 * One entry per file, in the order the files first appear, with the
 * locations of all its entries ("Page 3", "Slide 2", "Sheet: Costs", as the
 * backend names them). The first entry's fields (snippet, id) are kept.
 */
export function groupSources(sources: QuerySource[]): GroupedSource[] {
  const byFile = new Map<string, GroupedSource>();
  for (const s of sources) {
    const key = s.file_path || s.file_name;
    let g = byFile.get(key);
    if (!g) {
      g = { source: s, locations: [] };
      byFile.set(key, g);
    }
    if (s.page && !g.locations.includes(s.page)) g.locations.push(s.page);
  }
  return [...byFile.values()];
}

const PAGE = /^Page (\d+)$/;
const SLIDE = /^Slide (\d+)$/;
const SHEET = /^Sheet: (.+)$/;

/**
 * The locations of one file for display, in the app's language: pages and
 * slides as sorted number lists ("Pages 3, 4"), sheets by name, anything else
 * as the backend sent it. Empty when no location is known.
 */
export function formatLocations(locations: string[], t: LocationLabels): string {
  const pages: number[] = [];
  const slides: number[] = [];
  const rest: string[] = [];
  for (const loc of locations) {
    let m: RegExpMatchArray | null;
    if ((m = loc.match(PAGE))) pages.push(Number(m[1]));
    else if ((m = loc.match(SLIDE))) slides.push(Number(m[1]));
    else if ((m = loc.match(SHEET))) rest.push(t.sheet(m[1]));
    else rest.push(loc);
  }
  const list = (ns: number[]) => [...new Set(ns)].sort((a, b) => a - b).join(", ");
  const parts: string[] = [];
  if (pages.length) parts.push(pages.length === 1 ? t.page(String(pages[0])) : t.pages(list(pages)));
  if (slides.length) parts.push(slides.length === 1 ? t.slide(String(slides[0])) : t.slides(list(slides)));
  return [...parts, ...rest].join(" · ");
}
