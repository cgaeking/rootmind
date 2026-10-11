import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from "react";
import styles from "./ui.module.css";

export function Section({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className={styles.section}>
      <div className={styles.sectionHead}>
        <div>
          <h2 className={styles.sectionTitle}>{title}</h2>
          {description && <p className={styles.sectionDesc}>{description}</p>}
        </div>
        {actions}
      </div>
      <div className={styles.sectionBody}>{children}</div>
    </section>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className={styles.field}>
      <span className={styles.label}>{label}</span>
      {children}
      {hint && <span className={styles.hint}>{hint}</span>}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className, ...rest } = props;
  return <input {...rest} className={`${styles.input} ${className ?? ""}`} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  const { className, ...rest } = props;
  return <select {...rest} className={`${styles.input} ${className ?? ""}`} />;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "default";
};

export function Button({ variant = "default", className, ...props }: ButtonProps) {
  const variantClass = variant === "primary" ? styles.buttonPrimary : "";
  return (
    <button {...props} className={`${styles.button} ${variantClass} ${className ?? ""}`} />
  );
}

export function Badge({
  tone,
  children,
}: {
  tone: "ok" | "err" | "muted";
  children: ReactNode;
}) {
  const toneClass =
    tone === "ok" ? styles.badgeOk : tone === "err" ? styles.badgeErr : styles.badgeMuted;
  return <span className={`${styles.badge} ${toneClass}`}>{children}</span>;
}

export function Banner({
  tone = "info",
  children,
}: {
  tone?: "info" | "ok" | "err";
  children: ReactNode;
}) {
  const toneClass =
    tone === "ok" ? styles.bannerOk : tone === "err" ? styles.bannerErr : styles.bannerInfo;
  return <div className={`${styles.banner} ${toneClass}`}>{children}</div>;
}

export function Row({ children }: { children: ReactNode }) {
  return <div className={styles.row}>{children}</div>;
}
