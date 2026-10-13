// UI language, switchable at runtime (Settings → Appearance → Language).
//
// `labels` is a live binding: it always points at the label file of the
// current language, so code that reads `labels.x.y` when it runs (render
// functions, event handlers, store actions) gets the current text. Nothing may
// copy a label into a module-level constant.
//
// A language change re-renders the components that subscribe with
// `useLanguage()`: the app layout (sidebar, dialogs) and every screen. The
// screens are rendered by the router from fixed elements, so they do not
// re-render with the layout and need their own subscription.
//
// The choice ("system", "en" or "de") is stored per user in localStorage;
// "system" follows the Windows display language as the webview reports it
// (`navigator.language`): German for any German locale, English otherwise.
//
// Both label files must have exactly the same shape, key for key.

import { create } from "zustand";
import { labels as labelsDe } from "./labels.de";
import { labels as labelsEn } from "./labels.en";

export type Language = "en" | "de";
export type LanguagePref = "system" | Language;

export const LANGUAGE_STORAGE_KEY = "rlm.language";

const LABELS = { en: labelsEn, de: labelsDe } as const;

/** The language for a BCP 47 tag such as `de-AT`: German for `de*`, else English. */
export function languageForTag(tag: string | null | undefined): Language {
  return /^de(-|_|$)/i.test((tag ?? "").trim()) ? "de" : "en";
}

/** The display language the webview reports (WebView2 follows Windows). */
export function systemLanguage(): Language {
  return languageForTag(typeof navigator === "undefined" ? null : navigator.language);
}

/** The language a preference stands for. */
export function resolveLanguage(pref: LanguagePref, system: Language = systemLanguage()): Language {
  return pref === "system" ? system : pref;
}

/** The stored preference; "system" when none (or an unknown value) is stored. */
export function readLanguagePref(): LanguagePref {
  let v: string | null = null;
  try {
    v = localStorage.getItem(LANGUAGE_STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
  return v === "en" || v === "de" || v === "system" ? v : "system";
}

function writeLanguagePref(pref: LanguagePref) {
  try {
    // "system" is the default: store nothing, so a later change of the
    // Windows language is followed.
    if (pref === "system") localStorage.removeItem(LANGUAGE_STORAGE_KEY);
    else localStorage.setItem(LANGUAGE_STORAGE_KEY, pref);
  } catch {
    /* storage unavailable: the choice lasts for this session only */
  }
}

const initialPref = readLanguagePref();
const initialLanguage = resolveLanguage(initialPref);

/** The labels of the current language (see the comment at the top). */
export let labels: typeof labelsEn | typeof labelsDe = LABELS[initialLanguage];

function applyLanguage(language: Language) {
  labels = LABELS[language];
  if (typeof document !== "undefined") document.documentElement.lang = language;
}

// Before the first render, so the app starts in the right language.
applyLanguage(initialLanguage);

interface LanguageState {
  /** The stored choice. */
  pref: LanguagePref;
  /** The language in use. */
  language: Language;
  /** Store the choice and switch every screen to it. */
  setPref: (pref: LanguagePref) => void;
}

export const useLanguageStore = create<LanguageState>()((set) => ({
  pref: initialPref,
  language: initialLanguage,
  setPref: (pref) => {
    writeLanguagePref(pref);
    const language = resolveLanguage(pref);
    applyLanguage(language);
    set({ pref, language });
  },
}));

/** Subscribe a component to language changes; returns the language in use. */
export function useLanguage(): Language {
  return useLanguageStore((s) => s.language);
}

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
