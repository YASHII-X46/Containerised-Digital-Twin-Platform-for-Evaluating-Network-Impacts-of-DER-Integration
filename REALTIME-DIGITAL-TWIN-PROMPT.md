# Claude Code build prompt: turn Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration v5.0 into a real-time digital twin

**How to use this file.** Open a terminal at the repository root
(`.../Final Year Project/New-stack-5.0`), start Claude Code, and paste
everything from the `=== PROMPT STARTS HERE ===` line to the end of the file as
your first message. Work through it phase by phase and approve each gate.

Companion prompts: run `NETWORK-MODEL-BUILD-PROMPT.md` first so the twin has an
MV/LV model with zone-level detail. `EMS-BMS-DERMS-BUILD-PROMPT.md` is
independent of this work but composes with it, the control layers become
consumers of the twin's state and studies.

---

=== PROMPT STARTS HERE ===

You are working in the Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration v5.0 repository. Today it is an excellent
**scenario simulator**: a user uploads a network, picks controls, presses run,
and gets a batch result. Your task is to turn it into a **real-time digital
twin** of an MV/LV distribution network that runs continuously, stays
synchronised to measurements, and answers three questions at any moment:

1. What is the network doing right now?
2. What will it do over the next 24 hours?
3. What should be done about it?

The twin must support **24-hour QSTS power flow**, **optimal power flow**,
**distribution state estimation**, **contingency and N-1 analysis**, and
**harmonics and power quality**, executed on **OpenDSS or PSS SINCAL** behind
the existing solver contract, for studying the network impact of DER
penetration.

Do not start writing code yet. Follow the phase plan in section 16.

---

## 1. Read before you write

| Purpose | Files |
|---|---|
| Architecture, modularity story, honest scope limits | `README.md`, `WINDOWS.md` |
| The QSTS loop you are turning into a continuous process | `simulation-engine/app/simulation/qsts.py`, `app/simulation/results.py`, `app/simulation/loader.py` |
| The solver bus contract you will extend | `simulation-engine/app/solvers/registry.py`, `client.py`, `opendss-solver/dss_solver/service.py`, `engine.py`, `elements.py` |
| The SINCAL adapter, and how an optional dependency is handled honestly | `sincal-solver/README.md`, `sincal_solver/engine.py`, `service.py` |
| Coordination and control layers | `simulation-engine/app/control/remote_coordinator.py`, `envelopes.py`, `volt_var.py` |
| KPIs, tariffs, schemas | `app/metrics/kpi_registry.py`, `tariffs.py`, `app/models/schemas.py` |
| Bus contract and envelope format | `dr-controller/app/bus/participant.py`, `transport.py` |
| Reference NATS service to copy structurally | `dr-controller/`, `prosumer-shadow-twins/` |
| Profile generation that becomes the forecast source | `load-engine/app/profiles/` in full |
| UI wiring | `ui/server.js`, `ui/bus.js`, `ui/public/js/` in full |
| Deployment | `docker-compose.yml`, `docker-compose.windows.yml` |
| The network model the twin runs on | `sample-networks/README.md`, `MODEL-DESIGN.md` |

Write `docs/DIGITAL-TWIN-DESIGN.md` before coding. That is the phase 0
deliverable.

---

## 2. What "real digital twin" means here, precisely

Be honest about the maturity ladder and say in the documentation exactly which
rung this reaches.

| Rung | What it is | Status |
|---|---|---|
| Model | A validated network model solved on demand | The stack today |
| Shadow | The model driven continuously by real data, one way | What phases 1 to 3 deliver |
| Twin | The model reconciled with measurements, producing state, forecasts and recommended actions | What this whole build delivers |
| Autonomous | Actions dispatched back to plant without a human | **Explicitly out of scope**, and say so |

The defining difference between a simulator and a twin is **state estimation**:
a twin does not assume it knows the network state, it estimates it from
imperfect, sparse measurements and reports its own uncertainty. Everything else
here is machinery around that.

**Operating modes.** The twin ingests measurement frames from a pluggable
source and advances on a clock:

- `replay` (the default): measured or synthetic history replayed at wall-clock
  cadence, or accelerated by a speed factor, which is how every test and every
  study runs.
- `live`: the same code path fed by a real telemetry source. Build the seam,
  ship a source plugin interface, and do not pretend you have a live feed you
  cannot test.

---

## 3. Non-negotiable rules

These extend the stack's existing rules, they do not replace them.

1. **Bus only.** New services talk OpenFMB command and event messages over NATS
   with the existing envelope. No inter-container HTTP, no shared data volumes.
   The historian's database file is that service's private state, not an
   exchange medium.
2. **Registries, not engine edits.** Measurement sources, estimators, OPF
   formulations, contingency selectors, harmonic source models and alarm rules
   are all registries with listing commands, visible in the UI System view.
3. **Determinism survives real time.** All computation keys off a **logical
   timestamp** carried in the message, never off `datetime.now()` inside a
   solver or estimator. Wall-clock affects **pacing only**. A recorded session
   replayed at any speed factor must produce identical numbers, and there is a
   test that proves it.
4. **Backwards compatibility.** Every existing batch simulate request behaves
   exactly as today, and all 332 existing tests pass unchanged. Twin mode is
   additive.
5. **Capability negotiation, not assumption.** Solvers advertise what they can
   do. If a study is requested that the selected solver cannot perform, fail
   with a clear message naming the solver and the capability. Never silently
   substitute a different method.
6. **Convergence and observability discipline.** Results carry their
   convergence flag, estimates carry their observability status and residuals,
   and no KPI is computed over steps that failed either test.
7. **No new heavyweight dependencies.** numpy and scipy are already in the
   stack, use them. SQLite is standard library. Anything else, ask first.
8. **Honest limits.** Quasi-static and phasor domain only. No electromagnetic
   transients, no dynamic stability, no protection tripping simulation. Say so
   in every README that touches the twin.

---

## 4. Target architecture

Four new services, each following the `dr-controller` structural template
exactly (bus participant, readiness marker, healthcheck, loopback transport,
Dockerfile plus Dockerfile.windows, pytest.ini, README, tests).

```
telemetry-gateway ──measurement frames──▶ state-estimator ──estimated state──┐
        │                                        │                           │
        │                                        ▼                           ▼
        └────────────────────────────────▶  historian  ◀──── twin-orchestrator
                                                 ▲                  │
                                                 │                  │ study commands
                                          study results             ▼
                                                 └────────── simulation-engine
                                                                    │
                                                     solver bus contract
                                                                    ▼
                                                   opendss-solver / sincal-solver
```

- **`telemetry-gateway`** owns data ingestion: it reads from a registered
  measurement source, timestamps and validates frames, handles gaps and late or
  out-of-order samples, and publishes measurement frames on the bus at the
  configured cadence.
- **`state-estimator`** owns the present: it reconciles measurements with the
  network model and publishes the estimated state with its uncertainty and
  health.
- **`historian`** owns the past: a SQLite store of measurements, estimates,
  study results and alarms, with retention and a query interface over the bus.
- **`twin-orchestrator`** owns time and the study schedule: it holds the clock,
  advances the rolling window, and decides which study runs when, dispatching to
  the Simulation Engine.

The **Simulation Engine stays the study executor**, because it owns the solver
sessions and the network model. Do not move power flow anywhere else.

If at the phase 0 gate you believe the orchestrator belongs inside the
Simulation Engine rather than as its own service, argue the case. I would rather
change the design than have you build a shape you think is wrong.

---

## 5. Time, the rolling window, and the clock

- **Clock abstraction** in a shared module: `simulated` (steps as fast as it
  can, for tests and batch studies), `accelerated` (speed factor N against
  wall-clock), `wall_clock` (1:1). Every service takes its time from clock
  messages or from the logical timestamp on the frame it is processing, never
  from the system clock.
- **Cadence.** A configurable tick, default 1 minute, at which measurement
  frames arrive and the estimator runs. Study cadences are separate and
  configurable: look-ahead QSTS and OPF on a shorter cadence, contingency and
  harmonics on a longer one.
- **The rolling window** is 48 hours wide with a `now` pointer: the **past 24
  hours** of measurements and estimated states, and the **next 24 hours** of
  forecast and study results. Every tick advances the pointer, retires the
  oldest hour, and extends the forecast horizon so it always reaches 24 hours
  ahead.
- **Forecasts** come from the Load Engine's existing profile machinery,
  registered as forecast providers with `perfect`, `persistence` and `noisy`
  variants so forecast error can be studied honestly rather than assumed away.
- **Catch-up behaviour.** If a study overruns its cadence, define and document
  what happens: skip, queue, or degrade. Never let a backlog build silently, and
  publish an alarm when it happens.

---

## 6. Study 1: continuous 24-hour QSTS

The existing QSTS loop becomes a re-runnable look-ahead study rather than a
one-shot batch:

- Each look-ahead cadence, run a full 24-hour QSTS from the **estimated present
  state** as the initial condition, driven by forecast profiles, on the selected
  solver, producing the same result series and KPIs the stack already computes.
- Carry state across runs the way the stack already carries battery state of
  charge and state of health between days: the initial condition is the
  estimator's output, not a fresh assumption.
- Retain the previous run so the UI can show forecast revision, which is one of
  the more interesting things a twin can display: how last hour's 24-hour
  prediction differs from this hour's.
- Warm-start the solver session rather than rebuilding the circuit every run,
  and measure the saving.

---

## 7. Study 2: optimal power flow

### 7.1 An OPF registry

Mirror the solver registry pattern exactly, `register_opf(...)` with a listing
command:

| Formulation | Behaviour |
|---|---|
| `lindistflow_lp` (default) | Linearised DistFlow LP over the horizon, solved with `scipy.optimize.linprog` HiGHS. Deterministic, fast, the workhorse. |
| `ac_successive` | Successive linearisation: solve the LP, apply the setpoints to the real solver, take the AC solution, re-linearise around it, iterate to a documented convergence tolerance with an iteration cap. |
| `sincal_opf` | Optional backend using SINCAL's own optimisation module where the licence includes it. The Xplore edition may not, so probe for it, report it as an unsupported capability when absent, and fail cleanly exactly as the SINCAL solver does. |

### 7.2 What the OPF decides and respects

- **Decision variables**: DER active and reactive setpoints, curtailment, storage
  charge and discharge, transformer tap positions where an OLTC exists, and
  per-site export limits when the OPF is being used to compute operating
  envelopes.
- **Constraints**: bus voltage band, branch and transformer thermal ratings,
  inverter apparent power and reactive capability consistent with AS/NZS 4777.2,
  storage energy and power limits, tap range and discreteness (relaxed then
  rounded, with the rounding effect verified by an AC solve and reported).
- **Objectives**, each a registered entry: minimum curtailment, minimum losses,
  maximum DER hosting, minimum operating cost against the tariff registry, and
  voltage-deviation minimisation.

### 7.3 The verification rule

**An OPF result is not a result until a full AC power flow confirms it.** Every
OPF run ends by applying its setpoints to the real solver and reporting the
achieved voltages, loadings and losses alongside the predicted ones, plus the
error between them. If the linearisation error pushes a constraint into
violation, say so in the result rather than reporting the optimistic LP answer.
This single rule is what separates a credible OPF from a plausible one, and it
is also your best thesis figure.

---

## 8. Study 3: distribution state estimation

This is the heart of the twin. Give it the most care.

- **Measurement model** supporting the measurement types a distribution network
  actually has: bus voltage magnitude, branch active and reactive flow,
  injection at metered sites, transformer LV terminal quantities, and DER
  inverter output. Each measurement carries a variance reflecting its class,
  from a documented table.
- **Pseudo-measurements** from the Load Engine's profiles at every unmetered
  bus, with deliberately large variance, which is standard practice in
  distribution state estimation and is what makes a sparsely metered feeder
  observable at all. Document this clearly, it is an assumption a reader must be
  able to see.
- **Weighted least squares** solved with sparse linear algebra from scipy, with
  a documented convergence criterion and iteration cap. Support both a balanced
  positive-sequence formulation and a three-phase formulation, and say which the
  results came from.
- **Observability analysis** before solving: report unobservable islands rather
  than returning a confident wrong answer.
- **Bad data detection** by largest normalised residual, with a configurable
  threshold, identifying and flagging the suspect measurement and re-estimating
  without it, recording what it did.
- **Outputs**: per-bus voltage magnitude and angle with variance, estimated
  injections, the residual vector, chi-squared statistic, observability status
  and iteration count. All of it goes to the historian.
- **Estimator registry** so an alternative (for example a simple tracking
  estimator, or a linear WLS for speed) can be registered without touching the
  service.

---

## 9. Study 4: contingency and N-1

- **Outage set generation**, registered as selectors: every MV branch, every
  distribution transformer, a named user list, and an `n_minus_1` selector that
  enumerates single credible outages.
- **Two-stage screening**: rank outages using linear sensitivity factors
  computed from the base case, then run a full AC solve on the worst K, where K
  is configurable. Publish the K used and what was screened out, so nobody reads
  a partial sweep as exhaustive.
- **Switching state model**: branches gain an open or closed state, and the
  solver contract gains a switch operation. Islanding must be detected and
  reported as an unsolvable case, not as a convergence failure.
- **Results**: per-contingency worst voltage, worst thermal loading, unserved
  load if islanded, and a severity index with its formula documented. Rank and
  store.
- **Relevance to DER**: run the contingency set both with and without DER
  export, so the study answers whether DER penetration improves or worsens the
  post-contingency picture, which is a live question for distribution planners.

---

## 10. Study 5: harmonics and power quality

- **Solver capability.** OpenDSS has a harmonics mode, SINCAL has a harmonics
  module. Implement both behind one new solver-contract action, and have each
  solver advertise whether it supports it.
- **Harmonic source models** as a registry: inverter current spectra by class,
  with the source of each spectrum named, and an explicit statement that a
  spectrum is representative unless the user supplies measured data. Provide a
  CSV intake template for measured spectra in `sample-networks/real-data-templates/`.
- **Aggregation**: diversity between many small inverters matters, so implement
  a documented summation law rather than adding magnitudes arithmetically, and
  cite the basis.
- **Assessment against Australian limits**: voltage total harmonic distortion
  and individual harmonic orders assessed against the AS/NZS 61000.3.6
  allocation approach at MV and the relevant AS/NZS 61000 parts at LV, plus
  voltage unbalance. Report compliance, margin, and the limit applied, and state
  where an allocation requires a DNSP-specific value the model does not have.
- **Power quality KPIs**: `max_voltage_thd_pct`, `max_individual_harmonic_pct`,
  `harmonic_limit_margin_pct`, and the existing voltage unbalance KPI reused
  rather than duplicated.

---

## 11. Solver contract extensions

Extend the existing contract additively. Every current action keeps its exact
behaviour and payload.

| Action | Purpose |
|---|---|
| `capabilities` | Declare what this solver supports: `qsts`, `opf`, `harmonics`, `switching`, `sensitivity`, `three_phase`, plus `max_nodes` where the licence caps network size. The engine calls this once per session and refuses an oversized or unsupported study up front. |
| `set_switch` | Open or close a branch, for contingency and switching studies |
| `sensitivity` | Return voltage and loading sensitivity factors for the current operating point, used by OPF linearisation and contingency screening |
| `solve_harmonics` | Run a harmonic penetration solve with the supplied source spectra and return per-bus spectra |
| `snapshot` and `restore` | Save and restore solver state, so contingency screening does not need a rebuild per case |

Implement all of them in `opendss-solver`. Implement in `sincal-solver` what
SINCAL supports, declare the rest unsupported, and document precisely what a
future implementation would need. Keep both solvers dumb: no control logic, no
optimisation policy, solver-side.

---

## 12. Twin fidelity, the part most projects skip

A twin that never checks itself against reality is a simulator with extra steps.

- **Fidelity KPIs**, computed continuously and stored: voltage RMSE between
  estimated and measured buses, mean absolute error on metered branch flows,
  estimator chi-squared, share of measurements flagged bad, and forecast error
  at 1, 4 and 24 hours ahead.
- **Drift detection**: a rolling comparison that raises an alarm when model
  error exceeds a configurable threshold for a sustained period, which is how a
  real twin notices that the network changed and the model did not.
- **Calibration**, off by default and clearly documented: estimate a small,
  named set of model parameters (transformer tap position, a line impedance
  scaling factor per feeder section) from accumulated residuals. State plainly
  that calibration can mask a real network change, and require it to record
  every parameter it moved and by how much.
- **A twin health panel** in the UI, so a user can see at a glance whether to
  believe the twin: data freshness, estimator status, solver latency, last
  successful study per type, and current fidelity KPIs.

---

## 13. Historian

- SQLite in a dedicated `historian` service, schema versioned and migrated
  forward, with tables for measurement frames, estimated states, study results,
  fidelity metrics and alarms.
- Written through the bus, queried through the bus. Define the query commands
  narrowly (time range, series, aggregation), do not build a general query
  language.
- **Retention policy**, configurable: full resolution for a configurable recent
  window, then downsampled aggregates, then discard. Publish what was
  downsampled so a reader knows why old data looks smoother.
- **Export**: a command that writes a CSV or Parquet extract for offline
  analysis and thesis plots, into the service's own output directory, delivered
  the way the stack already delivers outputs.
- Restart safety: the twin resumes from the historian after a restart rather
  than starting blind, and the resume path is tested.

---

## 14. UI

The control panel gains a twin mode without losing the study mode it already
has.

- **Live view**: the topology map coloured by estimated voltage, updating each
  tick, with measured buses marked distinctly from estimated ones, because that
  distinction is the honest core of the display.
- **Now timeline**: a 48-hour ribbon with the `now` pointer, measured history
  behind it, forecast ahead of it, and the previous forecast overlaid so
  revision is visible.
- **Study panels**: OPF setpoints with predicted versus achieved values,
  contingency ranking table, harmonic spectra and limit assessment, and the
  rolling 24-hour QSTS result.
- **Alarms**: current and recent, filterable, each linking to the bus or branch
  it concerns.
- **Twin health panel** as described in section 12.
- **Historian queries** driving the charts, with the retention state visible.
- Live updates reach the browser over SSE or WebSocket from `ui/server.js`,
  which subscribes to the bus. Keep the existing request and response flows
  intact for batch studies.

---

## 15. Performance and its budget

Real-time means a deadline. Set one, measure it, publish it.

- Define a target per study at a 1-minute cadence on the campus MV/LV model, for
  example: state estimation under a few seconds, look-ahead 24-hour QSTS well
  inside its own cadence, OPF inside the look-ahead budget, contingency
  screening on a longer cadence, harmonics longer still.
- Instrument every study with its wall-clock duration, publish it as a metric,
  store it, and show it on the twin health panel.
- Optimise honestly: warm-started solver sessions, snapshot and restore for
  contingencies, sparse linear algebra in the estimator, batched element updates
  as the QSTS loop already does. Report before-and-after numbers.
- **State the SINCAL reality**: its COM automation is comparatively slow and it
  is Windows-only, so it is a validation and cross-check backend rather than the
  fast inner loop. Say this in the README rather than letting a user discover it
  by watching a cadence slip.
- **The available SINCAL licence is the Xplore edition, capped at about 50
  nodes.** The campus and residential models are built to fit inside that cap,
  so the same network runs in both engines, but the twin must still enforce it:
  `capabilities` reports the node budget, and the orchestrator refuses a study
  on an oversized network up front with a clear message rather than failing
  inside the solver. Which analysis modules the licence actually dispatches over
  COM, in particular optimal power flow, harmonics and time series, must be
  measured and recorded rather than assumed, and any that are unavailable are
  reported as unsupported capabilities.

---

## 16. Phase plan and gates

Stop at every gate and wait for my approval.

**Phase 0, design.** Write `docs/DIGITAL-TWIN-DESIGN.md`: the maturity rung
claimed, the service split and any disagreement with section 4, the clock and
window model, message schemas for measurements, estimates, studies and alarms,
the historian schema, the solver capability list, and the performance budget.
Gate: I approve the design.

**Phase 1, time and telemetry.** The clock abstraction, `telemetry-gateway`
with the measurement source registry (`csv_replay` and `synthetic` built in),
frame validation and gap handling, and the `historian` with its schema,
retention and query commands. Gate: a replay session streams frames at
wall-clock and accelerated speed, and the historian answers a range query.

**Phase 2, state estimation.** `state-estimator` with WLS, pseudo-measurements,
observability, bad data detection and the estimator registry. Gate: on a
noiseless synthetic case the estimate matches the true solved state to
tolerance, an injected bad measurement is identified, and an unobservable island
is reported rather than guessed.

**Phase 3, orchestration and continuous QSTS.** `twin-orchestrator` with the
rolling window and study scheduling, the Simulation Engine's twin-mode commands,
warm-started sessions, and continuous 24-hour look-ahead QSTS from the estimated
state. Gate: the twin runs unattended for a simulated 24 hours and the window
advances correctly across a day boundary.

**Phase 4, OPF.** The OPF registry, the three formulations, the objective set
and the AC verification rule. Gate: on a small case the LP result matches a
brute-force optimum, and every reported OPF result carries its AC verification.

**Phase 5, contingency and harmonics.** Solver contract extensions in both
solvers, capability negotiation, the contingency selectors and two-stage
screening, the harmonic source registry and the AS/NZS 61000 assessment. Gate: an
N-1 sweep ranks sensibly with the screening disclosed, and a harmonic study
produces a spectrum and a limit assessment on the campus model.

**Phase 6, fidelity and alarms.** Fidelity KPIs, drift detection, the optional
calibration routine, and the alarm registry. Gate: a deliberately detuned model
raises a drift alarm, and calibration recovers it while recording what it moved.

**Phase 7, UI.** Live view, timeline, study panels, alarms, twin health,
historian-driven charts. Gate: I can watch a replay day unfold in the browser
and tell you what the network is doing.

**Phase 8, deployment, performance and documentation.** Compose files for both
container modes, the performance report against the budget, all service READMEs,
the root README, and the validation document. Gate: `docker compose up --build`
brings the twin healthy and it runs a full replay day end to end.

At each gate report what changed, test counts before and after, measured
timings, what you compromised on, and anything in the existing code you found
wrong or fragile.

---

## 17. Testing

Target at least 70 new tests, no regressions.

- **Clock and replay**: each mode advances correctly, a recorded session
  replayed at different speed factors produces identical results, out-of-order
  and late frames are handled as documented, a gap raises the right alarm.
- **Telemetry**: each source plugin, frame validation, bad values rejected,
  registry listing.
- **Historian**: write and query round trip, retention downsampling, export,
  schema migration, resume after restart.
- **State estimation**: noiseless recovery of a known solved state, noisy
  recovery within tolerance, bad data identified and excluded, unobservable
  island reported, chi-squared behaves as expected, three-phase and
  positive-sequence formulations both exercised.
- **OPF**: LP optimum matches brute force on a small case, every constraint type
  binds correctly when it should, AC verification catches a deliberately
  over-optimistic linearisation, tap rounding effect reported, each objective
  produces the qualitatively right answer, `sincal_opf` fails cleanly with no
  licence.
- **Contingency**: screening ranking matches full AC ranking on a small case,
  islanding detected and reported distinctly from non-convergence, switch
  operations round trip through the solver contract, the disclosed K matches
  what was actually run.
- **Harmonics**: a hand-computable single-source case matches, the summation law
  behaves as documented, limit assessment flags a deliberate exceedance,
  unsupported-solver path fails with a clear message.
- **Capability negotiation**: requesting an unsupported study on a given solver
  produces the named error and no partial result.
- **Fidelity**: drift alarm on a detuned model, calibration records its changes,
  forecast error metrics computed correctly.
- **Regression**: every existing batch simulate request returns identical
  results, and all 332 existing tests pass unchanged.

Use the loopback bus in-process pattern so the real services are exercised
rather than mocked, as the existing suite already does.

---

## 18. Documentation

- A README per new service in the existing style: environment variables with
  the basis of each default, bus contract table, registry table, worked message
  examples, and an honest scope-limits paragraph.
- Root `README.md`: architecture diagram with the twin services, the service
  table, new registries in the modularity table, new KPIs, new environment
  variables, and the verification counts.
- `docs/DIGITAL-TWIN-DESIGN.md`, including the maturity-rung statement.
- `docs/STUDY-METHODS.md`: the mathematical formulation of the state estimator,
  the OPF, the contingency screening and the harmonic aggregation, each with its
  assumptions, its references, and its limitations. This is the document an
  examiner will interrogate hardest, write it as if it will be marked.
- `docs/TWIN-PERFORMANCE.md`: the budget, the measured timings, and what
  dominates each study.
- `docs/TWIN-VALIDATION.md`: the section 19 cases with expected and produced
  numbers.

---

## 19. Validation cases to demonstrate

1. **Estimator correctness.** Noiseless measurements from a known solved case
   recover the true state to numerical tolerance, and adding measurement noise
   degrades it in proportion to the assumed variances.
2. **Sparse metering.** Progressively remove real measurements and show how
   estimate uncertainty grows and where observability is lost, which is the
   practical question a DNSP asks before instrumenting a feeder.
3. **Bad data.** Inject a plausible but wrong measurement and show it detected,
   flagged and excluded, with the estimate recovering.
4. **OPF versus rule-based control.** The same 24-hour DER penetration scenario
   under autonomous Volt-Watt only, and under OPF-computed setpoints, comparing
   curtailed energy, losses and voltage violations.
5. **OPF fidelity.** Predicted versus AC-verified voltages across a day,
   reporting the linearisation error distribution and any case where the
   optimistic answer would have violated a limit.
6. **DER penetration sweep in twin mode.** Increasing DER penetration across the
   MV/LV model, reporting hosting capacity, voltage rise, reverse power hours,
   thermal utilisation and losses, with the results traceable to specific hours
   of the replayed day rather than to an abstract peak case.
7. **N-1 with and without DER.** The same contingency set run both ways,
   answering whether DER export helps or hurts the post-contingency picture.
8. **Harmonic penetration.** Increasing inverter count against the AS/NZS 61000
   assessment, reporting where the allocation is exceeded and by how much.
9. **Forecast revision.** How the 24-hour look-ahead changes hour by hour across
   a replayed day, and the resulting forecast error at 1, 4 and 24 hours.
10. **Drift and recovery.** Detune a model parameter, show the drift alarm, run
    calibration, show recovery and the recorded parameter change.
11. **Solver cross-check.** The same day solved on OpenDSS and on SINCAL on the
    identical network, comparing voltages, losses and timings. Because the model
    is built inside the licence cap there is no reduction step, so any difference
    is a solver or adapter difference and nothing else, which is what makes this
    a real cross-validation.
12. **Determinism.** A recorded replay day run at three different speed factors
    produces byte-identical results.

---

## 20. Standards to cite in code and documentation

- **AS/NZS 61000 series** for harmonic allocation and assessment, voltage
  fluctuation and unbalance, with the specific part named next to each limit.
- **AS 60038** standard voltages, for the nominal and the tolerance band the
  violation KPIs test against.
- **AS/NZS 4777.2** inverter responses and reactive capability, which bound the
  OPF's reactive decision variables.
- **CSIP-Aus** for operating envelopes where the OPF is used to compute them.
- **AS/NZS 3000** and **AS 5033** for the installation context.
- **IEC 61970 and CGMES** for the model exchange the twin consumes.
- **IEC 60076-7** transformer hot-spot ageing, already used by the stack.
- DNSP planning criteria where an allocation or limit requires a
  utility-specific value: state that the value is not public and show what was
  assumed instead.

---

## 21. House style

- Match the surrounding code: module docstrings that explain the pattern, type
  hints, dataclasses where the existing code uses them, `logger` not `print`, no
  bare excepts.
- Inline comments are single-sentence `#` lines. No block comment banners.
- No em dashes in prose you write, use a comma instead.
- Documentation tables stay plain, no colour, no styling beyond existing
  markdown.
- Numerical code carries its units in the name or the docstring, every time.
- Do not reformat files you are not otherwise changing.

---

## 22. Definition of done

- [ ] Four new containers build and run healthy under `docker compose up --build`
- [ ] The twin runs a full replayed 24-hour day unattended, at wall-clock and
      accelerated speed, and the window advances correctly across the day
      boundary
- [ ] State estimation recovers a known state, detects bad data, and reports
      unobservability rather than guessing
- [ ] Every OPF result is AC-verified, with predicted versus achieved reported
- [ ] Contingency screening discloses what it screened out, and islanding is
      distinguished from non-convergence
- [ ] Harmonic studies produce spectra and an AS/NZS 61000 assessment with the
      limit and its basis stated
- [ ] Solver capabilities are negotiated, including the licence node cap, and an
      unsupported or oversized study fails with a named error and no partial
      result
- [ ] The SINCAL Xplore node cap and available modules are measured, recorded,
      and enforced through `capabilities`
- [ ] Fidelity KPIs are computed continuously, drift raises an alarm, and
      calibration records every parameter it moves
- [ ] The historian survives a restart and the twin resumes from it
- [ ] A recorded replay produces byte-identical results at any speed factor
- [ ] Every existing batch simulate request is unchanged, all 332 existing tests
      pass, plus at least 70 new ones
- [ ] The performance budget is published with measured timings against it
- [ ] `docs/STUDY-METHODS.md` states every formulation, assumption and limitation
- [ ] The maturity rung claimed is stated plainly, and autonomous dispatch is
      declared out of scope

Begin with Phase 0.
