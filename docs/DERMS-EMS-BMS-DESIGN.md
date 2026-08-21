# DERMS, EMS and BMS layer, design

Phase 0 deliverable for `EMS-BMS-DERMS-BUILD-PROMPT.md`. No implementation code
has been written. This document is the artefact for the phase 0 gate. It records
the module layout, the message schemas, where the precedence ladder is
implemented, the OpenEMS concept mapping, the split between engine-owned physics
and DERMS-owned policy, and the places where I think the brief needs changing.

---

## 1. Prerequisite status

This brief states that `NETWORK-MODEL-BUILD-PROMPT.md` runs first because this
work consumes the buses, zones and DER records it produces. It has not been run.

| Artefact | Consumed by | Status |
|---|---|---|
| `sample-networks/swinburne_hawthorn_v5.0_scenario.json` | BMS zone presets, EMS site components, DERMS portfolio | Missing |
| `sample-networks/MODEL-DESIGN.md` | Reading list | Missing |
| `swinburne_hawthorn_v5.0` model | The network all three layers run on | Missing, only v3.0 exists |
| `../Report_DCH5_Final-Project.pdf` | Site, zone and equipment data | Present, read, see section 8 |

Phase 0 is unblocked because the brief carries the site data inline in its
section 4.1, and I have verified that data against the report. Phases 1 and
later need the scenario preset.

---

## 2. What each layer owns, and the module layout

Three new services, each copying the `dr-controller` structure exactly.

```
bms-controller/          the building
  app/bus/               vendored bus library, as every service does
  app/config.py          BMS_* settings with documented defaults
  app/main.py            NATS service entry, readiness marker
  app/openfmb.py         message builders
  app/zone_models.py     registry: rc1, rc2
  app/building_plugins.py registry: hvac 10, ventilation 15, lighting 20, dhw 30, plug 40
  app/comfort_policies.py registry: fixed_band, adaptive, precool
  app/shed_strategies.py registry: schedule, setpoint_setback, duty_cycle,
                         occupancy_scaled, priority_order, drm_mode
  app/presets.py         named building and zone presets, including swinburne_atc
  app/service.py         command handlers, session state

ems-controller/
  app/backend_registry.py  native (default), openems_edge (optional)
  app/domain/              OpenEMS domain model: component.py, nature.py,
                           channel.py, sum.py, cycle.py, scheduler.py,
                           power_solver.py
  app/controllers.py       registry: balancing, peak_shaving, grid_optimized_charge,
                           time_of_use_tariff, fix_active_power,
                           limit_total_discharge, envelope_compliance, drm_response
  app/objectives.py        registry: peak_shave, cost_min, self_consumption,
                           emissions_min, demand_charge_min, envelope_aware,
                           community_share
  app/optimisers.py        registry: rule_based, lp, mpc
  app/assets.py            registry: bess, ev, pv, bms_load, generic
  app/predictors.py        registry: perfect, persistence, noisy
  app/bidders.py           registry: none, truthful, cost_plus, withholding
  app/service.py

derms-controller/
  app/aggregation.py       by_feeder, by_transformer, by_site_group, flat
  app/objective_registry.py feeder_peak, constraint_relief, min_curtailment,
                           max_hosting, emissions_min, cost_of_dispatch
  app/issuance.py          equal, prorata, max_total, priority, rotating
  app/markets.py           none, uniform_price, pay_as_bid
  app/instructions.py      envelope_only, drm_mode, price_signal, capacity_limit
  app/compliance.py        observe_only, penalty, progressive_derate
  app/forecast.py          perfect, persistence, noisy
  app/service.py
```

---

## 3. The precedence ladder, and where it is implemented

### 3.1 Implementation point, a recommendation against extending RemoteCoordinator

The brief offers a choice: extend `RemoteCoordinator` or add a sibling composed
into the QSTS loop. I recommend the **sibling**, for two reasons.

`RemoteCoordinator` is already 279 lines and owns one job well, the twin and
DR-controller exchange with its own precedence rules for Volt-Watt and managed
envelopes. Adding three more layers with their own cadences would make it the
largest and least testable object in the stack, and it is covered by tests that
must keep passing unchanged.

Proposed shape:

```
QSTSSimulation.run()
  └─ ControlStack.step(t, engine, ...)        new, in app/control/control_stack.py
       ├─ DermsClient.plan(...)     on the DERMS cadence, not every step
       ├─ EmsClient.instruct(...)   when a new envelope or DRM mode arrives
       ├─ EmsClient.dispatch(...)   every step
       ├─ BmsClient.step(...)       every step
       ├─ apply setpoints to the solver
       ├─ RemoteCoordinator.coordinate(...)   unchanged, the existing DR layer
       ├─ EmsClient.settle(...)
       └─ DermsClient.settle(...)   on the DERMS cadence
```

`ControlStack` is inert when all three configs are absent, so the existing code
path is byte identical, which is what rule 4 requires.

### 3.2 The double-counting hazard, the most likely correctness bug in this build

The brief asks in section 9 for a Load Engine `hvac` plugin producing unmanaged
building HVAC, and in section 6 for a BMS that computes managed HVAC power. If
both reach the power flow, HVAC is counted twice.

Verified mechanism in the current code:

- `load-engine/app/profiles/generator.py:439` derives `other_der_kw` as
  `net_load_kw` minus the built-in contributions, so any registered plugin's
  series flows into it automatically.
- `simulation-engine/app/simulation/qsts.py:106` folds `other_der_kw` into the
  building load each timestep.

So the brief's section 9.5 claim holds: an `hvac` generation plugin reaches the
power flow with zero engine edits. That is exactly why the hazard is real.

**Rule, to be implemented and tested explicitly.** For any bus where the BMS is
active in a run, the Load Engine's `hvac_kw` is the **unmanaged reference
baseline only**. The BMS output replaces it in the power flow for that timestep,
and the difference is booked as `bms_shed_kwh`. For buses with no BMS, the
unmanaged series applies as generated. A dedicated test asserts that enabling
the BMS with a null demand limit reproduces the unmanaged HVAC energy to within
solver tolerance, which is the only way to prove no double count.

### 3.3 Cadence, and the message cost of the ladder

A naive reading gives six extra bus round trips per timestep. On a 96 step day
that is 576, and on the 31 day maximum horizon it is nearly 18,000. Design
choices that keep this affordable:

- Every payload is **all sites in one message**, which the brief's schemas
  already assume with their `sites[]` arrays. Never one message per site.
- DERMS `plan` and `settle` run on `DERMS_PLAN_CADENCE_STEPS`, default 4, which
  is one hour at 15 minute resolution, not every step.
- EMS `dispatch` and BMS `step` run every step, since they are the fast inner
  loop. That is two extra round trips per step, in the same order as the
  existing twin and DR exchange.

---

## 4. OpenEMS concept mapping

### 4.1 The backend decision, agreed

`native` in Python as the deterministic default, `openems_edge` as an optional
bridge that is absent from the default compose file and required by no test,
following the `sincal-solver` precedent for optional dependencies. This is the
right call and it is forced by the determinism rule: OpenEMS Edge is a JVM
service on a real-time cycle and cannot step deterministically in simulated
time.

### 4.2 What is adopted, and what is not

| OpenEMS concept | Adopted | How it appears here |
|---|---|---|
| Component with stable id | Yes | `ess0`, `meter0`, `pv0`, `evcs0`, `ctrl...`, configured per session |
| Nature, the typed interface | Yes | Symmetric and managed symmetric energy storage, electricity meter, DC charger, EV charging station. Controllers address Natures, never concrete devices |
| Channel with `component/Channel` address | Yes | Typed, united, read or write direction. All state crossing the bus is a channel value |
| Sum component | Yes | Site grid, production, consumption and storage aggregate |
| Cycle | Yes | Fixed order per timestep: read state into channels, run scheduler, controllers write, resolve constraints, apply |
| Scheduler | Yes | Explicit configured controller order, the same ordered-registry idea as the stack's control plugins |
| Power constraint solver | Yes | Linear constraints accumulated by controllers, resolved to a feasible active and reactive setpoint per storage component |
| Predictor | Yes | Forecast channels for load, production and price |
| OSGi runtime and bundles | No | Replaced by the stack's registry pattern |
| JSON-RPC and websocket API | No | The stack is bus only |
| Backend, UI and Edge deployment split | No | Out of scope, the UI here is the existing control panel |
| Persistence and Influx timeseries | No | The historian, if the twin build lands, otherwise the existing outputs |

No OpenEMS source is vendored. The lineage is attributed in
`ems-controller/README.md` with a link and a sentence on what was adopted.

---

## 5. Headroom physics versus policy, the split

The brief's rule 8 says physics stays in the Simulation Engine. Concretely:

| Concern | Owner | Note |
|---|---|---|
| Baseline no-export state, sensitivities, per-interval headroom | Simulation Engine, `app/control/envelopes.py` | Already implemented, unchanged |
| Allocation of that headroom to sites when DERMS is off | Simulation Engine, existing allocation registry | Unchanged, byte identical results |
| Allocation when DERMS is on | `derms-controller`, issuance registry | The engine passes headroom as data, DERMS returns limits |
| Enforcement of the resulting limits | Simulation Engine and dr-controller | Unchanged, physical enforcement stays where the solve is |

`equal`, `prorata` and `max_total` appear in both modules by design, with the
same semantics. Both READMEs must state which module owns which half so a reader
is not learning two vocabularies. The engine exposes headroom through a new
`headroom` field on the twin or simulate response rather than a new service.

---

## 6. Message schemas

Envelope as always. Payloads only, all sites or buildings batched.

### 6.1 `bms-controller/step`

```json
{
  "session_id": "s-1",
  "t": 34,
  "timestamp": "2026-02-01T08:30:00Z",
  "step_hours": 0.25,
  "ambient_temp_c": 27.4,
  "irradiance_wm2": 610.0,
  "buildings": [
    {"bus_id": 12, "demand_limit_kw": 180.0, "drm_mode": null,
     "setpoint_offset_c": 0.0, "mode": "normal"}
  ]
}
```

Response:

```json
{
  "buildings": [
    {"bus_id": 12, "hvac_kw": 164.2, "ventilation_kw": 11.0,
     "controllable_kw": 175.2, "shed_kw": 22.8, "min_feasible_kw": 121.5,
     "unmet_thermal_kwh": 0.0, "comfort_violation": false,
     "ventilation_violation": false,
     "zones": [{"zone_id": "ATC101", "zone_temp_c": 23.4, "zone_co2_ppm": 780.0}]}
  ]
}
```

`min_feasible_kw` is the contract that stops the EMS scheduling a demand limit
the building physically cannot meet, and it is computed from the comfort band
and the AS 1668.2 outside air rate together.

### 6.2 `ems-controller/dispatch`

Request carries per-site `soc`, `site_demand_kw`, `pv_kw`, `ev_pending_kwh`,
`voltage_pu`, `export_limit_kw`, `min_feasible_kw` and the forecast slice.
Response per site:

```json
{
  "sites": [
    {"site_id": "atc", "bess_charge_kw": 0.0, "bess_discharge_kw": 18.0,
     "ev_charge_kw": 7.0, "pv_limit_kw": 32.76, "bms_demand_limit_kw": 180.0,
     "inter_site_transfer_kw": 0.0, "import_target_kw": 145.0,
     "objective_value": -42.15, "binding_constraints": ["soc_min", "export_limit"],
     "solver_status": "optimal"}
  ]
}
```

### 6.3 `derms-controller/plan`

Response per site carries `export_limit_kw`, `import_limit_kw`, `drm_mode`,
`price_signal`, a validity interval, `objective_value` and
`binding_constraints`. Demand response modes are AS/NZS 4755 mode identifiers
and envelopes are CSIP-Aus `opModExpLimW` semantics, matching the vocabulary the
stack already uses.

---

## 7. Registries

All listed over the bus and shown in the UI System view, all supporting drop-in
loading through `*_PLUGIN_MODULES` and `*_PLUGINS_DIR` with errors logged and
skipped, never fatal.

Counts: BMS four registries, EMS seven, DERMS seven, eighteen new registries in
total, each with a listing command.

---

## 8. Swinburne preset data, verified against the report

Read from `../Report_DCH5_Final-Project.pdf`, cited as a data source only. None
of that project's control technology, protocols, gateways or emulator is used
here.

| Item | Report value | Page |
|---|---|---|
| Sites | ATC and AMDC, adjacent, Hawthorn campus | 12 |
| Selection criteria | Proximity, zoning diversity, roof space, timetable access, controllable HVAC | 12 |
| Transformer separation | A legislative prohibition against connecting two sources from two separate transformers, and the shared-meter option was discarded | 12 |
| PV | 84 x Trina TSM-390-DE09.08, 390 W, 15 degree tilt, ATC west wing roof | 16 |
| Inverters | 3 x Sungrow SH10RT hybrid, 10 kVA each | 16 |
| Battery | 3 x BYD HVS 10.2, 10.2 kWh each | 16 |
| Inverter roles | The three inverters represent ATC, AMDC and a shared community storage system, a logical split | 16 |
| Instrumented zones | ATC101, ATC103, ATC206, AMDC301, AMDC303, AMDC355, AMDC451 | 26 |
| Zone character | Large lecture theatres and private study areas, theatre use booked but dynamic, study area occupancy entirely stochastic | 26 |
| Installation standards | AS 1170.2, AS 4777.1, AS 4777.2, AS 5033, AS 3000, AS 3008 | 14, 16 |

The transformer separation finding is the physical basis for
`allow_inter_site_sharing` defaulting to false, and it comes from the report
rather than from an assumption.

**One figure I could not verify.** The brief's validation case 4 asks to compare
against "roughly 98 percent self-consumption the ATC system measured in
service". A text search of the report for self-consumption returns no match, so
that figure is either in a figure image, a table image, or phrased differently
in the results section. It must be located and cited exactly before it is used
as a validation target, or the case must be restated. I will not quote a number
I cannot point at.

Registry entries to add, named as the brief asks: battery `byd_hvs_10.2`, stack
`swinburne_atc_stack`, inverter `sungrow_sh10rt`, array `swinburne_atc_pv`. The
32.76 kWp against ATC's roughly 900 kW peak is about 3.6 percent, and that ratio
is stated wherever the preset is documented so nobody reads this as a
high-penetration site.

---

## 9. KPIs and tariff

Twenty-two new KPIs as listed in the brief's section 10.6, every one computable
on a run with all three layers off, reporting the honest baseline or zero and
never `None`. They follow the existing registry's docstring and unit discipline.

`tou_commercial` extends the `Tariff` dataclass with an optional demand-charge
rate and window defaulting to zero, so every existing tariff and every existing
cost result is unchanged. That default is what protects the regression.

---

## 10. Fairness

Every issuance policy declares in one sentence who bears curtailment first and
why. The KPI set reports `curtailment_fairness_index` across sites. The README
states plainly that `max_total` maximises total export at the cost of unequal
treatment and `equal` does the reverse. Validation case 10 reports both, and the
document states which I would defend to a regulator and why, rather than letting
a default make the ethical choice silently.

---

## 11. Testing, target at least 90 new tests

BMS 30, EMS 30, DERMS 20, Simulation Engine integration 10, Load Engine 5. The
integration set includes the full six-stage ladder in one test where a DERMS
envelope overrides an EMS intent and the gap appears in
`ems_intent_curtailed_kwh`, the no-double-count test from section 3.2, and a
regression test asserting a request without the new layers returns identical key
metrics to a stored baseline. Loopback in-process throughout, so the real
services are exercised rather than mocked.

---

## 12. Deployment

Three services added to both compose files following the existing patterns, bus
only with no published HTTP ports, readiness healthchecks, and the Simulation
Engine waiting on all three being healthy. The optional OpenEMS Edge bridge is
not in the default compose file. `WINDOWS.md` gains the three services and their
`Dockerfile.windows` files.

Note for the Windows deployment: this brief takes the stack from eight services
to eleven, and the twin brief would take it to fifteen. Every Windows image
shares the Server Core base layer, so disk grows by roughly the application
layer per service rather than the base, but build time grows linearly. A minimal
compose profile that omits the optional layers is worth having, and I will
propose one at the deployment phase.

---

## 13. Findings and disagreements

1. **Double counting of HVAC** is the highest-risk defect in this brief as
   written. Section 3.2 states the rule that prevents it and the test that
   proves it. This needs your agreement because it changes what the Load Engine
   plugin means when the BMS is active.
2. **`RemoteCoordinator` should not be extended.** Section 3.1 argues for a
   sibling `ControlStack`, which keeps the existing DR tests untouched.
3. **DERMS cadence must be explicit.** Running `plan` every timestep is
   affordable on a 96 step day and wasteful on a 31 day horizon. I propose
   `DERMS_PLAN_CADENCE_STEPS` defaulting to 4.
4. **The 98 percent self-consumption figure is unverified**, see section 8.
5. **The brief lists `mpc` as an optimiser** alongside `rule_based` and `lp`.
   Model predictive control over a receding horizon with the LP inside it is
   effectively the `lp` optimiser re-solved each step with updated state, which
   is what the dispatch loop already does. I propose implementing `mpc` as
   exactly that, a documented re-solve policy rather than a separate solver, and
   saying so plainly rather than implying a distinct algorithm.
6. **Sequencing.** This brief is independent of the twin brief but composes with
   it. If both are wanted, the control layers are more valuable once the twin
   exists, because DERMS then plans against estimated state rather than assumed
   state. My recommendation is in the twin design, section 15.

---

## 14. Decisions needed before phase 1

1. Approve the no-double-count rule in section 3.2.
2. Approve `ControlStack` as a sibling rather than extending `RemoteCoordinator`.
3. Confirm the sequencing against the network model brief and the twin brief.
4. Tell me whether to locate the self-consumption figure in the report images or
   restate validation case 4 without it.
