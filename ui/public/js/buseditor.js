/* Per-bus configuration editor + custom load profile manager (v3.0).
 *
 * The editor renders one row per network bus. In "Per-bus editor" mode the
 * pipeline sends the table as explicit `bus_data` to the Load Engine, giving
 * full control of load, profile, PV, BESS and EV at every bus.
 */
import { api } from "./api.js";
import { $, el } from "./dom.js";
import { toast, logLine } from "./toast.js";

const state = {
  network: null,        // full network detail ({id, buses, branches, source_bus, ...})
  classes: [],          // archetypes + custom:<name> entries
  bessConfigs: [],
  evConfigs: [],
  rendered: false,
  onEdited: null,       // callback fired when the user applies a bus config
};

function classOptions(selected) {
  return state.classes.map((c) => {
    const o = new Option(c, c, false, c === selected);
    return o;
  });
}

function derOptions(items, selected) {
  const opts = [new Option("None", "", false, !selected)];
  items.forEach((it) => opts.push(new Option(it, it, false, it === selected)));
  return opts;
}

function numCell(value, cls, step = "any") {
  return el("input", { type: "number", class: `be-num ${cls}`, value: String(value), min: "0", step });
}

/* Trim trailing zeros so 11.0 -> "11" and 0.4 -> "0.4". */
const fmtKv = (kv) => `${Number(Number(kv).toFixed(3))}`;

function rowFor(bus, cfg) {
  const isSource = state.network && bus.bus_id === state.network.source_bus;
  const tr = el("tr", { "data-bus": bus.bus_id, class: isSource ? "be-source" : "" });

  const profileSel = el("select", { class: "be-profile" }, ...classOptions(cfg.customer_class));
  const bessSel = el("select", { class: "be-bess" }, ...derOptions(state.bessConfigs, cfg.bess_config));
  const evSel = el("select", { class: "be-ev" }, ...derOptions(state.evConfigs, cfg.ev_config));

  // On multi-voltage feeders, tag each bus with its base voltage (LV vs MV).
  const busKv = Number(bus.base_kv) || state.network.base_voltage_kv;
  const kvTag = state._multiLevel ? el("span", { class: "be-kv" }, ` ${fmtKv(busKv)} kV`) : "";

  tr.append(
    el("td", {}, String(bus.bus_id) + (isSource ? " (source)" : ""), kvTag),
    el("td", {}, numCell(cfg.base_load_kw, "be-kw")),
    el("td", {}, numCell(cfg.base_load_kvar, "be-kvar")),
    el("td", {}, profileSel),
    el("td", {}, numCell(cfg.pv_capacity_kw, "be-pv")),
    el("td", {}, bessSel),
    el("td", {}, numCell(cfg.bess_max_charge_kw ?? 0, "be-charge")),
    el("td", {}, numCell(cfg.bess_max_discharge_kw ?? 0, "be-discharge")),
    el("td", {}, evSel),
  );
  if (isSource) {
    tr.querySelectorAll("input,select").forEach((i) => (i.disabled = true));
    tr.title = "Source or slack bus. Carries no load or DERs.";
  }
  return tr;
}

function defaultCfg(bus) {
  return {
    base_load_kw: bus.base_load_kw ?? 0,
    base_load_kvar: bus.base_load_kvar ?? Math.round((bus.base_load_kw ?? 0) * 0.4 * 10) / 10,
    customer_class: state.classes[0] || "res_detached_medium",
    pv_capacity_kw: 0,
    bess_config: null,
    bess_max_charge_kw: 0,
    bess_max_discharge_kw: 0,
    ev_config: null,
  };
}

function renderTable(cfgByBus = {}) {
  if (!state.network) return;
  // Detect multi-voltage feeders so rows can show each bus's level.
  const levels = new Set(state.network.buses.map((b) => Number(b.base_kv) || state.network.base_voltage_kv));
  state._multiLevel = levels.size > 1;
  const tbody = $("be-table").querySelector("tbody");
  tbody.replaceChildren(
    ...state.network.buses.map((bus) =>
      rowFor(bus, cfgByBus[bus.bus_id] || defaultCfg(bus))
    )
  );
  $("be-network").textContent = `${state.network.name || state.network.id} (${state.network.buses.length} buses)`;
  state.rendered = true;
}

/* Read the table back into a Load Engine bus_data array. */
export function getBusData() {
  if (!state.rendered || !state.network) return null;
  const rows = [...$("be-table").querySelectorAll("tbody tr")];
  return rows.map((tr) => {
    const busId = parseInt(tr.dataset.bus, 10);
    const isSource = busId === state.network.source_bus;
    const num = (sel) => Math.max(0, parseFloat(tr.querySelector(sel)?.value) || 0);
    return {
      bus_id: busId,
      base_load_kw: isSource ? 0 : num(".be-kw"),
      base_load_kvar: isSource ? 0 : num(".be-kvar"),
      customer_class: tr.querySelector(".be-profile").value,
      pv_capacity_kw: isSource ? 0 : num(".be-pv"),
      bess_config: (!isSource && tr.querySelector(".be-bess").value) || null,
      bess_max_charge_kw: isSource ? 0 : num(".be-charge"),
      bess_max_discharge_kw: isSource ? 0 : num(".be-discharge"),
      ev_config: (!isSource && tr.querySelector(".be-ev").value) || null,
    };
  });
}

export function getNetwork() { return state.network; }

/* Minimal bus list for Load Engine auto-assignment over this network. */
export function networkBuses() {
  if (!state.network) return null;
  return state.network.buses.map((b) => ({
    bus_id: b.bus_id,
    base_load_kw: b.bus_id === state.network.source_bus ? 0 : (b.base_load_kw ?? 0),
    base_load_kvar: b.bus_id === state.network.source_bus ? 0 : b.base_load_kvar,
  }));
}

export async function setNetwork(id) {
  state.network = await api.network(id);
  renderTable();
  return state.network;
}

export function setCatalogs({ classes, bessConfigs, evConfigs }) {
  if (classes) state.classes = classes;
  if (bessConfigs) state.bessConfigs = bessConfigs;
  if (evConfigs) state.evConfigs = evConfigs;
}

/* Prefill the table from the Load Engine's quick auto-assignment. */
async function prefill(getQuickParams) {
  if (!state.network) return;
  const params = { ...getQuickParams() };
  // The Load Engine has no built-in network, so always send the selected
  // network's bus list for auto-assignment (works for any network).
  params.network_buses = networkBuses();
  $("be-status").textContent = "Prefilling";
  try {
    const { bus_data } = await api.busPreview(params);
    const byBus = {};
    for (const b of bus_data) byBus[b.bus_id] = b;
    renderTable(byBus);
    $("be-status").textContent = `Prefilled from automatic settings at ${params.der_penetration_percent}% PV`;
    if (state.onEdited) state.onEdited();
  } catch (e) {
    toast("Prefill failed", String(e.payload?.detail || e), "error");
    $("be-status").textContent = "";
  }
}

function clearDers() {
  const rows = [...$("be-table").querySelectorAll("tbody tr")];
  rows.forEach((tr) => {
    const pv = tr.querySelector(".be-pv");
    if (pv && !pv.disabled) pv.value = "0";
    ["be-bess", "be-ev"].forEach((c) => {
      const sel = tr.querySelector(`.${c}`);
      if (sel && !sel.disabled) sel.value = "";
    });
  });
  $("be-status").textContent = "All DERs cleared";
  if (state.onEdited) state.onEdited();
}

/* ---------------- custom load profiles ---------------- */

export async function refreshCustomProfiles() {
  let profiles = [];
  try { profiles = (await api.loadProfiles()).profiles || []; } catch { /* engine down */ }
  const tbody = $("cp-table").querySelector("tbody");
  tbody.replaceChildren(...profiles.map((p) => {
    const kind = p.kind || "load";
    const del = el("button", { class: "btn ghost sm" }, "Remove");
    del.addEventListener("click", async () => {
      try {
        await api.deleteLoadProfile(p.name);
        logLine("Custom profile deleted", p.name);
        await refreshCustomProfiles();
      } catch (e) { toast("Delete failed", String(e), "error"); }
    });
    return el("tr", {},
      el("td", {}, kind === "load" ? `custom:${p.name}` : p.name),
      el("td", {}, kind), el("td", {}, String(p.points)),
      el("td", {}, p.description || ""), el("td", {}, del));
  }));
  // Only load-kind shapes are customer classes for the Profile column.
  const archetypes = state.classes.filter((c) => !c.startsWith("custom:"));
  state.classes = [
    ...archetypes,
    ...profiles.filter((p) => (p.kind || "load") === "load").map((p) => `custom:${p.name}`),
  ];
  // Update the table's Profile dropdowns in place, keeping selections.
  $("be-table").querySelectorAll(".be-profile").forEach((sel) => {
    const cur = sel.value;
    sel.replaceChildren(...classOptions(cur));
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  });
  // PV/EV day-shape selectors (Advanced modelling) list matching-kind shapes.
  for (const [id, kind] of [["pv_profile", "pv"], ["ev_profile", "ev"]]) {
    const sel = $(id);
    if (!sel) continue;
    const cur = sel.value;
    sel.replaceChildren(new Option("Model default", ""));
    profiles.filter((p) => p.kind === kind).forEach((p) => sel.append(new Option(p.name, p.name)));
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  }
  return profiles;
}

async function uploadProfile() {
  const name = $("cp-name").value.trim();
  const csvText = $("cp-values").value.trim();
  if (!name || !csvText) {
    toast("Missing input", "Profile name and CSV values are both required.", "error");
    return;
  }
  const kind = $("cp-kind")?.value || "load";
  try {
    const r = await api.uploadLoadProfile({ name, kind, csv_text: csvText });
    toast("Profile saved",
      kind === "load"
        ? `Now selectable as ${r.customer_class} in the Profile column (${r.points} points).`
        : `Now selectable as the ${kind.toUpperCase()} day-shape under Advanced modelling (${r.points} points).`,
      "success");
    logLine("Custom profile saved", r);
    $("cp-name").value = ""; $("cp-values").value = "";
    await refreshCustomProfiles();
  } catch (e) {
    toast("Upload failed", String(e.payload?.detail || e), "error", 7000);
  }
}

/* ---------------- init ---------------- */

export function initBusEditor(getQuickParams, onEdited = null) {
  state.onEdited = onEdited;
  $("be-prefill").addEventListener("click", () => prefill(getQuickParams));
  $("be-clear-der").addEventListener("click", clearDers);

  // Editing any cell in the table is the single way to configure a bus, and it
  // arms Per-bus editor mode so the next run uses the table.
  $("be-table").addEventListener("change", () => { if (state.onEdited) state.onEdited(); });
  $("cp-upload").addEventListener("click", uploadProfile);
  $("cp-file-btn").addEventListener("click", () => $("cp-file").click());
  $("cp-file").addEventListener("change", async () => {
    const f = $("cp-file").files[0];
    if (!f) return;
    $("cp-values").value = await f.text();
    if (!$("cp-name").value) $("cp-name").value = f.name.replace(/\.(csv|txt)$/i, "").replace(/[^a-zA-Z0-9_\-]/g, "_");
    $("cp-file").value = "";
  });
}
