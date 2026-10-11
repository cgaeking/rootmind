// ESLint flat config for the desktop UI. Type checking is done by `tsc`
// (`pnpm typecheck`); this config adds the JS/TS recommended rules and the
// React hooks rules.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import { defineConfig, globalIgnores } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  globalIgnores(["dist/", "src-tauri/"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    rules: {
      // The screens load data from the backend in effects (on mount, on
      // reconnect, on a polling interval), and the loaders set a "loading" or
      // "connection" state first. That is intended here, and there is no data
      // fetching library to move it into.
      "react-hooks/set-state-in-effect": "off",
    },
  },
]);
