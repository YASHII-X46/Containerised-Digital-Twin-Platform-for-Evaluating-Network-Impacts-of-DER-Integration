# Real-time digital twin, design

Phase 0 deliverable for `REALTIME-DIGITAL-TWIN-PROMPT.md`. No implementation
code has been written. This document is the artefact for the phase 0 gate, and
it records where the brief is followed, where it is refined, and what must be
resolved before phase 1 can start.

---

## 1. Maturity rung claimed

| Rung | Meaning | Where this build lands |
|---|---|---|
| Model | A validated network model solved on demand | Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration v5.0 today |
| Shadow | The model driven continuously by real data, one way | End of phase 3 |
| Twin | The model reconciled with measurements, producing state, forecasts and recommended actions | End of phase 6, the claim this build makes |
| Autonomous | Actions dispatched to plant without a human | Explicitly out of scope, no dispatch path is built |

The claim is the **Twin** rung and nothing beyond it. The stack will estimate
state from imperfect measurements, report its own uncertainty, forecast, and
recommend setpoints. It will never write a setpoint to real plant, and no code
path in this build is capable of doing so.

The feature that separates this from the current simulator is state estimation.
Everything else in the build is machinery around that.

## 2. Honest limits, repeated in every twin README

- Quasi-static and phasor domain only. No electromagnetic transients, no
  dynamic or transient stability, no protection operation or fault studies.
- Harmonics are a frequency-domain penetration study at harmonic orders, not a
  time-domain distortion simulation.
- State estimation on a distribution feeder with sparse metering is dominated by
  pseudo-measurements derived from load profiles. The estimate is only as good
  as those profiles in unmetered areas, and the design surfaces that rather than
  hiding it.
- `replay` mode is the tested path. `live` mode is a source plugin seam. No live
  telemetry feed exists to test against, and nothing will claim otherwise.

---

## 3. Prerequisite status, blocking for phase 1 and later

Both companion briefs require `NETWORK-MODEL-BUILD-PROMPT.md` to run first. It
has not been run, and its outputs are absent.

| Artefact | Required for | Status |
|---|---|---|
| `sample-networks/MODEL-DESIGN.md` | Both briefs, section 1 reading list | Missing |
| `sample-networks/swinburne_hawthorn_v5.0_scenario.json` | Zone, site and DER records | Missing |
| `swinburne_hawthorn_v5.0` in CIM, RAW and SINCAL form | The network the twin runs on | Missing, only v3.0 exists |
| `../Report_DCH5_Final-Project.pdf` | Site and equipment data source | Present, 60 pages, read |
| `sample-networks/real-data-templates/` | Measured data intake | Present |

Phase 0 does not depend on these, which is why this document could be written.
Phases 1 and later do. The state estimator needs zone-level buses and a metering
map, and the validation cases are written against the campus MV/LV model.

Recommended order is in section 15.

---

## 4. Service split

The brief's four-service split is accepted, with one structural addition and one
change to an assumption the brief makes.

```
telemetry-gateway ──frames──▶ state-estimator ──estimates──┐
        │                            │                     │
        └──────────────▶  historian  ◀──────────  twin-orchestrator
                              ▲                            │
                              │  results          study commands
                              └──────────  simulation-engine
                                                  │  solver bus contract
                                                  ▼
                                    opendss-solver / sincal-solver
```

Each new service copies the `dr-controller` template exactly: `app/bus/`,
`app/config.py`, `app/main.py`, a readiness marker honouring `READY_FILE`,
loopback transport for tests, a `Dockerfile`,
`.dockerignore`, `pytest.ini`, `README.md` and `tests/`.

### 4.1 The orchestrator stays a separate service

The brief invites an argument for folding it into the Simulation Engine. I argue
for keeping it separate, on three grounds.

1. **Lifecycle mismatch.** The Simulation Engine is request and response. Every
   entry point, HTTP route and bus command, is a function that returns. The
   orchestrator is a long-lived loop owning a clock and a schedule. A scheduler
   thread inside a FastAPI service means the engine can no longer be reasoned
   about, or tested, as a pure executor.
2. **Failure isolation.** If the schedule wedges or a study overruns, the engine
   must still answer batch simulate requests. Rule 4 of the brief requires
   exactly that, and process separation is the cheapest way to guarantee it.
3. **Testability.** A separate orchestrator can be driven by a fake clock over
   the loopback bus with no FastAPI app in the picture at all.

The cost is one more container and one more bus hop per study. At the study
costs measured in section 12 that hop is negligible.

### 4.2 The Simulation Engine needs a persistent twin session

This is the main gap between the brief's picture and the current code. Today
`run_simulation` builds a `RemoteSolverEngine`, runs, and calls `engine.close()`
in a `finally` block. Every study would therefore rebuild the circuit, and the
brief's warm-start requirement in section 6 would be unimplementable.

Add a twin session concept to the Simulation Engine:

| Command | Purpose |
|---|---|
| `twin-open` | Build a solver session for a network and solve mode, hold it, return `twin_session_id` |
| `twin-study` | Run a named study against the held session |
| `twin-close` | Release the session |

Batch `simulate` is untouched and keeps its build, run, close lifecycle. This is
what makes warm-starting real, and it is where the saving in section 12 comes
from.

### 4.3 The solver must become multi-session, a change to the brief

The brief assumes the existing solver contract absorbs the twin's study load. It
cannot as written. `opendss-solver` holds exactly one session, and a new `build`
evicts the previous one, because the service was written on the assumption that
OpenDSS is a process-wide singleton.

That assumption is out of date. Verified in this repository's environment:

```
opendssdirect 0.9.4, dss-python 0.15.7, dss.NewContext() is available
```

`NewContext()` returns an independent instance with its own circuit, so
concurrent sessions in one container are possible.

Without this change the twin serialises, and worse, it corrupts: continuous
look-ahead QSTS, OPF verification, contingency screening and harmonics would
each evict the others' circuits. The eviction is silent to the caller until the
next command fails with `SessionError`. That is a correctness problem, not only
a performance one.

Proposed change, additive and backwards compatible:

- `OpenDSSSolverService` holds a map of `session_id` to context and engine,
  bounded by `DSS_MAX_SESSIONS` with least-recently-used eviction and an
  explicit event published whenever a session is evicted.
- Single-session behaviour is unchanged for existing callers.
- `snapshot` and `restore` from the brief's section 11 are implemented per
  session on top of contexts.

The 22 existing solver tests must pass unchanged, plus new tests proving two
sessions do not contaminate each other.

### 4.4 Where the state estimator gets its network model

The estimator needs bus and branch data and a nodal admittance matrix. It must
not re-implement the importers or hold a second network model.

The estimator requests the model from the Simulation Engine over the bus using
the existing `get-network` command, and builds its admittance matrix from that
dict with scipy sparse routines. The model is fetched once per session and
cached against the network id, and the network id travels on every estimate so a
stale estimate can never be attributed to the wrong model.

---

## 5. Clock, cadence and the rolling window

### 5.1 Clock

A `twin_clock` module, vendored into each service the way `app/bus/` is vendored
today, since the stack has no shared package and the briefs do not introduce
one.

| Mode | Behaviour | Use |
|---|---|---|
| `simulated` | Advances as fast as the work completes | Tests, batch studies |
| `accelerated` | Speed factor N against wall clock | Demonstrations, thesis figures |
| `wall_clock` | One tick per real tick interval | Live operation |

The orchestrator owns the clock and publishes `clock/tick` events. Every other
service takes time from the logical timestamp on the message it is processing.
No estimator, solver or study calls `datetime.now()` for anything that affects a
number. Wall clock is used for pacing and for latency metrics only, and latency
metrics are excluded from determinism assertions.

### 5.2 Cadences, all configurable

| Activity | Default | Rationale |
|---|---|---|
| Measurement frame and state estimation | 1 minute | The brief's tick, and a typical DNSP SCADA cadence |
| Look-ahead 24 hour QSTS | 15 minutes | Matches profile resolution, so forecasts land on step boundaries |
| OPF | 15 minutes | Consumes the same forecast slice as the look-ahead |
| Contingency and N-1 | 60 minutes | Expensive, and the credible outage set does not change minute to minute |
| Harmonics | 60 minutes | Expensive, and spectra follow inverter loading, which is slow |
| Fidelity KPI update | 1 minute | Cheap, and drives the health panel |

### 5.3 Rolling window

48 hours wide with a `now` pointer in the middle, 24 hours of measured and
estimated history behind it, 24 hours of forecast and study results ahead. Each
tick advances the pointer, retires data past the retention edge, and extends the
forecast so it always reaches 24 hours ahead.

### 5.4 Catch-up policy, chosen per study

| Study | Policy on overrun |
|---|---|
| State estimation | Skip and alarm. A stale estimate is worse than a missing one, and the next tick is one minute away |
| Look-ahead QSTS, OPF | Coalesce. At most one queued run, superseded by the newest request, because an old forecast has no value |
| Contingency, harmonics | Queue with depth one, then drop and alarm |

Every skip, coalesce and drop publishes an alarm and increments a counter shown
on the twin health panel. No backlog is silent.

---

## 6. Message schemas

All messages ride the existing envelope from `BusParticipant.envelope()`:
`messageId`, `correlationId`, `timestamp`, `status`, `payload`. Only `payload`
is described here. `logical_time` is the authoritative time for computation, ISO
8601 UTC. The envelope `timestamp` stays wall clock, for tracing only.

### 6.1 Measurement frame, published by `telemetry-gateway`

```json
{
  "frame_id": "f-000123",
  "logical_time": "2026-02-01T03:15:00Z",
  "network_id": "swinburne_hawthorn_v5_0",
  "source": "csv_replay",
  "quality": {"complete": true, "late_count": 0, "gap_filled": 0},
  "measurements": [
    {"m_id": "v_bus_012", "type": "v_mag_pu", "bus_id": 12,
     "value": 1.0142, "sigma": 0.002, "quality": "good"},
    {"m_id": "p_br_004", "type": "p_flow_kw", "branch_id": 4, "terminal": "from",
     "value": 231.4, "sigma": 4.6, "quality": "good"},
    {"m_id": "p_inj_018", "type": "p_inj_kw", "bus_id": 18,
     "value": -12.7, "sigma": 0.5, "quality": "suspect"}
  ]
}
```

Measurement types: `v_mag_pu`, `p_flow_kw`, `q_flow_kvar`, `p_inj_kw`,
`q_inj_kvar`, `i_mag_a`, `der_p_kw`, `der_q_kvar`. Quality is `good`, `suspect`,
`bad` or `missing`. Sigma is a standard deviation in the measurement's own unit,
taken from the class table that will live in `docs/STUDY-METHODS.md`.

### 6.2 Estimated state, published by `state-estimator`

```json
{
  "estimate_id": "e-000123",
  "logical_time": "2026-02-01T03:15:00Z",
  "network_id": "swinburne_hawthorn_v5_0",
  "formulation": "wls_positive_sequence",
  "status": "converged",
  "observability": {"observable": true, "unobservable_islands": []},
  "iterations": 4,
  "chi_squared": 12.7,
  "chi_squared_threshold": 21.0,
  "bad_data": [{"m_id": "p_inj_018", "normalised_residual": 4.8, "action": "excluded"}],
  "buses": {
    "12": {"v_mag_pu": 1.0138, "v_ang_deg": -1.42, "v_mag_sigma_pu": 0.0011, "measured": true},
    "13": {"v_mag_pu": 1.0102, "v_ang_deg": -1.55, "v_mag_sigma_pu": 0.0068, "measured": false}
  },
  "injections": {"12": {"p_kw": 88.1, "q_kvar": 30.4}},
  "residual_norm": 0.81
}
```

The per-bus `measured` flag is what the UI uses to distinguish measured buses
from inferred ones, which the brief calls the honest core of the display.

### 6.3 Study request, orchestrator to Simulation Engine

```json
{
  "twin_session_id": "ts-7f3a",
  "study": "lookahead_qsts",
  "logical_time": "2026-02-01T03:15:00Z",
  "horizon_hours": 24,
  "initial_state": {"estimate_id": "e-000123"},
  "forecast": {"provider": "persistence", "seed": 42},
  "solver": "opendss",
  "options": {}
}
```

The result carries the study name, the logical time, `solver`, `duration_ms`,
the existing `result_series` and `kpis` shapes unchanged, and a study-specific
block. Study names are `lookahead_qsts`, `opf`, `contingency`, `harmonics` and
`sensitivity`.

### 6.4 Alarm

```json
{
  "alarm_id": "a-000045",
  "logical_time": "2026-02-01T03:16:00Z",
  "rule": "estimator_not_converged",
  "severity": "major",
  "subject": {"kind": "service", "id": "state-estimator"},
  "message": "WLS hit the iteration cap of 20 without meeting tolerance 1e-4",
  "value": 20, "threshold": 20,
  "state": "raised"
}
```

Severities are `info`, `minor`, `major`, `critical`. States are `raised` and
`cleared`. Alarm rules are a registry, listed over the bus and shown in the UI
System view.

---

## 7. Historian schema

SQLite, one file, schema version in a `meta` table, forward-only migrations
applied at startup and logged.

| Table | Key columns | Purpose |
|---|---|---|
| `meta` | `key`, `value` | Schema version, created time, retention state |
| `measurement_frame` | `frame_id`, `logical_time`, `network_id`, `source`, `quality_json` | One row per frame |
| `measurement` | `frame_id`, `m_id`, `type`, `bus_id`, `branch_id`, `value`, `sigma`, `quality` | One row per measurement |
| `estimate` | `estimate_id`, `logical_time`, `status`, `chi_squared`, `iterations`, `observable`, `residual_norm` | Estimator header |
| `estimate_bus` | `estimate_id`, `bus_id`, `v_mag_pu`, `v_ang_deg`, `v_mag_sigma_pu`, `measured` | Per-bus estimate |
| `study_result` | `study_id`, `study`, `logical_time`, `solver`, `duration_ms`, `status`, `summary_json` | Study header and KPIs |
| `study_series` | `study_id`, `series`, `payload_json` | Result series, stored whole |
| `fidelity_metric` | `logical_time`, `metric`, `value` | Rolling fidelity KPIs |
| `alarm` | `alarm_id`, `logical_time`, `rule`, `severity`, `subject_json`, `state` | Alarm log |

Indexes on `logical_time` for every time series table, and on
`(study, logical_time)` for `study_result`.

**Retention**, three bands, configurable: full resolution for
`HISTORIAN_FULL_HOURS` (default 48), then 15 minute aggregates for
`HISTORIAN_AGG_DAYS` (default 30), then discard. Every downsample writes a row
recording what was aggregated and when, so a reader can see why old data looks
smoother, and the UI shows the retention state.

**Query commands**, deliberately narrow, no general query language:
`query-range` (series, time range, optional aggregation), `query-latest`,
`query-alarms`, and `export` writing CSV or Parquet into the service's own
output directory.

**Restart safety.** On startup the historian reports its latest logical time and
the orchestrator resumes the window from there rather than starting blind. This
path gets a test.

---

## 8. Solver contract extensions

Additive. Every existing action keeps its exact payload and behaviour.

| Action | Purpose | opendss-solver | sincal-solver |
|---|---|---|---|
| `capabilities` | Declare supported study kinds | Yes | Yes, reporting the honest subset |
| `set_switch` | Open or close a branch | Yes | Planned, needs the database writer |
| `sensitivity` | Voltage and loading sensitivity factors at the operating point | Yes | Not supported initially |
| `solve_harmonics` | Harmonic penetration solve with supplied spectra | Yes, OpenDSS harmonics mode | Not supported initially |
| `snapshot`, `restore` | Save and restore session state | Yes, per context | Not supported initially |

Capability vocabulary: `qsts`, `opf`, `harmonics`, `switching`, `sensitivity`,
`three_phase`, `multi_session`.

The engine calls `capabilities` once per twin session and caches the answer. A
study requested against a solver that does not advertise the capability fails
with a message naming the solver, the capability and the study, and returns no
partial result. This is the brief's rule 5, and it gets a test per solver.

**SINCAL reality.** The SINCAL adapter's network export and per-step writes
still raise `SincalUnavailable`, so SINCAL can execute no study today. Every
SINCAL capability above is aspirational until that work lands, and the adapter
will advertise an empty capability set rather than claim support it cannot
honour. Validation case 11, the solver cross-check, is blocked on that work and
on a licensed environment.

---

## 9. Study approaches in brief

Full mathematics, assumptions and references go into `docs/STUDY-METHODS.md` at
the phase where each study is built.

- **Look-ahead QSTS.** The existing `QSTSSimulation` loop, unchanged in physics,
  driven from the estimator's output as its initial condition and forecast
  profiles across the horizon, against a held twin session.
- **State estimation.** Weighted least squares by Gauss-Newton, sparse normal
  equations through `scipy.sparse.linalg`. Positive-sequence formulation first,
  three-phase second, both registered. Observability by numerical rank test on
  the gain matrix with island reporting. Bad data by largest normalised
  residual, exclude and re-estimate, recording every exclusion.
- **OPF.** LinDistFlow LP over the horizon through `scipy.optimize.linprog`
  HiGHS as the default, successive linearisation as the AC-aware variant, and
  every result AC-verified against the real solver before it is reported.
- **Contingency.** Linear screening from `sensitivity`, full AC on the worst K,
  with K and the screened-out set published. Islanding detected by graph
  connectivity before the solve and reported as its own outcome, never as a
  convergence failure.
- **Harmonics.** Per-order penetration solve with registered inverter current
  spectra, a documented summation law for diversity, and assessment against the
  AS/NZS 61000.3.6 allocation approach with the limit and its basis stated.

---

## 10. Fidelity, drift and calibration

Fidelity KPIs computed every tick and stored: voltage RMSE between estimated and
measured buses, mean absolute error on metered branch flows, estimator
chi-squared, share of measurements flagged bad, and forecast error at 1, 4 and
24 hours ahead measured against what was later observed.

Drift detection is a rolling comparison raising an alarm when a fidelity metric
exceeds its threshold for a sustained period, both configurable.

Calibration is off by default, estimates only a named small set of parameters
(transformer tap position, a per-section line impedance scale factor), and
records every parameter it moved with before and after values. The README states
plainly that calibration can mask a real network change.

---

## 11. UI

Additive. Twin mode does not remove study mode. Live topology coloured by
estimated voltage with measured buses marked distinctly, a 48 hour timeline with
the `now` pointer and the previous forecast overlaid, study panels for OPF
predicted against achieved, contingency ranking, harmonic spectra and the
rolling QSTS, an alarm list, and the twin health panel.

Transport to the browser is Server-Sent Events from `ui/server.js`, which
already holds a NATS subscription. SSE rather than WebSocket because the flow is
one way, it survives proxies, and it needs no new dependency. Existing request
and response flows are untouched.

---

## 12. Performance budget

Measured baseline, taken in this environment before any of this work, on the 33
bus IEEE test feeder over the in-process loopback bus:

| Quantity | Measured |
|---|---|
| Solver session build, 33 buses, PV, BESS and EV elements | 77 ms |
| Full 24 hour QSTS, 96 steps, two bus round trips per step | 64 ms, 0.7 ms per step |

The loopback transport excludes NATS serialisation and network latency, so the
containerised figure will be higher. The budget below leaves roughly two orders
of magnitude of headroom, and phase 8 must publish measured figures over real
NATS against these targets.

| Study | Cadence | Budget | Basis |
|---|---|---|---|
| State estimation | 1 min | 3 s | Sparse WLS at this size is milliseconds of linear algebra, the budget covers model fetch and bus overhead |
| Look-ahead 24 h QSTS | 15 min | 20 s | Three hundred times the measured loopback figure, generous for NATS |
| OPF, LP plus AC verification | 15 min | 45 s | One LP solve plus one verification QSTS |
| Contingency, screen plus K AC solves | 60 min | 120 s | K default 10, each an AC snapshot solve |
| Harmonics | 60 min | 120 s | Per-order solves across the spectrum |
| Frame ingest to estimate published | 1 min | 5 s end to end | The number that decides whether the live view feels live |

Every study publishes `duration_ms`, the historian stores it, and the health
panel shows it against these budgets.

---

## 13. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Network model prerequisite not run | Phases 1 and later have no zone-level model or metering map | Run the network brief first, see section 15 |
| Single-session solver | Silent session eviction between concurrent studies | Multi-session contexts, section 4.3, with concurrency tests |
| SINCAL adapter incomplete | No cross-check backend, validation case 11 blocked | Advertise empty capabilities and document it, do not fake support |
| Sparse metering on the campus feeder | Estimator dominated by pseudo-measurements | Validation case 2 measures exactly this, and the UI marks inferred buses |
| Seven new containers across both briefs | Windows images are multi-GB, build time and disk grow | Shared base layers, and a documented minimal compose profile |
| Determinism versus wall clock | A stray `datetime.now()` silently breaks replay | Review rule plus the speed-factor determinism test |

---

## 14. Test plan, target at least 70 new tests

Clock and replay 10, telemetry 10, historian 10, state estimation 15, OPF 12,
contingency 8, harmonics 8, capability negotiation 4, fidelity and alarms 8, and
solver multi-session 5. All use the loopback in-process pattern so the real
services are exercised rather than mocked, as the existing suite does. The 332
existing tests must pass unchanged, and a regression test asserts a batch
simulate request returns identical key metrics to a stored baseline.

---

## 15. Decisions needed before phase 1

1. **Sequencing.** I recommend running `NETWORK-MODEL-BUILD-PROMPT.md` first,
   then this twin build, then the control build. The twin's validation cases and
   the estimator's metering map both need the v5.0 campus model. Approve, or
   direct me to proceed against the v3.0 17 bus model and rework later.
2. **Multi-session solver.** Section 4.3 changes an assumption in the brief and
   touches an existing, passing service. Approve before I modify it.
3. **Twin session in the Simulation Engine.** Section 4.2 adds three commands to
   an existing service. Approve the names and the shape.
4. **Scope of this pass.** The two briefs together are seven new containers, at
   least 160 new tests and sixteen gated phases. I recommend taking the twin
   build to the end of phase 3, the shadow rung, before starting the control
   build, so there is a working continuous twin for DERMS to hang from.
