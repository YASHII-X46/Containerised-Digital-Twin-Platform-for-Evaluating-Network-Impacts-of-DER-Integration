# sincal-solver

PSS SINCAL power-flow adapter for Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration: implements the same **solver bus
contract** as `opendss-solver/`, so the Simulation Engine can run studies on
Siemens PSS SINCAL by selecting `"solver": "sincal"` in the simulate request
(UI: Solver control). The Simulation Engine needs no changes to target it —
solvers are a registry, and each solver is a standalone service.

## The solver bus contract

Commands under `{prefix}/command/sincal-solver/{action}` (replies on the
matching event topics, correlation-id matched):

| Action | Payload | Reply |
|--------|---------|-------|
| `build` | `{session_id, network, solve_mode, elements}` | `{status: built, buses, elements}` |
| `solve` | `{session_id, updates: [{op, bus_id, kw?, kvar?}]}` | `{converged}` |
| `read` | `{session_id}` | `{voltages, loadings, losses_kw, power_kw, max_vuf_pct}` |
| `reset` | `{session_id}` | `{status}` |
| `teardown` | `{session_id}` | `{status}` |
| `health` | `{}` | status + a live probe of the local SINCAL COM server |

Update ops: `load` (kW+kvar), `pv` (kW), `pv_q` (kvar), `bess` (kW, signed),
`ev` (kW). The service holds one active session at a time; a new `build`
replaces it.

## Reality check: what runs where

PSS SINCAL is **proprietary, licensed, Windows-only** software driven through
its COM automation interface (`Sincal.Simulation`). Consequences:

- It cannot ship inside a Linux Docker image, so this service is **not** in
  the stack's `docker compose up` (that compose file uses the Linux engine).
- Run this adapter **where a licensed SINCAL is installed**: either directly
  on the Windows host (`pip install -r requirements.txt`, then
  `python -m sincal_solver.main` with `NATS_URL=nats://localhost:4222`), or
  as a Windows container built from `Dockerfile.windows` with your licensed
  installer added.
- Without SINCAL present, the service still starts and answers `health`
  (reporting `sincal_available: false`) and fails `build` with a clear error
  event — the Simulation Engine surfaces that as the simulate error.

## Completion checklist (needs a licensed SINCAL environment)

The COM lifecycle, contract handlers, per-step update mapping, result
readback, and per-session project cloning are implemented. Setup: create one
**empty project** in the SINCAL GUI and point `SINCAL_TEMPLATE` at its `.sin`
file — the adapter clones it (plus its `<name>_files` folder) per session.
Two integration points intentionally raise `SincalUnavailable` until
completed against your installation:

1. `SincalEngine._export_network` — after the clone, insert
   `Node`/`Line`/`TwoWindingTransformer`/`Load`/`DCInfeeder` rows from the
   stack's network model dict into the cloned project database (bus ids
   preserved in element names; Access or SQLite per the template's storage).
2. `SincalEngine._write_element_states` — per-step P/Q updates on those rows
   via the SINCAL database interface.

**PSS SINCAL Xplore note:** the free Xplore edition includes the COM
automation server (verified against 22.5: `Sincal.Simulation` dispatches),
but it caps the network size at a small node count — fine for IEEE test
feeders and thesis-scale studies, but check the cap against your Hawthorn
campus feeder before planning full-network SINCAL runs.

Verify the result-table field names (`U_Un`, `Util`, `Pv`, sequence voltages)
against your SINCAL version's automation documentation — they vary between
releases.

## Tests

`python -m pytest -q` — contract tests run the service handlers over the
in-process loopback bus with a mocked engine (no SINCAL needed), and verify
the no-SINCAL error path is clean.
