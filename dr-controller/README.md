# DR Controller

The DR Controller is a bus-only Python service. It connects to the NATS broker
as the `dr-controller` participant and answers OpenFMB command/event messages.

## Bus Commands

| Command | Purpose |
|---------|---------|
| `configure` | Start or update a coordination session for a DR strategy |
| `control` | Convert prosumer DER status messages into DER control setpoints |
| `strategies` | Return available DR strategies |
| `stop` | Clear a coordination session |

## Strategies

| Strategy | Behaviour |
|----------|-----------|
| `dr_only` | Volt-Watt PV curtailment and EV deferral |
| `dr_p2p` | `dr_only` plus self-absorption into the local battery before remaining curtailment |
| `pv_curtail_only` | Volt-Watt PV curtailment only (no EV) |

Strategies are registered in a catalog (`app/strategy_catalog.py`); registering a
new factory makes it selectable with no edits to the dispatcher.

## Control Plugins

DER setpoints are produced by an ordered registry of control plugins
(`app/control_plugins.py`), each responsible for one DER type:

| Plugin | Order | Action |
|--------|-------|--------|
| `bess` | 10 | Absorb excess PV into the local battery (`dr_p2p`) |
| `bess_support` | 12 | Discharge the battery on under-voltage (peak support), bounded by the energy above minimum SOC |
| `pv` | 20 | Volt-Watt curtailment of remaining over-voltage |
| `pv_reactive` | 22 | Commanded inverter VAr dispatch: inject on under-voltage, absorb on over-voltage (up to 44% of rating) |
| `envelope` | 25 | Hold site export at a published `exportLimit_kW` (managed dynamic operating envelopes): store the excess in the battery first, curtail the rest; curtailment combines with the voltage response by maximum |
| `ev` | 30 | Defer EV charging |

A new controllable DER (for example a heat pump that sheds) is added by writing
a `ControlPlugin` and calling `register()` — no edits to the controller or the
strategies. Plugins run in order, so earlier mitigations (storage) are tried
before later ones (curtailment).

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUS_PREFIX` | `openfmb` | OpenFMB command/event topic prefix |
| `BUS_TRANSPORT` | `nats` | `nats` or `loopback` |
| `NATS_URL` | `nats://localhost:4222` | NATS broker URL |
| `VOLTAGE_LOWER_PU` | `0.95` | Lower voltage limit |
| `VOLTAGE_UPPER_PU` | `1.05` | Upper voltage limit |
