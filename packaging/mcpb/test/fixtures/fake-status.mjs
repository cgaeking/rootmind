// Stand-in for `rootmind-backend.exe service status --json` in the tests.
// Reads a JSON control file (argv[2]): {"stdout": ..., "stderr": ..., "exit": N}.
// A non-string "stdout" is printed as JSON, like the real command does.
import { readFileSync } from "node:fs";

const control = JSON.parse(readFileSync(process.argv[2], "utf8"));
if (control.stderr) process.stderr.write(control.stderr);
if (control.stdout !== undefined) {
  process.stdout.write(
    typeof control.stdout === "string" ? control.stdout : JSON.stringify(control.stdout, null, 2),
  );
}
process.exitCode = control.exit ?? 0;
