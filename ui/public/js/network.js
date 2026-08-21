/* Interactive SVG feeder map.
 *
 * Lays the network out as a tidy tree (BFS spanning tree from the source bus,
 * children spread vertically) and colours each bus by its voltage after a run.
 * Pure SVG + DOM — no external graph library — so it scales with the viewport
 * via a fixed viewBox and stays inside the no-scroll layout.
 */
import { $, el } from "./dom.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const VB_W = 1000, VB_H = 620, MX = 46, MY = 38;

let state = null;   // { net, pos, source, voltsByBus }

const svg = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  return node;
};

const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const lerp = (a, b, t) => a.map((c, i) => Math.round(c + (b[i] - c) * t));
const rgb = (c) => `rgb(${c[0]},${c[1]},${c[2]})`;

const NOMINAL = [56, 211, 159], LOW = [58, 107, 255], HIGH = [255, 77, 77], NEUTRAL = [90, 107, 125];

/* Distinct hues for each voltage level (MV/LV regions), assigned by sorted kV. */
const LEVEL_PALETTE = ["#7c5cff", "#1fb6c9", "#e0902a", "#c850b0", "#5a9e3a", "#d85a7a"];

/* Trim trailing zeros so 11.0 -> "11" and 0.4 -> "0.4". */
const fmtKv = (kv) => (kv == null ? "" : `${Number(kv.toFixed(3))}`);

/* Diverging colour centred on 1.0 pu: blue (low) · green (nominal) · red (high). */
function voltageColor(v) {
  if (v == null) return rgb(NEUTRAL);
  if (v <= 1.0) return rgb(lerp(NOMINAL, LOW, clamp((1.0 - v) / 0.05, 0, 1)));
  return rgb(lerp(NOMINAL, HIGH, clamp((v - 1.0) / 0.05, 0, 1)));
}

/* BFS spanning tree from the source, then a tidy vertical layout (leaves get
 * sequential slots, parents sit at the mean of their children). */
function computeLayout(net) {
  const ids = net.buses.map((b) => b.bus_id);
  const adj = new Map(ids.map((id) => [id, []]));
  for (const br of net.branches || []) {
    if (adj.has(br.from_bus) && adj.has(br.to_bus)) {
      adj.get(br.from_bus).push(br.to_bus);
      adj.get(br.to_bus).push(br.from_bus);
    }
  }
  const source = net.source_bus ?? ids[0];
  const depth = new Map(), children = new Map(), seen = new Set();
  ids.forEach((id) => children.set(id, []));
  const queue = [source];
  seen.add(source); depth.set(source, 0);
  while (queue.length) {
    const u = queue.shift();
    for (const v of adj.get(u) || []) {
      if (!seen.has(v)) {
        seen.add(v); depth.set(v, depth.get(u) + 1);
        children.get(u).push(v); queue.push(v);
      }
    }
  }
  let slot = 0;
  const yRank = new Map();
  const assignY = (u) => {
    const kids = children.get(u);
    if (!kids.length) { yRank.set(u, slot++); return yRank.get(u); }
    const ys = kids.map(assignY);
    const y = ys.reduce((a, b) => a + b, 0) / ys.length;
    yRank.set(u, y); return y;
  };
  assignY(source);
  ids.forEach((id) => { if (!yRank.has(id)) { depth.set(id, 0); yRank.set(id, slot++); } });

  const maxD = Math.max(1, ...depth.values());
  const maxY = Math.max(1, slot - 1);
  const pos = new Map();
  for (const id of ids) {
    pos.set(id, {
      x: MX + (depth.get(id) / maxD) * (VB_W - 2 * MX),
      y: MY + (yRank.get(id) / maxY) * (VB_H - 2 * MY),
    });
  }
  return { pos, source };
}

function tooltip() {
  let tip = $("netgraph-tip");
  if (!tip) {
    tip = el("div", { id: "netgraph-tip", class: "netgraph-tip" });
    $("netgraph").append(tip);
  }
  return tip;
}

function showTip(bus_id, evt) {
  const tip = tooltip();
  const v = state.voltsByBus?.[bus_id];
  const isSource = bus_id === state.source;
  const kv = state.busKv?.get(bus_id);
  const hosting = state.hostingByBus?.[bus_id];
  const head = `<b>Bus ${bus_id}</b>${isSource ? " (source)" : ""}` +
    (kv != null ? ` <span class="muted">${fmtKv(kv)} kV</span>` : "") +
    (hosting != null ? `<br>Hosting capacity ${hosting} kW` : "");
  tip.innerHTML = v
    ? `${head}<br>` +
      `minimum ${v.min.toFixed(4)}, mean ${v.mean.toFixed(4)}, maximum ${v.max.toFixed(4)} pu` +
      (v.viol ? `<br><span class="tip-warn">${v.viol} violation${v.viol > 1 ? "s" : ""}</span>` : "")
    : `${head}<br><span class="muted">Run the pipeline to see voltages</span>`;
  const host = $("netgraph").getBoundingClientRect();
  tip.style.left = `${evt.clientX - host.left + 14}px`;
  tip.style.top = `${evt.clientY - host.top + 12}px`;
  tip.classList.add("show");
}

function hideTip() { $("netgraph-tip")?.classList.remove("show"); }

/* Render the whole graph (edges, then nodes). Called on network change. */
export function setNetworkGraph(net) {
  const host = $("netgraph");
  if (!host || !net || !net.buses?.length) {
    if (host) host.replaceChildren(el("div", { class: "ph show" }, "Select a network model to view its topology."));
    state = null;
    return;
  }
  const { pos, source } = computeLayout(net);
  // Per-bus base voltage and a distinct colour per level (high kV -> low kV).
  const busKv = new Map(net.buses.map((b) => [b.bus_id, Number(b.base_kv) || net.base_voltage_kv]));
  const levels = [...new Set(busKv.values())].sort((a, b) => b - a);
  const levelColor = new Map(levels.map((kv, i) => [kv, LEVEL_PALETTE[i % LEVEL_PALETTE.length]]));
  const multiLevel = levels.length > 1;
  const hasXfmr = (net.branches || []).some((br) => br.is_transformer);
  state = { net, pos, source, voltsByBus: null, busKv, levels, levelColor, multiLevel, hasXfmr };

  const root = svg("svg", { class: "netgraph-svg", viewBox: `0 0 ${VB_W} ${VB_H}`, preserveAspectRatio: "xMidYMid meet" });

  const edges = svg("g", { class: "edges" });
  for (const br of net.branches || []) {
    const a = pos.get(br.from_bus), b = pos.get(br.to_bus);
    if (!a || !b) continue;
    const isXfmr = !!br.is_transformer;
    edges.append(svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: `edge${isXfmr ? " transformer" : ""}` }));
    if (isXfmr) {
      // A diamond at the branch midpoint marks the step-up/step-down transformer.
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      edges.append(svg("rect", { x: mx - 5, y: my - 5, width: 10, height: 10,
        class: "xfmr-marker", transform: `rotate(45 ${mx} ${my})` }));
    }
  }
  root.append(edges);

  const nodes = svg("g", { class: "nodes" });
  for (const bus of net.buses) {
    const p = pos.get(bus.bus_id);
    const isSource = bus.bus_id === source;
    const g = svg("g", { class: `node ${isSource ? "source" : ""}`, transform: `translate(${p.x},${p.y})` });
    g.dataset.bus = bus.bus_id;
    g.append(svg("circle", { r: isSource ? 11 : 7, class: "node-dot", fill: rgb(NEUTRAL) }));
    const ring = svg("circle", { r: isSource ? 11 : 7, class: "node-ring" });
    // On multi-voltage feeders, the ring colour marks each bus's voltage level
    // (via a CSS var so a violation can still override it).
    if (state.multiLevel) g.style.setProperty("--level", state.levelColor.get(state.busKv.get(bus.bus_id)));
    g.append(ring);
    const label = svg("text", { class: "node-label", y: -13, "text-anchor": "middle" });
    label.textContent = bus.bus_id;
    g.append(label);
    g.addEventListener("mousemove", (e) => showTip(bus.bus_id, e));
    g.addEventListener("mouseleave", hideTip);
    nodes.append(g);
  }
  root.append(nodes);

  host.replaceChildren(root, legend());
}

function legend() {
  const items = [
    el("span", {}, el("i", { class: "lg lg-lo" }), "Under-voltage"),
    el("span", {}, el("i", { class: "lg lg-ok" }), "Nominal"),
    el("span", {}, el("i", { class: "lg lg-hi" }), "Over-voltage"),
  ];
  // On multi-voltage feeders, surface each level's ring colour and the transformer marker.
  if (state?.multiLevel) {
    for (const kv of state.levels) {
      items.push(el("span", {},
        el("i", { class: "lg lg-level", style: `border-color:${state.levelColor.get(kv)}` }),
        `${fmtKv(kv)} kV`));
    }
  }
  if (state?.hasXfmr) {
    items.push(el("span", {}, el("i", { class: "lg lg-xfmr" }), "Transformer"));
  }
  items.push(el("span", { class: "muted" }, "Hover over a bus for detail"));
  return el("div", { class: "netgraph-legend" }, ...items);
}

/* Paint per-bus (locational) hosting capacity onto the map: red = least
 * headroom, green = most. Buses without a result stay neutral. */
export function paintLocationalHosting(perBus) {
  if (!state || !Array.isArray(perBus) || !perBus.length) return;
  const byBus = {};
  for (const r of perBus) byBus[r.bus_id] = r.hosting_capacity_kw;
  state.hostingByBus = byBus;
  const values = perBus.map((r) => r.hosting_capacity_kw).filter((v) => v != null);
  const lo = Math.min(...values), hi = Math.max(...values);
  const scale = (v) => {
    if (v == null) return rgb(NEUTRAL);
    const t = hi > lo ? (v - lo) / (hi - lo) : 1.0;   // 0 = worst, 1 = best
    return t < 0.5 ? rgb(lerp(HIGH, [224, 144, 42], t * 2))
                   : rgb(lerp([224, 144, 42], NOMINAL, (t - 0.5) * 2));
  };
  $("netgraph").querySelectorAll(".node").forEach((g) => {
    const id = Number(g.dataset.bus);
    if (id === state.source) return;
    g.querySelector(".node-dot").setAttribute("fill", scale(byBus[id] ?? null));
  });
}

/* Recolour nodes by the per-bus voltage summary from a completed run. */
export function updateNetworkVoltages(busSummary) {
  if (!state || !Array.isArray(busSummary)) return;
  const by = {};
  for (const r of busSummary) {
    by[r.bus_id] = { min: r.min_voltage_pu, mean: r.mean_voltage_pu, max: r.max_voltage_pu, viol: r.violation_count };
  }
  state.voltsByBus = by;
  const host = $("netgraph");
  host.querySelectorAll(".node").forEach((g) => {
    const id = Number(g.dataset.bus);
    const v = by[id];
    const dot = g.querySelector(".node-dot");
    // Colour by the worst excursion from nominal (max for over-, min for under-).
    let pick = null;
    if (v) pick = Math.abs(v.max - 1.0) >= Math.abs(1.0 - v.min) ? v.max : v.min;
    dot.setAttribute("fill", voltageColor(id === state.source ? 1.0 : pick));
    g.classList.toggle("violating", !!(v && v.viol));
  });
}
