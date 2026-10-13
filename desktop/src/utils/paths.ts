// File system paths in the UI. The backend and config.yaml keep paths as they
// are (folders picked in the app are stored with forward slashes, which
// Windows accepts as well); the UI shows them with the native separator.

/** True in a Windows webview (WebView2 reports "Windows NT" in its user agent). */
export function isWindows(): boolean {
  return typeof navigator !== "undefined" && /Windows/i.test(navigator.userAgent);
}

/** A path for display: backslashes on Windows (`C:\Users\…`), unchanged elsewhere. */
export function displayPath(path: string, windows: boolean = isWindows()): string {
  return windows ? path.replace(/\//g, "\\") : path;
}

/**
 * A path the user typed or picked, in the stored form: trimmed, and with
 * forward slashes on Windows. Both separators are accepted as input.
 */
export function storedPath(input: string, windows: boolean = isWindows()): string {
  const path = input.trim();
  return windows ? path.replace(/\\/g, "/") : path;
}

/** Whether two paths name the same location, ignoring the separator on Windows. */
export function samePath(a: string, b: string, windows: boolean = isWindows()): boolean {
  return storedPath(a, windows) === storedPath(b, windows);
}
