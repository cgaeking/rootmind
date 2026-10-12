import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Unit and render tests for the UI (`pnpm test`). Tauri commands and the
// backend REST API are mocked in the tests; nothing here starts a process or
// opens a port.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
