# Simulation Engine

The Simulation Engine is a FastAPI service that runs Quasi-Static Time Series
(QSTS) power-flow studies on radial distribution networks. It is
**solver-agnostic**: the power flow itself runs in a separate solver
container (`opendss-solver/` by default; `sincal-solver/` for PSS SINCAL)
reached over the **solver bus contract** — `build`/`solve`/`read`/`reset`/
`teardown` OpenFMB commands. Solvers are a registry
(`app/solvers/registry.py`); the simulate request selects one by name
(`"solver": "opendss"` default) and `register_solver()` adds more without
touching this engine.

It receives Load Engine profiles inline over NATS, opens a solver session for
the selected network, drives every timestep, computes violations and KPIs,
and returns summary and chart data.

## Bus Input

The `sim-engine/simulate` NATS command requires a `profiles` payload in the Load
Engine wire format. `network_id` is required and must reference an uploaded
network (the engine ships none):

```json
{
  "scenario_name": "scenario",
  "network_id": "my_feeder",
  "seed": 42,
  "der_penetration_percent": 100,
  "coordination_mode": "uncoordinated",
  "solve_mode": "balanced",
  "volt_var": false,
  "twin_config": null,
  "profiles": {
    "metadata": {
      "timesteps": 96,
      "resolution_minutes": 15,
      "days": 1
    },
    "buses": {}
  }
}
```

The service validates that every profile bus exists in the selected network.

## Simulation Pipeline

1. Normalize the inline profiles payload.
2. Load the selected network from the registry.
3. Resolve the requested `solver` name to its bus service and send `build`
   with the network model, the `solve_mode` (balanced → symmetric
   three-phase; unbalanced → per-bus phases), and the per-element bus lists.
   The solver container builds the circuit (lines, transformers, DER
   elements) in its own engine and working directory.
4. DER element types come from a registry
   (`app/simulation/der_elements.py`) — each knows its buses, profile series,
   and solver update op — with the matching model builders registered
   solver-side, so a new DER type is added without editing either loop.
5. Run the QSTS loop, for each of the `days × timesteps` steps:
   - buffer building-load, PV, BESS, and EV updates (flushed as one batched
     `solve` command — two bus round trips per solve, any network size);
   - apply autonomous smart-inverter Volt-VAr/Volt-Watt fixed points when
     enabled, and hold operating-envelope export caps;
   - call the DR controller and prosumer shadow-twins modules over NATS when
     coordination is enabled;
   - `read` back voltages, branch loadings, losses, and the VUF, and record
     violations.
6. Release the solver session and return convergence counts, KPI values,
   result series, and per-bus/per-branch summaries.

## Network Registry

The engine ships no built-in or example networks. Every network is user-supplied:
upload JSON through the registry (or drop a JSON file into `NETWORKS_DIR`) and it
becomes selectable. The registry is empty until a network is provided.

Supported network import formats:

| Format | Notes |
|--------|-------|
| JSON | Native internal network model |
| PSS/E RAW | BUS, LOAD, BRANCH, and 2-winding transformer data |
| PSS/E RAWX | JSON RAW variant |
| CIM/CGMES | Single-file EQ-profile subset |
| OpenDSS (.dss) | Master-file subset: Circuit, Line (r1/x1), Load, 2-winding Transformer; linecode/geometry lines and `wdg=` transformers are skipped with a logged count |

Network models contain:

```json
{
  "id": "my_feeder",
  "name": "My feeder",
  "base_voltage_kv": 11.0,
  "source_bus": 1,
  "buses": [
    { "bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0 },
    { "bus_id": 3, "base_load_kw": 25.0, "base_load_kvar": 10.0, "base_kv": 0.4 }
  ],
  "branches": [
    { "branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 0.30, "x_ohm": 0.15, "rating_kva": 3000 },
    { "branch_id": 2, "from_bus": 2, "to_bus": 3, "is_transformer": true, "r_ohm": 0.02, "x_ohm": 0.12, "rating_kva": 500 }
  ]
}
```

Uploads require unique IDs, valid branch endpoints, connected buses, positive
base voltage, and positive branch ratings. A bus may carry an optional `phases`
field (`"a"`, `"ac"`, `"abc"`, or `[1, 3]`; default three-phase) used by
unbalanced `solve_mode` to model single-phase laterals.

A bus may also declare its own `base_kv` (defaulting to `base_voltage_kv`) for
multi-voltage feeders (MV/LV). A branch crossing two voltage levels must set
`"is_transformer": true` and is modelled as a transformer; an ordinary line
across two levels is rejected. PSS/E and CIM imports preserve per-bus voltages
and flag transformer records.

Optional branch fields:

- Lines: `r0_ohm` / `x0_ohm` — explicit zero-sequence impedance. When omitted,
  Z0 defaults to 3x the positive-sequence values (typical for distribution
  feeders); balanced results are unaffected, unbalanced solves gain realistic
  neutral-path impedance.
- Transformers: `"oltc": true` — on-load tap changer regulating the secondary
  side; `"connection"` — `"wye_wye"` (default) or `"delta_wye"` (the
  Dyn11-style group of Australian distribution transformers, isolating zero
  sequence between levels); `"tap"` — fixed secondary off-load tap in per-unit
  (0.8–1.2; values above 1 boost the LV voltage).

## Interfaces (NATS + HTTP)

The UI reaches this engine over the OpenFMB NATS bus only. Each operation is an
OpenFMB command (`openfmb/command/sim-engine/<action>` →
`openfmb/event/sim-engine/<action>`) with a matching FastAPI HTTP route kept for
direct host-side debugging:

| NATS command | HTTP route | Purpose |
|--------------|------------|---------|
| `simulate` | — (NATS only) | Run a QSTS study from inline profiles |
| `health` | GET `/health` | Service health |
| `config` | GET `/config` | Non-sensitive configuration |
| `list-networks` | GET `/networks` | List network models |
| `get-network` | GET `/networks/{id}` | Full topology for one network |
| `save-network` | POST `/networks` | Upload a native JSON network model |
| `import-network` | POST `/networks/import` | Import RAW, RAWX, CIM, or JSON network data |
| `delete-network` | DELETE `/networks/{id}` | Delete a user network model |
| `import-formats` | GET `/import-formats` | Supported network import formats |
| `strategies` | GET `/strategies` | DR coordination strategies |
| `der-elements` | GET `/der-elements` | Registered DER element types |
| `tariffs` | GET `/tariffs` | Registered tariff structures |
| `doe-allocations` | GET `/doe-allocations` | Registered envelope-allocation policies |
| `solvers` | GET `/solvers` | Registered power-flow solver backends |
| `kpis` | GET `/kpis` | Registered KPI names |

## Simulate Command Fields

| Field | Default | Purpose |
|-------|---------|---------|
| `scenario_name` | `scenario` | Scenario identifier |
| `profiles` | required | Inline Load Engine profiles payload |
| `network_id` | required | Network to solve; must reference an uploaded network |
| `seed` | `42` | Scenario seed metadata |
| `der_penetration_percent` | `100` | Scenario penetration metadata |
| `coordination_mode` | `uncoordinated` | `uncoordinated`, `dr_only`, `dr_p2p`, or another registered strategy |
| `solve_mode` | `balanced` | `balanced` (symmetric three-phase) or `unbalanced` (per-bus phases) power flow |
| `solver` | `opendss` | Power-flow solver backend (see `GET /solvers`); each runs as its own container |
| `volt_var` | `false` | Enable autonomous smart-inverter Volt-VAr (AS/NZS 4777.2) on PV |
| `volt_watt` | `false` | Enable autonomous smart-inverter Volt-Watt (AS/NZS 4777.2) on PV |
| `twin_config` | `null` | Optional prosumer shadow-twin configuration, forwarded over the bus |
| `doe` | `null` | Export-limit scheme: `{mode, allocation, method, fixed_export_kw, managed}` (see below) |
| `tariff` | `tou_residential` | Named tariff structure pricing the cost KPIs (see `GET /tariffs`) |

## Dynamic Operating Envelopes

The `doe` field models the Australian move from fixed export limits to
capacity-based dynamic operating envelopes (SA Power Networks Flexible
Exports, Energex/Ergon dynamic connections; CSIP-Aus `opModExpLimW`):

- `mode: "static"` — every export-capable site (PV capacity > 0) gets the
  constant `fixed_export_kw` cap (today's fixed limit; the comparison baseline).
- `mode: "dynamic"` — per-site, per-interval limits computed from network
  headroom on the no-export forecast (load + EV, no PV/BESS injection):
  `method: "sensitivity"` linearises with one perturbation per site and then
  allocates headroom under `allocation` — `equal` (same kW per site),
  `prorata` (proportional to capability), or `max_total` (maximise total
  export; LP via scipy, greedy fallback). `method: "search"` binary-searches
  the uniform capability fraction per interval (exact; pro-rata by
  construction).
- Enforcement: autonomous by default — the QSTS loop holds each site's net
  export (PV + battery discharge − load − EV) at its limit, reducing battery
  discharge first and curtailing PV for the remainder. With
  `managed: true` (requires a coordination mode) the coordinator instead
  publishes each site's `exportLimit_kW` with its status and the DR
  controller's envelope plugin enforces it (store first, curtail rest).
- Outputs: `doe_curtailed_kwh` and `doe_envelope_utilisation_pct` (also
  registered as KPIs), plus per-interval `doe_envelope_total` /
  `doe_export_total` series for charting. Envelope precedence composes with
  Volt-Watt and DR: envelope cap first, autonomous Volt-Watt on the capped
  output, commanded DR curtailment on top.

## Autonomous Inverter Responses

Independent of DR coordination, PV inverters can respond to their local voltage
per AS/NZS 4777.2 (local inverter behaviour, not controller commands):

- `volt_var: true` — reactive-power mode: absorb vars at high voltage, inject
  at low voltage, deadband around nominal.
- `volt_watt: true` — real-power backstop: output reduces linearly above the
  1.09 pu knee down to a 20% floor at 1.10 pu.

Both run as a short fixed point each timestep before the coordinator. When DR
coordination and Volt-Watt are both active, the precedence is defined:
commanded DR curtailment applies on top of the Volt-Watt-reduced output (from
the bus's local voltage), so a curtailment command never silently undoes the
autonomous standards response.

## Transformer Tap Changers (OLTC)

A transformer branch may set `"oltc": true` to carry an on-load tap changer:
an OpenDSS RegControl that holds the secondary side at 1.0 pu within a 2% band
using the transformer's ±10% tap range. Validation rejects `oltc` on ordinary
lines. OpenDSS iterates the tap position inside each solve.

## Response

The response includes:

- convergence count;
- voltage and thermal violation counts;
- min and max voltage;
- max branch loading;
- total losses in kWh;
- DER bus counts;
- DR coordination summary fields;
- registered KPI values;
- per-timestep chart series;
- per-bus voltage summary;
- per-branch loading summary.

## DR Coordination

When `coordination_mode` is not `uncoordinated`, the engine coordinates with two
bus-only modules through NATS:

| Module | Bus service | Responsibility |
|--------|-------------|----------------|
| DR Controller | `dr-controller` | Selects the registered DR strategy and returns DER control setpoints |
| Prosumer Shadow Twins | `prosumer-shadow-twins` | Maintains per-prosumer expected state and records controlled outcomes |

| Mode | Behaviour |
|------|-----------|
| `uncoordinated` | No grid-responsive DER control |
| `dr_only` | Volt-Watt PV curtailment and EV deferral |
| `dr_p2p` | Local battery absorption before remaining PV curtailment |

The Simulation Engine owns the solve loop (executed by the solver container).
During each coordinated timestep, it requests shadow-twin status from
`prosumer-shadow-twins`, sends that status to `dr-controller`, applies
returned setpoints through the solver session, and records the final
controlled outcome back to `prosumer-shadow-twins`.

## KPIs

KPIs are registered in `app/metrics/kpi_registry.py`. Built-in KPI outputs
include voltage extremes, violation rates, losses, reverse-power hours,
hosting-capacity-oriented metrics, the highest transformer loading
(`max_transformer_loading_pct`), tariff costs (`energy_cost_aud`,
`export_revenue_aud`, `net_energy_cost_aud` — priced by the named tariff from
the **tariff registry**, `app/metrics/tariffs.py`: built-ins `tou_residential`
and `flat`, both env-configurable; register a `Tariff` to add more), an
energy-balance self-check (`energy_balance_error_pct`, source power vs expected
net load plus losses — near 0% on a healthy uncoordinated run; control actions
raise it by design), the worst voltage-unbalance factor (`max_vuf_pct`,
negative/positive sequence, planning limit 2%), transformer insulation ageing
(`transformer_loss_of_life_pct`, an IEC 60076-7-style hot-spot model at 78 K
rated rise, 98 degC reference, 180,000 h life, ambient from
`TRANSFORMER_AMBIENT_C`), and grid emissions for imported energy
(`emissions_kg_co2e` at `EMISSIONS_KG_PER_KWH`).

Dynamic-operating-envelope allocation policies live in their own registry
(`app/control/envelopes.py`): built-ins `equal`, `prorata`, `max_total`;
`register_allocation()` makes a new policy selectable by name from the
simulate request with no engine edits.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUS_PREFIX` | `openfmb` | OpenFMB command/event topic prefix |
| `BUS_ENABLED` | `true` | Start the NATS bus participant on boot |
| `BUS_TRANSPORT` | `nats` | `nats` or `loopback` |
| `NATS_URL` | `nats://localhost:4222` | NATS broker URL |
| `PROFILES_DIR` | `outputs/profiles` | Directory used by local profile helper utilities |
| `NETWORKS_DIR` | `outputs/networks` | User network model directory |
| `DEFAULT_NETWORK` | _(empty)_ | Optional preferred network id; empty uses the first available uploaded network |
| `VOLTAGE_LOWER_PU` | `0.95` | Lower voltage limit |
| `VOLTAGE_UPPER_PU` | `1.05` | Upper voltage limit |
| `THERMAL_LIMIT_PCT` | `100.0` | Branch loading limit |
| `TARIFF_PEAK_RATE` | `0.45` | Import tariff in the peak window (AUD/kWh) |
| `TARIFF_OFFPEAK_RATE` | `0.22` | Import tariff outside the peak window (AUD/kWh) |
| `TARIFF_FEED_IN_RATE` | `0.05` | Feed-in rate for exported energy (AUD/kWh) |
| `TARIFF_PEAK_START` | `15.0` | Peak window start hour (24-h clock) |
| `TARIFF_PEAK_END` | `21.0` | Peak window end hour (24-h clock) |
| `FLAT_RATE` | `0.30` | Anytime rate for the built-in `flat` tariff (AUD/kWh) |
| `TRANSFORMER_AMBIENT_C` | `25.0` | Ambient temperature for the transformer ageing KPI |
| `EMISSIONS_KG_PER_KWH` | `0.60` | Grid emissions intensity for imported energy |

## Local Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8002
```

The full stack normally starts this service through `docker compose up --build`.
