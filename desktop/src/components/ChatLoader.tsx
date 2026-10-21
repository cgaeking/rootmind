import { labels } from "../i18n/labels";
import styles from "./ChatLoader.module.css";

/**
 * Waiting animation for the chat while the LLM responds. An equalizer of
 * pulsing bars in the spirit of the provided Lottie bar motif; uses the accent
 * color so it reads on both light and dark themes. The default text follows
 * the UI language; the default is evaluated on every render, so it reads the
 * live `labels` binding.
 */
export function ChatLoader({ label = labels.chat.thinking }: { label?: string }) {
  return (
    <div className={styles.loader} role="status" aria-label={label}>
      <svg className={styles.bars} viewBox="0 0 100 44" width="46" height="20" aria-hidden="true">
        <rect className={styles.bar} x="6" y="7" width="14" height="30" rx="5" />
        <rect className={styles.bar} x="28" y="7" width="14" height="30" rx="5" />
        <rect className={styles.bar} x="50" y="7" width="14" height="30" rx="5" />
        <rect className={styles.bar} x="72" y="7" width="14" height="30" rx="5" />
      </svg>
      {label && <span className={styles.label}>{label}</span>}
    </div>
  );
}
