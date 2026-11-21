// Starts the Python backend and the Node front end together.
//   npm start          -> real charger over Bluetooth
//   npm run sim        -> simulator, no hardware needed
// Extra args are passed to the backend (e.g. `npm start -- --allow-raw`).
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const backendDir = path.join(here, "..", "backend");
const backendArgs = process.argv.slice(2);
const isWin = process.platform === "win32";

const procs = [];
function run(name, cmd, args, opts) {
  const p = spawn(cmd, args, { stdio: "inherit", shell: isWin, ...opts });
  p.on("exit", (code) => {
    console.log(`[${name}] exited (${code}). Shutting down.`);
    for (const other of procs) if (other !== p) other.kill();
    process.exit(code ?? 0);
  });
  p.on("error", (e) => {
    console.error(`[${name}] failed to start: ${e.message}`);
    if (name === "backend") console.error("Is uv installed? https://docs.astral.sh/uv/  (or run the backend yourself and use `npm run frontend`)");
    process.exit(1);
  });
  procs.push(p);
}

run("backend", "uv", ["run", "air8-backend", ...backendArgs], { cwd: backendDir });
run("frontend", process.execPath, [path.join(here, "server.js")], { cwd: here });

for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => procs.forEach((p) => p.kill()));
