#!/usr/bin/env node
// npm-style entry point for MCUM. Delegates to the Python installer so the
// whole stack (pip + npm + PostgreSQL schema + model + agent registration)
// installs from one command:  npx mcum bootstrap   /   npm run setup
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const mcumRoot = path.resolve(__dirname, "..");
const installer = path.join(mcumRoot, "mcum_install.py");

// Pick a Python launcher: `py` on Windows, `python3` elsewhere (overridable).
const pyCandidates = process.env.MCUM_PYTHON
  ? [process.env.MCUM_PYTHON]
  : process.platform === "win32"
    ? ["py", "python"]
    : ["python3", "python"];

const argv = process.argv.slice(2);
// Default to `bootstrap` when invoked with no subcommand (npm run setup).
const args = argv.length ? argv : ["bootstrap"];

function tryRun(index) {
  if (index >= pyCandidates.length) {
    console.error("MCUM: no Python interpreter found (tried: " + pyCandidates.join(", ") + ").");
    process.exit(1);
  }
  const child = spawn(pyCandidates[index], [installer, ...args], { stdio: "inherit" });
  child.on("error", () => tryRun(index + 1));
  child.on("exit", (code) => process.exit(code ?? 0));
}

tryRun(0);
