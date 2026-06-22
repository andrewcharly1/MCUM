#!/usr/bin/env node
/**
 * MCUM embedded PostgreSQL manager (Windows / Linux / macOS).
 *
 * Runs a PORTABLE PostgreSQL using the bundled binaries (from @embedded-postgres
 * platform package) via **pg_ctl**, so the server is a real background process
 * that PERSISTS after this script (and the agent) exit — and starts with NO
 * visible window. The cluster is initialised as UTF8/C so the MCUM schema and
 * Spanish data store cleanly.
 *
 *   node db/embedded_pg.mjs start    # initdb (first run) + pg_ctl start (detached)
 *   node db/embedded_pg.mjs stop
 *   node db/embedded_pg.mjs status
 *
 * Idempotent: `start` returns immediately if the port already accepts connections.
 */
import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const mcumRoot = path.resolve(__dirname, "..");

const PORT = Number(process.env.MCUM_DB_PORT || 5432);
const PASSWORD = process.env.MCUM_DB_PASSWORD || "admin1234";

function dataDir() {
  if (process.env.MCUM_PG_DATA_DIR) return process.env.MCUM_PG_DATA_DIR;
  const home = os.homedir();
  if (process.platform === "win32")
    return path.join(process.env.LOCALAPPDATA || path.join(home, "AppData", "Local"), "mcum", "pgdata");
  if (process.platform === "darwin")
    return path.join(home, "Library", "Application Support", "mcum", "pgdata");
  return path.join(process.env.XDG_DATA_HOME || path.join(home, ".local", "share"), "mcum", "pgdata");
}

function binDir() {
  // node_modules/@embedded-postgres/<platform>/native/bin
  const base = path.join(mcumRoot, "node_modules", "@embedded-postgres");
  if (!fs.existsSync(base)) return null;
  for (const pkg of fs.readdirSync(base)) {
    const bin = path.join(base, pkg, "native", "bin");
    if (fs.existsSync(bin)) return bin;
  }
  return null;
}

function exe(bin, name) {
  return path.join(bin, process.platform === "win32" ? `${name}.exe` : name);
}

function portOpen(port) {
  return new Promise((resolve) => {
    const s = new net.Socket();
    s.setTimeout(800);
    s.once("connect", () => { s.destroy(); resolve(true); });
    s.once("timeout", () => { s.destroy(); resolve(false); });
    s.once("error", () => resolve(false));
    s.connect(port, "127.0.0.1");
  });
}

const RUN = { stdio: "ignore", windowsHide: true };  // never pop a console window

async function start() {
  if (await portOpen(PORT)) {
    console.log(`Embedded PostgreSQL already running on port ${PORT}.`);
    return;
  }
  const bin = binDir();
  if (!bin) {
    console.error("Embedded PostgreSQL binaries not found (run npm install).");
    process.exit(2);
  }
  const dir = dataDir();
  fs.mkdirSync(path.dirname(dir), { recursive: true });

  const fresh = !fs.existsSync(path.join(dir, "PG_VERSION"));
  if (fresh) {
    console.log(`Initialising embedded PostgreSQL at ${dir} (UTF8) ...`);
    const pwfile = path.join(os.tmpdir(), `mcum_pw_${process.pid}.txt`);
    fs.writeFileSync(pwfile, PASSWORD, "utf8");
    try {
      execFileSync(exe(bin, "initdb"), [
        "-D", dir, "-U", "postgres", "--auth=scram-sha-256",
        "--pwfile=" + pwfile, "--encoding=UTF8", "--locale=C",
      ], RUN);
    } finally {
      try { fs.unlinkSync(pwfile); } catch {}
    }
  }
  const log = path.join(dir, "mcum-server.log");
  console.log(`Starting embedded PostgreSQL on port ${PORT} (persistent, no window) ...`);
  // pg_ctl start launches postgres as a detached background process and (-w)
  // waits until it accepts connections. It survives this script exiting.
  execFileSync(exe(bin, "pg_ctl"), [
    "start", "-D", dir, "-l", log, "-w", "-t", "60",
    "-o", `-p ${PORT} -c listen_addresses=127.0.0.1`,
  ], RUN);
  console.log(`READY. postgresql://postgres:***@localhost:${PORT}/postgres`);
}

function stop() {
  const bin = binDir();
  if (!bin) { console.log("binaries not found"); return; }
  spawnSync(exe(bin, "pg_ctl"), ["stop", "-D", dataDir(), "-m", "fast"], RUN);
  console.log("Embedded PostgreSQL stopped.");
}

async function status() {
  console.log(JSON.stringify({
    data_dir: dataDir(),
    initialised: fs.existsSync(path.join(dataDir(), "PG_VERSION")),
    port: PORT,
    running: await portOpen(PORT),
  }, null, 2));
}

const cmd = process.argv[2] || "status";
const actions = { start, stop, status };
if (!actions[cmd]) {
  console.error(`Unknown command '${cmd}'. Use: start | stop | status`);
  process.exit(2);
}
Promise.resolve(actions[cmd]()).catch((err) => {
  console.error(`embedded_pg ${cmd} failed:`, err?.message || err);
  process.exit(1);
});
