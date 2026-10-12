// Build-time language switch, not a runtime toggle. Every string is static per
// build, so no component needs to re-render on a language change and nothing
// here has to be reactive.
//
// English is the default. `VITE_APP_LANG=de` builds the German variant:
//   bash:        VITE_APP_LANG=de pnpm tauri build
//   PowerShell:  $env:VITE_APP_LANG='de'; pnpm tauri build
//
// Both label files must have exactly the same shape, key for key.

import { labels as labelsDe } from "./labels.de";
import { labels as labelsEn } from "./labels.en";

export const labels = import.meta.env.VITE_APP_LANG === "de" ? labelsDe : labelsEn;

/** The shape of a label file: texts are strings, label functions keep their parameters. */
type LabelShape<T> = T extends string
  ? string
  : T extends (...args: infer A) => string
    ? (...args: A) => string
    : { readonly [K in keyof T]: LabelShape<T[K]> };

type Expect<T extends true> = T;

/**
 * Compile-time check (`pnpm typecheck`) that both files have the same keys:
 * each one has to fit the other's shape.
 */
export type LabelFilesMatch = [
  Expect<typeof labelsDe extends LabelShape<typeof labelsEn> ? true : false>,
  Expect<typeof labelsEn extends LabelShape<typeof labelsDe> ? true : false>,
];
