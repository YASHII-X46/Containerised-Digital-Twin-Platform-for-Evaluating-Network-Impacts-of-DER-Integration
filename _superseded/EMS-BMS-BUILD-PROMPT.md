# Claude Code build prompt: EMS and BMS layer for DT-Stack v5.0

**How to use this file.** Open a terminal at the repository root
(`.../Final Year Project/New-stack-5.0`), start Claude Code, and paste
everything from the `=== PROMPT STARTS HERE ===` line to the end of the file as
your first message. Work through it phase by phase and approve each phase gate
before Claude Code moves on.

Version 2. This revision pins the EMS and BMS defaults to the real hardware
installed at Swinburne Hawthorn under the i-Hub DCH5 project, as documented in
`../Report_DCH5_Final-Project.pdf`.

---

=== PROMPT STARTS HERE ===

You are working in the DT-Stack v5.0 repository, a Dockerised digital-twin
stack for distribution-network DER penetration studies. Your task is to add two
new capabilities to the stack: an **Energy Management System (EMS)** and a
**Building Management System (BMS)**, each as its own container on the existing
OpenFMB/NATS bus, wired into the existing simulation pipeline, UI, KPIs, and
test suites.

This is not a greenfield design. A real transactive demand response system was
built and commissioned on the Swinburne Hawthorn campus under the i-Hub DCH5
project, and the digital twin you are extending exists to model that system.
Section 4 pins your defaults to that hardware. Read it before you design
anything.

Do not start writing code yet. Follow the phase plan in section 15.

---

## 1. Read before you write

Read these first and build a mental model of the contracts you must respect. Do
not skim, the whole design below assumes you have matched the existing patterns
exactly.

| Purpose | Files |
|---|---|
| The physical system being modelled | `../Report_DCH5_Final-Project.pdf` (i-Hub DCH5 Milestone Report M7), sections 1.4, 2.1, 4, 5, 7 |
| Overall architecture and modularity story | `README.md`, `WINDOWS.md` |
| Bus contract and envelope format | `dr-controller/app/bus/participant.py`, `dr-controller/app/bus/transport.py`, `dr-controller/app/openfmb.py` |
| Reference NATS service to copy structurally | `dr-controller/` in full (`app/main.py`, `app/config.py`, `app/controller.py`, `app/control_plugins.py`, `app/strategy_registry.py`, `Dockerfile`, `Dockerfile.windows`, `pytest.ini`, `tests/`) |
| Second reference service, session and state style | `prosumer-shadow-twins/` in full |
| Coordination loop that will call your new services | `simulation-engine/app/control/remote_coordinator.py`, `simulation-engine/app/simulation/qsts.py` |
| Request and response schemas you will extend | `simulation-engine/app/models/schemas.py` |
| Registries you will extend | `simulation-engine/app/metrics/kpi_registry.py`, `simulation-engine/app/metrics/tariffs.py`, `simulation-engine/app/simulation/der_elements.py`, `simulation-engine/app/control/envelopes.py`, `load-engine/app/profiles/der_plugins.py`, `load-engine/app/profiles/archetypes.py`, `load-engine/app/profiles/commercial_models.py`, `load-engine/app/profiles/bess_model.py`, `load-engine/app/profiles/weather.py` |
| Solver-side element builders | `opendss-solver/dss_solver/elements.py`, `opendss-solver/dss_solver/engine.py` |
| The campus feeder already modelled | `sample-networks/README.md`, `sample-networks/swinburne_hawthorn_v3.0.raw`, `sample-networks/real-data-templates/` |
| UI wiring | `ui/server.js`, `ui/bus.js`, `ui/public/js/controls.js`, `ui/public/js/api.js`, `ui/public/js/charts.js`, `ui/public/js/modules.js`, `ui/public/js/main.js` |
| Deployment | `docker-compose.yml`, `docker-compose.windows.yml` |

After reading, write `docs/EMS-BMS-DESIGN.md` summarising what you found and how
you intend to fit in. That document is the phase 0 deliverable.

---

## 2. Non-negotiable architectural rules

These rules already hold across the stack. Breaking one is a build failure, not
a style issue.

1. **Bus only.** Containers talk over NATS with OpenFMB-style command and event
   messages on `{prefix}/command/{service}/{action}` and
   `{prefix}/event/{service}/{action}`, carrying the
   `messageId`/`correlationId`/`timestamp`/`status`/`payload` envelope from
   `BusParticipant.envelope()`. No inter-container HTTP, no shared volumes for
   data exchange. Payloads travel inline.
2. **Registries, not engine edits.** Every new capability is a registered plugin
   behind a `register_*()` call, discoverable through a listing command so the
   UI System view can show it. If you find yourself adding an
   `if kind == "hvac"` branch inside a loop, you have taken the wrong path.
3. **The stack ships no built-in networks.** Nothing you add may assume a
   particular feeder or bus numbering. The Swinburne hardware in section 4
   becomes **named configuration presets and template files**, selectable by
   name, never hardcoded into engine logic. A user with a different campus must
   get the same capability by supplying their own numbers.
4. **Backwards compatibility.** A simulate request that omits every EMS and BMS
   field must produce numerically identical results to the current code. All 332
   existing tests must still pass unchanged.
5. **Health and lifecycle parity.** Every new container writes the readiness
   marker after `participant.start()`, is `restart: on-failure`, has a
   healthcheck, and honours `READY_FILE`, `NATS_URL`, `BUS_PREFIX`,
   `BUS_TRANSPORT` (including `loopback` for tests) exactly as `dr-controller`
   does.
6. **Determinism.** Same seed and same request produce the same numbers. Seed
   any stochastic element (occupancy, forecast error) from the existing per-bus
   seed derivation.
7. **Convergence discipline.** Anything you add to KPIs or summaries uses
   converged timesteps only, consistent with the existing engine.
8. **Honest limits.** Quasi-static only. No dynamics, protection, or
   sub-timestep control. Document every simplification in the service README.

---

## 3. What EMS means here, and what BMS means here

**BMS, Building Management System.** The building-side layer. It owns the
controllable thermal and electrical loads inside a building: HVAC, lighting,
domestic hot water, and deferrable plug loads. It holds a thermal state per
zone, tracks occupant comfort and air quality, and converts a power instruction
into a physically achievable load while reporting what that cost in comfort
terms. The BMS is the only component allowed to decide how a building meets a
demand limit.

**EMS, Energy Management System.** The site-level or campus-level layer above
the BMS. It owns dispatch of a site's assets against an objective over a rolling
horizon: battery charge and discharge, EV charging, PV curtailment for economic
reasons, and the demand limit it hands down to the BMS. The EMS is an economic
and operational scheduler, not a network controller.

**The boundary with the existing DR and DOE layers.** The DR controller and the
dynamic operating envelope machinery are the network operator's layers, acting
on voltage, thermal, and export-limit constraints. The EMS acts on the
customer's objective. Both exist in the real world and they can disagree. Your
implementation must make that disagreement explicit and resolvable rather than
hidden, see section 5.

---

## 4. Reference hardware, the physical system this must represent

Source: `../Report_DCH5_Final-Project.pdf`, i-Hub DCH5 Milestone Report M7,
Swinburne University of Technology, ARENA and AIRAH funded, lead investigator
Dr Mehdi Seyedmahmoudian. Read section 2.1 (Table 1), section 4, section 5, and
section 7 of that report yourself before implementing. Cite it in the service
READMEs as the calibration source, the same way the Load Engine README cites its
Australian data sources.

### 4.1 Site

Two adjacent buildings on the Hawthorn campus form the community microgrid
precinct: **ATC** (Advanced Technology Centre, 427 to 451 Burwood Road) and
**AMDC** (Advanced Manufacturing and Design Centre). They were chosen for
proximity, zoning diversity, roof space, timetable access, and BMS
controllability.

One constraint from that report is directly relevant to your EMS design: **the
two buildings sit behind separate transformers, and the grid code prohibited
connecting two sources originating from separate transformers**, so physical
energy sharing between them was ruled out and emulated in hardware instead. Your
EMS must therefore model inter-site energy sharing as a **permissioned
constraint**, defaulting to disallowed across a transformer boundary, and it
must derive that boundary from the network model's `is_transformer` branches
rather than from a hardcoded list. Make `allow_inter_site_sharing` an explicit
per-run knob so the counterfactual (what sharing would have been worth) is
measurable. That comparison is a genuine research contribution and it falls out
of the stack for free once the constraint is modelled properly.

Instrumented zones, useful as the default building and zone set in the preset:
ATC101, ATC103, ATC206, AMDC301, AMDC303, AMDC355, AMDC451. These are lecture
theatres and private study areas, chosen because their occupancy is dynamic
(theatres are timetabled but also used for public exhibitions, study areas are
stochastic).

### 4.2 Installed DER, ATC West Wing roof

| Item | Specification |
|---|---|
| PV modules | 84 x Trina Vertex TSM-390-DE09.08, 390 W each |
| Array rating | 32.76 kWp (the report also rounds to 32 and 33 kWp), 167 m2 |
| Mounting | 15 degree tilt, 1250 mm row spacing to avoid self-shading, PVsyst shading study in the appendix |
| Inverters | 3 x Sungrow SH10RT hybrid, 10 kVA each, 30 kVA total |
| Battery | 3 x BYD HVS 10.2 kWh, 30.6 kWh total (reported as 30 kWh) |
| Operating floor | Battery discharges to a 20 percent backup reserve, enforced by the Modbus gateway logic |
| Compliance | AS 1170.2, AS 4777.1, AS 4777.2, AS 5033, AS 3000, AS 3008 |

The three inverters exist so one represents ATC generation, one AMDC
generation, and one a shared community battery. Preserve that three-way
partition in the preset, it is what makes the transactive scenarios meaningful.

Add these as **named configurations** in the existing registries, exactly as
`BESS_CONFIGS` already names Powerwall 2/3, BYD HVS, and Enphase 5P:

- Battery config `byd_hvs_10.2` if not already present, with usable capacity
  10.2 kWh, and a campus stack `swinburne_atc_stack` of 3 units.
- Inverter and PV config `sungrow_sh10rt` at 10 kVA hybrid, and array preset
  `swinburne_atc_pv` at 32.76 kWp with the 15 degree tilt and 167 m2 area.
- Default EMS state-of-charge band `soc_min = 0.20`, `soc_max = 1.00`,
  round-trip efficiency `0.90`, cycle cost `0.02` AUD/kWh. These are the values
  the DCH5 microgrid optimiser actually ran with, per the pymgrid parameter dump
  in section 7.3 of the report.

### 4.3 Measurement and control interfaces that exist on site

Your message vocabulary should map onto these, so a future hardware-in-the-loop
run is a transport swap and not a redesign. You are not implementing drivers,
you are naming things so the drivers would drop in.

| Subsystem | Hardware and protocol | What it provides |
|---|---|---|
| Building management | Alerton BMS, BACnet/IP, in-house Python BACnet sniffer on a Raspberry Pi, later replaced by an NI device | Zone temperature setpoints, VSD speeds, valve positions, thermostat points, supervisory HVAC control |
| Semantic model | Brick Schema model built from the BACnet point discovery, with CSIRO | Point to equipment to location relationships |
| Inverters and battery | Modbus gateway plus iSolarCloud API | Generation, state of charge, connected load, charge and discharge control |
| Lighting | Clipsal C-Bus network | Zone lighting on/off and level, occupancy feedback |
| Plug load prototype | TP-Link smart switches and lamps | Monitored and switchable plug load |
| Occupancy | Terabee 3D time-of-flight camera plus Raspberry Pi at room entry points | People count in and out, privacy preserving, chosen over IP and thermal cameras |
| Air quality | Custom ESP32 plus Sensirion SCD30 module, battery powered, MQTT every 15 minutes | Zone CO2, relative humidity, temperature |
| Weather | ECOWITT HP2551 station on the ATC South West Wing roof, local scraping server, Weather Underground and ECOWITT APIs available | Outdoor temperature, humidity, irradiance proxy |
| Metering | Building smart meters, additional metering requested during the project | Energy consumption per entity |
| Community microgrid emulator | NI LabVIEW based, built with Bramec, AC/DC converters into fixed resistances as programmable loads, solid-state relay to reassign load between inverters, MQTT driven | Hardware in the loop emulation of energy sharing |
| Compute | 3 laptops, 1 virtual top node (VTN) and 2 virtual end nodes (VEN) | Agent hosting |
| Messaging | OpenADR 2.0b VEN and VTN pattern, custom OpenADR to BuildingJSON parser, MQTT, publication to the CSIRO Data Clearing House via Senaps.io | Standardised DR event and telemetry exchange |

**Naming rule.** Use BACnet and Brick point vocabulary for BMS payload keys
(`zone_air_temp_c`, `zone_air_temp_setpoint_c`, `supply_fan_vsd_pct`,
`valve_position_pct`, `zone_co2_ppm`, `zone_rh_pct`, `occupancy_count`), and
OpenADR event vocabulary for the EMS to BMS instruction (`event_id`,
`signal_name`, `signal_type`, `interval_start`, `duration_s`, `payload_value`).
Carry them inside the existing OpenFMB NATS envelope. Do not introduce a second
bus, a second broker, or an MQTT dependency. The point is that the twin speaks
the same words the hardware does.

### 4.4 Control strategies already proven on site

Implement these as the built-in BMS shed strategies, with the report's own
parameters as defaults, because it makes your results directly comparable to
measured outcomes.

- **Timetable scheduling.** Room bookings pulled from the university timetable
  API, thresholded on a 15 minute interval, HVAC switched through the BACnet
  gateway. The report found roughly 40 percent of spaces unused while HVAC ran
  continuously from 07:00 to 23:00, and estimated about AUD 15,200 per year
  saved per HVAC device in large lecture theatres.
- **Forecast and occupancy driven setpoint optimisation.** Setpoint chosen from
  forecast outdoor temperature and predicted zone occupancy, about AUD 11,200
  per year in private study areas.
- **Direct load control by duty cycling.** Compressor cycled with a default 50
  percent off-cycle fraction on a 0.5 hour cycling period, with 25, 30, 33, 65,
  75, and 100 percent as documented alternatives, and shorter cycling periods
  preferred for comfort.
- **Setpoint setback.** Cooling setpoints evaluated at 22 C and 26 C, the
  sedentary summer-clothing comfort range used in the study.
- **Ventilation floor.** Minimum 10 L/s/person outside air for students over 16
  under AS 1668.2, which is a hard constraint on how far a CO2-aware shed may
  go. Enforce it, and report any breach as a violation rather than absorbing it.

### 4.5 Measured outcomes to reproduce

From section 7 of the report, over the commissioning period:

- April generation 1.73 MWh, May generation 1.473 MWh to 26 May.
- About **97.77 percent of on-site generation consumed on site**, with the
  Modbus gateway biasing battery dispatch toward self-consumption.
- CO2 avoided 14,814 kg over the period.
- Optimisation benchmarked as rule-based against MILP and Q-learning using
  pymgrid, with cost terms for unserved load, over-generation, and CO2.

These become validation targets in section 16. You are not expected to match
them exactly, the twin has different weather and load inputs. You are expected
to land in the same region and to explain any gap.

### 4.6 Measured data ingestion

The stack already accepts measured data through the `file` weather provider and
the kind-aware custom profile CSVs. Extend that path rather than inventing a new
one, and add CSV templates under `sample-networks/real-data-templates/` for:

- ECOWITT weather export mapped to the existing `temp_C[,ghi_Wm2]` schema.
- BMS trend export: timestamp, point name, value, so a real zone temperature or
  VSD trend can drive or validate the thermal model.
- iSolarCloud export: timestamp, PV kW, battery kW, state of charge, so a
  measured inverter day can be replayed as a custom `pv` shape.
- Occupancy counts: timestamp, zone, count.

Each template gets a README row explaining the source system it came from. Add a
`bms_source` run knob with values `model` and `measured`, so a study can be run
against synthetic physics or against a replayed measured day.

---

## 5. Control precedence ladder

The stack already documents a precedence for its existing layers: envelope cap,
then autonomous Volt-Watt, then commanded DR curtailment. You are inserting two
layers below and beside that. Implement, document, and test this exact order
inside a single timestep:

1. **EMS schedules** first, on the customer objective, producing intended
   setpoints for BESS, EV, PV limit, and a per-building demand limit.
2. **BMS realises** the building demand limit within its comfort, air quality,
   and equipment constraints, returning achievable HVAC and controllable load
   power plus any unmet energy.
3. **Network layers act last and win.** The dynamic operating envelope cap, the
   autonomous AS/NZS 4777.2 inverter response, and the DR controller's commanded
   setpoints apply on top of the EMS schedule and may override it.
4. **The EMS observes the override.** Whatever the network layers changed is fed
   back into EMS state for the next step so a receding-horizon optimiser
   re-plans against reality rather than against its own intention. Record the
   difference as an explicit result series and KPI (curtailed EMS intent).

This mirrors the real hierarchy in the DCH5 report: sensor agents, then local
EMS agents, then the market and community agents above them, with the network
and the grid code binding at the top. Add `docs/CONTROL-PRECEDENCE.md` with a
diagram of the full ladder including the pre-existing layers and a worked
numeric example of a step where all four layers act.

---

## 6. New service: `bms-controller/`

Mirror the `dr-controller/` layout exactly: `app/__init__.py`, `app/bus/` (copy
the transport and participant modules as the other services do),
`app/config.py`, `app/main.py`, `app/openfmb.py`, the model and registry modules
below, `Dockerfile`, `Dockerfile.windows`, `.dockerignore`, `requirements.txt`,
`pytest.ini`, `README.md`, `tests/`.

### 6.1 Building physics

Implement a lumped-parameter thermal model per zone, defaulting to a 2R2C
network (indoor air node and thermal mass node) with a documented reduction to
1R1C for cheap runs. Follow the simple hourly method lineage of ISO 13790 and
EN ISO 52016 so the model is defensible in a thesis. Required behaviour:

- Heat balance per timestep: conduction to ambient, solar gain through glazing
  driven by the same irradiance trace the Load Engine already uses, internal
  gains from occupancy, lighting, and equipment, and the HVAC sensible
  contribution.
- HVAC electrical power derived from thermal power through a coefficient of
  performance that varies with outdoor temperature, not a constant, with a
  documented curve and a rated capacity that can saturate.
- A ventilation term driven by occupancy, with the AS 1668.2 floor of 10
  L/s/person enforced and CO2 concentration tracked per zone from a simple mass
  balance, so a CO2-aware shed strategy has something real to act on. This is
  what the SCD30 modules measure on site, model the same quantity.
- Named building configurations aligned with the three existing commercial
  archetypes in `load-engine/app/profiles/commercial_models.py`, plus
  `swinburne_atc` and `swinburne_amdc` presets covering the instrumented zone
  types (large lecture theatre, private study area, office floor).
- Comfort tracked against a deadband, ASHRAE 55 and ISO 7730 cited, with an
  occupied and an unoccupied band, the 22 C and 26 C study setpoints as
  defaults, and a strict rule that comfort violation is recorded rather than
  silently allowed.

### 6.2 Registries

| Registry | Module | Built-ins | Extension call |
|---|---|---|---|
| Zone thermal models | `app/zone_models.py` | `rc1`, `rc2` | `register_zone_model(...)` |
| Building device plugins | `app/building_plugins.py` | `hvac` (order 10), `ventilation` (15), `lighting` (20), `dhw` (30), `plug` (40) | `register(BuildingPlugin())` |
| Comfort policies | `app/comfort_policies.py` | `fixed_band`, `adaptive`, `precool` | `register_policy(...)` |
| Shed strategies | `app/shed_strategies.py` | `timetable`, `setpoint_setback`, `duty_cycle`, `occupancy_scaled`, `priority_order` | `register_strategy(...)` |

Building device plugins follow the `ControlPlugin` pattern from
`dr-controller/app/control_plugins.py`: an ordered registry, each plugin reads a
shared context and writes its own power contribution, so a new controllable
building load is a new file and a `register()` call.

Defaults for `duty_cycle` are the report's: 50 percent off-cycle fraction, 0.5
hour cycling period. `timetable` accepts a booking schedule per zone at 15
minute resolution and falls back to the archetype occupancy schedule when none
is supplied.

Support drop-in loading exactly as the Load Engine does, through
`BMS_PLUGIN_MODULES` and `BMS_PLUGINS_DIR`, with import errors logged and
skipped rather than fatal.

### 6.3 Bus contract

Service name `bms-controller`. Commands:

| Action | Payload in | Payload out |
|---|---|---|
| `configure` | `session_id`, `buildings[]`, `step_minutes`, `topic_prefix`, optional `config` overrides | echo of accepted config, `building_count`, `zone_count`, resolved model names |
| `step` | `session_id`, `t`, `timestamp`, `step_hours`, `ambient_temp_c`, `irradiance_wm2`, `buildings[]` each with `bus_id`, `demand_limit_kw` (nullable), `setpoint_offset_c`, `mode`, optional OpenADR-style `event` block | `buildings[]` each with `bus_id`, `hvac_kw`, `ventilation_kw`, `controllable_kw`, per-zone `zone_air_temp_c`, `zone_co2_ppm`, `shed_kw`, `unmet_thermal_kwh`, `comfort_violation` flag and magnitude, `ventilation_violation` flag |
| `buildings` | `session_id` | current per-building and per-zone state, for the UI and debugging |
| `models` | none | installed zone models, device plugins, comfort policies, shed strategies |
| `stop` | `session_id` | `stopped` flag |

A `configure` entry per building carries at minimum `bus_id`, optional
`building_id` and `name`, `preset` or `archetype`, `floor_area_m2`,
`ua_kw_per_k`, `capacitance_kwh_per_k`, `hvac_rated_kw`, `cop_nominal`,
`zone_air_temp_setpoint_c`, `comfort_band_c`, `occupied_hours` or a `timetable`,
`solar_aperture_m2`, `internal_gain_w_per_m2`, `design_occupancy`, `policy`,
`shed_strategy`. Every field except `bus_id` defaults sensibly from the preset,
and unknown fields are rejected with a clear error.

### 6.4 Environment configuration

`BMS_STEP_MINUTES`, `BMS_DEFAULT_SETPOINT_C` (22.0), `BMS_COMFORT_BAND_C` (2.0),
`BMS_UNOCCUPIED_BAND_C` (4.0), `BMS_MAX_PRECOOL_C` (2.0), `BMS_MIN_COP`,
`BMS_VENTILATION_L_PER_S_PERSON` (10.0), `BMS_CO2_LIMIT_PPM` (1000),
`BMS_DUTY_OFF_FRACTION` (0.5), `BMS_DUTY_PERIOD_H` (0.5), `BMS_PLUGIN_MODULES`,
`BMS_PLUGINS_DIR`, plus the standard bus variables. Document every one in the
service README table, matching the existing README style.

---

## 7. New service: `ems-controller/`

Same layout discipline as above.

### 7.1 What it does

For each configured site (a named group of buses, typically a building or a
campus connection point) the EMS runs a receding-horizon dispatch each timestep:
it takes a forecast of load, PV, and price over a horizon, the current asset
states, and the active constraints, and returns setpoints. Sites solve
independently unless an inter-site sharing objective is selected and sharing is
permitted per section 4.1.

### 7.2 Registries

| Registry | Module | Built-ins | Extension call |
|---|---|---|---|
| Objectives | `app/objective_registry.py` | `peak_shave`, `cost_min`, `self_consumption`, `emissions_min`, `demand_charge_min`, `doe_aware`, `community_share` | `register_objective(...)` |
| Optimisers | `app/optimiser_registry.py` | `rule_based`, `lp`, `mpc` (receding-horizon wrapper around `lp`) | `register_optimiser(...)` |
| Dispatchable assets | `app/asset_registry.py` | `bess`, `ev`, `pv`, `bms_load`, `generic` | `register_asset(...)` |
| Forecast providers | `app/forecast.py` | `perfect`, `persistence`, `noisy` | `register_forecast(...)` |

`rule_based` reproduces the Modbus gateway logic actually deployed on site:
prioritise self-consumption, discharge the battery down to the 20 percent
backup reserve, export the remainder. It is the baseline every other optimiser
is measured against, exactly as the DCH5 report benchmarks rule-based against
MILP and Q-learning.

`community_share` models the transactive case: minimise total community cost by
transferring surplus between sites, subject to the sharing permission flag. When
sharing is disallowed (the real ATC and AMDC situation, separate transformers)
it must degrade cleanly to per-site optimisation and report the foregone benefit.

The `lp` optimiser uses `scipy.optimize.linprog` with the HiGHS method and falls
back to a documented greedy heuristic when scipy is unavailable. This is the
precedent already set by `max_total` in
`simulation-engine/app/control/envelopes.py`, follow it including the log line
on fallback. Pin scipy in `requirements.txt` the same way the simulation engine
does, respecting the NumPy 1.x pin used across the stack.

The forecast providers matter for the thesis: `perfect` is the upper bound,
`persistence` and `noisy` (with a configurable error sigma) show how much of the
EMS benefit survives real forecast uncertainty. The DCH5 project used LSTM and
XGBoost forecasters, note that in the README as the real-world counterpart of
the `noisy` provider, and keep the interface open so a trained model could be
registered later.

### 7.3 The optimisation problem

For each site and horizon:

- Decision variables per interval: battery charge power, battery discharge
  power, EV charge power, PV curtailment, building demand limit, grid import and
  export split, and inter-site transfer when sharing is permitted.
- Constraints: energy balance per interval, battery power and energy limits with
  round-trip efficiency (default 0.90) and a state-of-charge band (default 0.20
  to 1.00), terminal state-of-charge condition to prevent horizon-end dumping,
  EV energy delivered by departure time, PV curtailment bounded by available PV,
  building demand limit bounded below by the BMS reported minimum feasible load,
  non-negative import and export, an export cap when a dynamic operating
  envelope is active, and zero inter-site transfer across a transformer boundary
  unless explicitly permitted.
- Objective built from the selected registry entry, priced by the existing
  tariff registry values so EMS economics and the existing cost KPIs agree, with
  an optional battery cycle cost (default 0.02 AUD/kWh) and CO2 term.

Keep the linear program strictly linear: model the import and export split with
two non-negative variables and rely on the price structure to prevent
simultaneous import and export, and document the one edge case where that
assumption can break (negative feed-in prices) rather than pretending it cannot.
Do the same for simultaneous battery charge and discharge.

### 7.4 Bus contract

Service name `ems-controller`. Commands:

| Action | Payload in | Payload out |
|---|---|---|
| `configure` | `session_id`, `sites[]`, `objective`, `optimiser`, `horizon_steps`, `step_minutes`, `tariff`, `forecast`, `allow_inter_site_sharing`, `topic_prefix` | accepted configuration, `site_count`, resolved names, horizon in hours |
| `dispatch` | `session_id`, `t`, `timestamp`, `step_hours`, `sites[]` with current state (`soc`, `site_demand_kw`, `pv_kw`, `ev_pending_kwh`, `voltage_pu`, `export_limit_kw`), plus the forecast slice | `sites[]` with `bess_charge_kw`, `bess_discharge_kw`, `ev_charge_kw`, `pv_limit_kw`, `bms_demand_limit_kw`, `inter_site_transfer_kw`, `import_target_kw`, `objective_value`, `binding_constraints[]`, `solver_status` |
| `settle` | `session_id`, `t`, per-site actually applied values after the network layers acted | acknowledgement, updated internal state |
| `objectives`, `optimisers`, `assets`, `forecasts` | none | registry listings for the System view |
| `stop` | `session_id` | `stopped` flag |

`settle` is what closes the loop described in section 5. Do not skip it.

### 7.5 Environment configuration

`EMS_HORIZON_STEPS`, `EMS_OBJECTIVE`, `EMS_OPTIMISER`, `EMS_FORECAST`,
`EMS_FORECAST_SIGMA`, `EMS_SOC_MIN` (0.20), `EMS_SOC_MAX` (1.00),
`EMS_TERMINAL_SOC`, `EMS_ROUND_TRIP_EFFICIENCY` (0.90),
`EMS_CYCLE_COST_AUD_PER_KWH` (0.02), `EMS_DEMAND_CHARGE_AUD_PER_KVA`,
`EMS_ALLOW_INTER_SITE_SHARING` (false), `EMS_SOLVER_TIMEOUT_S`, plus the
standard bus variables.

---

## 8. Load Engine changes

The building baseline has to exist before the BMS can modulate it, and the
stack's own rule is that a new physical DER is three small plugins. Do it that
way.

1. Add an `hvac` DER generation plugin in
   `load-engine/app/profiles/der_plugins.py` (or a new module registered the
   same way) producing the unmanaged building HVAC series for buses flagged as
   buildings, contributing `hvac_kw` with `net_load` sign `+1`, ordered after
   load and PV but before BESS so the battery still dispatches against true net
   demand.
2. Reuse the existing weather machinery: the plugin consumes the same
   temperature and irradiance traces already carried on `GenerationContext`, so
   the `file` weather source (an ECOWITT export, per section 4.6) drives
   building thermal behaviour with measured data.
3. Carry building metadata through the profiles payload so the simulation engine
   and the BMS configure themselves from a single generate call. Extend the
   per-bus config with an optional `building` block rather than adding top-level
   keys.
4. Add the `swinburne_atc` and `swinburne_amdc` presets and the campus PV and
   battery configurations from section 4.2 to the relevant registries, as named
   entries only.
5. Keep multi-day continuity: zone temperature at the end of day N seeds day N
   plus 1, exactly as battery state of charge and state of health already do.
6. Verify the claim in the root README that a generation plugin's extra series
   automatically reaches the power flow through `other_der_kw`, the twins, and
   the charts. If it holds, take the zero-edit path. If it does not hold for a
   controllable load, say so explicitly and make the smallest possible change.

---

## 9. Simulation Engine changes

1. **Schemas.** Add `EmsConfig` and `BmsConfig` to `app/models/schemas.py`, both
   optional and defaulting to off, following the `DoeConfig` style including
   field descriptions. Add matching summary fields to `SimulationResponse` with
   safe defaults so old clients keep working.
2. **Site coordination.** Extend `RemoteCoordinator`, or add a sibling
   `SiteCoordinator` composed into the QSTS loop, performing the section 5
   ladder each timestep: EMS `dispatch`, BMS `step`, apply to the solver, let
   the existing envelope, Volt-Watt, and DR layers act, then EMS `settle`. Use
   the existing `_request_ok` helper so bus timeouts and error events surface as
   proper HTTP errors rather than silent zeros.
3. **Site and transformer topology.** Derive each site's transformer boundary
   from the network model so the sharing constraint in section 4.1 is a property
   of the uploaded network, not a configuration guess. Expose the derived
   grouping in the response so the UI can show it.
4. **DER element.** Register an `HvacElement` in
   `app/simulation/der_elements.py` if a dedicated solver element is needed,
   otherwise document why the generic path suffices.
5. **KPIs.** Register these in `app/metrics/kpi_registry.py`, each with the same
   docstring and unit discipline as the existing eighteen: `peak_demand_kw`,
   `peak_demand_reduction_pct`, `load_factor`, `self_consumption_pct`,
   `self_sufficiency_pct`, `demand_charge_aud`, `ems_cost_saving_aud`,
   `ems_intent_curtailed_kwh`, `community_shared_kwh`,
   `comfort_violation_hours`, `max_comfort_deviation_c`,
   `ventilation_violation_hours`, `max_zone_co2_ppm`, `unmet_thermal_kwh`,
   `bms_shed_kwh`, `bms_energy_kwh`. Every one must be computable on runs where
   EMS and BMS are off, reporting the honest baseline value or zero, never
   `None`.
6. **Tariffs.** Add a `tou_commercial` tariff with a demand-charge component,
   since a campus connection is billed that way in Australia and
   `demand_charge_min` is meaningless without it. Extend the `Tariff` dataclass
   with an optional demand-charge rate and window, defaulting to zero so
   existing tariffs and cost KPI results are unchanged.
7. **Result series.** Add per-timestep series for the new charts: site demand
   against the EMS demand limit, battery state of charge against the EMS plan,
   zone temperature against the comfort band, zone CO2 against the limit, and
   EMS intended against actually applied dispatch.
8. **Failure behaviour.** If EMS or BMS is requested but the container does not
   answer, fail the run with a clear message naming the service and action.
   Never degrade silently to an uncoordinated run.

---

## 10. Solver changes

Prefer no solver changes. If a controllable building load needs its own OpenDSS
element, register an element builder in `opendss-solver/dss_solver/elements.py`
through the existing registry, keep the solver dumb (no control logic
solver-side), and confirm the PSS SINCAL adapter still satisfies the same
contract or document precisely what a SINCAL implementation would need.

---

## 11. UI changes

The control panel is the examinable artefact, treat it as a first-class
deliverable and match the existing visual language rather than inventing a new
one.

- **Scenario controls** (`ui/public/js/controls.js`): an EMS section (enable,
  objective, optimiser, horizon, forecast provider and sigma, state-of-charge
  band, terminal state of charge, cycle cost, demand-charge rate, inter-site
  sharing permission) and a BMS section (enable, preset, per-building setpoint
  and comfort band, policy, shed strategy and its parameters, ventilation floor,
  CO2 limit, which buses are buildings, `bms_source` model or measured). Keep
  them collapsible and off by default.
- **Saved scenario configurations** must round-trip the new controls.
- **Results**: summary metric cards for peak reduction, self-consumption,
  self-sufficiency, cost saving, comfort violation hours, ventilation violation
  hours, and unmet thermal energy. New charts: dispatch stack (load, PV,
  battery, EV, HVAC) against the demand limit, zone temperature with the comfort
  band shaded, zone CO2 against the limit, EMS intent against applied dispatch,
  and battery state of charge with the planned trajectory overlaid. Reuse the
  existing chart helpers and multi-day separator behaviour.
- **System view** (`ui/public/js/modules.js`): list the new registries live from
  the new services' listing commands, so the modularity claim stays
  self-evidencing.
- **CSV export** must include the new series.
- `ui/server.js` and `ui/bus.js` gain the new command routes over NATS. No HTTP
  to the new containers.

---

## 12. Deployment

Add both services to `docker-compose.yml` and `docker-compose.windows.yml`
following the existing patterns exactly: build context, container name, bus
environment variables, `depends_on` the broker, `restart: on-failure`, readiness
healthcheck, and the simulation engine waiting on both new services being
healthy. Windows images follow the `Dockerfile.windows` and `READY_FILE`
conventions already in the repo. Do not publish HTTP ports for the new services,
they are bus-only participants. Update `WINDOWS.md` with the two new services,
their footprint, and any base image notes.

---

## 13. Testing

The stack currently has 332 tests and its credibility rests on them. Target at
least 60 new tests, and no regressions.

**`bms-controller/tests/`** (target 25 plus): thermal model conservation,
rated-capacity saturation, coefficient of performance varying with ambient
temperature, comfort band enforcement occupied and unoccupied, CO2 mass balance
and the AS 1668.2 ventilation floor binding, precool then coast, demand limit
honoured exactly when feasible and shortfall reported when not, unmet thermal
energy accounting, multi-day zone temperature continuity, each shed strategy
including timetable and the 50 percent 0.5 hour duty cycle, registry listing
commands, drop-in plugin loading, loopback bus round trip.

**`ems-controller/tests/`** (target 25 plus): each objective produces the
qualitatively correct schedule on a hand-built case (peak shave flattens, cost
minimisation shifts to the off-peak window, self consumption charges midday,
demand-charge minimisation clips the peak), the `rule_based` optimiser
reproduces the 20 percent reserve behaviour, battery power and energy
constraints respected, round-trip efficiency losses present, terminal
state-of-charge respected, EV energy delivered by departure, export cap
respected when an envelope is active, inter-site transfer zero across a
transformer boundary unless permitted, scipy path and greedy fallback agree in
ordering on a case with a known optimum, forecast providers behave as
documented, `settle` changes the next plan, solver timeout handled, registry
listings, loopback round trip.

**`simulation-engine/tests/`** (target 10 plus): precedence ladder integration
test where an EMS intent is overridden by a dynamic operating envelope and the
gap appears in `ems_intent_curtailed_kwh`, each new KPI on a run with EMS and
BMS off, demand-charge tariff pricing, transformer-boundary site grouping
derived from the network, new result series present and correctly shaped,
request schema defaults, and a regression test asserting that a request without
EMS or BMS returns identical key metrics to the pre-change baseline. Use the
existing loopback-bus in-process pattern so the real services are exercised, do
not mock the contract you just wrote.

**`load-engine/tests/`**: the HVAC generation plugin, weather coupling, the
Swinburne presets, building metadata pass-through, multi-day continuity.

Every service must pass `python -m pytest -q` from its own directory. Report the
new total test count and update the Verification section of the root README.

---

## 14. Documentation

- `bms-controller/README.md` and `ems-controller/README.md` in the exact style
  of the existing service READMEs: full environment variable table, bus contract
  table, registry table, worked message examples, the DCH5 report cited as the
  calibration source, and an honest scope-limits paragraph.
- Root `README.md`: architecture diagram updated, service table updated, new
  registry rows in the modularity table, new KPI names listed, new environment
  variables in the core table, verification counts updated.
- `docs/EMS-BMS-DESIGN.md` and `docs/CONTROL-PRECEDENCE.md`.
- `docs/HARDWARE-MAPPING.md`: a table mapping every physical device in section
  4.3 to the model element, registry entry, or message field that represents it,
  and an explicit list of what is not represented. This is the document that
  proves the twin corresponds to the plant.
- `docs/EMS-BMS-VALIDATION.md`: the validation cases from section 16 with
  expected and produced numbers.

---

## 15. Phase plan and gates

Stop at every gate and wait for my approval. Do not batch phases.

**Phase 0, survey and design.** Read section 1 including the DCH5 report, write
`docs/EMS-BMS-DESIGN.md` and a first draft of `docs/HARDWARE-MAPPING.md` with
the proposed module layout, message schemas, precedence implementation point,
and anything in this brief you think is wrong or unnecessary. Gate: I approve
the design.

**Phase 1, BMS service.** Build `bms-controller/` standalone with its tests,
runnable on the loopback bus, including the Swinburne presets and all shed
strategies. Gate: tests pass and a hand-run thermal and CO2 trace behaves
sensibly for a lecture theatre across a teaching day.

**Phase 2, EMS service.** Build `ems-controller/` standalone with its tests,
both scipy and fallback paths, and the rule-based baseline matching the deployed
Modbus logic. Gate: tests pass and each objective is demonstrated on a
hand-built case.

**Phase 3, Load Engine.** HVAC generation plugin, building metadata, Swinburne
presets, measured-data templates, multi-day continuity. Gate: load-engine tests
pass and generated profiles show a believable campus HVAC shape on a summer and
a winter design day.

**Phase 4, Simulation Engine integration.** Schemas, coordination ladder,
transformer-boundary grouping, KPIs, tariff, result series. Gate: the precedence
integration test passes and the no-EMS regression test proves identical baseline
results.

**Phase 5, UI.** Controls, charts, cards, System view, CSV, saved scenarios.
Gate: an end-to-end run through the browser on an uploaded network.

**Phase 6, deployment, validation, documentation.** Compose files, Windows
guide, all READMEs, hardware mapping, validation document, final test count.
Gate: `docker compose up --build` brings the whole stack healthy and a
coordinated EMS plus BMS run completes from the browser.

At each gate report: what changed, test counts before and after, anything you
compromised on, and anything you found in the existing code that is wrong or
fragile.

---

## 16. Validation cases to demonstrate

Record all of these in `docs/EMS-BMS-VALIDATION.md` at phase 6.

1. **Energy conservation.** A zone with HVAC disabled drifts to ambient at the
   rate the RC parameters predict, checked against a closed-form exponential.
2. **Timetable scheduling.** Applying the report's timetable strategy to a
   lecture-theatre-heavy building reduces HVAC energy by an amount in the region
   of the 40 percent the DCH5 study measured, and the residual is explainable.
3. **Peak shaving.** The ATC preset (32.76 kWp PV, 30.6 kWh battery, 30 kVA
   inverters) under `peak_shave` reduces peak demand by an amount consistent
   with the battery energy and power available, and `load_factor` improves.
4. **Self-consumption.** The `rule_based` optimiser with the 20 percent reserve
   reproduces a high self-consumption fraction, compared against the 97.77
   percent the site measured, with any gap attributed to load and weather
   differences.
5. **Cost minimisation against a time-of-use tariff.** Charging moves into the
   off-peak window and `net_energy_cost_aud` falls by an amount consistent with
   the price spread and round-trip efficiency.
6. **Comfort and air quality trade-off.** Sweeping the comfort band from tight
   to loose produces a monotonic trade-off between `bms_shed_kwh` and
   `comfort_violation_hours`, and the AS 1668.2 ventilation floor caps the shed
   before CO2 breaches the limit. Non-monotonicity is a bug, find it.
7. **Precedence conflict.** A dynamic operating envelope binds while the EMS
   wants to export. Show the envelope winning, the intent gap recorded, and the
   next EMS plan adapting after `settle`.
8. **The sharing counterfactual.** Run ATC and AMDC with
   `allow_inter_site_sharing` false (the real grid-code situation) and true, and
   quantify the foregone community benefit. This is the question the DCH5
   project had to answer with a hardware emulator, your twin answers it in
   software.
9. **Forecast sensitivity.** The same scenario under `perfect`, `persistence`,
   and `noisy` forecasts, reporting how much benefit survives.
10. **Modularity proof.** Add one throwaway building device plugin and one
    throwaway EMS objective from a drop-in directory, without rebuilding an
    image, and show both appearing in the UI System view.

---

## 17. Standards and references to cite in code and documentation

Follow the repository's habit of naming the standard next to the behaviour it
encodes.

- ISO 50001, energy management systems, for the EMS framing.
- OpenADR 2.0b, VEN and VTN roles and event signals, the vocabulary the DCH5
  system used and the one your EMS to BMS instruction should mirror.
- ASHRAE Standard 135 (BACnet) and Brick Schema, for BMS point naming, matching
  the Alerton BACnet points and the Brick model built on site.
- ASHRAE Guideline 36, high-performance sequences of operation, for setback and
  duty-cycle logic.
- ASHRAE Standard 55 and ISO 7730, thermal comfort, for the comfort band.
- AS 1668.2, mechanical ventilation, for the 10 L/s/person outside-air floor.
- ISO 13790 and EN ISO 52016-1, simple hourly building energy calculation, for
  the RC thermal model lineage.
- AS/NZS 4777.1 and 4777.2, AS 5033, AS 3000, AS 3008, AS 1170.2, the
  installation and inverter standards the ATC system was built to.
- CSIP-Aus and IEEE 2030.5 for the envelope interaction, already used.
- AEMO Wholesale Demand Response Mechanism for the market framing of
  `peak_shave` and `demand_charge_min`.
- NABERS and National Construction Code Section J for Australian building energy
  context in the archetype calibration.
- i-Hub DCH5 Milestone Report M7, Swinburne University of Technology, for the
  hardware, the deployed control strategies, and the measured outcomes.

Where a default is calibrated rather than derived, say so and name the source,
exactly as the Load Engine README does for its Australian defaults.

---

## 18. House style

- Match the surrounding code. Module docstrings that explain the pattern, type
  hints, dataclasses where the existing code uses them, `logger` rather than
  `print`, no bare excepts.
- Inline comments are single-sentence `#` lines. No block comment banners, no
  multi-sentence inline commentary.
- No em dashes in prose you write, use a comma instead.
- Documentation tables stay plain, no colour, no styling beyond existing
  markdown.
- Do not add dependencies beyond scipy for the EMS optimiser without asking me
  first.
- Do not reformat, reorder, or tidy files you are not otherwise changing.

---

## 19. Definition of done

- [ ] Both new containers build and run healthy under `docker compose up --build`
- [ ] A coordinated run with EMS and BMS enabled completes from the browser on an
      uploaded network
- [ ] A run with EMS and BMS disabled produces results identical to the
      pre-change baseline
- [ ] All previously existing tests pass unchanged, plus at least 60 new tests
- [ ] Every new capability is a registered plugin, visible in the UI System view
- [ ] The Swinburne hardware from section 4 exists only as named presets and
      template files, never as engine logic
- [ ] The precedence ladder is implemented, documented, and covered by an
      integration test
- [ ] All ten validation cases are recorded with numbers in
      `docs/EMS-BMS-VALIDATION.md`
- [ ] `docs/HARDWARE-MAPPING.md` accounts for every device in section 4.3, and
      states what is not modelled
- [ ] Root README, both service READMEs, and `WINDOWS.md` are updated and accurate
- [ ] No inter-container HTTP, no shared data volumes, no built-in network
      assumptions

Begin with Phase 0.
