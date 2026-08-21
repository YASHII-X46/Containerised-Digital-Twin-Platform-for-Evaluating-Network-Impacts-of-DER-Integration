# Claude Code build prompt: DERMS, EMS and BMS layer for Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration v5.0

**How to use this file.** Open a terminal at the repository root
(`.../Final Year Project/New-stack-5.0`), start Claude Code, and paste
everything from the `=== PROMPT STARTS HERE ===` line to the end of the file as
your first message. Work through it phase by phase and approve each phase gate
before Claude Code moves on.

Run `NETWORK-MODEL-BUILD-PROMPT.md` first, this work consumes the buses, zones
and DER records that produces.

---

=== PROMPT STARTS HERE ===

You are working in the Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration v5.0 repository, a Dockerised digital-twin stack
for distribution-network DER penetration studies. Your task is to add three new
control layers, each as its own container on the existing OpenFMB/NATS bus,
wired into the simulation pipeline, UI, KPIs and test suites:

- **BMS**, Building Management System, inside the building.
- **EMS**, Energy Management System, at the site or campus, built on the
  **OpenEMS** domain model.
- **DERMS**, Distributed Energy Resource Management System, at the utility.

The control vocabulary throughout is **Australian standards**: AS/NZS 4755
demand response modes for instructions, CSIP-Aus for operating envelopes,
AS/NZS 4777.2 for inverter behaviour, AS 1668.2 for ventilation. Do not
introduce foreign protocol vocabularies or vendor-specific naming anywhere.

Do not start writing code yet. Follow the phase plan in section 16.

---

## 1. Read before you write

Do not skim. The design below assumes you have matched the existing patterns
exactly.

| Purpose | Files |
|---|---|
| The network model this runs on | `sample-networks/README.md`, `sample-networks/MODEL-DESIGN.md`, `swinburne_hawthorn_v5.0_scenario.json` |
| Overall architecture and modularity story | `README.md`, `WINDOWS.md` |
| Bus contract and envelope format | `dr-controller/app/bus/participant.py`, `transport.py`, `dr-controller/app/openfmb.py` |
| Reference NATS service to copy structurally | `dr-controller/` in full |
| Second reference service, session and state style | `prosumer-shadow-twins/` in full |
| Coordination loop you will extend | `simulation-engine/app/control/remote_coordinator.py`, `app/simulation/qsts.py` |
| The envelope machinery DERMS will steer | `simulation-engine/app/control/envelopes.py`, `app/control/strategy_catalog.py` |
| The optional-backend pattern to copy for OpenEMS | `simulation-engine/app/solvers/registry.py`, `client.py`, `sincal-solver/README.md` |
| Request and response schemas | `simulation-engine/app/models/schemas.py` |
| Registries you will extend | `app/metrics/kpi_registry.py`, `app/metrics/tariffs.py`, `app/simulation/der_elements.py`, `load-engine/app/profiles/der_plugins.py`, `archetypes.py`, `commercial_models.py`, `bess_model.py`, `weather.py` |
| UI wiring | `ui/server.js`, `ui/bus.js`, `ui/public/js/controls.js`, `api.js`, `charts.js`, `modules.js`, `main.js` |
| Deployment | `docker-compose.yml`, `docker-compose.windows.yml` |
| Swinburne site, building and DER data | `../Report_DCH5_Final-Project.pdf`, sections 1.4, 2.1 (Table 1), 4, 7.1 |

**How to use the report.** It is a **data source only**: the campus site, the
buildings, the instrumented zones, and the installed generation and storage
equipment with its nameplate ratings and operating parameters. Take none of its
control technology, protocols, gateways, sensor hardware, emulator, or software
framework into this repository. The control vocabulary here is Australian
standards, and the EMS architecture comes from OpenEMS.

Also read the public OpenEMS documentation and source model (openems.io, EPL-2.0)
far enough to reproduce its domain model faithfully: components and component
ids, Natures, Channels and channel addresses, the Scheduler, the Cycle, the
power constraint solver, and the standard controller set. You are adopting its
architecture, section 7 says exactly how far.

After reading, write `docs/DERMS-EMS-BMS-DESIGN.md`. That is the phase 0
deliverable.

---

## 2. Non-negotiable architectural rules

1. **Bus only.** Containers talk over NATS with OpenFMB-style command and event
   messages on `{prefix}/command/{service}/{action}` and
   `{prefix}/event/{service}/{action}`, carrying the
   `messageId`/`correlationId`/`timestamp`/`status`/`payload` envelope from
   `BusParticipant.envelope()`. No inter-container HTTP, no shared volumes for
   data exchange. Payloads travel inline.
2. **Registries, not engine edits.** Every new capability is a registered plugin
   behind a `register_*()` call, discoverable through a listing command so the UI
   System view can show it. If you find yourself adding an `if kind == "hvac"`
   branch inside a loop, you have taken the wrong path.
3. **The stack ships no built-in networks.** Nothing may assume a particular
   feeder or bus numbering. Sites, buildings and zones come from the uploaded
   network model and its scenario preset, or from the request. Named presets are
   fine, hardcoded topology is not.
4. **Backwards compatibility.** A simulate request that omits every DERMS, EMS
   and BMS field must produce numerically identical results to the current code.
   All 332 existing tests pass unchanged.
5. **Health and lifecycle parity.** Every new container writes the readiness
   marker after `participant.start()`, is `restart: on-failure`, has a
   healthcheck, and honours `READY_FILE`, `NATS_URL`, `BUS_PREFIX` and
   `BUS_TRANSPORT` (including `loopback` for tests) exactly as `dr-controller`
   does.
6. **Determinism.** Same seed and same request produce the same numbers. Seed
   any stochastic element from the existing per-bus seed derivation. This rule
   is what decides the OpenEMS integration shape in section 7, do not weaken it.
7. **Convergence discipline.** New KPIs and summaries use converged timesteps
   only.
8. **Physics stays where the solver is.** DERMS owns policy, not power flow. Any
   headroom or sensitivity calculation stays in the Simulation Engine, which
   owns the solve, and is passed to DERMS as data. Do not build a second network
   model inside a controller.
9. **Honest limits.** Quasi-static only. No dynamics, protection or sub-timestep
   control. Document every simplification in the service README.

---

## 3. What each layer owns

**BMS, the building.** Controllable thermal and electrical loads inside a
building: HVAC, ventilation, lighting, domestic hot water, deferrable plug
loads. It holds a thermal state per zone, tracks comfort and air quality, and
converts a power instruction into a physically achievable load while reporting
what that cost in comfort terms. It is the only component allowed to decide how
a building meets a demand limit.

**EMS, the site.** Dispatch of one site's assets against the customer's
objective over a rolling horizon: battery charge and discharge, EV charging, PV
curtailment for economic reasons, and the demand limit handed down to the BMS.
It receives utility instructions and decides how to respond to them.

**DERMS, the utility.** Management of the portfolio of sites connected to the
network: forecasting available headroom, allocating it as operating envelopes,
issuing demand response instructions, optionally running a flexibility market,
and monitoring compliance and fairness across sites. It optimises the network
operator's objective, not any one customer's.

**What already exists and stays.** The `dr-controller` remains the fast local
device-control layer with its six ordered control plugins, and the Simulation
Engine keeps the autonomous AS/NZS 4777.2 responses and the physical envelope
enforcement. DERMS sits above them and decides what limits and instructions to
issue. When DERMS is disabled the existing envelope and DR behaviour is exactly
as it is today, byte for byte in the results.

---

## 4. Where site and asset configuration comes from

### 4.1 The Swinburne reference case

The campus this stack models is real, and its equipment data is known. Use it as
the default preset, cited to the report, and keep it as **named presets and
scenario data, never as engine logic**.

**Sites.** ATC (Advanced Technology Centre, 427 to 451 Burwood Road) and AMDC
(Advanced Manufacturing and Design Centre), adjacent buildings on the Hawthorn
campus, each behind its own distribution transformer. They were selected for
proximity, zoning diversity, roof space, timetable access and controllable HVAC.

**Instrumented zones**, the ones with real measured data and therefore the ones
a validation study can use: ATC101, ATC103, ATC206, AMDC301, AMDC303, AMDC355,
AMDC451. They are large lecture theatres and private study areas, chosen because
theatre occupancy is timetabled but variable and study-area occupancy is
stochastic. That occupancy character is what makes them worth controlling, and
your BMS presets should reflect it.

**Installed DER at ATC**, from the report's Table 1:

| Item | Value |
|---|---|
| PV modules | 84 x Trina Vertex TSM-390-DE09.08, 390 W each |
| Array rating | 32.76 kWp, 167 m2, 15 degree tilt |
| Inverters | 3 x Sungrow SH10RT hybrid, 10 kVA each, 30 kVA total |
| Battery | 3 x BYD HVS 10.2, 30.6 kWh total |
| Operating floor | 20 percent state-of-charge backup reserve |
| Operating parameters | state of charge 0.20 to 1.00, round-trip efficiency 0.90, cycle cost 0.02 AUD/kWh |

All three inverters are physically in the ATC inverter room; their assignment to
ATC, AMDC and a shared community role is logical, not electrical. Add them as
named registry entries the way `BESS_CONFIGS` already names Powerwall 2/3, BYD
HVS and Enphase 5P: battery `byd_hvs_10.2`, campus stack `swinburne_atc_stack`
of three units, inverter `sungrow_sh10rt`, array `swinburne_atc_pv`. The
operating parameters above are the EMS defaults.

32.76 kWp against ATC's 900 kW peak is about 3.6 percent, and the report is
explicit that on-site generation was small relative to building load. State that
ratio wherever the preset is documented rather than implying a high-penetration
site.

### 4.2 Everything else comes from the model and the request

Nothing in these three services carries a site definition of its own.

- **Sites, buildings, zones and transformer boundaries** come from the uploaded
  network model and, where richer detail is needed, from the scenario preset
  file that ships beside it. The Simulation Engine derives the transformer
  boundary from the network's `is_transformer` branches and passes the grouping
  down.
- **DER ratings** come from the per-bus `der` hint the importers populate, or
  from the request when the user overrides them.
- **Building thermal parameters** come from named presets in the BMS registry,
  seeded from the zone use type and floor area in the scenario preset, and
  overridable per request.
- **Defaults are engineering defaults, not a particular installation.** State
  the basis of every default in the README table: a state-of-charge band, a
  round-trip efficiency, a cycle cost, a coefficient of performance curve, a
  ventilation rate. A user with real nameplate data replaces them through the
  request, never through code.

**Inter-site energy sharing.** Under the Australian grid code, sources behind
separate distribution transformers cannot be directly interconnected. Model
sharing as a permissioned constraint, defaulting to disallowed across a
transformer boundary, derived from the network model rather than a hardcoded
list, with `allow_inter_site_sharing` as an explicit run knob so the
counterfactual is measurable.

---

## 5. Control precedence ladder

The stack already documents a precedence for its existing layers: envelope cap,
then autonomous Volt-Watt, then commanded DR curtailment. You are inserting
three layers around that. Implement, document and test this exact order:

1. **DERMS plans**, over its horizon, on the network objective: per-site export
   and import envelopes in CSIP-Aus terms, and demand response instructions
   expressed as AS/NZS 4755 demand response modes.
2. **EMS schedules** the site against the customer objective, subject to the
   envelope and the demand response mode it received, producing intended
   setpoints for storage, EV, PV limit and a per-building demand limit.
3. **BMS realises** the building demand limit within comfort, air quality and
   equipment constraints, returning achievable power plus any unmet energy.
4. **Network layers act last and win.** The physical envelope cap, the
   autonomous AS/NZS 4777.2 inverter response, and the `dr-controller` commanded
   setpoints apply on top and may override everything above.
5. **EMS settles.** What the network actually allowed is fed back so the
   receding-horizon plan re-plans against reality. Record the difference as
   `ems_intent_curtailed_kwh`.
6. **DERMS settles.** Per-site compliance, envelope utilisation, delivered
   flexibility and fairness are recorded, and feed the next planning round.

Write `docs/CONTROL-PRECEDENCE.md` with a diagram of the full ladder including
the pre-existing layers, and a worked numeric example of a step where all six
stages act.

---

## 6. New service: `bms-controller/`

Mirror the `dr-controller/` layout exactly: `app/__init__.py`, `app/bus/`,
`app/config.py`, `app/main.py`, `app/openfmb.py`, the modules below,
`Dockerfile`, `Dockerfile.windows`, `.dockerignore`, `requirements.txt`,
`pytest.ini`, `README.md`, `tests/`.

### 6.1 Building physics

Lumped-parameter thermal model per zone, defaulting to 2R2C (indoor air node and
thermal mass node) with a documented reduction to 1R1C for cheap runs.

- Heat balance per timestep: conduction to ambient, solar gain through glazing
  driven by the same irradiance trace the Load Engine already uses, internal
  gains from occupancy, lighting and equipment, and the HVAC sensible
  contribution.
- HVAC electrical power from thermal power through a coefficient of performance
  that varies with outdoor temperature, not a constant, with a documented curve
  and a rated capacity that can saturate.
- A ventilation term driven by occupancy, with the **AS 1668.2** minimum outside
  air rate enforced as a hard constraint, and zone CO2 tracked from a simple
  mass balance so a CO2-aware shed has something real to act on.
- Named building presets aligned with the three existing commercial archetypes
  in `load-engine/app/profiles/commercial_models.py`, plus `swinburne_atc` and
  `swinburne_amdc` covering the instrumented zone types from section 4.1 (large
  lecture theatre, private study area, teaching and office space), seeded from
  the zone use type and floor area in the network scenario preset.
- Comfort tracked against a deadband with occupied and unoccupied bands, sized
  from Australian practice for conditioned commercial space (NCC Section J and
  AIRAH design guidance), with the calculation-method lineage of the simple
  hourly building energy method cited in the README. Comfort violations are
  recorded, never silently absorbed.

### 6.2 Registries

| Registry | Module | Built-ins | Extension call |
|---|---|---|---|
| Zone thermal models | `app/zone_models.py` | `rc1`, `rc2` | `register_zone_model(...)` |
| Building device plugins | `app/building_plugins.py` | `hvac` (10), `ventilation` (15), `lighting` (20), `dhw` (30), `plug` (40) | `register(BuildingPlugin())` |
| Comfort policies | `app/comfort_policies.py` | `fixed_band`, `adaptive`, `precool` | `register_policy(...)` |
| Shed strategies | `app/shed_strategies.py` | `schedule`, `setpoint_setback`, `duty_cycle`, `occupancy_scaled`, `priority_order`, `drm_mode` | `register_strategy(...)` |

Device plugins follow the `ControlPlugin` pattern from
`dr-controller/app/control_plugins.py`: ordered registry, each plugin reads a
shared context and writes its own power contribution.

`drm_mode` is the strategy that implements the **AS/NZS 4755 demand response
modes** directly: the instruction arrives as a mode, and the strategy maps it to
the building's response. Implement the modes relevant to air conditioning and
controllable load, cite the standard part in the docstring, and reject a mode
the configured devices cannot honour rather than approximating it silently.

`duty_cycle` and `setpoint_setback` parameters (off-cycle fraction, cycling
period, setback magnitude) are configurable with documented engineering
defaults. `schedule` accepts a per-zone occupancy or booking schedule at the run
resolution and falls back to the archetype occupancy schedule.

Support drop-in loading through `BMS_PLUGIN_MODULES` and `BMS_PLUGINS_DIR`,
errors logged and skipped, never fatal.

### 6.3 Bus contract

Service `bms-controller`:

| Action | Payload in | Payload out |
|---|---|---|
| `configure` | `session_id`, `buildings[]`, `step_minutes`, `topic_prefix`, optional overrides | accepted config, `building_count`, `zone_count`, resolved model names |
| `step` | `session_id`, `t`, `timestamp`, `step_hours`, `ambient_temp_c`, `irradiance_wm2`, `buildings[]` with `bus_id`, `demand_limit_kw` (nullable), `drm_mode` (nullable), `setpoint_offset_c`, `mode` | `buildings[]` with `bus_id`, `hvac_kw`, `ventilation_kw`, `controllable_kw`, per-zone `zone_temp_c` and `zone_co2_ppm`, `shed_kw`, `min_feasible_kw`, `unmet_thermal_kwh`, `comfort_violation`, `ventilation_violation` |
| `buildings` | `session_id` | current per-building and per-zone state |
| `models` | none | installed zone models, device plugins, comfort policies, shed strategies |
| `stop` | `session_id` | `stopped` flag |

`min_feasible_kw` matters: the EMS needs a lower bound on building load that
respects comfort and ventilation, otherwise it will schedule demand limits the
building cannot meet.

### 6.4 Environment configuration

`BMS_STEP_MINUTES`, `BMS_DEFAULT_SETPOINT_C`, `BMS_COMFORT_BAND_C`,
`BMS_UNOCCUPIED_BAND_C`, `BMS_MAX_PRECOOL_C`, `BMS_MIN_COP`,
`BMS_VENTILATION_L_PER_S_PERSON`, `BMS_CO2_LIMIT_PPM`, `BMS_DUTY_OFF_FRACTION`,
`BMS_DUTY_PERIOD_H`, `BMS_PLUGIN_MODULES`, `BMS_PLUGINS_DIR`, plus the standard
bus variables. Document every one in the README table with its basis.

---

## 7. New service: `ems-controller/`, built on the OpenEMS domain model

### 7.1 The integration decision

OpenEMS is an established open-source energy management platform (EPL-2.0). Its
value here is its **architecture**, which is close to what this stack already
does well: typed device abstractions, addressable data points, an ordered
controller scheduler, and a constraint solver that turns controller intent into
a feasible setpoint.

Running OpenEMS Edge itself inside the QSTS loop conflicts with rule 6: it is a
JVM service on a real-time cycle, and this stack must step deterministically in
simulated time and reproduce results exactly. So implement it as the stack
already handles solvers, where OpenDSS is the default and SINCAL is an optional
second implementation behind one contract:

- **`native` backend, the default.** A Python implementation of the OpenEMS
  domain model inside `ems-controller`, stepping deterministically in simulated
  time. This is what every test and every study uses.
- **`openems_edge` backend, optional.** A documented bridge to a real OpenEMS
  Edge instance for anyone who wants the production stack in the loop, selected
  per run, absent from the default compose file, and never required by any test.
  Follow the `sincal-solver` precedent exactly: it starts without its dependency
  present, reports availability through `health`, and fails a run with a clear
  error rather than degrading silently.

Register both behind an `app/backend_registry.py` with `register_backend(...)`,
so a third implementation is a registration and not a rewrite.

### 7.2 The OpenEMS domain model to reproduce

Reproduce these concepts faithfully and use OpenEMS names for them, so anyone
who knows the platform can read your code and so the optional bridge maps
one to one:

- **Components** with stable ids (`ess0`, `meter0`, `pv0`, `evcs0`, `ctrl...`),
  each configured from the session configuration.
- **Natures**, the typed interfaces a component implements (symmetric energy
  storage, managed symmetric energy storage, electricity meter, DC charger, EV
  charging station). A controller talks to Natures, never to a concrete device.
- **Channels**, typed addressable data points with `component/Channel`
  addressing, units, and a read or write direction. All state that crosses the
  bus or reaches the UI is a channel value.
- **A Sum component** aggregating site grid, production, consumption and storage
  into the site-level view the objectives act on.
- **A Cycle** with a fixed order per timestep: read device state into channels,
  run the scheduler, let controllers write to write-channels, resolve
  constraints, apply setpoints.
- **A Scheduler** running controllers in an explicit configured order, so
  couplings hold. This is the same ordered-registry idea as the stack's own
  control plugins, and the two should read alike.
- **A power constraint solver** turning the controllers' accumulated linear
  constraints into a feasible active and reactive setpoint per storage
  component, including power and energy limits, state-of-charge bounds and the
  externally imposed envelope. Keep it linear and document the resolution rule
  when constraints conflict.
- **Predictors** producing forecast channels for load, production and price,
  which the horizon-based controllers consume.

### 7.3 Controllers, the EMS registry

Provide controllers mirroring the OpenEMS standard set, each registered:

| Controller | Behaviour |
|---|---|
| `balancing` | Self-consumption, charge surplus and discharge deficit at the grid connection point |
| `peak_shaving` | Hold site import below a threshold using storage |
| `grid_optimized_charge` | Shift charging to avoid feed-in curtailment and evening peak |
| `time_of_use_tariff` | Schedule against the tariff registry over the horizon |
| `fix_active_power` | Hold a commanded setpoint, the primitive DERMS instructions land on |
| `limit_total_discharge` | Enforce the state-of-charge reserve floor |
| `envelope_compliance` | Hold site export and import inside the CSIP-Aus envelope |
| `drm_response` | Translate an AS/NZS 4755 demand response mode into asset action |

Also register **objectives** (`peak_shave`, `cost_min`, `self_consumption`,
`emissions_min`, `demand_charge_min`, `envelope_aware`, `community_share`),
**optimisers** (`rule_based`, `lp`, `mpc`), **assets** (`bess`, `ev`, `pv`,
`bms_load`, `generic`), **predictors** (`perfect`, `persistence`, `noisy`), and
**bidding strategies** (`none`, `truthful`, `cost_plus`, `withholding`).

`rule_based` is the self-consumption baseline every other optimiser is measured
against, and it reproduces how the real ATC system is operated: prioritise
self-consumption, discharge storage down to the 20 percent reserve, export the
remainder. `lp` uses `scipy.optimize.linprog` with HiGHS and falls back to a
documented greedy heuristic when scipy is unavailable, the precedent already set
by `max_total` in `simulation-engine/app/control/envelopes.py`, including the
log line on fallback. Pin scipy as the simulation engine does, respecting the
NumPy 1.x pin.

### 7.4 The optimisation problem

Per site and horizon:

- Decision variables per interval: storage charge power, storage discharge
  power, EV charge power, PV curtailment, building demand limit, grid import and
  export split, and inter-site transfer when permitted.
- Constraints: energy balance, storage power and energy limits with round-trip
  efficiency (0.90 by default) and a state-of-charge band (0.20 to 1.00 by
  default), terminal state-of-charge to prevent
  horizon-end dumping, EV energy delivered by departure, PV curtailment bounded
  by available PV, building demand limit bounded below by the BMS
  `min_feasible_kw`, non-negative import and export, the DERMS-issued envelope,
  and zero inter-site transfer across a transformer boundary unless permitted.
- Objective from the registry, priced by the existing tariff registry so EMS
  economics and the existing cost KPIs agree, with an optional storage cycle
  cost and CO2 term.

Keep the linear program strictly linear: model the import and export split with
two non-negative variables and rely on the price structure to prevent
simultaneous import and export, and document the one edge case where that
assumption breaks (negative feed-in prices) rather than pretending it cannot. Do
the same for simultaneous charge and discharge.

### 7.5 Bus contract

Service `ems-controller`:

| Action | Payload in | Payload out |
|---|---|---|
| `configure` | `session_id`, `sites[]` as OpenEMS-style component configurations, `backend`, `objective`, `optimiser`, `controllers[]` in scheduler order, `horizon_steps`, `step_minutes`, `tariff`, `predictor`, `bidder`, `allow_inter_site_sharing`, `topic_prefix` | accepted configuration, `site_count`, resolved component ids and channel addresses, horizon in hours |
| `instruct` | `session_id`, per-site envelope (`export_limit_kw`, `import_limit_kw`), `drm_mode`, price signal, validity interval | per-site acceptance or refusal with a reason |
| `bid` | `session_id`, `t`, product definition, price ladder | per-site bid curves with the marginal cost basis |
| `dispatch` | `session_id`, `t`, `timestamp`, `step_hours`, `sites[]` with `soc`, `site_demand_kw`, `pv_kw`, `ev_pending_kwh`, `voltage_pu`, `export_limit_kw`, `min_feasible_kw`, plus the forecast slice | `sites[]` with `bess_charge_kw`, `bess_discharge_kw`, `ev_charge_kw`, `pv_limit_kw`, `bms_demand_limit_kw`, `inter_site_transfer_kw`, `import_target_kw`, `objective_value`, `binding_constraints[]`, `solver_status` |
| `channels` | `session_id`, optional address filter | current channel values, the OpenEMS-style live view the UI renders |
| `settle` | `session_id`, `t`, per-site applied values after the network layers acted | acknowledgement, updated internal state |
| `backends`, `controllers`, `objectives`, `optimisers`, `assets`, `predictors`, `bidders` | none | registry listings |
| `stop` | `session_id` | `stopped` flag |

### 7.6 Environment configuration

`EMS_BACKEND` (`native` default), `EMS_HORIZON_STEPS`, `EMS_OBJECTIVE`,
`EMS_OPTIMISER`, `EMS_CONTROLLERS`, `EMS_PREDICTOR`, `EMS_PREDICTOR_SIGMA`,
`EMS_BIDDER`, `EMS_SOC_MIN`, `EMS_SOC_MAX`, `EMS_TERMINAL_SOC`,
`EMS_ROUND_TRIP_EFFICIENCY`, `EMS_CYCLE_COST_AUD_PER_KWH`,
`EMS_DEMAND_CHARGE_AUD_PER_KVA`, `EMS_ALLOW_INTER_SITE_SHARING`,
`EMS_SOLVER_TIMEOUT_S`, and for the optional bridge `OPENEMS_EDGE_URL`,
`OPENEMS_EDGE_TIMEOUT_S`.

### 7.7 Licensing and attribution

OpenEMS is EPL-2.0. You are reproducing an architecture and using its
terminology, not copying its code. Do not paste OpenEMS source into this
repository. Attribute the design lineage in `ems-controller/README.md` with a
link to the project and a sentence on what was adopted, and if you ever do
vendor code, stop and ask first.

---

## 8. New service: `derms-controller/`

### 8.1 What it does

Each planning round DERMS:

1. Receives network state and available headroom from the Simulation Engine,
   which owns the solve. It does not compute power flow itself.
2. Aggregates sites into portfolios, by feeder, by transformer, or by a named
   grouping.
3. Allocates headroom to sites as CSIP-Aus operating envelopes.
4. Issues demand response instructions to the EMS layer as AS/NZS 4755 demand
   response modes with a validity interval.
5. Optionally runs a flexibility market: collects bids, clears, issues
   commitments.
6. After the step, records compliance, envelope utilisation, delivered
   flexibility and fairness, feeding the next round.

### 8.2 Registries

| Registry | Module | Built-ins | Extension call |
|---|---|---|---|
| Aggregation policies | `app/aggregation.py` | `by_feeder`, `by_transformer`, `by_site_group`, `flat` | `register_aggregation(...)` |
| Portfolio objectives | `app/objective_registry.py` | `feeder_peak`, `constraint_relief`, `min_curtailment`, `max_hosting`, `emissions_min`, `cost_of_dispatch` | `register_objective(...)` |
| Envelope issuance policies | `app/issuance.py` | `equal`, `prorata`, `max_total`, `priority`, `rotating` | `register_issuance(...)` |
| Market mechanisms | `app/markets.py` | `none`, `uniform_price`, `pay_as_bid` | `register_market(...)` |
| Instruction templates | `app/instructions.py` | `envelope_only`, `drm_mode`, `price_signal`, `capacity_limit` | `register_instruction(...)` |
| Compliance policies | `app/compliance.py` | `observe_only`, `penalty`, `progressive_derate` | `register_compliance(...)` |
| Predictors | `app/forecast.py` | `perfect`, `persistence`, `noisy` | `register_forecast(...)` |

`equal`, `prorata` and `max_total` deliberately mirror the allocation names
already in `simulation-engine/app/control/envelopes.py`. Reuse the semantics
exactly so a reader is not learning two vocabularies, and state in both READMEs
which module owns which half: the engine owns the headroom physics, DERMS owns
the allocation policy when DERMS is enabled.

### 8.3 Fairness must be explicit

Curtailment allocation is the ethically loaded part of a DERMS and a thesis
should not hide it behind a default. Every issuance policy declares in one
sentence who bears curtailment first and why, the KPI set reports a fairness
index across sites, and the README states plainly that `max_total` maximises
total export at the cost of unequal treatment while `equal` does the reverse.
Show the trade, do not pick for the reader silently.

### 8.4 Bus contract

Service `derms-controller`:

| Action | Payload in | Payload out |
|---|---|---|
| `configure` | `session_id`, `portfolio` (sites, groupings, transformer boundaries), `objective`, `aggregation`, `issuance`, `market`, `compliance`, `horizon_steps`, `step_minutes`, `topic_prefix` | accepted configuration, `site_count`, `group_count`, resolved names |
| `plan` | `session_id`, `t`, `timestamp`, `horizon`, per-site or per-group headroom from the engine, forecast slice, current site states | per-site `export_limit_kw`, `import_limit_kw`, `drm_mode`, `price_signal`, validity interval, `objective_value`, `binding_constraints[]` |
| `clear` | `session_id`, `t`, bids collected from the EMS layer | cleared quantities and prices per site, unmatched volume, clearing rule used |
| `settle` | `session_id`, `t`, per-site applied values and measured compliance | per-site compliance record, utilisation, fairness index, penalties if the policy applies them |
| `portfolio` | `session_id` | current groupings and per-site state, for the UI |
| `objectives`, `aggregations`, `issuances`, `markets`, `instructions`, `compliances` | none | registry listings |
| `stop` | `session_id` | `stopped` flag |

### 8.5 Environment configuration

`DERMS_HORIZON_STEPS`, `DERMS_OBJECTIVE`, `DERMS_AGGREGATION`, `DERMS_ISSUANCE`,
`DERMS_MARKET`, `DERMS_COMPLIANCE`, `DERMS_FORECAST`, `DERMS_MIN_EXPORT_KW`,
`DERMS_MAX_EXPORT_KW`, `DERMS_INSTRUCTION_LEAD_TIME_S`,
`DERMS_HEADROOM_MARGIN_PCT`, plus the standard bus variables.

---

## 9. Load Engine changes

1. Add an `hvac` DER generation plugin producing the unmanaged building HVAC
   series for buses flagged as buildings, contributing `hvac_kw` with `net_load`
   sign `+1`, ordered after load and PV but before BESS so the battery still
   dispatches against true net demand.
2. Reuse the existing weather machinery so the `file` weather source drives
   building thermal behaviour with measured data.
3. Carry building and zone metadata through the profiles payload so the
   Simulation Engine, BMS and EMS configure themselves from one generate call.
   Extend the per-bus config with an optional `building` block rather than
   adding top-level keys, and read the zone list from the network scenario
   preset when one is present.
4. Multi-day continuity: zone temperature at the end of day N seeds day N plus
   1, as battery state of charge and state of health already do.
5. Verify the root README's claim that a generation plugin's extra series
   automatically reaches the power flow through `other_der_kw`, the twins and
   the charts. If it holds, take the zero-edit path. If it does not hold for a
   controllable load, say so and make the smallest possible change.

---

## 10. Simulation Engine changes

1. **Schemas.** Add `DermsConfig`, `EmsConfig` and `BmsConfig` to
   `app/models/schemas.py`, all optional and defaulting to off, in the
   `DoeConfig` style including field descriptions. Add matching summary fields
   to `SimulationResponse` with safe defaults.
2. **Coordination.** Extend `RemoteCoordinator`, or add a sibling coordinator
   composed into the QSTS loop, performing the section 5 ladder each timestep:
   DERMS `plan` on its own cadence, EMS `instruct` then `dispatch`, BMS `step`,
   apply to the solver, let the envelope, Volt-Watt and DR layers act, then EMS
   `settle` and DERMS `settle`. Use the existing `_request_ok` helper so bus
   timeouts and error events surface as proper HTTP errors rather than silent
   zeros.
3. **Headroom exposure.** Expose the existing envelope headroom calculation as
   data DERMS can consume, without moving the physics out of the engine. When
   DERMS is off, the engine's own allocation path runs exactly as today.
4. **Site and transformer topology.** Derive each site's transformer boundary
   from the network model and expose the grouping in the response so the UI can
   show it.
5. **DER element.** Register an `HvacElement` in `app/simulation/der_elements.py`
   if a dedicated solver element is needed, otherwise document why the generic
   path suffices.
6. **KPIs.** Register these with the same docstring and unit discipline as the
   existing eighteen. Building and site: `peak_demand_kw`,
   `peak_demand_reduction_pct`, `load_factor`, `self_consumption_pct`,
   `self_sufficiency_pct`, `demand_charge_aud`, `ems_cost_saving_aud`,
   `ems_intent_curtailed_kwh`, `comfort_violation_hours`,
   `max_comfort_deviation_c`, `ventilation_violation_hours`, `max_zone_co2_ppm`,
   `unmet_thermal_kwh`, `bms_shed_kwh`, `bms_energy_kwh`. Portfolio:
   `portfolio_peak_kw`, `curtailment_fairness_index`,
   `envelope_compliance_rate_pct`, `non_compliance_events`,
   `flexibility_delivered_kwh`, `community_shared_kwh`,
   `derms_curtailment_avoided_kwh`. Every one must be computable on runs where
   all three layers are off, reporting the honest baseline or zero, never
   `None`.
7. **Tariffs.** Add `tou_commercial` with a demand-charge component, extending
   the `Tariff` dataclass with an optional demand-charge rate and window
   defaulting to zero so existing tariffs and results are unchanged.
8. **Result series.** Site demand against the EMS demand limit, storage state of
   charge against the EMS plan, zone temperature against the comfort band, zone
   CO2 against the limit, EMS intent against applied dispatch, and per-site
   envelope against actual export.
9. **Failure behaviour.** If a layer is requested but its container does not
   answer, fail the run with a clear message naming the service and action.
   Never degrade silently to an uncoordinated run.

---

## 11. Solver changes

Prefer none. If a controllable building load needs its own OpenDSS element,
register a builder in `opendss-solver/dss_solver/elements.py` through the
existing registry, keep the solver dumb, and confirm the PSS SINCAL adapter
still satisfies the same contract or document what a SINCAL implementation would
need.

---

## 12. UI changes

The control panel is the examinable artefact. Match the existing visual
language.

- **Scenario controls**: three collapsible sections, all off by default. DERMS
  (enable, portfolio objective, aggregation, issuance policy, market mechanism,
  compliance policy, horizon, instruction lead time, headroom margin). EMS
  (backend, objective, optimiser, controller order, horizon, predictor and
  sigma, state-of-charge band, terminal state of charge, cycle cost,
  demand-charge rate, bidder, inter-site sharing permission). BMS (preset,
  per-building setpoint and comfort band, policy, shed strategy and parameters,
  ventilation rate, CO2 limit, which buses are buildings).
- **Saved scenario configurations** round-trip every new control.
- **Results**: metric cards for peak reduction, self-consumption,
  self-sufficiency, cost saving, comfort and ventilation violation hours,
  fairness index and envelope compliance. New charts: dispatch stack against the
  demand limit, zone temperature with the comfort band shaded, zone CO2 against
  the limit, EMS intent against applied dispatch, storage state of charge with
  the planned trajectory, and a per-site envelope-versus-export panel.
- **Portfolio view**: sites and groupings painted onto the topology map with the
  transformer boundary visible, so the sharing constraint is legible.
- **EMS channel view**: the OpenEMS-style live channel list for the selected
  site, which is also the natural debugging surface.
- **System view**: list all new registries live from the listing commands.
- **CSV export** includes the new series.
- `ui/server.js` and `ui/bus.js` gain the new routes over NATS. No HTTP to the
  new containers.

---

## 13. Deployment

Add all three services to `docker-compose.yml` and `docker-compose.windows.yml`
following the existing patterns exactly: build context, container name, bus
environment, `depends_on` the broker, `restart: on-failure`, readiness
healthcheck, and the simulation engine waiting on all three being healthy. The
optional OpenEMS Edge bridge is **not** in the default compose file, document it
the way the SINCAL adapter is documented. Windows images follow the
`Dockerfile.windows` and `READY_FILE` conventions. Do not publish HTTP ports,
these are bus-only participants. Update `WINDOWS.md`.

---

## 14. Testing

The stack has 332 tests and its credibility rests on them. Target at least 90
new tests, no regressions.

**`bms-controller/tests/`** (30 plus): thermal conservation, capacity
saturation, coefficient of performance varying with ambient, comfort band
occupied and unoccupied, CO2 mass balance and the AS 1668.2 rate binding,
precool then coast, demand limit honoured exactly when feasible and shortfall
reported when not, `min_feasible_kw` correctness, unmet thermal accounting,
multi-day continuity, every shed strategy including each supported AS/NZS 4755
mode and the rejection of an unsupportable one, registry listings, drop-in
loading, loopback round trip.

**`ems-controller/tests/`** (30 plus): the cycle runs components and controllers
in the configured order, channel addressing round-trips, the power constraint
solver returns a feasible setpoint and a documented resolution when constraints
conflict, each controller behaves correctly in isolation, each objective is
qualitatively correct on a hand-built case, `rule_based` reproduces the
self-consumption baseline, storage power and energy constraints, round-trip
losses, terminal state of charge, EV energy by departure, envelope respected,
inter-site transfer zero across a transformer boundary unless permitted, scipy
and greedy paths agreeing in ordering on a known optimum, predictors, each
bidding strategy producing a well-formed monotonic bid curve, `settle` changing
the next plan, backend registry listing both backends, the `openems_edge`
backend failing cleanly when no Edge is reachable, solver timeout, loopback
round trip.

**`derms-controller/tests/`** (20 plus): each aggregation policy groups correctly
from a supplied topology, each issuance policy allocates a known headroom
correctly and the fairness index moves as expected, `max_total` beats `equal` on
total export and loses on fairness, each market mechanism clears a hand-built
bid set to a hand-computed answer, compliance policies record and penalise
correctly, instruction payloads carry valid AS/NZS 4755 modes and CSIP-Aus
envelope fields, a site refusing an instruction is handled, registry listings,
loopback round trip.

**`simulation-engine/tests/`** (10 plus): the full precedence ladder in one
integration test where a DERMS envelope overrides an EMS intent and the gap
appears in `ems_intent_curtailed_kwh`, every new KPI on a run with all layers
off, demand-charge tariff pricing, transformer-boundary grouping derived from
the network, new result series shapes, schema defaults, and a regression test
asserting a request without the new layers returns identical key metrics to the
pre-change baseline. Use the existing loopback-bus in-process pattern so the
real services are exercised, do not mock the contract you just wrote.

**`load-engine/tests/`**: the HVAC plugin, weather coupling, building and zone
metadata pass-through, multi-day continuity.

Every service passes `python -m pytest -q` from its own directory. Report the
new total and update the Verification section of the root README.

---

## 15. Documentation

- `bms-controller/README.md`, `ems-controller/README.md` and
  `derms-controller/README.md` in the exact style of the existing service
  READMEs: environment variable table with the basis of every default, bus
  contract table, registry table, worked message examples, and an honest
  scope-limits paragraph. The EMS README additionally documents the OpenEMS
  design lineage, the two backends, and how to run the optional Edge bridge.
- Root `README.md`: architecture diagram, service table, modularity registry
  rows, KPI names, environment variables, verification counts.
- `docs/DERMS-EMS-BMS-DESIGN.md`, including the OpenEMS concept mapping table.
- `docs/CONTROL-PRECEDENCE.md` with the six-stage ladder and a worked example.
- `docs/STANDARDS-MAPPING.md`: each Australian standard in section 18 mapped to
  the code that implements it, and an explicit list of what is referenced but
  not implemented.
- `docs/DERMS-EMS-BMS-VALIDATION.md`: the section 17 cases with expected and
  produced numbers.

---

## 16. Phase plan and gates

Stop at every gate and wait for my approval. Do not batch phases.

**Phase 0, design.** Write `docs/DERMS-EMS-BMS-DESIGN.md`: module layout,
message schemas, the precedence implementation point, the OpenEMS concept
mapping and what you are and are not adopting, the split between engine-owned
headroom physics and DERMS-owned policy, and anything in this brief you think is
wrong. Gate: I approve the design.

**Phase 1, BMS.** Build `bms-controller/` standalone with its tests and all shed
strategies. Gate: tests pass and a hand-run thermal and CO2 trace behaves
sensibly for a lecture theatre across a teaching day.

**Phase 2, EMS core.** The OpenEMS-shaped `native` backend: components,
channels, cycle, scheduler, constraint solver, and the controller set, with its
tests. Gate: tests pass, the cycle order is demonstrable, and each controller is
shown working in isolation.

**Phase 3, EMS optimisation.** Objectives, optimisers, predictors, bidders, both
scipy and fallback paths, and the optional `openems_edge` backend stub with its
clean-failure behaviour. Gate: each objective demonstrated on a hand-built case.

**Phase 4, DERMS.** Build `derms-controller/` standalone with its tests, every
registry and the market mechanisms. Gate: tests pass and you show a hand-built
portfolio where the issuance policies visibly trade total export against
fairness.

**Phase 5, Load Engine.** HVAC plugin, building and zone metadata, multi-day
continuity. Gate: load-engine tests pass and profiles show a believable campus
HVAC shape on a summer and a winter design day.

**Phase 6, Simulation Engine integration.** Schemas, the six-stage ladder,
headroom exposure, transformer grouping, KPIs, tariff, result series. Gate: the
precedence integration test passes and the all-layers-off regression test proves
identical baseline results.

**Phase 7, UI.** Controls, charts, cards, portfolio view, channel view, System
view, CSV, saved scenarios. Gate: an end-to-end run through the browser on the
v5.0 campus network.

**Phase 8, deployment, validation, documentation.** Compose files, Windows
guide, all READMEs, standards mapping, validation document, final test count.
Gate: `docker compose up --build` brings the stack healthy and a coordinated
DERMS plus EMS plus BMS run completes from the browser.

At each gate report what changed, test counts before and after, anything you
compromised on, and anything you found in the existing code that is wrong.

---

## 17. Validation cases to demonstrate

Record all of these in `docs/DERMS-EMS-BMS-VALIDATION.md` at phase 8.

1. **Energy conservation.** A zone with HVAC disabled drifts to ambient at the
   rate the RC parameters predict, checked against a closed-form exponential.
2. **Cycle determinism.** The same scenario run twice produces byte-identical
   channel traces, and changing the controller order changes the result in the
   direction the scheduler semantics predict.
3. **Peak shaving.** The `peak_shaving` controller reduces peak demand
   consistently with the storage energy and power available, and `load_factor`
   improves.
4. **Self-consumption.** The `balancing` controller with the 20 percent reserve
   raises `self_consumption_pct` against the uncoordinated baseline by an amount
   consistent with the storage size and the PV surplus profile. Compare the
   result against the roughly 98 percent self-consumption the ATC system
   measured in service, and attribute any gap to load and weather differences
   rather than explaining it away.
5. **Cost minimisation.** Charging moves into the off-peak window and
   `net_energy_cost_aud` falls consistently with the price spread and round-trip
   efficiency.
6. **Schedule-driven shed.** Applying the `schedule` strategy to a building with
   substantial unoccupied conditioned space reduces HVAC energy by an amount
   consistent with the unoccupied fraction, and the residual is explained.
7. **Comfort and air quality trade-off.** Sweeping the comfort band produces a
   monotonic trade between `bms_shed_kwh` and `comfort_violation_hours`, and the
   AS 1668.2 rate caps the shed before CO2 breaches the limit. Non-monotonicity
   is a bug, find it.
8. **Demand response modes.** Each supported AS/NZS 4755 mode produces the load
   response the standard defines, verified against the mode's own definition.
9. **Precedence conflict.** A DERMS envelope binds while the EMS wants to
   export. Show the envelope winning, the intent gap recorded, and the next EMS
   plan adapting after `settle`.
10. **Fairness versus throughput.** The same feeder constraint allocated by
    `equal`, `prorata` and `max_total`, reporting total export, per-site
    curtailment and the fairness index for each. State which you would defend to
    a regulator and why.
11. **Market versus central dispatch.** The same scenario cleared by
    `uniform_price` and `pay_as_bid`, compared against a centrally optimised
    benchmark, reporting the efficiency gap.
12. **The sharing counterfactual.** Two buildings behind separate transformers
    with `allow_inter_site_sharing` false, the grid-code case, and true.
    Quantify the foregone community benefit.
13. **Forecast sensitivity.** The same scenario under `perfect`, `persistence`
    and `noisy` predictors, reporting how much benefit survives.
14. **Modularity proof.** Add one throwaway building device plugin, one
    throwaway EMS controller and one throwaway DERMS issuance policy from a
    drop-in directory, without rebuilding an image, and show all three in the UI
    System view.

---

## 18. Standards to cite in code and documentation

Australian standards are the primary vocabulary. Follow the repository's habit
of naming the standard next to the behaviour it encodes.

- **AS/NZS 4755** demand response modes and the demand response enabling device
  interface, for every demand response instruction, with the relevant part cited
  per device class.
- **CSIP-Aus**, the Australian smart inverter profile, for operating envelopes
  and export limits, already used by the stack.
- **AS/NZS 4777.2** inverter grid-connection responses, Volt-VAr and Volt-Watt
  and reactive capability, already used by the stack.
- **AS/NZS 4777.1**, **AS 5033**, **AS 3000**, **AS 3008** for the installation
  context of the modelled DER.
- **AS 1668.2** mechanical ventilation, for the outside-air rate that bounds any
  building shed.
- **NCC Section J** and **NABERS** for Australian building energy context,
  conditioned-space assumptions and archetype calibration.
- **AS/NZS ISO 50001** energy management systems, for the EMS framing.
- **AS/NZS 3598.2** energy audits for commercial buildings, for the measurement
  and verification framing of any claimed saving.
- **AEMO Wholesale Demand Response Mechanism** and the **AER** flexible-export
  and ring-fencing frameworks, for the market and utility framing.
- **IEC 60076-7** transformer hot-spot ageing, already used by the stack's KPI.

The Swinburne site, building, zone and equipment data is cited to
`../Report_DCH5_Final-Project.pdf` as a **data source**. Say so explicitly in
each service README, and make clear that none of that project's control
technology is used here.

Where a default is an engineering assumption rather than a standard
requirement, say so and name its basis. Anything referenced but not implemented
goes in `docs/STANDARDS-MAPPING.md` as not implemented, rather than being
implied.

---

## 19. House style

- Match the surrounding code. Module docstrings explaining the pattern, type
  hints, dataclasses where the existing code uses them, `logger` not `print`, no
  bare excepts.
- Inline comments are single-sentence `#` lines. No block comment banners.
- No em dashes in prose you write, use a comma instead.
- Documentation tables stay plain, no colour, no styling beyond existing
  markdown.
- Do not add dependencies beyond scipy for the optimisers without asking first.
- Do not vendor OpenEMS source. Reproduce the architecture, cite the project.
- Do not reformat files you are not otherwise changing.

---

## 20. Definition of done

- [ ] Three new containers build and run healthy under `docker compose up --build`
- [ ] A coordinated DERMS plus EMS plus BMS run completes from the browser on the
      v5.0 campus network
- [ ] A run with all three layers disabled produces results identical to the
      pre-change baseline
- [ ] All previously existing tests pass unchanged, plus at least 90 new tests
- [ ] The EMS reproduces the OpenEMS domain model faithfully, with `native` as
      the deterministic default and `openems_edge` as an optional backend that
      fails cleanly when absent
- [ ] No OpenEMS source is vendored, and the design lineage is attributed
- [ ] Every new capability is a registered plugin, visible in the UI System view
- [ ] Demand response instructions are AS/NZS 4755 modes and envelopes are
      CSIP-Aus, with no foreign protocol vocabulary anywhere
- [ ] The Swinburne site, zone and equipment data is present as named presets
      only, cited as a data source, with none of that project's control
      technology in the repository
- [ ] The six-stage precedence ladder is implemented, documented and covered by
      an integration test
- [ ] Headroom physics stayed in the Simulation Engine, DERMS owns only policy
- [ ] Fairness is explicit: every issuance policy states who bears curtailment
      first, and the fairness index is reported
- [ ] All fourteen validation cases recorded with numbers
- [ ] `docs/STANDARDS-MAPPING.md` states what is implemented and what is only
      referenced
- [ ] Root README, three service READMEs and `WINDOWS.md` updated and accurate
- [ ] No inter-container HTTP, no shared data volumes, no built-in network
      assumptions

Begin with Phase 0.
