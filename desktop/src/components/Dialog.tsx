import { useEffect } from "react";
import type { ReactNode } from "react";
import styles from "./Dialog.module.css";

export { styles as dialogStyles };

/**
 * A modal card over a dimmed backdrop. With `onClose`, Escape closes it.
 * `wide` makes room for tables and commands (help dialogs). `icon` stands
 * before the title.
 */
export function Dialog({
  title,
  children,
  actions,
  onClose,
  wide = false,
  icon,
}: {
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
  onClose?: () => void;
  wide?: boolean;
  icon?: ReactNode;
}) {
  useEffect(() => {
    if (!onClose) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className={styles.overlay}>
      <div
        className={wide ? `${styles.card} ${styles.wide}` : styles.card}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <h2 className={styles.title}>
          {icon}
          {title}
        </h2>
        {children}
        {actions && <div className={styles.actions}>{actions}</div>}
      </div>
    </div>
  );
}
