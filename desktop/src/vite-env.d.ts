/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Build-time UI language (default: English). See src/i18n/labels.ts. */
  readonly VITE_APP_LANG?: "de" | "en";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
