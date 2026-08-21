# opendss-solver

Standalone OpenDSS power-flow container for Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration. The Simulation Engine
contains no solver code: it opens a session here over the OpenFMB/NATS bus and
drives the circuit step by step. Any alternative engine that implements the
same contract is a drop-in solver (see `sincal-solver/` for the PSS SINCAL
adapter); the simulate request picks the backend by name (`"solver":
"opendss"` is the default).

## The solver bus contract

Commands under `{prefix}/command/opendss-solver/{action}`, replies on the
matching event topics (correlation-id matched):

| Action | Payload | Reply |
|--------|---------|-------|
| `build` | `{session_id, network, solve_mode, elements: {type: [bus dicts]}}` | `{status: built, buses, elements: {type: count}}` |
| `solve` | `{session_id, updates: [{op, bus_id, kw?, kvar?}]}` | `{converged}` |
| `read` | `{session_id}` | `{voltages, loadings, losses_kw, power_kw, max_vuf_pct}` |
| `reset` | `{session_id}` | `{status}` |
| `teardown` | `{session_id}` | `{status, existed}` |
| `health` | `{}` | service status + registered element types |

Update ops (batched by the client, applied before each solve): `load`
(kW + kvar), `pv` (kW), `pv_q` (kvar, autonomous/commanded VAr), `bess`
(signed kW), `ev` (kW).

The service is **pure power flow** — no control logic lives here. Inverter
responses (Volt-VAr/Volt-Watt), operating envelopes, and DR coordination stay
in the Simulation Engine, which iterates solve/read to its own fixed points.
That keeps every solver backend dumb, swappable, and easy to validate.

OpenDSS is a process-wide singleton, so one session is active at a time; a
new `build` replaces the previous session (logged, and the old session's
commands then error cleanly). Each build writes its OpenDSS files into its
own per-session working directory under `DSS_DIR` — no shared-file collisions.

## Layout

- `dss_solver/engine.py` — the OpenDSSDirect.py wrapper (solve + readback)
- `dss_solver/dss_model.py` — network model dict → `.dss` script generation
  (multi-voltage transformers, vector groups, taps, OLTC, zero-sequence,
  balanced/unbalanced connection models)
- `dss_solver/elements.py` — pluggable DER element builders (pv/bess/ev +
  `register()` for new device types; unknown types are skipped with a log)
- `dss_solver/service.py` — the bus command handlers
- `dss_solver/network.py` — read-only view of the (already validated) model

The package is named `dss_solver` (not `app`) so the Simulation Engine's test
suite can import it next to its own `app` package and run this service
in-process on the loopback transport — the tests exercise the exact code the
container runs.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `NATS_URL` / `BUS_PREFIX` / `BUS_TRANSPORT` | `nats://localhost:4222` / `openfmb` / `nats` | Bus wiring (`loopback` for tests) |
| `DSS_DIR` | `dss_work` | Root for per-session OpenDSS working directories |

## Tests

`python -m pytest -q` — real-OpenDSS physics tests (OLTC regulation, fixed
taps, delta-wye groups, VUF on unbalanced solves, multi-voltage bases) plus
`.dss` generation and full bus-contract round trips.
