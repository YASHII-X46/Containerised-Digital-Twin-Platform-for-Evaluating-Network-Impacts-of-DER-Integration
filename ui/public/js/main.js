/* Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration control panel — entry point. */
import { api } from "./api.js";
import { $, $$, el, fmt } from "./dom.js";
import { toast, logLine } from "./toast.js";
import { ChartManager } from "./charts.js";
import {
  applyPreset, applySavedConfig, bindSliders, deleteSavedConfig, fillSelect,
  loadParams, refreshSavedConfigs, saveCurrentConfig, simParams,
} from "./controls.js";
import { drawHeatmap } from "./heatmap.js";
import { paintLocationalHosting, setNetworkGraph, updateNetworkVoltages } from "./network.js";
import { renderModules } from "./modules.js";
import {
  initBusEditor, setNetwork, setCatalogs, getBusData, getNetwork,
  networkBuses, refreshCustomProfiles,
} from "./buseditor.js";

let syncSliders = () => {};
let lastScenario = null;
// Network-import formats come from the Simulation Engine's importer registry, so
// the upload control adapts as formats are added engine-side — nothing here is
// hardcoded. NATIVE_FORMAT is the one self-describing model format (saved as-is,
// preserving its own id); every other format is parsed/mapped by the engine.
const NATIVE_FORMAT = "json";
let importFormats = [];
let busMode = "quick";          // "quick" auto-assign | "advanced" per-bus editor
let busRows = [];               // full per-bus voltage summary
let busPage = 0;
let heatState = null;           // { matrix, vmin, vmax } for the voltage heatmap
const sessionLosses = {};       // scenario -> total_losses_kwh (this session only)

/* Toggle a panel's "data not available" placeholder. msg=null hides it. */
function ph(wrapId, msg) {
  const w = $(wrapId);
  const p = w && w.querySelector(".ph");
  if (!p) return;
  if (msg) { p.textContent = msg; p.classList.add("show"); }
  else { p.classList.remove("show"); }
}

/* ---------------- busy / pipeline stages ---------------- */
function busy(on) {
  $("pipe-spin").classList.toggle("hidden", !on);
  $$(".action-bar .btn").forEach((b) => (b.disabled = on));
}
async function withBusy(label, fn) {
  busy(true);
  try { return await fn(); }
  catch (e) {
    logLine(`${label} ERROR`, e.payload || String(e));
    toast(`${label} failed`, (e.payload && JSON.stringify(e.payload).slice(0, 120)) || String(e), "error", 7000);
  } finally { busy(false); }
}

function resetStages() { $$(".stage").forEach((s) => s.classList.remove("active", "done", "error")); }
function setStage(name, state) {
  const s = document.querySelector(`.stage[data-stage="${name}"]`);
  if (s) { s.classList.remove("active", "done", "error"); if (state) s.classList.add(state); }
}
function failActiveStage() {
  const a = document.querySelector(".stage.active");
  if (a) { a.classList.remove("active"); a.classList.add("error"); }
}

/* The only action: run the pipeline as two visible stages. */
async function doPipeline() {
  await withBusy("Pipeline", async () => {
    const load = loadParams();
    if (!load.network_id) {
      toast("No network selected", "Upload and select a network model before running.", "error");
      return;
    }

    // v3.0 modular config: per-bus editor overrides; otherwise quick mode
    // auto-assigns over whichever network is selected.
    if (busMode === "advanced") {
      const busData = getBusData();
      if (!busData) {
        toast("No bus configuration", "Open the Buses tab (or click Prefill) before running in per-bus mode.", "error");
        return;
      }
      load.bus_data = busData;
    } else {
      // Quick mode: auto-assign over whichever network is selected. The engine
      // is network-agnostic, so always hand it the selected network's bus list.
      load.network_buses = networkBuses();
    }

    resetStages();
    try {
      // All inter-module communication is over the OpenFMB NATS bus: profiles +
      // results travel in the messages and charts render from the event data —
      // no HTTP between engines, no shared files.
      setStage("generate", "active");
      logLine("Pipeline started over the OpenFMB bus", { scenario: load.scenario_name, network: load.network_id });
      const r = await api.pipeline(load, simParams());
      setStage("generate", "done");
      setStage("simulate", "active");
      setStage("simulate", "done");
      onSimResult(r.simulate, load.scenario_name, { profiles: r.profiles, result_series: r.simulate.result_series, generate: r.generate });
      toast("Pipeline complete", `${load.scenario_name}, maximum voltage ${r.simulate.max_voltage_pu} pu`, "success");
    } catch (e) { failActiveStage(); throw e; }
  });
}

/* ---------------- result rendering ---------------- */
function onSimResult(sim, scenario, inline) {
  if (!sim) return;
  lastScenario = scenario;
  renderMetrics(sim, inline.generate);         // Summary KPIs (+ battery ageing)
  updateNetworkVoltages(sim.bus_voltage_summary);  // recolour the feeder map
  renderBusTable(sim.bus_voltage_summary);     // Data table (sets busRows)
  ChartManager.vprofile(busRows); ph("wrap-vprofile", null);   // Voltage (spatial, 3b)
  ChartManager.violbar(busRows);  ph("wrap-violbar", null);    // Violations (3d)
  sessionLosses[scenario] = sim.total_losses_kwh;
  renderInlineCharts(sim, inline.profiles);    // NATS-only: chart from the event data
}

/* Aggregate the inline profiles payload into the duck/DER-mix chart shape. */
function aggregateProfiles(profiles) {
  const T = profiles.metadata.timesteps;
  const z = () => Array(T).fill(0);
  const total_load_kw = z(), total_pv_kw = z(), total_ev_kw = z(), total_bess_kw = z(), total_net_kw = z();
  // Aggregate the net contribution of any DER plugins beyond PV/EV/BESS so the
  // DER-mix chart reflects new DER types with no UI edits (modular end to end).
  const total_other_kw = z();
  for (const b of Object.values(profiles.buses)) {
    b.timeseries.forEach((ts, i) => {
      total_load_kw[i] += ts.load_kw; total_pv_kw[i] += ts.pv_kw;
      total_ev_kw[i] += ts.ev_charge_kw; total_bess_kw[i] += ts.bess_power_kw;
      total_net_kw[i] += ts.net_load_kw; total_other_kw[i] += ts.other_der_kw || 0;
    });
  }
  return {
    timesteps: Array.from({ length: T }, (_, i) => i + 1),
    total_load_kw, total_pv_kw, total_ev_kw, total_bess_kw, total_net_kw, total_other_kw,
  };
}

/* Render every time-series chart from the bus event — no shared files. */
function renderInlineCharts(sim, profiles) {
  const agg = aggregateProfiles(profiles);
  // Multi-day horizons get dashed day separators and a per-day energy strip.
  const days = profiles.metadata.days || 1;
  const stepsPerDay = Math.round(profiles.metadata.timesteps / days);
  const marks = days > 1 ? { days, stepsPerDay } : null;
  agg.dayMarks = marks;
  // Operating-envelope band (published limit vs achieved export), when a
  // scheme ran — drawn onto the DER-mix chart.
  const s0 = sim.result_series || {};
  if (s0.doe_envelope_total?.length) {
    agg.doe = { envelope: s0.doe_envelope_total, export: s0.doe_export_total || [] };
  }
  ChartManager.duck(agg, marks);   ph("wrap-duck", null);
  ChartManager.dermix(agg); ph("wrap-dermix", null);
  renderDayEnergy(agg, days, stepsPerDay, profiles.metadata.resolution_minutes || 15);

  const s = sim.result_series || {};
  if (s.v_max?.length) {
    ChartManager.voltage({
      timesteps: s.v_max.map((_, i) => i),
      max_voltage_pu: s.v_max, mean_voltage_pu: s.v_mean, min_voltage_pu: s.v_min,
    }, marks);
    ph("wrap-voltage", null);
  }
  ChartManager.branch({ branch_max_loading: sim.branch_loading_summary }); ph("wrap-branch", null);
  if (s.max_loading_over_time?.length) {
    ChartManager.loadtime(s.max_loading_over_time, marks); ph("wrap-loadtime", null);
  }
}

/* Per-day energy table under the duck curve (multi-day runs only). */
function renderDayEnergy(agg, days, stepsPerDay, resolutionMinutes) {
  const host = $("day-energy");
  if (!host) return;
  if (!days || days < 2) { host.hidden = true; host.innerHTML = ""; return; }
  const stepH = resolutionMinutes / 60.0;
  const sumDay = (arr, d) => {
    let s = 0;
    for (let i = d * stepsPerDay; i < Math.min((d + 1) * stepsPerDay, arr.length); i++) s += arr[i];
    return s * stepH;
  };
  const row = (d) => {
    const load = sumDay(agg.total_load_kw, d), pv = sumDay(agg.total_pv_kw, d);
    const ev = sumDay(agg.total_ev_kw, d), net = sumDay(agg.total_net_kw, d);
    return `<tr><td>Day ${d + 1}</td><td>${load.toFixed(0)}</td><td>${pv.toFixed(0)}</td>` +
           `<td>${ev.toFixed(0)}</td><td>${net.toFixed(0)}</td></tr>`;
  };
  host.innerHTML =
    "<table><thead><tr><th>Energy (kWh)</th><th>Load</th><th>PV</th><th>EV</th><th>Net</th></tr></thead>" +
    `<tbody>${Array.from({ length: days }, (_, d) => row(d)).join("")}</tbody></table>`;
  host.hidden = false;
}

function renderMetrics(r, gen) {
  const conv = r.converged_timesteps === r.total_timesteps;
  const cards = [
    { k: "Converged", v: `${r.converged_timesteps}/${r.total_timesteps}`, cls: conv ? "good" : "warn" },
    { k: "Minimum voltage", v: `${fmt(r.min_voltage_pu, 3)} pu`, cls: r.min_voltage_pu >= 0.95 ? "good" : "warn" },
    { k: "Maximum voltage", v: `${fmt(r.max_voltage_pu, 3)} pu`, cls: r.max_voltage_pu <= 1.05 ? "good" : "warn" },
    { k: "Maximum loading", v: `${fmt(r.max_loading_pct, 1)} %`, cls: r.max_loading_pct <= 100 ? "good" : "warn" },
    { k: "Total losses", v: `${fmt(r.total_losses_kwh, 0)} kWh` },
    { k: "Voltage violations", v: r.total_voltage_violations, cls: r.total_voltage_violations ? "warn" : "good" },
    { k: "Thermal violations", v: r.total_thermal_violations, cls: r.total_thermal_violations ? "warn" : "good" },
    { k: "Buses with PV, battery, EV", v: `${r.buses_with_pv}, ${r.buses_with_bess}, ${r.buses_with_ev}` },
    { k: "Simulation time", v: `${fmt(r.simulation_time_seconds, 2)} s` },
  ];

  // Battery ageing (from the generation stage), shown when batteries are present.
  if (gen && gen.buses_with_bess > 0) {
    cards.push(
      { k: "Battery cycles", v: fmt(gen.total_bess_cycles, 2) },
      { k: "Mean battery health", v: `${fmt(gen.mean_bess_soh * 100, 2)} %`,
        cls: gen.mean_bess_soh >= 0.8 ? "good" : "warn" },
    );
  }

  // Export-limit (operating-envelope) outcomes, when a scheme ran.
  if (r.doe_mode && r.doe_mode !== "off") {
    const scheme = r.doe_mode === "static"
      ? "Fixed limit" : `Dynamic (${r.doe_allocation || "equal"})`;
    cards.push({ k: "Export limit scheme", v: scheme });
    cards.push({ k: "Curtailed by envelope", v: `${fmt(r.doe_curtailed_kwh, 1)} kWh`,
                 cls: r.doe_curtailed_kwh > 0 ? "warn" : "good" });
    if (r.doe_envelope_utilisation_pct > 0) {
      cards.push({ k: "Envelope utilisation", v: `${fmt(r.doe_envelope_utilisation_pct, 1)} %` });
    }
  }

  // Demand-response outcomes, shown only when a coordination strategy ran.
  if (r.coordination_mode && r.coordination_mode !== "uncoordinated") {
    cards.push(
      { k: "Prosumer twins", v: r.prosumer_twins ?? 0 },
      { k: "PV curtailed", v: `${fmt(r.total_pv_curtailed_kwh, 1)} kWh` },
      { k: "EV deferred", v: `${fmt(r.total_ev_deferred_kwh, 1)} kWh` },
      { k: "PV stored in battery", v: `${fmt(r.total_pv_shared_kwh, 1)} kWh` },
    );
    // Battery peak support and custom shed appear only when their plugins acted.
    if (r.total_bess_support_kwh > 0) {
      cards.push({ k: "Battery peak support", v: `${fmt(r.total_bess_support_kwh, 1)} kWh` });
    }
    if (r.total_other_shed_kwh > 0) {
      cards.push({ k: "Other DER shed", v: `${fmt(r.total_other_shed_kwh, 1)} kWh` });
    }
  }

  $("metrics").replaceChildren(...cards.map((c) =>
    el("div", { class: `metric ${c.cls || ""}` }, el("div", { class: "v" }, c.v), el("div", { class: "k" }, c.k))
  ));
}

/* ---- Data table (paginated, no inner scroll) ---- */
function renderBusTable(summary) { busRows = summary || []; busPage = 0; renderBusPage(); }
function busPageSize() {
  const wrap = $("buses-wrap");
  const head = wrap.querySelector("thead");
  const avail = wrap.clientHeight - (head ? head.offsetHeight : 28) - 12;
  return Math.max(1, Math.floor(avail / 30));
}
function renderBusPage() {
  const tbody = $("bus-table").querySelector("tbody");
  if (!busRows.length) { tbody.replaceChildren(); $("bus-page-ind").textContent = "—"; return; }
  const size = busPageSize();
  const pages = Math.ceil(busRows.length / size);
  busPage = Math.min(busPage, pages - 1);
  const slice = busRows.slice(busPage * size, busPage * size + size);
  tbody.replaceChildren(...slice.map((b) =>
    el("tr", { class: b.violation_count ? "viol" : "" },
      el("td", {}, b.bus_id), el("td", {}, fmt(b.min_voltage_pu, 4)),
      el("td", {}, fmt(b.mean_voltage_pu, 4)), el("td", {}, fmt(b.max_voltage_pu, 4)),
      el("td", {}, b.violation_count))
  ));
  $("bus-page-ind").textContent = `Buses ${busPage * size + 1} to ${busPage * size + slice.length} of ${busRows.length}, page ${busPage + 1} of ${pages}`;
  $("bus-prev").disabled = busPage === 0;
  $("bus-next").disabled = busPage >= pages - 1;
}

function drawHeatmapNow() {
  if (heatState) drawHeatmap($("heatmap"), heatState.matrix, { vmin: heatState.vmin, vmax: heatState.vmax, busLabels: heatState.buses });
}

/* ---- Compare (3f): overlay scenarios held on disk; losses from session ---- */
let comparing = false;
async function loadCompare() {
  if (comparing) return;
  comparing = true;
  try {
    const persisted = await api.losses().catch(() => ({}));
    // Cross-scenario voltage / duck-curve comparison now lives in the Study tab
    // (run over the bus); here we keep the losses-vs-penetration trend.
    ["wrap-cmp-maxv", "wrap-cmp-duck"].forEach((w) =>
      ph(w, "Use the Study tab for cross-scenario voltage / duck-curve comparison."));

    const penOf = (n) => { const m = /pen(\d+)/i.exec(n); return m ? +m[1] : NaN; };
    const lossOf = (n) => sessionLosses[n] ?? persisted?.[n]?.total_losses_kwh;
    const names = new Set([...Object.keys(sessionLosses), ...Object.keys(persisted || {})]);
    const pts = [...names]
      .map((n) => ({ pen: penOf(n), label: isNaN(penOf(n)) ? n : `${penOf(n)}%`, loss: lossOf(n) }))
      .filter((p) => p.loss != null)
      .sort((a, b) => (isNaN(a.pen) ? 1e9 : a.pen) - (isNaN(b.pen) ? 1e9 : b.pen));
    if (pts.length >= 2) {
      ChartManager.compareLoss(pts.map((p) => ({ label: p.label, loss: p.loss }))); ph("wrap-cmp-loss", null);
    } else {
      ph("wrap-cmp-loss", "Run ≥2 scenarios to populate this chart (losses are saved as you run them).");
    }
    requestAnimationFrame(() => ChartManager.resizeAll());
  } finally { comparing = false; }
}

/* ---------------- shell: tabs, theme, health ---------------- */
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
  requestAnimationFrame(() => {
    ChartManager.resizeAll();
    if (name === "data") renderBusPage();
    if (name === "violations") drawHeatmapNow();
    if (name === "compare") loadCompare();
  });
}

function initTheme() {
  document.documentElement.dataset.theme = localStorage.getItem("dt-theme") || "dark";
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("dt-theme", next);
    ChartManager.redrawAll();
    drawHeatmapNow();
  });
}

function setPill(svc, up) {
  const pill = document.querySelector(`.dot-pill[data-svc="${svc}"]`);
  if (!pill) return;
  pill.classList.toggle("up", up);
  pill.classList.toggle("down", !up);
}
async function refreshHealth() {
  try {
    const h = await api.health();
    setPill("load", h.load_engine.up);
    setPill("sim", h.sim_engine.up);
    setPill("broker", h.broker.up);
  } catch { ["load", "sim", "broker"].forEach((s) => setPill(s, false)); }
}
async function loadMeta() {
  try {
    const m = await api.meta();
    fillSelect("bess_config", m.bess_configs, "powerwall_2");
    fillSelect("ev_config", m.ev_configs, "level2_7kw");

    // System tab: render the live modular registries from every engine.
    renderModules(m);

    // Network-import formats drive the file picker (extensions + tooltip).
    importFormats = m.import_formats || [];
    applyImportFormats();

    // Coordination strategies come from the sim engine's registry.
    if (m.strategies?.length) {
      const cs = $("coordination_mode");
      const cur = cs.value || "uncoordinated";
      cs.replaceChildren(...m.strategies.map((s) => {
        const o = new Option(s.name, s.name);
        if (s.description) o.title = s.description;
        return o;
      }));
      cs.value = m.strategies.some((s) => s.name === cur) ? cur : "uncoordinated";
    }

    if (m.kpis?.length) fillSelect("study_kpi", m.kpis.map((k) => k.name), "voltage_violations");
    if (m.tariffs?.length) fillSelect("tariff", m.tariffs.map((t) => t.name), "tou_residential");
    if (m.doe_allocations?.length) fillSelect("doe_allocation", m.doe_allocations.map((a) => a.name), "equal");

    const classes = [...(m.classes?.archetypes || []), ...(m.classes?.custom || [])];
    setCatalogs({ classes, bessConfigs: m.bess_configs, evConfigs: m.ev_configs });

    const nets = m.networks?.networks || [];
    const sel = $("network_id");
    const current = sel.value;
    if (!nets.length) {
      // The stack ships no networks: prompt the user to upload one before running.
      sel.replaceChildren(new Option("Upload a network model to begin", ""));
      sel.value = "";
      $("net-chip").textContent = "No network. Upload a model.";
      $("network-info").textContent = "Upload a network model in JSON, RAW, or CIM format to begin.";
      logLine("No networks registered. Upload a model to begin.");
      return;
    }
    sel.replaceChildren(...nets.map((n) =>
      new Option(`${n.name || n.id}${n.builtin ? "" : " (user)"}`, n.id)));
    sel.value = nets.some((n) => n.id === current) ? current : (m.networks?.default || nets[0]?.id || "");
    await onNetworkChange();
  } catch { logLine("meta load failed (engines may still be starting)"); }
}

/* Fetch the selected network's detail, update chips, re-render the bus editor. */
async function onNetworkChange() {
  const id = $("network_id").value;
  if (!id) return;
  try {
    const net = await setNetwork(id);
    setNetworkGraph(net);   // interactive topology map
    // Show every distinct voltage level (MV/LV) for multi-voltage feeders.
    const levels = [...new Set(net.buses.map((b) => Number(b.base_kv) || net.base_voltage_kv))]
      .sort((a, b) => b - a);
    const kvText = levels.length > 1
      ? `${levels.map((v) => Number(v.toFixed(3))).join(" / ")} kV`
      : `${net.base_voltage_kv} kV`;
    const info = `${net.buses.length} buses, ${net.branches.length} branches, ${kvText}`;
    $("net-chip").textContent = `${net.id}: ${info}`;
    $("network-info").textContent = info;
    // Bus ids differ per network; clear any stale PV-bus selection so quick mode
    // auto-assigns PV across all load buses (empty = all) of the chosen network.
    $("pv_buses").value = "";
    logLine("network selected", { id, buses: net.buses.length });
  } catch (e) {
    toast("Network load failed", String(e.payload?.detail || e), "error");
  }
}

/* Configure the network file picker from the engine's importer registry: the
 * accepted extensions and the tooltip both follow whatever formats the
 * Simulation Engine reports, so adding an importer engine-side needs no UI edit. */
function applyImportFormats() {
  const input = $("network-file");
  const btn = $("btn-upload-network");
  if (!input || !importFormats.length) return;
  // Each format contributes its own name plus any extension aliases (e.g. cim → xml).
  const exts = [...new Set(importFormats.flatMap((f) => [f.name, ...(f.extensions || [])]))]
    .map((e) => `.${e}`);
  input.setAttribute("accept", [...exts, "application/json", "application/xml"].join(","));
  if (btn) {
    const labels = importFormats.map((f) => f.description || f.name).join(", ");
    btn.title = `Upload a network model. Accepted formats: ${labels}.`;
  }
}

async function uploadNetworkFile() {
  const f = $("network-file").files[0];
  $("network-file").value = "";
  if (!f) return;
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  try {
    const content = await f.text();
    let r;
    if (ext === NATIVE_FORMAT) {
      // Native self-describing model: saved as-is, preserving its own id.
      r = await api.uploadNetwork(JSON.parse(content));
    } else {
      // Foreign format: the engine infers the format from the filename/content
      // and maps it to the internal model — no format hardcoded in the UI.
      const id = (f.name.replace(/\.[^.]+$/, "") || "imported")
        .replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
      r = await api.importNetwork({ id, content, filename: f.name });
    }
    toast("Network imported", `${r.id}: ${r.buses} buses, ${r.branches} branches`, "success");
    logLine("network imported", r);
    await loadMeta();
    $("network_id").value = r.id;
    await onNetworkChange();
  } catch (e) {
    toast("Network import failed", String(e.payload?.detail || e.message || e), "error", 8000);
  }
}

function setBusMode(mode) {
  busMode = mode;
  $$("#busmode .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  if (mode === "advanced") {
    toast("Per-bus mode", "The pipeline will use the Buses tab configuration.", "info", 3500);
    switchTab("buses");
  }
}

/* ---------------- study runner ---------------- */
const parseNums = (s) => s.split(",").map((x) => parseFloat(x.trim())).filter((n) => !isNaN(n));

function updateStudyFields() {
  const mode = $("study_mode").value;
  // Locational hosting reuses the hosting-capacity fields (indicator/threshold/range).
  const effective = mode === "locational_hosting" ? "hosting_capacity" : mode;
  $$("[data-study]").forEach((el) => { el.style.display = el.dataset.study === effective ? "" : "none"; });
}

function renderStudy(r) {
  const table = $("study-table");
  if (r.mode === "locational_hosting") {
    // Per-bus capacities, worst first; the topology map is painted to match.
    const rows = [...r.per_bus].sort((a, b) =>
      (a.hosting_capacity_kw ?? -1) - (b.hosting_capacity_kw ?? -1));
    table.querySelector("thead").innerHTML =
      "<tr><th>Bus</th><th>Hosting capacity (kW)</th><th>Last safe penetration (%)</th></tr>";
    table.querySelector("tbody").innerHTML = rows.map((x) =>
      `<tr><td>${x.bus_id}</td><td>${x.hosting_capacity_kw ?? "below start"}</td>` +
      `<td>${x.hosting_penetration_pct ?? "—"}</td></tr>`).join("");
    return;
  }
  const ok = r.runs.filter((x) => x.ok);
  const keys = ok.length ? Object.keys(ok[0].kpis) : [];
  table.querySelector("thead").innerHTML =
    "<tr><th>Pen %</th><th>Seed</th>" + keys.map((k) => `<th>${k}</th>`).join("") + "</tr>";
  table.querySelector("tbody").innerHTML = r.runs.map((x) =>
    x.ok
      ? `<tr><td>${x.penetration}</td><td>${x.seed}</td>` + keys.map((k) => `<td>${x.kpis[k]}</td>`).join("") + "</tr>"
      : `<tr><td>${x.penetration}</td><td>${x.seed}</td><td colspan="${Math.max(1, keys.length)}" class="muted">error</td></tr>`
  ).join("");
}

async function runStudy() {
  if (!loadParams().network_id) {
    toast("No network selected", "Upload and select a network model before running a study.", "error");
    return;
  }
  const mode = $("study_mode").value;
  const params = {};
  if (mode === "penetration_sweep") params.penetrations = parseNums($("study_pens").value);
  else if (mode === "monte_carlo") params.seeds = parseNums($("study_seeds").value);
  else if (mode === "hosting_capacity" || mode === "locational_hosting") {
    params.kpi = $("study_kpi").value;
    params.threshold = parseFloat($("study_threshold").value);
    const [start, step, max] = parseNums($("study_range").value);
    Object.assign(params, { start, step, max });
  }
  const btn = $("study-run"), spin = $("study-spin");
  btn.disabled = true; spin.classList.remove("hidden"); $("study-status").textContent = "Running";
  try {
    const r = await api.study(mode, loadParams(), simParams(), params);
    renderStudy(r);
    if (r.mode === "locational_hosting") {
      paintLocationalHosting(r.per_bus);
      const solved = r.per_bus.filter((b) => b.hosting_capacity_kw != null).length;
      $("study-status").textContent =
        `Locational hosting capacity for ${r.per_bus.length} buses (${solved} within range). ` +
        `The Network tab map is coloured red (least headroom) to green (most).`;
    } else {
      $("study-status").textContent = r.hosting_capacity != null
        ? `Hosting capacity: ${r.hosting_capacity}% (limit on ${r.kpi} ≥ ${r.threshold})`
        : `${r.runs.length} runs complete`;
    }
    logLine("study complete", { mode, runs: r.runs.length, hosting_capacity: r.hosting_capacity });
  } catch (e) {
    toast("Study failed", String(e.payload?.error || e.message || e), "error", 8000);
    $("study-status").textContent = "failed";
  } finally {
    btn.disabled = false; spin.classList.add("hidden");
  }
}

/* ---------------- boot ---------------- */
function init() {
  initTheme();
  syncSliders = bindSliders();

  $("btn-pipeline").addEventListener("click", doPipeline);

  $("bus-prev").addEventListener("click", () => { busPage = Math.max(0, busPage - 1); renderBusPage(); });
  $("bus-next").addEventListener("click", () => { busPage += 1; renderBusPage(); });

  // v3.0 modular config
  $("network_id").addEventListener("change", onNetworkChange);
  $("btn-upload-network").addEventListener("click", () => $("network-file").click());
  $("network-file").addEventListener("change", uploadNetworkFile);
  $$("#busmode .seg-btn").forEach((b) => b.addEventListener("click", () => setBusMode(b.dataset.mode)));
  // Applying a bus config in the Buses tab arms per-bus mode automatically,
  // so the next pipeline run uses the edited table.
  initBusEditor(loadParams, () => { if (busMode !== "advanced") setBusMode("advanced"); });
  refreshCustomProfiles();

  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $$(".chip").forEach((c) => c.addEventListener("click", () => applyPreset(c.dataset.preset, syncSliders)));

  // Prosumer shadow-twin config is only relevant under DR coordination.
  const coordSel = $("coordination_mode");
  const toggleTwinCfg = () => { $("twin-cfg").hidden = coordSel.value === "uncoordinated"; };
  coordSel.addEventListener("change", toggleTwinCfg);
  toggleTwinCfg();

  // Time-of-use charge/discharge windows apply only to that battery dispatch mode.
  const bessMode = $("bess_dispatch_mode");
  const toggleBessTou = () => {
    const show = bessMode.value === "time_of_use";
    $$(".bess-tou").forEach((r) => { r.hidden = !show; });
  };
  bessMode.addEventListener("change", toggleBessTou);
  toggleBessTou();

  // Export-limit fields follow the selected scheme (fixed cap vs dynamic).
  const doeMode = $("doe_mode");
  const toggleDoe = () => {
    $$(".doe-static").forEach((el) => { el.hidden = doeMode.value !== "static"; });
    $$(".doe-dynamic").forEach((el) => { el.hidden = doeMode.value !== "dynamic"; });
  };
  doeMode.addEventListener("change", toggleDoe);
  toggleDoe();

  // Study runner
  $("study-run").addEventListener("click", runStudy);
  $("study_mode").addEventListener("change", updateStudyFields);
  updateStudyFields();

  // Saved scenario configurations (browser localStorage).
  refreshSavedConfigs();
  $("cfg-save").addEventListener("click", () => {
    const name = saveCurrentConfig();
    toast("Configuration saved", `All controls stored as "${name}".`, "success");
  });
  $("saved_configs").addEventListener("change", () => {
    const name = $("saved_configs").value;
    if (name && applySavedConfig(name)) {
      syncSliders();
      logLine("configuration applied", name);
    }
  });
  $("cfg-delete").addEventListener("click", () => {
    const name = $("saved_configs").value;
    if (name && deleteSavedConfig(name)) toast("Configuration deleted", name, "info");
  });

  let rz;
  window.addEventListener("resize", () => {
    clearTimeout(rz);
    rz = setTimeout(() => { ChartManager.resizeAll(); renderBusPage(); drawHeatmapNow(); }, 120);
  });

  refreshHealth();
  loadMeta();
  setInterval(refreshHealth, 8000);
  logLine("Interface ready. Configure a scenario and select Run pipeline.");
  toast("Ready", "Connecting to engines", "info", 3000);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
