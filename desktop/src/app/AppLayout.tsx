import { useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { BackendModal } from "./BackendModal";
import { FirstRunChoice } from "./FirstRunChoice";
import { useBackendHeartbeat } from "../hooks/useBackend";
import { useBackendBootstrap } from "../hooks/useBackendBootstrap";
import { useStore } from "../state/store";
import { labels } from "../i18n/labels";
import styles from "./AppLayout.module.css";

export function AppLayout() {
  // Choose the backend once at start, then keep checking it.
  const firstRun = useBackendBootstrap();
  useBackendHeartbeat();
  const conn = useStore((s) => s.conn);
  const kind = useStore((s) => s.kind);

  // Mobile nav drawer (hidden on desktop via CSS). Closed on every navigation.
  const [navOpen, setNavOpen] = useState(false);

  // Starting, switching and a built-in or service backend that answered 401
  // (it gets a fresh token on the next check) do not open the dialog.
  const showBackendModal = conn === "offline" || (conn === "unauthorized" && kind === "custom");

  return (
    <div className={styles.layout}>
      {navOpen && <div className={styles.backdrop} onClick={() => setNavOpen(false)} />}
      <Sidebar open={navOpen} onNavigate={() => setNavOpen(false)} />
      <div className={styles.main}>
        <header className={styles.topbar}>
          <button
            type="button"
            className={styles.menuBtn}
            aria-label={labels.common.menuOpen}
            aria-expanded={navOpen}
            onClick={() => setNavOpen((o) => !o)}
          >
            ☰
          </button>
          <span className={styles.topbarTitle}>RootMind</span>
        </header>
        <main className={styles.content}>
          <Outlet />
        </main>
      </div>
      {firstRun.url && firstRun.answer && (
        <FirstRunChoice url={firstRun.url} onAnswer={firstRun.answer} />
      )}
      {showBackendModal && <BackendModal />}
    </div>
  );
}
