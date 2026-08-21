/* "System" tab — a live map of the modular stack.
 *
 * Every engine exposes its registries over the OpenFMB bus; this renders them so
 * the plug-and-play architecture is visible and always reflects what is actually
 * registered (DER generators, sim DER elements, control devices, KPIs, …).
 */
import { $, el } from "./dom.js";

function pills(items) {
  if (!items || !items.length) return [el("span", { class: "reg-empty" }, "none")];
  return items.map((it) => el("span", { class: "reg-pill" }, String(it)));
}

function registry(title, items) {
  return el("div", { class: "reg" },
    el("div", { class: "reg-head" },
      el("span", { class: "reg-title" }, title),
      el("span", { class: "reg-count" }, String(items?.length || 0)),
    ),
    el("div", { class: "reg-pills" }, ...pills(items)),
  );
}

function moduleCard(name, subtitle, badge, registries) {
  return el("div", { class: "mod-card" },
    el("div", { class: "mod-head" },
      el("div", { class: "mod-dot" }),
      el("div", { class: "mod-name" }, name),
      el("span", { class: "mod-badge" }, badge),
    ),
    el("div", { class: "mod-sub" }, subtitle),
    el("div", { class: "mod-body" }, ...registries),
  );
}

export function renderModules(m) {
  const root = $("modules-grid");
  if (!root) return;
  const classes = [...(m.classes?.archetypes || []), ...(m.classes?.custom || [])];
  const derGen = (m.der_types || []).map((d) => (typeof d === "string" ? d : d.name));
  const kpis = (m.kpis || []).map((k) => (typeof k === "string" ? k : k.name));
  const strategies = (m.strategies || []).map((s) => (typeof s === "string" ? s : s.name));
  const formats = (m.import_formats || []).map((f) => (typeof f === "string" ? f : f.name));

  root.replaceChildren(
    moduleCard("Load Engine", "Per-bus demand and DER profile generation", "Service", [
      registry("DER generators", derGen),
      registry("Customer classes", classes),
      registry("Battery configurations", m.bess_configs),
      registry("EV configurations", m.ev_configs),
    ]),
    moduleCard("Simulation Engine", "OpenDSS quasi-static time-series power flow", "Service", [
      registry("DER elements", m.der_elements),
      registry("Impact indicators", kpis),
      registry("Network formats", formats),
    ]),
    moduleCard("DR Controller", "Demand-response coordination", "Module", [
      registry("Coordination strategies", strategies),
      registry("Control devices", m.control_devices),
    ]),
    moduleCard("Prosumer Twins", "Per-bus shadow state and DR outcomes", "Module", [
      registry("Tracked DERs", ["pv", "bess", "ev", "custom"]),
      registry("DR outcomes", ["curtailed", "deferred", "stored", "shed"]),
    ]),
  );
}
