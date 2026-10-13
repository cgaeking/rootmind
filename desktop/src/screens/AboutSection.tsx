import { labels } from "../i18n/labels";
import { useStore } from "../state/store";
import { openExternal } from "../utils/openExternal";
import { Section } from "../components/ui";
import styles from "./AboutSection.module.css";

export const REPOSITORY_URL = "https://github.com/cgaeking/rootmind";
export const RELEASES_URL = `${REPOSITORY_URL}/releases`;
export const ISSUES_URL = `${REPOSITORY_URL}/issues`;

const AUTHOR = "Christian Gäking";
const LICENSE = "MIT";

/** A link that opens in the system browser, never in the app's webview. */
function ExternalLink({ href, children }: { href: string; children: string }) {
  return (
    <a
      href={href}
      onClick={(e) => {
        e.preventDefault();
        void openExternal(href);
      }}
      // Middle-click would otherwise open the link in a new webview window.
      onAuxClick={(e) => e.preventDefault()}
    >
      {children}
    </a>
  );
}

/**
 * Settings → About: the app and backend versions as they run (they differ
 * when the RootMind service runs an older build), author, license, links.
 */
export function AboutSection() {
  // The app reports its version via app_mode (null in the browser preview);
  // the backend's comes from its GET / (null while not connected).
  const appVersion = useStore((s) => s.appMode?.version ?? null);
  const backendVersion = useStore((s) => s.version);

  return (
    <Section title={labels.settings.aboutTitle} description={labels.settings.aboutDesc}>
      <dl className={styles.facts}>
        <dt>{labels.settings.aboutAppVersion}</dt>
        <dd>
          {labels.app} {appVersion ?? labels.settings.aboutUnknown}
        </dd>
        <dt>{labels.settings.aboutBackendVersion}</dt>
        <dd>{backendVersion ?? labels.settings.aboutUnknown}</dd>
        <dt>{labels.settings.aboutAuthor}</dt>
        <dd>{AUTHOR}</dd>
        <dt>{labels.settings.aboutLicense}</dt>
        <dd>{LICENSE}</dd>
      </dl>
      <div className={styles.links}>
        <ExternalLink href={REPOSITORY_URL}>{labels.settings.aboutRepository}</ExternalLink>
        <ExternalLink href={RELEASES_URL}>{labels.settings.aboutReleases}</ExternalLink>
        <ExternalLink href={ISSUES_URL}>{labels.settings.aboutIssues}</ExternalLink>
      </div>
    </Section>
  );
}
