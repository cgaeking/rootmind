import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { BackendModal } from "./BackendModal";
import { useBackend } from "../hooks/useBackend";
import { useStore } from "../state/store";
import { labels } from "../i18n/labels";
import styles from "./AppLayout.module.css";

export function AppLayout() {
  const { conn, recheck } = useBackend();
  const baseUrl = useStore((s) => s.baseUrl);
  const setBaseUrl = useStore((s) => s.setBaseUrl);

  // Mobile nav drawer (hidden on desktop via CSS). Closed on every navigation.
  const [navOpen, setNavOpen] = useState(false);

  // Heartbeat: re-check the backend every 5s so a reconnect (e.g. the user
  // starts the service) is picked up automatically. Paused while a query is in
  // flight so a backend busy answering it isn't misreported as offline.
  useEffect(() => {
    const id = setInterval(() => {
      if (useStore.getState().busy) return;
      void recheck();
    }, 5000);
    return () => clearInterval(id);
  }, [recheck]);

  async function connect(url: string) {
    const clean = url.trim().replace(/\/+$/, "");
    if (clean && clean !== baseUrl) setBaseUrl(clean);
    await recheck();
  }

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
      {conn === "offline" && <BackendModal baseUrl={baseUrl} onConnect={connect} />}
    </div>
  );
}
