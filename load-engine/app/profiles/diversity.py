"""Load diversity / aggregation model.

A distribution bus does not serve one home — it aggregates the many households
downstream of it, and their demand peaks do not coincide. The bus peak is
therefore lower and broader than the sum of the individual household peaks (the
After-Diversity Maximum Demand, ADMD). The built-in archetypes and uploaded
custom profiles describe a *single* household; this module turns one such
per-unit day-shape into the diversified demand of a whole bus.

Per bus (deterministic in ``seed``):
  * the household count ``N`` is derived from the bus peak demand and an assumed
    per-household ADMD;
  * each household copies the template and applies an independent
      - circular time-shift  ~ Normal(0, sigma_minutes)  (activities differ),
      - magnitude scale       ~ LogNormal(mean 1, cv)      (homes differ in size),
      - per-timestep appliance switching noise (small, multiplicative);
  * the bus shape is the mean across households.

Aggregating independent shifts fills the overnight/midday valleys and rounds
off the sharp archetype peak. Because each bus draws its own realisation, two
buses of the same archetype no longer peak at the same instant — giving the
feeder a realistic coincidence factor < 1 instead of the previous perfectly
synchronised peaks.
"""

import numpy as np


def households_for_bus(peak_kw: float, admd_kw: float, max_households: int) -> int:
    """Number of households a bus aggregates, from its peak demand and ADMD.

    ``base_load_kw`` is treated as the bus's diversified maximum demand, so
    ``N ~= base_load_kw / admd_kw``. Clamped to ``[1, max_households]``.
    """
    if peak_kw <= 0 or admd_kw <= 0:
        return 1
    return int(np.clip(round(peak_kw / admd_kw), 1, max_households))


def _lognormal_params(cv: float) -> tuple[float, float]:
    """(mu, sigma) of the underlying normal so the lognormal has mean 1 for ``cv``."""
    if cv <= 0:
        return 0.0, 0.0
    sigma = float(np.sqrt(np.log(1.0 + cv * cv)))
    mu = -0.5 * sigma * sigma
    return mu, sigma


def diversified_shape(
    template: np.ndarray,
    *,
    n_households: int,
    seed: int,
    sigma_minutes: float = 45.0,
    magnitude_cv: float = 0.4,
    appliance_cv: float = 0.1,
    resolution_minutes: float = 15.0,
    preserve_peak: bool = True,
) -> np.ndarray:
    """Aggregate ``n_households`` jittered copies of a per-unit ``template``.

    Args:
        template: Per-unit single-household day-shape (length T).
        n_households: How many households this bus aggregates.
        seed: Per-bus RNG seed (reproducible).
        sigma_minutes: Std-dev of the per-household activity time-shift.
        magnitude_cv: Coefficient of variation of per-household magnitude.
        appliance_cv: Per-timestep multiplicative switching-noise CV.
        resolution_minutes: Minutes per timestep (to convert the time-shift).
        preserve_peak: If True, renormalise the bus shape to a unit peak so the
            bus peak stays equal to ``base_load_kw`` (i.e. ``base_load_kw`` is
            the ADMD). If False, keep the natural diversified peak (< 1).

    Returns:
        A per-unit bus day-shape of length T.
    """
    template = np.asarray(template, dtype=float)
    timesteps = template.shape[0]
    n = max(1, int(n_households))
    rng = np.random.default_rng(seed)

    # Independent circular time-shift per household, expressed in timesteps.
    sigma_steps = sigma_minutes / max(resolution_minutes, 1e-9)
    shifts = np.rint(rng.normal(0.0, sigma_steps, n)).astype(int)
    # households[i] is the template circularly rolled by shift_i.
    idx = (np.arange(timesteps)[None, :] - shifts[:, None]) % timesteps
    households = template[idx]  # shape (n, T)

    # Per-household magnitude variation (lognormal, mean 1).
    if magnitude_cv > 0:
        mu, sigma = _lognormal_params(magnitude_cv)
        households = households * rng.lognormal(mu, sigma, size=n)[:, None]

    # Per-timestep appliance switching noise (multiplicative, non-negative).
    # Its effect averages out as N grows — exactly as real aggregated demand does.
    if appliance_cv > 0:
        noise = np.clip(rng.normal(1.0, appliance_cv, size=(n, timesteps)), 0.0, None)
        households = households * noise

    bus_shape = np.clip(households.mean(axis=0), 0.0, None)

    if preserve_peak:
        peak = bus_shape.max()
        if peak > 0:
            bus_shape = bus_shape / peak

    return bus_shape
