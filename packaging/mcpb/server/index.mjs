// Entry point of the RootMind MCP Bundle; see bridge.mjs.
import { createBridge } from "./bridge.mjs";

await createBridge().run();
