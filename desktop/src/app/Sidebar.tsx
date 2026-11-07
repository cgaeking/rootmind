import { useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { labels } from "../i18n/labels";
import { useChatStore } from "../state/chatStore";
import { cancelRun, useRuns } from "../state/runs";
import { AboutDialog } from "./AboutDialog";
import { NavIcon } from "./navIcons";
import type { NavIconName } from "./navIcons";
import styles from "./Sidebar.module.css";

// The label is looked up when rendering, so it follows a language change.
const navItems: { to: string; label: keyof typeof labels.nav; icon: NavIconName }[] = [
  { to: "/chat", label: "chat", icon: "chat" },
  { to: "/folders", label: "folders", icon: "folder" },
  { to: "/status", label: "status", icon: "chart" },
  { to: "/settings", label: "settings", icon: "settings" },
];

export function Sidebar({
  open = false,
  onNavigate,
}: {
  open?: boolean;
  onNavigate?: () => void;
}) {
  const navigate = useNavigate();
  const list = useChatStore((s) => s.list);
  const active = useChatStore((s) => s.active);
  const refresh = useChatStore((s) => s.refresh);
  const newChat = useChatStore((s) => s.newChat);
  const openConv = useChatStore((s) => s.open);
  const remove = useChatStore((s) => s.remove);
  // Conversations with a request in flight, and those answered while not on screen.
  const runs = useRuns((s) => s.runs);
  const unread = useRuns((s) => s.unread);
  const markRead = useRuns((s) => s.markRead);
  const [about, setAbout] = useState(false);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function handleNew() {
    newChat();
    navigate("/chat");
    onNavigate?.();
  }

  function handleOpen(id: string) {
    markRead(id);
    void openConv(id);
    navigate("/chat");
    onNavigate?.();
  }

  return (
    <aside className={`${styles.sidebar} ${open ? styles.open : ""}`}>
      <button className={styles.newChat} onClick={handleNew}>
        {labels.sidebar.newChat}
      </button>

      <div className={styles.convList}>
        {list.length === 0 && <p className={styles.emptyList}>{labels.sidebar.noConversations}</p>}
        {list.map((c) => (
          <div
            key={c.id}
            className={`${styles.convItem} ${active?.id === c.id ? styles.convActive : ""}`}
            onClick={() => handleOpen(c.id)}
            title={c.title}
          >
            <span className={styles.convTitle}>{c.title}</span>
            {runs[c.id] ? (
              <span
                className={styles.convRunning}
                role="img"
                aria-label={labels.sidebar.running}
                title={labels.sidebar.running}
              />
            ) : (
              unread[c.id] && (
                <span
                  className={styles.convUnread}
                  role="img"
                  aria-label={labels.sidebar.unread}
                  title={labels.sidebar.unread}
                />
              )
            )}
            <button
              className={styles.convDelete}
              title={labels.sidebar.deleteConversation}
              aria-label={labels.sidebar.deleteConversation}
              onClick={(e) => {
                e.stopPropagation();
                cancelRun(c.id);
                markRead(c.id);
                void remove(c.id);
              }}
            >
              ✕
            </button>
          </div>
        ))}
      </div>

      <nav className={styles.nav}>
        {navItems.map((it) => (
          <NavLink
            key={it.to}
            to={it.to}
            onClick={() => onNavigate?.()}
            className={({ isActive }) =>
              isActive ? `${styles.link} ${styles.active}` : styles.link
            }
          >
            <NavIcon name={it.icon} size={20} className={styles.linkIcon} />
            <span>{labels.nav[it.label]}</span>
          </NavLink>
        ))}
        <button
          type="button"
          className={styles.link}
          onClick={() => {
            setAbout(true);
            onNavigate?.();
          }}
        >
          <NavIcon name="info" size={20} className={styles.linkIcon} />
          <span>{labels.nav.about}</span>
        </button>
      </nav>
      {about && <AboutDialog onClose={() => setAbout(false)} />}
    </aside>
  );
}
