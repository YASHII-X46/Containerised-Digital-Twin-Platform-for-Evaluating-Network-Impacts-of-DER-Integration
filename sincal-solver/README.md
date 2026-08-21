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

- Run this adapter **where a licensed SINCAL is installed**. In the stack it
  is a Windows container built from `Dockerfile`, on a base image carrying
  your licensed installation (build arg `SINCAL_BASE`).
- It also runs directly on a Windows host for development:
  `pip install -r requirements.txt`, then `python -m sincal_solver.main` with
  `NATS_URL=nats://localhost:4222`.
- Without SINCAL present, the service still starts and answers `health`
  (reporting `sincal_available: false`) and fails `build` with a clear error
  event — the Simulation Engine surfaces that as the simulate error.

## Status (needs a licensed SINCAL environment to run)

Complete and exercised end to end: the COM lifecycle, the contract handlers,
per-session project creation, the network export, per-step P/Q updates and
result readback. Driven against a 48-bus MV/LV model it agrees with OpenDSS to
within the transformer magnetising current and iron losses, balanced and
unbalanced.

Each session gets its own project, created with SINCAL's `SinDBCreate.exe`
(`/DBSYS:SQLITE /TYPE:E`). Set `SINCAL_TEMPLATE` to an existing `.sin` to have
every run inherit that project's house settings; the adapter then clones the
file together with its `<name>_files` folder.

Model rows are written by `sincal_schema`, which matches every write against the
columns the installed release carries, so the mapping holds across SINCAL
versions. That module lives in two places by design — the generators keep a copy
and this package vendors one — and a test asserts the two are byte identical.

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
