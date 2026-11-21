// BILLEE Air8 charger UI. Talks to the Node server, which proxies /api and /ws
// to the Python Bluetooth backend.

const $ = (id) => document.getElementById(id);
const state = {
  limits: null,
  connected: false,
  simulated: false,
  allowRaw: false,
  valid: false,
  history: [], // {t, v, a}
  lastElectric: null,
  lastWork: null,
};
const HISTORY_MAX = 1800; // ~30 min at 1 Hz

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || res.statusText);
  return data;
}
const fmt = (n, d = 2) => (n == null || Number.isNaN(n) ? "-" : Number(n).toFixed(d));
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kids) e.append(k);
  return e;
};
function fmtDuration(ms) {
  if (ms == null) return "-";
  const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
function log(line, kind = "log") {
  if ((kind === "rx" || kind === "tx") && !$("show-hex").checked) return;
  const pre = $("log");
  const ts = new Date().toLocaleTimeString();
  pre.textContent += `${ts}  ${kind === "log" ? "" : kind.toUpperCase() + " "}${line}\n`;
  if (pre.textContent.length > 200_000) pre.textContent = pre.textContent.slice(-150_000);
  pre.scrollTop = pre.scrollHeight;
}

// ---------------------------------------------------------------- job form
function task() { return document.querySelector('input[name="task"]:checked').value; }
function chem() { return state.limits.chemistries[$("chemistry").value]; }
function isDelta() { return task() === "charge" && chem().delta_peak; }

function cutoffRange() {
  const c = chem();
  switch (task()) {
    case "charge": return c.charge;
    case "storage": return [c.discharge[1], c.charge[1], c.storage_mv];
    case "discharge": return c.discharge;
    default: return [0, c.discharge[1], 0];
  }
}

function syncFormToChemistry(resetCutoff = true) {
  const c = chem();
  const cellsSel = $("cells");
  const prev = cellsSel.value;
  cellsSel.replaceChildren(el("option", { value: "0", text: "Auto" }));
  for (let i = 1; i <= c.max_cells; i++) cellsSel.append(el("option", { value: String(i), text: `${i}S` }));
  cellsSel.value = Number(prev) <= c.max_cells ? prev : "0";

  document.querySelector('input[name="task"][value="storage"]').disabled = !c.storage;
  if (task() === "storage" && !c.storage) document.querySelector('input[name="task"][value="charge"]').checked = true;

  const [lo, hi, dflt] = cutoffRange();
  const cut = $("cutoff");
  if (isDelta()) {
    $("cutoff-label").textContent = "Delta-peak (mV)";
    cut.step = "1"; cut.min = lo; cut.max = hi;
    if (resetCutoff) cut.value = dflt;
    $("cutoff-hint").textContent = `${lo}-${hi} mV`;
  } else {
    $("cutoff-label").textContent = task() === "charge" ? "Charge to (V/cell)" : task() === "storage" ? "Storage (V/cell)" : "Discharge to (V/cell)";
    cut.step = "0.01"; cut.min = lo / 1000; cut.max = hi / 1000;
    if (resetCutoff) cut.value = (dflt / 1000).toFixed(2);
    $("cutoff-hint").textContent = `${(lo / 1000).toFixed(2)}-${(hi / 1000).toFixed(2)} V`;
  }
  $("destroy-confirm-row").hidden = task() !== "destroy";
  updateCurrentHint();
  scheduleValidate();
}

function updateCurrentHint() {
  const pack = Number($("pack_mah").value);
  const a = Number($("current_a").value);
  $("current-hint").textContent = pack > 0 && a > 0 ? `= ${(a * 1000 / pack).toFixed(2)}C` : "max 20 A";
}

function applyCRate() {
  const pack = Number($("pack_mah").value), c = Number($("crate").value);
  if (pack > 0 && c > 0) {
    $("current_a").value = Math.min(20, Math.max(0.1, (pack * c) / 1000)).toFixed(1);
  }
  updateCurrentHint();
  scheduleValidate();
}

function jobBody() {
  const cut = Number($("cutoff").value);
  return {
    task: task(),
    chemistry: $("chemistry").value,
    cells: Number($("cells").value),
    current_ma: Math.round(Number($("current_a").value) * 1000),
    cutoff_mv: isDelta() ? Math.round(cut) : Math.round(cut * 1000),
    capacity_limit_mah: Math.max(0, Math.round(Number($("cap_limit").value) || 0)),
    confirm_destroy: $("destroy-confirm").checked,
  };
}

let validateTimer = null;
function scheduleValidate() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validate, 150);
}
async function validate() {
  const box = $("job-messages");
  box.replaceChildren();
  try {
    const r = await api("/api/job/validate", { method: "POST", body: jobBody() });
    state.valid = true;
    $("packet-preview").textContent = r.packet;
    for (const w of r.warnings) box.append(el("div", { class: "msg warn", text: w }));
  } catch (e) {
    state.valid = false;
    $("packet-preview").textContent = "-";
    box.append(el("div", { class: "msg err", text: e.message }));
  }
  updateButtons();
}

function describeJob(b) {
  const delta = b.task === "charge" && state.limits.chemistries[b.chemistry].delta_peak;
  return [
    ["Mode", b.task],
    ["Battery", `${b.chemistry} ${b.cells ? b.cells + "S" : "(auto cells)"}`],
    ["Current", `${(b.current_ma / 1000).toFixed(1)} A`],
    [delta ? "Delta-peak" : "Cut-off", delta ? `${b.cutoff_mv} mV` : `${(b.cutoff_mv / 1000).toFixed(2)} V/cell`],
    ["Capacity limit", b.capacity_limit_mah ? `${b.capacity_limit_mah} mAh` : "off"],
  ];
}

async function startJob(ev) {
  ev.preventDefault();
  const body = jobBody();
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of describeJob(body)) dl.append(el("dt", { text: k }), el("dd", { text: v }));
  $("confirm-body").replaceChildren(dl, el("p", { class: "msg warn", text: "Check the settings match the pack. To stop, unplug the battery." }));
  const dlg = $("confirm-dialog");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", async function once() {
    dlg.removeEventListener("close", once);
    if (dlg.returnValue !== "ok") return;
    const box = $("job-messages");
    try {
      const r = await api("/api/job/start", { method: "POST", body });
      box.replaceChildren();
      for (const w of r.warnings) box.append(el("div", { class: "msg warn", text: w }));
      if (r.ack && r.ack.ok) box.append(el("div", { class: "msg ok", text: "Charger accepted the job." }));
      else if (r.ack) box.append(el("div", { class: "msg err", text: `Charger rejected the job (status ${r.ack.raw_status}).` }));
      else box.append(el("div", { class: "msg warn", text: "Job sent, but the charger didn't confirm. Watch the live readings to check it started." }));
    } catch (e) {
      box.append(el("div", { class: "msg err", text: e.message }));
    }
  });
}

async function stopJob() {
  const btn = $("stop-btn");
  btn.disabled = true; btn.textContent = "Stopping...";
  const box = $("job-messages");
  try {
    const r = await api("/api/stop", { method: "POST" });
    box.replaceChildren(r.confirmed
      ? el("div", { class: "msg ok", text: "Stopped: the charger is no longer working the pack." })
      : el("div", { class: "msg err", text: r.ack && !r.ack.ok
          ? `Charger refused the stop (status ${r.ack.raw_status}). Unplug the battery now.`
          : "Stop not confirmed. If the current hasn't dropped, unplug the battery now." }));
  } catch (e) {
    box.replaceChildren(el("div", { class: "msg err", text: `Stop failed: ${e.message}. Unplug the battery now.` }));
  } finally {
    btn.textContent = "■ Stop job"; updateButtons();
  }
}

// ---------------------------------------------------------------- presets
async function loadPresets(list) {
  list = list || (await api("/presets"));
  const sel = $("preset-select");
  sel.replaceChildren(el("option", { value: "", text: "- choose preset -" }));
  for (const p of list) sel.append(el("option", { value: p.name, text: p.name }));
  sel._data = list;
}
function applyPreset(p) {
  document.querySelector(`input[name="task"][value="${p.task}"]`).checked = true;
  $("chemistry").value = p.chemistry;
  syncFormToChemistry(false);
  $("cells").value = String(p.cells ?? 0);
  $("current_a").value = ((p.current_ma ?? 1000) / 1000).toFixed(1);
  $("cutoff").value = isDelta() ? p.cutoff_mv : (p.cutoff_mv / 1000).toFixed(2);
  $("cap_limit").value = p.capacity_limit_mah ?? 0;
  $("pack_mah").value = p.pack_mah ?? "";
  updateCurrentHint();
  scheduleValidate();
}

// ---------------------------------------------------------------- connection
async function scan() {
  const btn = $("scan-btn"), list = $("device-list");
  btn.disabled = true; btn.textContent = "Scanning...";
  list.replaceChildren();
  try {
    const devs = await api("/api/scan?timeout=6");
    if (!devs.length) list.append(el("li", { text: "No ISDT chargers found. Is it powered on and not connected to a phone?" }));
    for (const d of devs) {
      const b = el("button", { class: "small", text: "Connect" });
      b.onclick = () => connect(d.address, b);
      list.append(el("li", {}, el("div", {}, el("div", { text: d.name + (d.is_air8 ? "" : " (not an Air8?)") }),
        el("div", { class: "meta", text: `${d.address} · ${d.rssi ?? "?"} dBm` })), b));
    }
  } catch (e) {
    list.append(el("li", { class: "msg err", text: e.message }));
  } finally {
    btn.disabled = false; btn.textContent = "Scan";
  }
}
async function connect(address, btn) {
  btn.disabled = true; btn.textContent = "Connecting...";
  try { await api("/api/connect", { method: "POST", body: { address } }); }
  catch (e) { log(`Connect failed: ${e.message}`); btn.disabled = false; btn.textContent = "Connect"; }
}

function renderStatus(s) {
  state.connected = s.connected;
  state.simulated = s.simulated;
  const badge = $("conn-badge");
  badge.textContent = s.connected ? `Connected · ${s.name || s.address}`
    : s.reconnecting ? "Reconnecting..." : "Disconnected";
  badge.className = "badge " + (s.connected ? "badge-on" : s.reconnecting ? "badge-sim" : "badge-off");
  $("sim-badge").hidden = !s.simulated;
  $("disconnect-btn").disabled = !s.connected;
  $("alarm-toggle").disabled = !s.connected;
  if (s.connected) $("device-list").replaceChildren();
  const hw = $("hw-info");
  hw.hidden = !s.connected;
  if (s.connected) {
    hw.replaceChildren();
    const rows = [["Address", s.address], ["Hardware", s.hw_info?.hw_version ?? "?"],
      ["Firmware", s.hw_info?.fw_version ?? "?"], ["Bound", s.bound == null ? "no reply" : s.bound ? "yes" : "rejected"]];
    for (const [k, v] of rows) hw.append(el("dt", { text: k }), el("dd", { text: String(v) }));
  }
  if (s.latest?.electric) onElectric(s.latest.electric, false);
  if (s.latest?.work_state) onWork(s.latest.work_state);
  updateButtons();
}
function updateButtons() {
  $("start-btn").disabled = !(state.connected && state.valid);
  $("stop-btn").disabled = !state.connected;
}

// ---------------------------------------------------------------- live data
function onElectric(e, record = true) {
  state.lastElectric = e;
  if (record) {
    state.history.push({ t: Date.now(), v: e.output_mv / 1000, a: e.charge_ma / 1000 });
    if (state.history.length > HISTORY_MAX) state.history.shift();
  }
  renderTiles();
  renderCells(e.cell_mv);
  lineChart($("volt-chart"), state.history, "v", "V", 3);
  lineChart($("amp-chart"), state.history, "a", "A", 2);
}
function onWork(w) { state.lastWork = w; renderTiles(); }

function renderTiles() {
  const e = state.lastElectric || {}, w = state.lastWork || {};
  const watts = e.output_mv != null ? (e.output_mv * e.charge_ma) / 1e6 : null;
  const tiles = [
    ["State", w.state_label ? `${w.state_label}` : "-", w.state != null ? `#${w.state}` : ""],
    ["Pack", fmt(e.output_mv / 1000, 2), "V"],
    ["Current", fmt(e.charge_ma / 1000, 2), "A"],
    ["Power", fmt(watts, 0), "W"],
    ["Capacity", w.capacity_mah ?? "-", "mAh"],
    ["Energy", fmt(w.energy_mwh / 1000, 2), "Wh"],
    ["Elapsed", fmtDuration(w.elapsed_ms), ""],
    ["Progress", w.progress_pct ?? "-", "%"],
    ["Input", fmt(e.input_mv / 1000, 1), "V"],
    ["Battery", w.battery_name ? `${w.battery_name} ${w.cells || "?"}S` : "-", ""],
  ];
  if (w.error_code) tiles.push(["Error", String(w.error_code), ""]);
  $("tiles").replaceChildren(...tiles.map(([l, v, u]) =>
    el("div", { class: "tile" }, el("div", { class: "label", text: l }),
      el("div", { class: "value" }, String(v), el("span", { class: "unit", text: u })))));
}

// ---------------------------------------------------------------- charts
const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};
function niceTicks(lo, hi, count = 4) {
  if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
  const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let t = start; t <= end + step / 2; t += step) ticks.push(+t.toFixed(10));
  return { ticks, lo: start, hi: end, step };
}
function tooltipFor(host) {
  let tip = host.querySelector(".tooltip");
  if (!tip) { tip = el("div", { class: "tooltip" }); tip.hidden = true; host.append(tip); }
  return tip;
}

function lineChart(host, data, key, unit, digits) {
  const W = host.clientWidth || 300, H = host.clientHeight || 180;
  const m = { l: 44, r: 10, t: 8, b: 22 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  if (data.length < 2) {
    const t = svgEl("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty" });
    t.textContent = "Waiting for data";
    svg.append(t);
    host.replaceChildren(svg);
    return;
  }
  const vals = data.map((d) => d[key]);
  const { ticks, lo, hi } = niceTicks(Math.min(...vals), Math.max(...vals));
  const t0 = data[0].t, t1 = data[data.length - 1].t;
  const x = (t) => m.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - m.l - m.r);
  const y = (v) => H - m.b - ((v - lo) / (hi - lo)) * (H - m.t - m.b);
  for (const tv of ticks) {
    svg.append(svgEl("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv), class: "gridline" }));
    const lbl = svgEl("text", { x: m.l - 6, y: y(tv) + 4, "text-anchor": "end", class: "axis" });
    lbl.textContent = tv.toFixed(digits > 2 ? 2 : 1);
    svg.append(lbl);
  }
  const span = (t1 - t0) / 1000;
  for (const [frac, anchor] of [[0, "start"], [1, "end"]]) {
    const lbl = svgEl("text", { x: frac ? W - m.r : m.l, y: H - 5, "text-anchor": anchor, class: "axis" });
    lbl.textContent = frac ? "now" : `-${fmtDuration(span * 1000)}`;
    svg.append(lbl);
  }
  svg.append(svgEl("path", { class: "line", d: data.map((d, i) => `${i ? "L" : "M"}${x(d.t).toFixed(1)},${y(d[key]).toFixed(1)}`).join("") }));
  const cross = svgEl("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
  const dot = svgEl("circle", { class: "dot", r: 4, visibility: "hidden" });
  svg.append(cross, dot);
  host.replaceChildren(svg);
  const tip = tooltipFor(host);
  const hit = svgEl("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
  svg.append(hit);
  hit.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const tt = t0 + ((px - m.l) / (W - m.l - m.r)) * (t1 - t0);
    let best = data[0];
    for (const d of data) if (Math.abs(d.t - tt) < Math.abs(best.t - tt)) best = d;
    cross.setAttribute("x1", x(best.t)); cross.setAttribute("x2", x(best.t));
    dot.setAttribute("cx", x(best.t)); dot.setAttribute("cy", y(best[key]));
    cross.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.textContent = `${new Date(best.t).toLocaleTimeString()} · ${best[key].toFixed(digits)} ${unit}`;
    const left = (x(best.t) / W) * r.width;
    tip.style.left = `${Math.min(Math.max(0, left - 60), r.width - 140)}px`;
    tip.style.top = `0px`;
  });
  hit.addEventListener("pointerleave", () => {
    tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
  });
}

// Per-cell deviation from the pack average, in mV, bars from a zero baseline.
// Each bar is labelled with the absolute cell voltage.
function renderCells(cells) {
  const host = $("cell-chart");
  const W = host.clientWidth || 300, H = host.clientHeight || 180;
  const m = { l: 44, r: 10, t: 18, b: 22 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  if (!cells || !cells.length) {
    const t = svgEl("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty" });
    t.textContent = "No cell data (balance lead connected?)";
    svg.append(t);
    host.replaceChildren(svg);
    $("cell-spread").textContent = "";
    return;
  }
  const avg = cells.reduce((a, b) => a + b, 0) / cells.length;
  const dev = cells.map((c) => c - avg);
  const spread = Math.max(...cells) - Math.min(...cells);
  $("cell-spread").textContent = `· spread ${spread} mV · bars show mV from average`;
  const lim = Math.max(5, ...dev.map(Math.abs));
  const { ticks, lo, hi } = niceTicks(-lim, lim, 4);
  const y = (v) => H - m.b - ((v - lo) / (hi - lo)) * (H - m.t - m.b);
  for (const tv of ticks) {
    svg.append(svgEl("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv), class: "gridline" }));
    const lbl = svgEl("text", { x: m.l - 6, y: y(tv) + 4, "text-anchor": "end", class: "axis" });
    lbl.textContent = (tv > 0 ? "+" : "") + Math.round(tv);
    svg.append(lbl);
  }
  const band = (W - m.l - m.r) / cells.length;
  const bw = Math.min(36, band * 0.6);
  const tip = tooltipFor(host);
  cells.forEach((c, i) => {
    const cx = m.l + band * (i + 0.5);
    const y0 = y(0), y1 = y(dev[i]);
    const top = Math.min(y0, y1), h = Math.max(1, Math.abs(y1 - y0));
    svg.append(svgEl("rect", { class: "bar", x: cx - bw / 2, y: top, width: bw, height: h, rx: Math.min(4, h / 2) }));
    const lbl = svgEl("text", { x: cx, y: H - 6, "text-anchor": "middle", class: "barlabel" });
    lbl.textContent = (c / 1000).toFixed(3);
    svg.append(lbl);
    const hit = svgEl("rect", { x: cx - band / 2, y: 0, width: band, height: H, fill: "transparent" });
    hit.addEventListener("pointerenter", () => {
      tip.hidden = false;
      tip.textContent = `Cell ${i + 1}: ${(c / 1000).toFixed(3)} V (${dev[i] >= 0 ? "+" : ""}${dev[i].toFixed(0)} mV)`;
      tip.style.left = `${Math.min(Math.max(0, (cx / W) * host.clientWidth - 70), host.clientWidth - 170)}px`;
      tip.style.top = "0px";
    });
    hit.addEventListener("pointerleave", () => { tip.hidden = true; });
    svg.append(hit);
  });
  host.replaceChildren(svg, tip);
}

// ---------------------------------------------------------------- websocket
function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "status": renderStatus(msg.data); break;
      case "electric": onElectric(msg.data); break;
      case "work_state": onWork(msg.data); break;
      case "task_ack": log(`Charger ${msg.data.ok ? "accepted" : "rejected"} job (status ${msg.data.raw_status})`); break;
      case "rx": case "tx": log(msg.data, msg.type); break;
      case "log": log(msg.data); break;
    }
  };
  ws.onclose = () => {
    renderStatus({ connected: false, simulated: state.simulated });
    setTimeout(connectWs, 2000);
  };
}

// ---------------------------------------------------------------- init
async function init() {
  try {
    const health = await api("/api/health");
    state.allowRaw = health.allow_raw;
    $("raw-form").hidden = !health.allow_raw;
    if (health.stop_method === "captured") $("stop-note").textContent = "The Stop button sends the stop command captured from the ISDLink app.";
    state.limits = await api("/api/limits");
  } catch (e) {
    $("job-messages").append(el("div", { class: "msg err", text: `Backend unavailable: ${e.message}` }));
    setTimeout(init, 3000);
    return;
  }
  const sel = $("chemistry");
  sel.replaceChildren(...Object.keys(state.limits.chemistries).map((k) => el("option", { value: k, text: k === "NiMH" ? "NiMH / NiCd" : k })));
  sel.value = "LiPo";
  syncFormToChemistry();
  $("cells").value = "6";

  $("job-form").addEventListener("change", (ev) => {
    if (ev.target.name === "task" || ev.target.id === "chemistry") syncFormToChemistry();
    else scheduleValidate();
  });
  $("job-form").addEventListener("input", (ev) => {
    if (ev.target.id === "pack_mah" || ev.target.id === "crate") applyCRate();
    else { updateCurrentHint(); scheduleValidate(); }
  });
  $("job-form").addEventListener("submit", startJob);

  $("scan-btn").onclick = scan;
  $("stop-btn").onclick = stopJob;
  $("disconnect-btn").onclick = () => api("/api/disconnect", { method: "POST" }).catch((e) => log(e.message));
  $("alarm-toggle").onchange = (ev) => api("/api/alarm", { method: "POST", body: { enabled: ev.target.checked } }).catch((e) => log(e.message));
  $("log-clear").onclick = () => { $("log").textContent = ""; };

  $("preset-save").onclick = async () => {
    const name = $("preset-name").value.trim();
    if (!name) return $("preset-name").focus();
    const b = jobBody();
    delete b.confirm_destroy;
    b.pack_mah = Number($("pack_mah").value) || null;
    await loadPresets(await api(`/presets/${encodeURIComponent(name)}`, { method: "PUT", body: b }));
    $("preset-select").value = name;
  };
  $("preset-load").onclick = () => {
    const p = ($("preset-select")._data || []).find((x) => x.name === $("preset-select").value);
    if (p) { applyPreset(p); $("preset-name").value = p.name; }
  };
  $("preset-delete").onclick = async () => {
    const name = $("preset-select").value;
    if (name && confirm(`Delete preset "${name}"?`)) await loadPresets(await api(`/presets/${encodeURIComponent(name)}`, { method: "DELETE" }));
  };
  $("raw-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    api("/api/raw", { method: "POST", body: { characteristic: $("raw-char").value, hex: $("raw-hex").value } })
      .catch((e) => log(`Raw send failed: ${e.message}`));
  });

  loadPresets().catch(() => {});
  renderTiles();
  renderCells([]);
  lineChart($("volt-chart"), [], "v", "V", 3);
  lineChart($("amp-chart"), [], "a", "A", 2);
  window.addEventListener("resize", () => {
    if (state.lastElectric) onElectric(state.lastElectric, false);
  });
  connectWs();
}
init();
