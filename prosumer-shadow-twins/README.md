# Prosumer Shadow Twins

Prosumer Shadow Twins is a bus-only Python service. It connects to the NATS
broker as the `prosumer-shadow-twins` participant and owns per-prosumer twin
state for coordinated Simulation Engine runs.

## Bus Commands

| Command | Purpose |
|---------|---------|
| `start` | Create a coordination session from inline Load Engine profiles |
| `status` | Return DER status messages for each prosumer twin |
| `record` | Record final controlled outcomes for a timestep |
| `summary` | Return accumulated coordination outcomes |
| `stop` | Clear a coordination session |

## Twin State

Each twin mirrors expected PV, BESS, EV, and state-of-charge values from the
Load Engine profiles. During coordinated runs it records measured voltage,
curtailed PV energy, deferred EV energy, PV energy absorbed by storage, and
battery energy discharged for under-voltage (peak) support. It
also accepts extra readings, so outcomes for custom or other DER types (e.g.
energy shed by a non-built-in DER) are accumulated into the summary alongside
the built-in PV/BESS/EV figures. Status messages include the site's demand
(`loadDemand_kW`), which backs export-limit (dynamic-operating-envelope)
control in the DR controller.

## Twin Selection

Which buses become shadow twins is configurable. A bus qualifies when one of its
DER capacities exceeds the matching threshold (all-zero thresholds reproduce the
"any DER present" rule); `INCLUDE_EV_ONLY` controls whether EV-only buses are
tracked. Thresholds default from the environment and can be overridden per
session via the `start` command's `config` block (forwarded from the Simulation
Engine's `twin_config`), so selection is tunable without redeploying.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUS_PREFIX` | `openfmb` | OpenFMB command/event topic prefix |
| `BUS_TRANSPORT` | `nats` | `nats` or `loopback` |
| `NATS_URL` | `nats://localhost:4222` | NATS broker URL |
| `MIN_PV_KW` | `0.0` | Minimum PV capacity for a bus to become a twin |
| `MIN_BESS_KWH` | `0.0` | Minimum battery capacity for a bus to become a twin |
| `MIN_EV_KW` | `0.0` | Minimum EV charge rate for a bus to become a twin |
| `INCLUDE_EV_ONLY` | `true` | Track EV-only buses (no PV/BESS) as twins |
