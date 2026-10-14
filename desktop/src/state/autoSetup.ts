// Linux: the app sets up and updates the RootMind service on its own.
//
// On Windows the installer sets the service up (one administrator approval)
// and the app offers Set up, Repair and Update service. On Linux there is no
// installer step and no elevation: the service is two systemd user services
// of the account, which the backend that ships with the app sets up
// (`service install`). So the app runs it by itself, once per app run each:
// - "setup" when no service is set up yet (the first start), and
// - "update" when the service runs an older version than the app (after a
//   package update).
// Set up, Repair and Update service stay as the fallback, and a failure is
// shown in the backend dialog and in Settings › RootMind service.

import { serviceUpdate } from "../api/backend";
import type { AppPlatform, ServiceInfo } from "../api/backend";
import { runServiceChange } from "./backendActions";
import { changeMessage, isOlderVersion } from "./service";
import type { ServiceProblem } from "./service";
import { useStore } from "./store";
import type { BackendKind, ConnState } from "./store";

export type AutoAction = "setup" | "update";

export interface AutoInputs {
  platform: AppPlatform | null;
  /** A debug build never changes the installed service on its own. */
  dev: boolean;
  kind: BackendKind | null;
  conn: ConnState;
  problem: ServiceProblem | null;
  info: ServiceInfo | null;
  /** The running service's version (GET /), and the app's. */
  serviceVersion: string | null;
  appVersion: string | null;
  /** What already ran in this app run. */
  attempted: ReadonlySet<AutoAction>;
}

/** What the app does on its own now; null for nothing. Pure (unit-tested). */
export function autoServiceAction(i: AutoInputs): AutoAction | null {
  if (i.platform !== "linux" || i.dev || i.kind !== "service") return null;
  if (
    i.conn === "offline" &&
    i.problem === "notInstalled" &&
    !i.attempted.has("setup") &&
    (i.info?.canElevate ?? true)
  ) {
    return "setup";
  }
  if (
    i.conn === "online" &&
    !!i.serviceVersion &&
    !!i.appVersion &&
    isOlderVersion(i.serviceVersion, i.appVersion) &&
    !i.attempted.has("update")
  ) {
    return "update";
  }
  return null;
}

const attempted = new Set<AutoAction>();
let running = false;

/** Forget what ran (tests only). */
export function resetAutoSetup() {
  attempted.clear();
  running = false;
}

/**
 * Run `action` once per app run and record its outcome in the store
 * (`autoService`): running, then cleared on success or failed with the
 * message. A set-up keeps the backend dialog open meanwhile; an update
 * switches the connection like Update service does.
 */
export async function runAutoAction(action: AutoAction): Promise<void> {
  if (running || attempted.has(action)) return;
  attempted.add(action);
  running = true;
  const s = useStore.getState();
  s.setAutoService({ action, status: "running", message: null });
  try {
    const outcome = await runServiceChange(serviceUpdate, { switching: action === "update" });
    const message = changeMessage(outcome);
    useStore
      .getState()
      .setAutoService(message ? { action, status: "failed", message: message.text } : null);
  } finally {
    running = false;
  }
}

/** Check the inputs and start what is due (called by the hook on every change). */
export function checkAutoSetup(info: ServiceInfo | null): Promise<void> | null {
  const s = useStore.getState();
  const action = autoServiceAction({
    platform: s.appMode?.platform ?? null,
    dev: s.appMode?.dev ?? true,
    kind: s.kind,
    conn: s.conn,
    problem: s.serviceProblem,
    info,
    serviceVersion: s.version,
    appVersion: s.appMode?.version ?? null,
    attempted,
  });
  return action && !running ? runAutoAction(action) : null;
}
