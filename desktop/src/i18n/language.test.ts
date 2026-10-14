import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as i18n from "./labels";
import { labels as de } from "./labels.de";
import { labels as en } from "./labels.en";

const { LANGUAGE_STORAGE_KEY, languageForTag, readLanguagePref, resolveLanguage, useLanguageStore } =
  i18n;

/**
 * Load labels.ts afresh, as on an app start with this display language. The
 * label files are loaded afresh too, so compare by their locale.
 */
async function startWith(language: string) {
  vi.spyOn(navigator, "language", "get").mockReturnValue(language);
  vi.resetModules();
  return import("./labels");
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  useLanguageStore.getState().setPref("en");
  localStorage.clear();
});

describe("language detection", () => {
  it("picks German for every German locale and English for anything else", () => {
    for (const tag of ["de", "de-DE", "de-AT", "de-CH", "DE-de", "de_DE"]) {
      expect(languageForTag(tag)).toBe("de");
    }
    for (const tag of ["en-US", "en-GB", "fr-FR", "nl-NL", "dev", "", null, undefined]) {
      expect(languageForTag(tag)).toBe("en");
    }
  });

  it("resolves the preference, with System following the display language", () => {
    expect(resolveLanguage("system", "de")).toBe("de");
    expect(resolveLanguage("system", "en")).toBe("en");
    expect(resolveLanguage("en", "de")).toBe("en");
    expect(resolveLanguage("de", "en")).toBe("de");
  });

  it("starts in German on a German Windows", async () => {
    const fresh = await startWith("de-DE");
    expect(fresh.labels.locale).toBe("de-DE");
    expect(fresh.useLanguageStore.getState()).toMatchObject({ pref: "system", language: "de" });
    expect(document.documentElement.lang).toBe("de");
  });

  it("starts in English on any other Windows language", async () => {
    const fresh = await startWith("fr-FR");
    expect(fresh.labels.locale).toBe("en-US");
    expect(fresh.useLanguageStore.getState().language).toBe("en");
  });
});

describe("stored language", () => {
  it("is System when nothing or an unknown value is stored", () => {
    expect(readLanguagePref()).toBe("system");
    localStorage.setItem(LANGUAGE_STORAGE_KEY, "fr");
    expect(readLanguagePref()).toBe("system");
  });

  it("wins over the display language at start", async () => {
    localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    const fresh = await startWith("de-DE");
    expect(fresh.labels.locale).toBe("en-US");
    expect(fresh.useLanguageStore.getState()).toMatchObject({ pref: "en", language: "en" });
  });

  it("is saved and switches the labels at once", () => {
    useLanguageStore.getState().setPref("de");
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("de");
    expect(readLanguagePref()).toBe("de");
    // The live binding every module reads now points at the German labels.
    expect(i18n.labels).toBe(de);
    expect(useLanguageStore.getState().language).toBe("de");
    expect(document.documentElement.lang).toBe("de");

    useLanguageStore.getState().setPref("en");
    expect(i18n.labels).toBe(en);
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en");
  });

  it("stores nothing for System, so a later Windows language change is followed", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("de-DE");
    useLanguageStore.getState().setPref("system");
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBeNull();
    expect(useLanguageStore.getState()).toMatchObject({ pref: "system", language: "de" });
    expect(i18n.labels).toBe(de);
  });
});

describe("platform wording", () => {
  it("switches the live labels to the Linux texts and back", () => {
    useLanguageStore.getState().setPref("en");
    useLanguageStore.getState().setPlatform("linux");
    expect(i18n.labels.service.desc).toBe(en.linux.service.desc);
    useLanguageStore.getState().setPref("de");
    expect(i18n.labels.service.desc).toBe(de.linux.service.desc);
    useLanguageStore.getState().setPlatform("windows");
    expect(i18n.labels.service.desc).toBe(de.service.desc);
  });
});
