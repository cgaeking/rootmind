// The bridge with its real stdio and stderr log, but with the fake status
// command (argv[2]: its control file) instead of rootmind-backend.exe.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createBridge } from "../../server/bridge.mjs";

const fakeStatus = path.join(path.dirname(fileURLToPath(import.meta.url)), "fake-status.mjs");
await createBridge({
  statusCommand: { file: process.execPath, args: [fakeStatus, process.argv[2]] },
}).run();
