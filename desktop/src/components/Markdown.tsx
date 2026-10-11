import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./Markdown.module.css";

// Links in model output are untrusted. Only absolute web and mail links are
// opened, and always in the system browser; the app's webview never navigates
// or opens a popup. The opener capability enforces the same URL schemes.
const EXTERNAL_URL = /^(https?|mailto):/i;
const IN_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

async function openExternal(url: string) {
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

const components: Components = {
  a({ href, children }) {
    return (
      <a
        href={href}
        onClick={(e) => {
          e.preventDefault();
          if (href) void openExternal(href);
        }}
        // Middle-click would otherwise open the link in a new webview window.
        onAuxClick={(e) => e.preventDefault()}
      >
        {children}
      </a>
    );
  },
};

/**
 * Renders untrusted LLM output as React elements (GFM: tables, lists, code).
 * Raw HTML is disabled (react-markdown default), and remote images are blocked
 * by the CSP in tauri.conf.json.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className={styles.md}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
