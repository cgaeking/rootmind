import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { openExternal } from "../utils/openExternal";
import styles from "./Markdown.module.css";

// Links in model output are untrusted. Only absolute web and mail links are
// opened, and always in the system browser (see openExternal); the app's
// webview never navigates or opens a popup.

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
