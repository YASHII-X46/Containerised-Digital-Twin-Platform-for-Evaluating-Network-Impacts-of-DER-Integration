/* Reads/writes the configuration form and builds engine payloads. */
import { $, $$ } from "./dom.js";

/* Numeric field value with a fallback ONLY when the field is empty/invalid.
 * `parseFloat(x) || fb` would also replace a legitimate 0 (e.g. a battery
 * charge window starting at midnight), silently changing the user's input. */
const num = (id, fallback) => {
  const v = parseFloat($(id)?.value);
  return Number.isFinite(v) ? v : fallback;
};

export function loadParams() {
  const pv = $("pv_buses").value.split(",").map((s) => parseInt(s.trim(), 10)).filter(Number.isInteger);
  return {
    scenario_name: $("scenario_name").value.trim() || "scenario",
    seed: parseInt($("seed").value, 10),
    network_id: $("network_id").value,
    der_penetration_percent: parseFloat($("der_penetration_percent").value),
    season: $("season").value,
    timesteps: parseInt($("timesteps").value, 10),
    resolution_minutes: parseInt($("resolution_minutes").value, 10),
    days: parseInt($("days")?.value, 10) || 1,
    pv_buses: pv.length ? pv : null,   // null = all load buses
    bess_penetration: parseFloat($("bess_penetration").value),
    bess_config: $("bess_config").value,
    bess_dispatch_mode: $("bess_dispatch_mode")?.value || "self_consumption",
    bess_charge_window: [num("bess_charge_start", 1), num("bess_charge_end", 6)],
    bess_discharge_window: [num("bess_discharge_start", 17), num("bess_discharge_end", 21)],
    ev_penetration: parseFloat($("ev_penetration").value),
    ev_config: $("ev_config").value,
    // Scenario-level custom DER day-shapes (null = built-in models).
    pv_profile: $("pv_profile")?.value || null,
    ev_profile: $("ev_profile")?.value || null,
    // Advanced modelling (modular)
    weather_source: $("weather_source")?.value || "none",
    reactive_floor: parseFloat($("reactive_floor").value),
    ev_charging_mode: $("ev_charging_mode").value,
    ev_offpeak_start_hour: parseFloat($("ev_offpeak_start_hour").value),
    diversity: {
      enabled: $("diversity_enabled").checked,
      admd_kw: parseFloat($("diversity_admd").value),
      sigma_minutes: parseFloat($("diversity_sigma").value),
    },
    ev_diversity: {
      enabled: $("ev_diversity_enabled").checked,
      arrival_sigma_minutes: parseFloat($("ev_diversity_sigma").value),
    },
  };
}

export function simParams() {
  const coordination_mode = $("coordination_mode").value;
  const doeMode = $("doe_mode")?.value || "off";
  return {
    coordination_mode,
    solve_mode: $("solve_mode")?.value || "balanced",
    solver: $("solver")?.value || "opendss",
    volt_var: $("volt_var")?.checked || false,
    volt_watt: $("volt_watt")?.checked || false,
    // Export-limit scheme: null for none, else fixed cap or dynamic envelopes.
    doe: doeMode === "off" ? null : {
      mode: doeMode,
      fixed_export_kw: num("doe_fixed_kw", 1.5),
      allocation: $("doe_allocation")?.value || "equal",
      managed: $("doe_managed")?.checked || false,
    },
    tariff: $("tariff")?.value || "tou_residential",
    network_id: $("network_id").value,
    // Prosumer shadow-twin config is only meaningful under DR coordination; the
    // engine ignores it otherwise. Sent as null when uncoordinated.
    twin_config: coordination_mode === "uncoordinated" ? null : twinConfig(),
  };
}

/* Read the prosumer shadow-twin configuration form into an override object. */
function twinConfig() {
  return {
    min_pv_kw: parseFloat($("twin_min_pv_kw")?.value) || 0,
    nominal_voltage_pu: parseFloat($("twin_nominal_v")?.value) || 1.0,
    include_ev_only: $("twin_include_ev_only")?.checked ?? true,
  };
}

/* ---------------- saved scenario configurations (localStorage) ---------------- */

const CFG_KEY = "dtstack_saved_configs";

const cfgFields = () =>
  [...document.querySelectorAll("#config-panel .fields input[id], #config-panel .fields select[id]")]
    .filter((el) => el.type !== "file" && el.id !== "saved_configs");

export function savedConfigs() {
  try { return JSON.parse(localStorage.getItem(CFG_KEY)) || {}; } catch { return {}; }
}

export function refreshSavedConfigs(selected = "") {
  const sel = $("saved_configs");
  if (!sel) return;
  const names = Object.keys(savedConfigs()).sort();
  sel.replaceChildren(new Option("Select a configuration", ""));
  names.forEach((n) => sel.append(new Option(n, n)));
  if (names.includes(selected)) sel.value = selected;
}

/* Save every sidebar control under the current scenario name. */
export function saveCurrentConfig() {
  const name = $("scenario_name").value.trim() || "scenario";
  const all = savedConfigs();
  const snapshot = {};
  for (const el of cfgFields()) snapshot[el.id] = el.type === "checkbox" ? el.checked : el.value;
  all[name] = snapshot;
  localStorage.setItem(CFG_KEY, JSON.stringify(all));
  refreshSavedConfigs(name);
  return name;
}

/* Restore a saved snapshot, firing input/change so dependent UI follows. */
export function applySavedConfig(name) {
  const cfg = savedConfigs()[name];
  if (!cfg) return false;
  for (const el of cfgFields()) {
    if (!(el.id in cfg)) continue;
    if (el.type === "checkbox") el.checked = !!cfg[el.id];
    else el.value = cfg[el.id];
    el.dispatchEvent(new Event("input"));
    el.dispatchEvent(new Event("change"));
  }
  return true;
}

export function deleteSavedConfig(name) {
  const all = savedConfigs();
  if (!(name in all)) return false;
  delete all[name];
  localStorage.setItem(CFG_KEY, JSON.stringify(all));
  refreshSavedConfigs();
  return true;
}

export function fillSelect(id, items, preferred) {
  const sel = $(id);
  if (!items?.length) return;
  sel.innerHTML = "";
  items.forEach((it) => sel.append(new Option(it, it)));
  if (items.includes(preferred)) sel.value = preferred;
}

// Low/mid/high DER quick-select levels. The scenario name is derived from the
// chosen penetration and the current seed, so nothing is hardcoded.
const PRESETS = {
  low: { der_penetration_percent: 50, bess_penetration: 0.2, ev_penetration: 0.1 },
  mid: { der_penetration_percent: 100, bess_penetration: 0.3, ev_penetration: 0.2 },
  high: { der_penetration_percent: 150, bess_penetration: 0.4, ev_penetration: 0.3 },
};

export function applyPreset(name, syncSliders) {
  const p = PRESETS[name];
  if (!p) return;
  $("der_penetration_percent").value = p.der_penetration_percent;
  const seed = ($("seed").value || "0").trim();
  $("scenario_name").value = `pen${p.der_penetration_percent}_seed${seed}`;
  $("bess_penetration").value = p.bess_penetration;
  $("ev_penetration").value = p.ev_penetration;
  $$(".chip").forEach((c) => c.classList.toggle("active", c.dataset.preset === name));
  syncSliders();
}

/** Wire range sliders to their value labels. */
export function bindSliders() {
  const sync = () => {
    $("der_val").textContent = `${$("der_penetration_percent").value}%`;
    $("bess_val").textContent = (+$("bess_penetration").value).toFixed(2);
    $("ev_val").textContent = (+$("ev_penetration").value).toFixed(2);
  };
  ["der_penetration_percent", "bess_penetration", "ev_penetration"].forEach((id) =>
    $(id).addEventListener("input", () => { sync(); $$(".chip").forEach((c) => c.classList.remove("active")); })
  );
  sync();
  return sync;
}
