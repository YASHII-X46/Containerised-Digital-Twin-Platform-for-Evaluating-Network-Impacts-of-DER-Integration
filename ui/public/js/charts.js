/* Chart.js wrappers — one instance per canvas, theme-aware. */
import { $ } from "./dom.js";

const COLORS = {
  load: "#4f8cff", pv: "#f5b942", ev: "#a06cff", bess: "#2bb8c4", net: "#38d39f",
  maxv: "#ff5d6c", meanv: "#38d39f", minv: "#4f8cff", bar: "#4f8cff", loss: "#38d39f",
  warn: "#ff5d6c", amber: "#f5b942",
};
const SERIES = ["#4f8cff", "#38d39f", "#f5b942", "#ff5d6c", "#a06cff"];

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function gridTick() {
  return { grid: cssVar("--grid") || "#232c38", tick: cssVar("--muted") || "#8696a7", text: cssVar("--text") || "#e8eef5" };
}

const charts = {};

/* Dashed vertical separators at day boundaries on multi-day horizons.
 * Enabled per chart via options.dayMarkers = { days, stepsPerDay }. */
const dayMarkers = {
  id: "dayMarkers",
  afterDraw(chart) {
    const m = chart.config.options?.dayMarkers;
    if (!m || !m.days || m.days < 2 || !m.stepsPerDay) return;
    const { ctx, chartArea, scales } = chart;
    if (!scales.x || !chartArea) return;
    ctx.save();
    ctx.strokeStyle = cssVar("--muted") || "#8696a7";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.globalAlpha = 0.55;
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.font = "9px sans-serif";
    for (let d = 1; d < m.days; d++) {
      const px = scales.x.getPixelForValue(d * m.stepsPerDay);
      if (!Number.isFinite(px) || px < chartArea.left || px > chartArea.right) continue;
      ctx.beginPath();
      ctx.moveTo(px, chartArea.top);
      ctx.lineTo(px, chartArea.bottom);
      ctx.stroke();
      ctx.fillText(`Day ${d + 1}`, px + 3, chartArea.top + 9);
    }
    ctx.restore();
  },
};

function render(id, config) {
  const Chart = window.Chart;
  if (!Chart || !$(id)) return;
  const { grid, tick, text } = gridTick();
  config.options = config.options || {};
  config.options.responsive = true;
  config.options.maintainAspectRatio = false;
  config.options.animation = { duration: 350 };
  config.options.plugins = {
    legend: { labels: { color: text, boxWidth: 12, font: { size: 10 } } },
    ...(config.options.plugins || {}),
  };
  const baseScale = { grid: { color: grid }, ticks: { color: tick, font: { size: 10 } } };
  config.options.scales = Object.fromEntries(
    Object.entries(config.options.scales || { x: {}, y: {} }).map(([k, v]) => [
      k,
      { ...baseScale, ...v, grid: { ...baseScale.grid, ...(v.grid || {}) }, ticks: { ...baseScale.ticks, ...(v.ticks || {}) } },
    ])
  );
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), { ...config, plugins: [...(config.plugins || []), dayMarkers] });
}

const line = (label, data, color, w = 2) => ({ label, data, borderColor: color, backgroundColor: color, pointRadius: 0, borderWidth: w, tension: 0.3 });
const refLine = (label, value, n, color) => ({ type: "line", label, data: Array(n).fill(value), borderColor: color, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, fill: false });
const axis = (text) => ({ title: { display: true, text, color: cssVar("--muted") } });

export const ChartManager = {
  /* ---- Summary: aggregate feeder net-load duck curve ---- */
  duck(d, marks) {
    render("chart-duck", {
      type: "line",
      data: { labels: d.timesteps, datasets: [
        { label: "Net load (duck)", data: d.total_net_kw, borderColor: COLORS.net, backgroundColor: "rgba(56,211,159,0.16)", fill: true, pointRadius: 0, borderWidth: 2.5, tension: 0.35 },
        line("Gross load", d.total_load_kw, COLORS.load, 1.5),
      ] },
      options: { dayMarkers: marks, scales: { x: axis("timestep"), y: axis("kW") } },
    });
  },

  /* ---- DER Mix: 3a component decomposition ---- */
  dermix(d) {
    const negPv = d.total_pv_kw.map((v) => -v);
    const datasets = [
      line("Building load", d.total_load_kw, COLORS.load),
      line("PV generation", negPv, COLORS.pv),
      line("EV charging", d.total_ev_kw, COLORS.ev),
      line("BESS dispatch", d.total_bess_kw, COLORS.bess),
    ];
    // Show the aggregate of any extra DER plugins only when present, so new DER
    // types appear automatically without hardcoding them here.
    const other = d.total_other_kw || [];
    if (other.some((v) => Math.abs(v) > 1e-9)) {
      datasets.push(line("Other DER", other, "#c084fc"));
    }
    // Operating-envelope band: published export limit (dashed) vs achieved
    // export — only when an export-limit scheme ran.
    if (d.doe?.envelope?.length) {
      datasets.push({ ...line("Export envelope", d.doe.envelope, "#e0902a", 1.5), borderDash: [6, 4] });
      if (d.doe.export?.length) datasets.push(line("Site export", d.doe.export, "#c084fc", 1.5));
    }
    datasets.push(line("Net load", d.total_net_kw, COLORS.net, 3));
    render("chart-dermix", {
      type: "line",
      data: { labels: d.timesteps, datasets },
      options: { dayMarkers: d.dayMarks, scales: { x: axis("timestep"), y: axis("kW") } },
    });
  },

  /* ---- Voltage: temporal envelope (existing) ---- */
  voltage(d, marks) {
    render("chart-voltage", {
      type: "line",
      data: { labels: d.timesteps, datasets: [
        line("max", d.max_voltage_pu, COLORS.maxv),
        line("mean", d.mean_voltage_pu, COLORS.meanv),
        line("min", d.min_voltage_pu, COLORS.minv),
        refLine("0.95", 0.95, d.timesteps.length, COLORS.amber),
      ] },
      options: { dayMarkers: marks, scales: { x: axis("timestep"), y: { ...axis("pu"), suggestedMin: 0.92, suggestedMax: 1.08 } } },
    });
  },

  /* ---- Voltage: 3b spatial profile along feeder (min-max band + mean) ---- */
  vprofile(rows) {
    const r = rows.filter((b) => b.bus_id >= 1);
    const labels = r.map((b) => b.bus_id);
    render("chart-vprofile", {
      type: "line",
      data: { labels, datasets: [
        { label: "max", data: r.map((b) => b.max_voltage_pu), borderColor: COLORS.maxv, backgroundColor: "rgba(79,140,255,0.18)", pointRadius: 0, borderWidth: 1.5, fill: "+1", tension: 0.2 },
        { label: "min", data: r.map((b) => b.min_voltage_pu), borderColor: COLORS.minv, pointRadius: 0, borderWidth: 1.5, fill: false, tension: 0.2 },
        line("mean", r.map((b) => b.mean_voltage_pu), COLORS.meanv),
        refLine("0.95", 0.95, labels.length, COLORS.amber),
      ] },
      options: { scales: { x: axis("Bus ID"), y: { ...axis("pu"), suggestedMin: 0.92, suggestedMax: 1.08 } } },
    });
  },

  /* ---- Violations: 3d per-bus violation count ---- */
  violbar(rows) {
    const r = rows.filter((b) => b.bus_id >= 1);
    render("chart-violbar", {
      type: "bar",
      data: { labels: r.map((b) => b.bus_id), datasets: [
        { label: "Violations", data: r.map((b) => b.violation_count), borderRadius: 3,
          backgroundColor: r.map((b) => (b.violation_count > 0 ? COLORS.warn : COLORS.bar)) },
      ] },
      options: { plugins: { legend: { display: false } }, scales: { x: axis("Bus ID"), y: { ...axis("count"), beginAtZero: true } } },
    });
  },

  /* ---- Thermal: per-branch max loading bar (existing) ---- */
  branch(d) {
    if (!d.branch_max_loading?.length) return;
    render("chart-branch", {
      type: "bar",
      data: { labels: d.branch_max_loading.map((b) => b.branch_id),
        datasets: [{ label: "Max loading %", data: d.branch_max_loading.map((b) => b.max_loading_pct), backgroundColor: COLORS.bar, borderRadius: 4 }] },
      options: { plugins: { legend: { display: false } }, scales: { x: axis("Branch ID"), y: axis("%") } },
    });
  },

  /* ---- Thermal: 3e max branch loading over time ---- */
  loadtime(arr, marks) {
    render("chart-loadtime", {
      type: "line",
      data: { labels: arr.map((_, i) => i), datasets: [
        line("Max branch loading", arr, COLORS.bar),
        refLine("100% limit", 100, arr.length, COLORS.warn),
      ] },
      options: { dayMarkers: marks, scales: { x: axis("timestep"), y: { ...axis("%"), beginAtZero: true } } },
    });
  },

  /* ---- Compare: 3f cross-scenario overlays ---- */
  compareMaxV(points) {
    render("chart-cmp-maxv", {
      type: "bar",
      data: { labels: points.map((p) => p.label), datasets: [
        { label: "Max voltage (pu)", data: points.map((p) => p.maxv), backgroundColor: COLORS.maxv, borderRadius: 4 },
        refLine("1.05", 1.05, points.length, COLORS.amber),
      ] },
      options: { scales: { x: axis("penetration"), y: { ...axis("pu"), suggestedMin: 1.0, suggestedMax: 1.1 } } },
    });
  },
  compareLoss(points) {
    render("chart-cmp-loss", {
      type: "line",
      data: { labels: points.map((p) => p.label), datasets: [
        { ...line("Total losses (kWh)", points.map((p) => p.loss), COLORS.loss, 2.5),
          backgroundColor: "rgba(56,211,159,0.16)", fill: true },
      ] },
      options: { scales: { x: axis("penetration"), y: { ...axis("kWh"), beginAtZero: false } } },
    });
  },
  compareDuck(series) {
    render("chart-cmp-duck", {
      type: "line",
      data: { labels: series[0]?.labels || [], datasets: series.map((s, i) => line(s.label, s.net, SERIES[i % SERIES.length])) },
      options: { scales: { x: axis("timestep"), y: axis("net kW") } },
    });
  },

  redrawAll() { Object.values(charts).forEach((c) => c.update()); },
  resizeAll() { Object.values(charts).forEach((c) => c.resize()); },
};
