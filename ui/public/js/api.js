/* Thin API client for the Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration orchestrator. */

async function req(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error ? JSON.stringify(body.error) : `HTTP ${res.status}`);
    err.payload = body;
    throw err;
  }
  return body;
}

const post = (path, data) =>
  req(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(data) });

export const api = {
  health: () => req("/api/health"),
  meta: () => req("/api/meta"),
  pipeline: (load, sim) => post("/api/pipeline", { load, sim }),
  busStatus: () => req("/api/bus/status"),
  sweep: (penetrations, base, sim) => post("/api/sweep", { penetrations, base, sim }),
  study: (mode, base, sim, params) => post("/api/study", { mode, base, sim, params }),
  losses: () => req("/api/losses"),
  // v3.0: pluggable networks + custom load profiles + per-bus editor
  networks: () => req("/api/networks"),
  network: (id) => req(`/api/networks/${encodeURIComponent(id)}`),
  uploadNetwork: (model) => post("/api/networks", model),
  importNetwork: (payload) => post("/api/networks/import", payload),
  deleteNetwork: (id) => req(`/api/networks/${encodeURIComponent(id)}`, { method: "DELETE" }),
  loadProfiles: () => req("/api/load-profiles"),
  uploadLoadProfile: (p) => post("/api/load-profiles", p),
  deleteLoadProfile: (name) => req(`/api/load-profiles/${encodeURIComponent(name)}`, { method: "DELETE" }),
  busPreview: (params) => post("/api/bus-preview", params),
};
