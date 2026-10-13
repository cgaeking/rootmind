// Opening web and mail links outside the app.

const EXTERNAL_URL = /^(https?|mailto):/i;
const IN_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/**
 * Open an absolute web or mail link in the system browser; anything else is
 * ignored. The app's webview never navigates or opens a popup, and the opener
 * capability (opener:default) allows the same URL schemes.
 */
export async function openExternal(url: string) {
  if (!EXTERNAL_URL.test(url)) return;
  try {
    if (IN_TAURI) {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(url);
    } else {
      // Dev preview in a plain browser, where the Tauri plugin is absent.
      window.open(url, "_blank", "noopener,noreferrer");
    }
  } catch (e) {
    console.warn("Could not open link:", e);
  }
}
