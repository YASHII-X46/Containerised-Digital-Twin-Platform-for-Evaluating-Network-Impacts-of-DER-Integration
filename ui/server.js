/*
 * Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration control panel (Node.js / Express).
 *
 * The Python engines do the heavy lifting (profile generation + OpenDSS power
 * flow). This server bridges the browser (HTTP) to the engines over the OpenFMB
 * NATS bus: it publishes generate/simulate commands and gets the profiles +
 * result series back in the events, then returns them to the browser to chart —
 * no inter-engine HTTP, no shared CSV files.
 */

const express = require("express");
const fs = require("fs");
const path = require("path");
const net = require("net");
const { getBus } = require("./bus");

const app = express();
app.use(express.json({ limit: "2mb" }));
app.use(express.static(path.join(__dirname, "public")));

const PORT = process.env.PORT || 3000;
// UI-owned persisted summaries. Engines do not share this directory; generate
// and simulate payloads travel over NATS.
const OUTPUTS_DIR = process.env.OUTPUTS_DIR || path.join(__dirname, "..", "outputs");
// Broker host/port for the health probe, derived from the NATS bus URL.
const NATS_URL = process.env.NATS_URL || "nats://localhost:4222";
const [, BROKER_HOST = "localhost", NATS_PORT = "4222"] =
  NATS_URL.match(/nats:\/\/([^:]+):(\d+)/) || [];

// Short timeout for the small metadata request/reply round trips. Generate and
// simulate keep the bus client's long default (they can run for minutes).
const META_TIMEOUT_MS = 8000;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// Every engine interaction is an OpenFMB request/reply over NATS — the UI never
// talks to the engines over HTTP. Publishes a command and resolves the matching
// event payload, throwing on an engine-side error event or a bus timeout.
async function busCall(service, action, payload = {}, timeoutMs) {
  const bus = getBus();
  if (!bus) throw new Error("Bus transport unavailable (NATS not installed or broker unreachable).");
  const evt = await bus.request(service, action, payload, timeoutMs);
  if (!evt || evt.status !== "ok") {
    const detail = evt && evt.payload && evt.payload.error;
    const err = new Error(detail || `bus ${service}/${action} failed`);
    err.payload = (evt && evt.payload) || {};
    throw err;
  }
  return evt.payload;
}

// Raw TCP probe — NATS speaks its own protocol, not HTTP, so just check the port.
function tcpProbe(host, port, timeoutMs = 2000) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let done = false;
    const finish = (up) => {
      if (done) return;
      done = true;
      sock.destroy();
      resolve(up);
    };
    sock.setTimeout(timeoutMs);
    sock.once("connect", () => finish(true));
    sock.once("timeout", () => finish(false));
    sock.once("error", () => finish(false));
    sock.connect(port, host);
  });
}

// Per-scenario losses aren't written to the result CSVs, so persist them here
// (keyed by scenario) the moment a simulate call returns. This lets the Compare
// tab plot "losses vs penetration" from disk across reloads, not just the
// scenarios run in the current browser session.
const LOSSES_FILE = path.join(OUTPUTS_DIR, "results", "losses.json");
function readLosses() {
  try { return JSON.parse(fs.readFileSync(LOSSES_FILE, "utf8")); }
  catch { return {}; }
}
function persistLosses(simBody) {
  if (!simBody || typeof simBody !== "object") return;
  const scenario = simBody.scenario_name;
  if (!scenario || simBody.total_losses_kwh == null) return;
  try {
    const all = readLosses();
    all[scenario] = {
      total_losses_kwh: simBody.total_losses_kwh,
      der_penetration_percent: simBody.der_penetration_percent ?? null,
      max_voltage_pu: simBody.max_voltage_pu ?? null,
      updated: new Date().toISOString(),
    };
    fs.mkdirSync(path.dirname(LOSSES_FILE), { recursive: true });
    fs.writeFileSync(LOSSES_FILE, JSON.stringify(all, null, 2));
  } catch (e) {
    console.warn("could not persist losses:", String(e));
  }
}

// ---------------------------------------------------------------------------
// status / metadata
// ---------------------------------------------------------------------------

app.get("/api/health", async (_req, res) => {
  const probe = (service) =>
    busCall(service, "health", {}, 4000)
      .then((detail) => ({ up: true, detail }))
      .catch((e) => ({ up: false, detail: { error: String(e.message || e) } }));
  const [load, sim, broker] = await Promise.all([
    probe("load-engine"),
    probe("sim-engine"),
    tcpProbe(BROKER_HOST, parseInt(NATS_PORT, 10)),
  ]);
  res.json({
    load_engine: { up: load.up, detail: load.detail },
    sim_engine: { up: sim.up, detail: sim.detail },
    broker: { up: broker, host: BROKER_HOST, port: parseInt(NATS_PORT, 10) },
  });
});

// Populate the UI dropdowns from the engines themselves (over the NATS bus).
app.get("/api/meta", async (_req, res) => {
  const safe = (p) => p.catch(() => null);
  const [loadCfg, bess, ev, archetypes, classes, simCfg, networks, derTypes, strategies, kpis, importFormats, derElements, controlDevices, tariffs, doeAllocations] = await Promise.all([
    safe(busCall("load-engine", "config", {}, META_TIMEOUT_MS)),
    safe(busCall("load-engine", "bess-configs", {}, META_TIMEOUT_MS)),
    safe(busCall("load-engine", "ev-configs", {}, META_TIMEOUT_MS)),
    safe(busCall("load-engine", "archetypes", {}, META_TIMEOUT_MS)),
    safe(busCall("load-engine", "classes", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "config", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "list-networks", {}, META_TIMEOUT_MS)),
    safe(busCall("load-engine", "der-types", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "strategies", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "kpis", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "import-formats", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "der-elements", {}, META_TIMEOUT_MS)),
    safe(busCall("dr-controller", "control-devices", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "tariffs", {}, META_TIMEOUT_MS)),
    safe(busCall("sim-engine", "doe-allocations", {}, META_TIMEOUT_MS)),
  ]);
  res.json({
    load_config: loadCfg,
    bess_configs: bess ? Object.keys(bess) : [],
    ev_configs: ev ? Object.keys(ev) : [],
    archetypes: archetypes ? Object.keys(archetypes) : [],
    classes: classes || { archetypes: [], custom: [] },
    sim_config: simCfg,
    networks: networks || { default: null, networks: [] },
    der_types: derTypes?.der_types || [],
    strategies: strategies?.strategies || [],
    kpis: kpis?.kpis || [],
    import_formats: importFormats?.formats || [],
    der_elements: derElements?.der_elements || [],
    control_devices: controlDevices?.control_devices || [],
    tariffs: tariffs?.tariffs || [],
    doe_allocations: doeAllocations?.allocations || [],
  });
});

// ---------------------------------------------------------------------------
// network models (plug-and-play, over the NATS bus to the Simulation Engine)
// ---------------------------------------------------------------------------

app.get("/api/networks", async (_req, res) => {
  try { res.json(await busCall("sim-engine", "list-networks", {}, META_TIMEOUT_MS)); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

app.get("/api/networks/:id", async (req, res) => {
  try { res.json(await busCall("sim-engine", "get-network", { network_id: req.params.id }, META_TIMEOUT_MS)); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

app.post("/api/networks", async (req, res) => {
  try { res.status(201).json(await busCall("sim-engine", "save-network", req.body)); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

// Import a PSS/E RAW/RAWX or CIM/CGMES network model.
app.post("/api/networks/import", async (req, res) => {
  try { res.status(201).json(await busCall("sim-engine", "import-network", req.body)); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

app.delete("/api/networks/:id", async (req, res) => {
  try { res.json(await busCall("sim-engine", "delete-network", { network_id: req.params.id })); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

// ---------------------------------------------------------------------------
// custom load profiles (over the NATS bus to the Load Engine)
// ---------------------------------------------------------------------------

app.get("/api/load-profiles", async (_req, res) => {
  try { res.json(await busCall("load-engine", "list-custom-profiles", {}, META_TIMEOUT_MS)); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

app.post("/api/load-profiles", async (req, res) => {
  try { res.status(201).json(await busCall("load-engine", "save-custom-profile", req.body)); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

app.delete("/api/load-profiles/:name", async (req, res) => {
  try { res.json(await busCall("load-engine", "delete-custom-profile", { name: req.params.name })); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

// Auto-assignment preview for the per-bus editor.
app.post("/api/bus-preview", async (req, res) => {
  try { res.json(await busCall("load-engine", "bus-data-preview", req.body)); }
  catch (e) { res.status(502).json({ error: String(e.message || e) }); }
});

// ---------------------------------------------------------------------------
// engine actions
// ---------------------------------------------------------------------------

// Bus status: is the UI able to act as an OpenFMB (NATS) bus participant?
app.get("/api/bus/status", async (_req, res) => {
  const nats = NATS_URL;
  try {
    const bus = getBus();
    await Promise.race([
      bus.ready,
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 2000)),
    ]);
    res.json({ available: true, transport: "nats", broker: nats });
  } catch (e) {
    res.json({ available: false, transport: "nats", broker: nats, error: String(e.message || e) });
  }
});

// Drive generate -> simulate over the OpenFMB command/event bus — NATS only,
// no shared file volume: the profiles travel inline in the messages and the
// result series comes back in the simulate event.
async function pipelineOverBus(load, sim) {
  const bus = getBus();
  if (!bus) throw new Error("Bus transport unavailable (NATS not installed or broker unreachable).");
  const scenario = load.scenario_name || "scenario";

  const genEvt = await bus.request("load-engine", "generate", load);
  if (genEvt.status !== "ok") throw new Error(`generate: ${JSON.stringify(genEvt.payload)}`);
  const profiles = genEvt.payload.profiles;

  const simEvt = await bus.request("sim-engine", "simulate", {
    scenario_name: scenario,
    seed: load.seed,
    der_penetration_percent: load.der_penetration_percent,
    network_id: sim.network_id || load.network_id,   // selected network (no hardcoded default)
    profiles,                       // inline over the bus — no profile_csv
    coordination_mode: sim.coordination_mode || "uncoordinated",
    solve_mode: sim.solve_mode || "balanced",   // balanced | unbalanced power flow
    solver: sim.solver || "opendss",            // power-flow backend (see GET /solvers)
    volt_var: sim.volt_var || false,            // autonomous smart-inverter Volt-VAr
    volt_watt: sim.volt_watt || false,          // autonomous smart-inverter Volt-Watt
    doe: sim.doe || null,                       // export-limit scheme (fixed / dynamic envelopes)
    tariff: sim.tariff || "tou_residential",    // named tariff for the cost KPIs
    twin_config: sim.twin_config || null,       // prosumer shadow-twin config (forwarded over the bus)
  });
  if (simEvt.status !== "ok") throw new Error(`simulate: ${JSON.stringify(simEvt.payload)}`);

  persistLosses(simEvt.payload);
  return { stage: "done", scenario, generate: genEvt.payload, simulate: simEvt.payload, profiles };
}

// 3) Full pipeline: generate -> simulate over the OpenFMB NATS bus (only path).
app.post("/api/pipeline", async (req, res) => {
  const { load = {}, sim = {} } = req.body;
  try {
    res.json(await pipelineOverBus(load, sim));
  } catch (e) {
    res.status(502).json({ stage: "bus", error: String(e.message || e) });
  }
});

// 4) Penetration sweep — run several scenarios back-to-back over the bus.
app.post("/api/sweep", async (req, res) => {
  const { penetrations = [50, 100, 150], base = {}, sim = {} } = req.body;
  const seed = base.seed ?? 42;
  const results = [];
  for (const pen of penetrations) {
    const scenario = `pen${pen}_seed${seed}`;
    const r = await runScenario(
      { ...base, seed, scenario_name: scenario, der_penetration_percent: pen }, sim);
    results.push({ pen, scenario, ...r });
  }
  res.json({ results });
});

// Run one generate -> simulate scenario over the bus; returns { ok, scenario, simulate, kpis }.
async function runScenario(load, sim = {}) {
  try {
    const r = await pipelineOverBus(load, sim);
    return { ok: true, scenario: r.scenario, simulate: r.simulate, kpis: r.simulate.kpis || {} };
  } catch (e) {
    return { ok: false, scenario: load.scenario_name, error: String(e.message || e) };
  }
}

// 5) Modular study runner: penetration sweep, hosting capacity, or Monte Carlo.
app.post("/api/study", async (req, res) => {
  const { mode = "penetration_sweep", base = {}, sim = {}, params = {} } = req.body;
  const seed = base.seed ?? 42;
  const runs = [];

  if (mode === "penetration_sweep") {
    const pens = params.penetrations || [50, 100, 150];
    for (const pen of pens) {
      const r = await runScenario(
        { ...base, seed, scenario_name: `pen${pen}_seed${seed}`, der_penetration_percent: pen }, sim);
      runs.push({ penetration: pen, seed, ...r });
    }
  } else if (mode === "monte_carlo") {
    const pen = base.der_penetration_percent ?? 100;
    const seeds = params.seeds || [1, 2, 3, 4, 5];
    for (const s of seeds) {
      const r = await runScenario(
        { ...base, seed: s, scenario_name: `mc_pen${pen}_seed${s}`, der_penetration_percent: pen }, sim);
      runs.push({ penetration: pen, seed: s, ...r });
    }
  } else if (mode === "hosting_capacity") {
    const kpi = params.kpi || "voltage_violations";
    const threshold = params.threshold ?? 1;       // unsafe when kpi >= threshold
    const start = params.start ?? 50, step = params.step ?? 25, max = params.max ?? 300;
    let hostingCapacity = null;
    for (let pen = start; pen <= max; pen += step) {
      const r = await runScenario(
        { ...base, seed, scenario_name: `hc_pen${pen}_seed${seed}`, der_penetration_percent: pen }, sim);
      runs.push({ penetration: pen, seed, ...r });
      if (!r.ok) break;
      const val = r.kpis?.[kpi];
      if (val != null && val >= threshold) break;   // crossed the limit
      hostingCapacity = pen;                          // last safe level
    }
    return res.json({ mode, kpi, threshold, hosting_capacity: hostingCapacity, runs });
  } else if (mode === "locational_hosting") {
    // Per-bus (locational) hosting capacity: place ALL the PV at one bus and
    // raise penetration until the chosen indicator crosses its limit; repeat
    // for every load bus. Reports the last safe level per bus, in kW.
    const kpi = params.kpi || "voltage_violations";
    const threshold = params.threshold ?? 1;
    const start = params.start ?? 50, step = params.step ?? 50, max = params.max ?? 200;
    const netId = base.network_id || sim.network_id;
    let net;
    try { net = await busCall("sim-engine", "get-network", { network_id: netId }); }
    catch (e) { return res.status(502).json({ error: `get-network: ${String(e.message || e)}` }); }
    const loadBuses = net.buses
      .filter((b) => b.bus_id !== net.source_bus && (b.base_load_kw || 0) > 0)
      .map((b) => b.bus_id);
    const totalBase = net.buses.reduce(
      (s, b) => s + (b.bus_id === net.source_bus ? 0 : (b.base_load_kw || 0)), 0);
    if (!base.network_buses) {
      base.network_buses = net.buses.map((b) => ({
        bus_id: b.bus_id,
        base_load_kw: b.bus_id === net.source_bus ? 0 : (b.base_load_kw || 0),
        base_load_kvar: b.bus_id === net.source_bus ? 0 : (b.base_load_kvar || 0),
      }));
    }
    const targets = (params.buses && params.buses.length) ? params.buses : loadBuses;
    const perBus = [];
    for (const bus of targets) {
      let lastSafe = null;
      for (let pen = start; pen <= max; pen += step) {
        const r = await runScenario(
          { ...base, seed, scenario_name: `lhc_bus${bus}_pen${pen}`,
            der_penetration_percent: pen, pv_buses: [bus] }, sim);
        runs.push({ bus_id: bus, penetration: pen, seed, ...r });
        if (!r.ok) break;
        const val = r.kpis?.[kpi];
        if (val != null && val >= threshold) break;
        lastSafe = pen;
      }
      perBus.push({
        bus_id: bus,
        hosting_penetration_pct: lastSafe,
        hosting_capacity_kw: lastSafe != null
          ? Math.round((lastSafe / 100) * totalBase * 10) / 10 : null,
      });
    }
    return res.json({
      mode, kpi, threshold, total_base_load_kw: totalBase, per_bus: perBus, runs,
    });
  } else {
    return res.status(400).json({ error: `Unknown study mode '${mode}'.` });
  }
  res.json({ mode, runs });
});

// Persisted per-scenario losses (written on each simulate) for the Compare tab.
app.get("/api/losses", (_req, res) => res.json(readLosses()));

app.listen(PORT, () => {
  console.log(`Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration UI listening on http://localhost:${PORT}`);
  console.log(`  engines:     load-engine + sim-engine over the OpenFMB NATS bus`);
  console.log(`  NATS bus:    ${NATS_URL}`);
  console.log(`  outputs dir: ${OUTPUTS_DIR}`);
});
