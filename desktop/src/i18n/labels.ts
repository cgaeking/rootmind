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
