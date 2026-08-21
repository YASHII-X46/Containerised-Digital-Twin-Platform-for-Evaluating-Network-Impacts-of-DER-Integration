"""Dynamic operating envelopes (DOEs) — per-site, per-interval export limits.

Models the Australian DNSP practice (SA Power Networks Flexible Exports,
Energex/Ergon dynamic connections, Project EDGE / evolve) of replacing fixed
export limits with time-varying limits computed from actual network headroom
and published to each connection point (CSIP-Aus ``opModExpLimW``).

Three modes:

  - ``static``  — every export-capable site gets the same constant limit
                  (today's fixed export limit; the comparison baseline).
  - ``dynamic`` — limits computed per site and per interval from the network:
      * ``search``      — per interval, binary-search the uniform fraction of
        site capability the network can absorb before a voltage/thermal
        violation (exact, nonlinear; allocation is pro-rata by construction).
      * ``sensitivity`` — linearised: perturb each site once to build dV/dP and
        dLoading/dP sensitivities, then allocate the per-interval headroom
        under an allocation policy (the linearised-OPF approach of the trials).

Allocation policies (sensitivity method): ``equal`` (same kW per site),
``prorata`` (proportional to site capability), ``max_total`` (maximise total
export — LP via scipy when available, else a documented greedy heuristic that
is exact when a single constraint binds, as on most radial feeders).

The baseline state for headroom is the forecast WITHOUT controllable export
(building load + EV charging, no PV/BESS injection) — how envelope engines are
fed in practice. Sensitivities are taken in the worsening direction only
(exports that relieve a constraint earn no extra credit), which keeps the
linearisation conservative.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)

ALLOCATIONS = ("equal", "prorata", "max_total")
METHODS = ("sensitivity", "search")

_PERTURB_KW = 10.0        # injection step used to estimate sensitivities
_SEARCH_ITERATIONS = 8    # binary-search refinement steps per interval


def envelope_buses(profiles: dict) -> list[int]:
    """Export-capable connection points: buses with PV capacity."""
    return sorted(
        int(b) for b, d in profiles["buses"].items()
        if float(d.get("pv_capacity_kw", 0.0)) > 0.0
    )


def site_capability_kw(profiles: dict, bus_id: int) -> float:
    """A site's maximum plausible export (its PV nameplate)."""
    return float(profiles["buses"][bus_id].get("pv_capacity_kw", 0.0))


def _series(profiles: dict, bus_id: int, key: str, t: int) -> float:
    return float(profiles["buses"][bus_id]["timeseries"][t].get(key, 0.0))


def _apply_baseline(engine, network, profiles, t: int) -> None:
    """Drive the no-export forecast state for interval ``t``.

    Building load (plus any extra-DER contribution) and EV charging are served;
    PV and BESS injections are zero — the state an envelope engine allocates
    headroom on top of.
    """
    for bus_id, bus in profiles["buses"].items():
        if bus_id == network.source_bus:
            continue
        ts = bus["timeseries"][t]
        engine.update_load(
            bus_id,
            float(ts.get("load_kw", 0.0)) + float(ts.get("other_der_kw", 0.0)),
            float(ts.get("load_kvar", 0.0)),
        )
        if float(bus.get("pv_capacity_kw", 0.0)) > 0.0:
            engine.update_pv(bus_id, 0.0)
        if float(bus.get("bess_capacity_kwh", 0.0)) > 0.0:
            engine.update_bess(bus_id, 0.0)
        if float(bus.get("ev_charge_rate_kw", 0.0)) > 0.0:
            engine.update_ev(bus_id, float(ts.get("ev_charge_kw", 0.0)))


def _violates(engine, v_limit: float, thermal_limit: float) -> bool:
    voltages = engine.get_bus_voltages_pu()
    if max(voltages.values()) > v_limit:
        return True
    loadings = engine.get_branch_loadings_pct()
    return bool(loadings) and max(loadings.values()) > thermal_limit


def _sensitivities(engine, buses: list[int], v0: dict, l0: dict):
    """Per-site worsening-direction sensitivities from one perturbation each.

    Returns (S_v, S_l): rows are monitored buses / branches, columns are
    envelope sites; entries are max(d/dP, 0) per kW of export.
    """
    bus_ids = sorted(v0)
    branch_ids = sorted(l0)
    S_v = np.zeros((len(bus_ids), len(buses)))
    S_l = np.zeros((len(branch_ids), len(buses)))
    for col, site in enumerate(buses):
        engine.update_pv(site, _PERTURB_KW)
        engine.solve()
        v1 = engine.get_bus_voltages_pu()
        l1 = engine.get_branch_loadings_pct()
        engine.update_pv(site, 0.0)
        for row, b in enumerate(bus_ids):
            S_v[row, col] = max(0.0, (v1[b] - v0[b]) / _PERTURB_KW)
        for row, br in enumerate(branch_ids):
            S_l[row, col] = max(0.0, (l1.get(br, 0.0) - l0.get(br, 0.0)) / _PERTURB_KW)
    engine.solve()   # restore the unperturbed baseline solution
    return bus_ids, branch_ids, S_v, S_l


# ---------------------------------------------------------------------------
# Allocation-policy registry: how per-interval headroom is shared between
# sites. A policy is ``(S, headroom, caps) -> limits`` where ``S`` stacks every
# constraint row (voltage then thermal), ``headroom`` is the matching slack,
# and ``caps`` bounds each site at its capability. Register a policy to make
# it selectable by name from the simulate request — no engine edits.
# ---------------------------------------------------------------------------

_ALLOCATION_REGISTRY: dict[str, dict] = {}

_TINY = 1e-12


def register_allocation(name: str, fn, description: str = "") -> None:
    _ALLOCATION_REGISTRY[name] = {"fn": fn, "description": description}


def available_allocations() -> list[dict]:
    return [
        {"name": n, "description": _ALLOCATION_REGISTRY[n]["description"]}
        for n in sorted(_ALLOCATION_REGISTRY)
    ]


def _allocate_equal(S: np.ndarray, headroom: np.ndarray, caps: np.ndarray) -> np.ndarray:
    """One common per-site kW: the binding constraint sets it."""
    row_sums = S.sum(axis=1)
    with np.errstate(divide="ignore"):
        per_site = np.min(np.where(row_sums > _TINY, headroom / row_sums, np.inf))
    per_site = 0.0 if not np.isfinite(per_site) else max(0.0, per_site)
    return np.minimum(np.full(len(caps), per_site), caps)


def _allocate_prorata(S: np.ndarray, headroom: np.ndarray, caps: np.ndarray) -> np.ndarray:
    """Limits proportional to capability: find the common multiplier."""
    weighted = S @ caps
    with np.errstate(divide="ignore"):
        m = np.min(np.where(weighted > _TINY, headroom / weighted, np.inf))
    m = 0.0 if not np.isfinite(m) else max(0.0, m)
    return np.minimum(m * caps, caps)


def _allocate_max_total(S: np.ndarray, headroom: np.ndarray, caps: np.ndarray) -> np.ndarray:
    """Maximise total export: LP when scipy is present, else a greedy heuristic
    (exact when a single constraint binds — the usual radial-feeder case)."""
    n = len(caps)
    try:
        from scipy.optimize import linprog

        res = linprog(
            c=-np.ones(n), A_ub=S, b_ub=headroom,
            bounds=[(0.0, float(c)) for c in caps], method="highs",
        )
        if res.success:
            return np.clip(res.x, 0.0, caps)
        logger.warning("max_total LP failed (%s); using greedy fallback", res.message)
    except ImportError:
        logger.info("scipy unavailable; max_total uses the greedy heuristic")

    limits = np.zeros(n)
    remaining = headroom.copy()
    # Cheapest headroom consumers first (electrically strongest sites).
    order = np.argsort(S.sum(axis=0))
    for i in order:
        col = S[:, i]
        with np.errstate(divide="ignore"):
            room = np.min(np.where(col > _TINY, remaining / col, np.inf))
        take = float(min(caps[i], room if np.isfinite(room) else caps[i]))
        take = max(0.0, take)
        limits[i] = take
        remaining -= col * take
        remaining = np.maximum(remaining, 0.0)
    return limits


register_allocation("equal", _allocate_equal, "Equal kW per site")
register_allocation("prorata", _allocate_prorata, "Pro-rata by site capability")
register_allocation("max_total", _allocate_max_total,
                    "Maximise total export (LP; greedy fallback)")


def _allocate(S: np.ndarray, headroom: np.ndarray, caps: np.ndarray,
              allocation: str) -> np.ndarray:
    """Dispatch to the registered allocation policy (unknown names raise)."""
    if allocation not in _ALLOCATION_REGISTRY:
        raise ValueError(
            f"Unknown envelope allocation '{allocation}'. "
            f"Available: {[a['name'] for a in available_allocations()]}."
        )
    headroom = np.maximum(headroom, 0.0)
    return _ALLOCATION_REGISTRY[allocation]["fn"](S, headroom, caps)


def compute_envelopes(
    engine,
    network,
    profiles: dict,
    mode: str,
    allocation: str = "equal",
    method: str = "sensitivity",
    fixed_export_kw: float = 1.5,
    v_limit: float = 1.05,
    thermal_limit: float = 100.0,
) -> dict[int, np.ndarray]:
    """Per-site export limits (kW) for every interval of the horizon.

    Returns ``{bus_id: array[T]}`` over the export-capable buses; empty when
    there are none. ``static`` mode is the constant fixed limit; ``dynamic``
    computes limits from network headroom per the selected method/allocation.
    """
    buses = envelope_buses(profiles)
    timesteps = int(profiles["metadata"]["timesteps"])
    if not buses:
        return {}

    if mode == "static":
        return {b: np.full(timesteps, float(fixed_export_kw)) for b in buses}

    caps = np.array([site_capability_kw(profiles, b) for b in buses])
    limits = {b: np.zeros(timesteps) for b in buses}

    if method == "search":
        # Exact nonlinear search: the largest uniform fraction of capability
        # the network absorbs without violation (pro-rata by construction).
        for t in range(timesteps):
            _apply_baseline(engine, network, profiles, t)
            engine.solve()
            if _violates(engine, v_limit, thermal_limit):
                continue   # no headroom this interval: limits stay 0
            lo, hi = 0.0, 1.0
            for _ in range(_SEARCH_ITERATIONS):
                mid = (lo + hi) / 2.0
                for i, b in enumerate(buses):
                    engine.update_pv(b, mid * caps[i])
                engine.solve()
                if _violates(engine, v_limit, thermal_limit):
                    hi = mid
                else:
                    lo = mid
            for i, b in enumerate(buses):
                limits[b][t] = lo * caps[i]
                engine.update_pv(b, 0.0)
        return limits

    # Sensitivity method: one perturbation pass at the most-loaded interval,
    # then a per-interval linear allocation of the measured headroom.
    total_load = [
        sum(_series(profiles, b, "load_kw", t) for b in profiles["buses"]
            if b != network.source_bus)
        for t in range(timesteps)
    ]
    t_ref = int(np.argmax(total_load))
    _apply_baseline(engine, network, profiles, t_ref)
    engine.solve()
    v0 = engine.get_bus_voltages_pu()
    l0 = engine.get_branch_loadings_pct()
    bus_ids, branch_ids, S_v, S_l = _sensitivities(engine, buses, v0, l0)
    S = np.vstack([S_v, S_l])

    for t in range(timesteps):
        _apply_baseline(engine, network, profiles, t)
        engine.solve()
        v_t = engine.get_bus_voltages_pu()
        l_t = engine.get_branch_loadings_pct()
        head_v = np.array([v_limit - v_t[b] for b in bus_ids])
        head_l = np.array([thermal_limit - l_t.get(br, 0.0) for br in branch_ids])
        allocated = _allocate(S, np.concatenate([head_v, head_l]), caps, allocation)
        for i, b in enumerate(buses):
            limits[b][t] = allocated[i]
    return limits
