/* Client-side CSV fetch + parse (reads the already-exported result CSVs
   via the /files static route — no backend/data/CSV logic is changed). */

export function parseCsv(text) {
  const lines = text.split(/\r?\n/).filter((l) => l.length);
  if (!lines.length) return { header: [], rows: [] };
  const header = lines[0].split(",");
  const rows = lines.slice(1).map((line) => {
    const cells = line.split(",");
    const o = {};
    header.forEach((h, i) => (o[h] = cells[i]));
    return o;
  });
  return { header, rows };
}

export async function fetchCsv(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return parseCsv(await r.text());
}

/* voltages.csv → { matrix[busIdx][timestep] = voltage_pu, buses, T } */
export function buildVoltageMatrix(rows) {
  const buses = [...new Set(rows.map((r) => +r.bus_id))].sort((a, b) => a - b);
  const T = Math.max(...rows.map((r) => +r.timestep)) + 1;
  const idx = new Map(buses.map((b, i) => [b, i]));
  const matrix = buses.map(() => new Array(T).fill(NaN));
  for (const r of rows) matrix[idx.get(+r.bus_id)][+r.timestep] = +r.voltage_pu;
  return { matrix, buses, T };
}

/* branches.csv → array of max loading_pct across all branches per timestep */
export function maxLoadingPerStep(rows) {
  const T = Math.max(...rows.map((r) => +r.timestep)) + 1;
  const arr = new Array(T).fill(0);
  for (const r of rows) {
    const t = +r.timestep, v = +r.loading_pct;
    if (v > arr[t]) arr[t] = v;
  }
  return arr;
}
