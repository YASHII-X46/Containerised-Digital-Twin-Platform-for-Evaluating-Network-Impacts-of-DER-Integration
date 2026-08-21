# Load Engine

The Load Engine is a FastAPI service that generates per-bus load, PV, BESS, and
EV time-series profiles for a selected distribution network.

The stack reaches this engine over the OpenFMB/NATS command bus only — every
operation (generation, health, metadata, custom load-profile management, and bus
assignment previews) is an OpenFMB command. The matching FastAPI HTTP routes are
kept for direct host-side debugging and are not used between containers.

## Inputs

Profile generation needs either:

- `network_buses`: a list of buses from the selected network, used for automatic DER assignment.
- `bus_data`: an explicit per-bus configuration, used as the source of truth.

The engine ships **no built-in network** — supply one of the above so it works
with any uploaded network model.

Each day spans `timesteps × resolution_minutes = 1440` minutes (one 24-hour
day). The `days` field repeats the day with day-to-day variation, so the total
horizon is `days × timesteps` points (see [Multi-day horizons](#multi-day-horizons)).

## Profile Pipeline

For each bus, per day:

1. Build a load day-shape from the assigned archetype (residential or
   commercial) or a custom profile, on the fixed season curves.
2. Apply household (or EV) diversity for aggregated bus load.
3. Scale real and reactive load to the bus base demand.
4. Generate PV output when PV is assigned (weather-adjusted: temperature
   derating, and measured irradiance when the source supplies it).
5. Generate EV charging demand when EV is assigned.
6. Dispatch BESS (self-consumption or time-of-use) against local net demand.
7. Return load, DER, and net-load time series.

Net load is aggregated from every installed DER plugin's signed contribution
(`+1` = consumer, `-1` = generation/discharge), so a newly registered DER type
feeds net load with no edits to the generator. With the built-in plugins this
reduces to:

```text
net_load_kw = load_kw - pv_kw + ev_charge_kw - bess_power_kw
```

Positive `bess_power_kw` means battery discharge. Negative `bess_power_kw`
means battery charging.

## Load Profiles

Customer-class archetypes are Australian-context: six residential classes
covering Australian housing stock, plus commercial classes for the office and
education buildings found on an urban feeder or university campus
(occupancy-driven, with season design-day HVAC).

| Category | Class | Building |
|----------|-------|----------|
| Residential | `res_detached_small` | Small detached home |
| Residential | `res_detached_medium` | Medium detached home |
| Residential | `res_detached_large` | Large detached home (pool) |
| Residential | `res_townhouse` | Townhouse |
| Residential | `res_apartment_lowrise` | Low-rise apartment |
| Residential | `res_apartment_highrise` | High-rise apartment |
| Commercial | `com_small_office` | Small office |
| Commercial | `com_medium_office` | Medium office |
| Commercial | `com_education` | University/TAFE teaching building (evening classes, weekend library-level activity) |

`GET /classes` lists every archetype plus custom profiles; `GET /archetypes`
returns archetype metadata (category and seasonal peaks). In auto-assignment
the engine rotates through residential classes; commercial classes are
selected explicitly via `bus_data`.

Custom profiles are uploaded through `POST /profiles/custom` with a `kind`:
`load` shapes become customer classes referenced as `custom:<name>`; `pv`
shapes (a measured PV day) are selected scenario-wide via `pv_profile`; `ev`
shapes (a per-charger day) via `ev_profile`. Values are normalized to per-unit
peak and stored in `CUSTOM_PROFILES_DIR`; the store rejects a shape used as the
wrong kind.

## Weather

Weather drives **PV generation only** (Australian-calibrated PV model): the
temperature trace sets the PV cell-temperature derating, and irradiance,
when supplied, drives PV output directly. Load shapes always use their fixed
season curves. Selected with `weather_source`:

| Source | Behaviour |
|--------|-----------|
| `none` | No temperature derating; season curves everywhere (default; fully offline) |
| `synthetic` | Offline diurnal temperature model that varies per day |
| `file` | Local CSV of hourly `temp_C[,ghi_Wm2]` rows via `WEATHER_FILE` — measured on-site data, fully reproducible |

When a source supplies irradiance (a two-column `file`), PV output is driven
directly by the measured sky — replacing the synthetic cloud model — with
temperature derating still applied. Temperature degrades to the synthetic
model on any provider failure; irradiance is best-effort (absent → the cloud
model applies).

`GET /weather-sources` lists the available sources. All sources are fully
offline, so generation never blocks on a network call. For multi-day runs
each day gets its own temperature (and, where available, irradiance) trace.

## DER Models

**PV** generation uses a season-aware clear-sky curve with autocorrelated
cloud attenuation — unless the weather source supplies measured irradiance,
in which case the measured sky drives output directly (see Weather).

**BESS** is dispatched in one of two modes (`bess_dispatch_mode`):

| Mode | Behaviour |
|------|-----------|
| `self_consumption` | Charge from excess PV, discharge during local deficit (default) |
| `time_of_use` | Charge within `bess_charge_window`, discharge within `bess_discharge_window` |

Batteries respect capacity, separate charge/discharge power limits, SOC, and
round-trip efficiency. Named configs (`GET /bess-configs`): `powerwall_2`,
`powerwall_3`, `byd_hvs`, `enphase_5p`, `generic_small`, `generic_medium`, and
`hybrid_asym_3_6` (an asymmetric inverter that charges at 3.3 kW and discharges
at 6.6 kW). Charge and discharge limits can be overridden per bus with
`bess_max_charge_kw` and `bess_max_discharge_kw`.

Battery **ageing** is tracked: state of health (SoH) falls with energy
throughput (≈80% SoH near 4000 equivalent full cycles) plus calendar fade
(≈2%/year). SoH carries across days and shrinks the usable capacity; each bus
summary reports `bess_soh` and `bess_cycles`, and the response aggregates
`mean_bess_soh` and `total_bess_cycles`.

**EV** charging supports:

| Mode | Behaviour |
|------|-----------|
| `uncontrolled` | Charge at full rate after plug-in |
| `offpeak` | Delay charging until `ev_offpeak_start_hour` |
| `smart` | Spread energy across the plugged-in window |

Named EV configs (`GET /ev-configs`): `level1_2kw`, `level2_7kw`, `level2_11kw`.

## Multi-day Horizons

Setting `days > 1` simulates consecutive days (up to 31). The per-day shape
repeats with day-to-day variation, and across days:

- **Calendar awareness** — weekday and weekend day-shapes differ (lower
  office/campus occupancy at weekends).
- **BESS continuity** — battery SOC and SoH carry from one day into the next.
- **Weather continuity** — each day pulls its own temperature/irradiance
  trace for the PV model.

The merged profiles span `days × timesteps` points; metadata records `days`.

## Drop-in Extensions (plugin auto-discovery)

DER plugins, archetypes, and weather providers can be added without editing or
rebuilding the package. At startup the engine imports:

- `DER_PLUGIN_MODULES` — a comma-separated list of importable module names.
- `DER_PLUGINS_DIR` — a directory whose `*.py` files are imported in order
  (`_`-prefixed files are skipped).

Each module simply calls the relevant `register()` (a DER plugin, an archetype,
or a weather provider) at import. Import errors are logged and skipped, so a
single bad plugin never blocks startup. Both are empty (off) by default.

## Calibration (Australia-wide)

Defaults are nationally representative — grounded in published Australian DNSP,
AER/AEMO and APVI data — rather than tuned to one network. Because magnitudes are
driven by the uploaded network's base loads plus per-request knobs, the engine is
tuned to any specific DNSP region by setting those knobs, not by code changes.

| Quantity | National default | Observed AU range | Knob to match a DNSP |
|----------|------------------|-------------------|----------------------|
| Household daily energy | ~15 kWh/day | ~12.6 (VIC) – ~23 (TAS/NT) | network `base_load_kw` + `admd_kw` |
| Residential ADMD (diversified) | 1.5 kW | ~1–2 (established) – ~3–5 (new all-electric) | `diversity.admd_kw` |
| Rooftop PV yield | ~4 kWh/kWp/day | ~3.6–5 (PR ~0.80) | `season` |
| EV daily top-up | ~12 kWh | ~8–12 (ACN-Data sessions) | `ev_config`, `ev_charging_mode` |

DNSP *design* maximum-demand allowances for new connections (~6–8 kVA/dwelling)
are higher than the measured diversified ADMD above; use the design figure in
`admd_kw` when sizing new estates. HVAC archetype shapes are temperate-AU
representative — for tropical/arid regions, select `season` or supply a
custom profile.

## Interfaces (NATS + HTTP)

Each operation is an OpenFMB command (`openfmb/command/load-engine/<action>` →
`openfmb/event/load-engine/<action>`) — the path the stack uses — with a matching
FastAPI HTTP route kept for host-side debugging:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Service health |
| GET | `/config` | Non-sensitive configuration |
| GET | `/archetypes` | Archetype metadata (category, DOE equivalent) |
| GET | `/classes` | Built-in archetypes and custom customer classes |
| GET | `/der-types` | Registered DER generation plugins |
| GET | `/bess-configs` | BESS configuration names and values |
| GET | `/ev-configs` | EV charger configuration names and values |
| GET | `/weather-sources` | Available temperature/weather sources |
| GET | `/profiles/custom` | List uploaded custom profiles |
| POST | `/profiles/custom` | Upload a custom profile |
| DELETE | `/profiles/custom/{name}` | Delete a custom profile |
| POST | `/bus-data/preview` | Preview automatic bus assignment |

## Bus Command

The `load-engine/generate` NATS command returns the profile summary plus the
full profiles payload consumed by the Simulation Engine.

Important command fields:

| Field | Default | Purpose |
|-------|---------|---------|
| `scenario_name` | `scenario` | Scenario identifier |
| `seed` | `42` | Random seed |
| `network_id` | _(empty)_ | Network id recorded in metadata; set by the UI to the selected network |
| `network_buses` | none | Bus list for automatic assignment |
| `bus_data` | none | Explicit per-bus configuration |
| `der_penetration_percent` | `100` | Total PV capacity as percent of total base load |
| `timesteps` | `96` | Number of points in a day |
| `resolution_minutes` | `15` | Minutes per timestep |
| `days` | `1` | Number of consecutive days (1–31) |
| `pv_buses` | all load buses | Buses that receive PV in automatic assignment |
| `bess_penetration` | `0.3` | Fraction of PV buses with BESS |
| `bess_config` | `powerwall_2` | BESS configuration name |
| `bess_dispatch_mode` | `self_consumption` | `self_consumption` or `time_of_use` |
| `bess_charge_window` | `[1, 6]` | Time-of-use charge window `[start_hour, end_hour]` |
| `bess_discharge_window` | `[17, 21]` | Time-of-use discharge window `[start_hour, end_hour]` |
| `ev_penetration` | `0.2` | Fraction of load buses with EV charging |
| `ev_config` | `level2_7kw` | EV charger configuration name |
| `ev_charging_mode` | `uncontrolled` | EV charging mode |
| `ev_offpeak_start_hour` | `23.0` | Start hour for `offpeak` mode |
| `pv_profile` | none | Custom PV day-shape name (kind `pv`) replacing the PV model |
| `ev_profile` | none | Custom per-charger EV day-shape name (kind `ev`) replacing the session model |
| `season` | `summer` | Load and PV seasonal setting |
| `weather_source` | `none` | `none`, `synthetic`, or `file` |
| `reactive_floor` | `0.0` | Constant reactive-load fraction |
| `diversity` | enabled | Household load-diversity settings |
| `ev_diversity` | enabled | EV arrival-diversity settings |

## Per-Bus Configuration

`bus_data` entries use:

```json
{
  "bus_id": 2,
  "base_load_kw": 150.0,
  "base_load_kvar": 60.0,
  "customer_class": "res_detached_medium",
  "pv_capacity_kw": 50.0,
  "bess_config": "powerwall_2",
  "bess_capacity_kwh": 13.5,
  "bess_max_charge_kw": 5.0,
  "bess_max_discharge_kw": 5.0,
  "ev_config": "level2_7kw",
  "ev_charge_rate_kw": 7.0
}
```

`bess_max_charge_kw`/`bess_max_discharge_kw` of 0 fall back to the named config's
rates. `customer_class` is any archetype name or `custom:<name>`.

## Bus Payload

The generated profiles payload has:

```json
{
  "metadata": {
    "scenario_name": "scenario",
    "seed": 42,
    "der_penetration_percent": 100,
    "total_buses": 33,
    "timesteps": 96,
    "resolution_minutes": 15,
    "days": 1
  },
  "buses": {
    "2": {
      "customer_class": "res_detached_medium",
      "base_load_kw": 150.0,
      "base_load_kvar": 60.0,
      "pv_capacity_kw": 50.0,
      "bess_capacity_kwh": 13.5,
      "bess_soh": 1.0,
      "bess_cycles": 0.0,
      "ev_charge_rate_kw": 7.0,
      "timeseries": []
    }
  }
}
```

The Simulation Engine accepts this payload inline through its `profiles` field.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUS_PREFIX` | `openfmb` | OpenFMB command/event topic prefix |
| `BUS_ENABLED` | `true` | Start the bus participant on boot |
| `BUS_TRANSPORT` | `nats` | `nats` or `loopback` |
| `NATS_URL` | `nats://localhost:4222` | NATS broker URL |
| `DEFAULT_SEED` | `42` | Default random seed |
| `CUSTOM_PROFILES_DIR` | `outputs/custom_profiles` | Custom profile directory |
| `DER_PLUGIN_MODULES` | _(empty)_ | Comma-separated extension modules to import at startup |
| `DER_PLUGINS_DIR` | _(empty)_ | Directory of `*.py` extension files to import at startup |
| `WEATHER_FILE` | _(empty)_ | CSV of hourly `temp_C[,ghi_Wm2]` rows for the `file` weather source |

## Local Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

The full stack normally starts this service through `docker compose up --build`.
