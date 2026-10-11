import styles from "./ChatLoader.module.css";

/**
 * Waiting animation for the chat while the LLM responds. An equalizer of
 * pulsing bars in the spirit of the provided Lottie bar motif; uses the accent
 * color so it reads on both light and dark themes.
 */
export function ChatLoader({ label = "Denkt nach …" }: { label?: string }) {
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
