// Node front end: serves the UI, stores battery presets, and proxies
// /api + /ws to the Python backend that owns the Bluetooth link.
import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || "127.0.0.1";
const BACKEND = process.env.BACKEND_URL || "http://127.0.0.1:8765";
const PRESETS_FILE = process.env.PRESETS_FILE || path.join(here, "data", "presets.json");

const app = express();

// ---- presets (saved battery/job profiles, shared by everyone using this laptop)
async function readPresets() {
  try {
    return JSON.parse(await fs.readFile(PRESETS_FILE, "utf8"));
  } catch {
    return [];
  }
}
async function writePresets(list) {
  await fs.mkdir(path.dirname(PRESETS_FILE), { recursive: true });
  const tmp = PRESETS_FILE + ".tmp";
  await fs.writeFile(tmp, JSON.stringify(list, null, 2));
  await fs.rename(tmp, PRESETS_FILE);
}
const PRESET_FIELDS = ["task", "chemistry", "cells", "current_ma", "cutoff_mv", "capacity_limit_mah", "pack_mah"];

app.get("/presets", async (_req, res) => res.json(await readPresets()));

app.put("/presets/:name", express.json({ limit: "16kb" }), async (req, res) => {
  const name = String(req.params.name).trim().slice(0, 60);
  if (!name) return res.status(400).json({ detail: "name required" });
  const job = {};
  for (const k of PRESET_FIELDS) if (k in req.body) job[k] = req.body[k];
  const list = (await readPresets()).filter((p) => p.name !== name);
  list.push({ name, ...job });
  list.sort((a, b) => a.name.localeCompare(b.name));
  await writePresets(list);
  res.json(list);
});

app.delete("/presets/:name", async (req, res) => {
  const list = (await readPresets()).filter((p) => p.name !== req.params.name);
  await writePresets(list);
  res.json(list);
});

// ---- backend proxy
const proxy = createProxyMiddleware({
  target: BACKEND,
  changeOrigin: true,
  ws: true,
  pathFilter: ["/api", "/ws"],
  on: {
    error: (_err, _req, res) => {
      if (res && typeof res.writeHead === "function" && !res.headersSent) {
        res.writeHead(502, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ detail: `Python backend not reachable at ${BACKEND}` }));
      }
    },
  },
});
app.use(proxy);

app.use(express.static(path.join(here, "public")));

const server = app.listen(PORT, HOST, () => {
  console.log(`Air8 UI:  http://${HOST === "0.0.0.0" ? "localhost" : HOST}:${PORT}`);
  console.log(`Backend:  ${BACKEND}`);
});
server.on("upgrade", proxy.upgrade);
