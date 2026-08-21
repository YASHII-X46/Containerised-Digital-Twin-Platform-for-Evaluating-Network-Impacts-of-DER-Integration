"""Generate report figures from stack CSV exports.

Reads the Simulation Engine result CSVs and the Load Engine profile CSVs and
writes PNG figures into reports/figures/.

Nothing about the network or scenarios is hardcoded: the scenarios, bus list,
timestep count, and time resolution are all discovered from the CSV files
themselves, so the figures work for any network and any set of runs. The only
configurable inputs are the reference limits drawn on the charts, taken from the
same environment variables the engines use.
"""
import glob
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
STACK_ROOT = os.path.dirname(ROOT)
RESULTS = os.path.join(STACK_ROOT, "outputs", "results")
PROFILES = os.path.join(STACK_ROOT, "outputs", "profiles")
FIGDIR = os.path.join(ROOT, "reports", "figures")
os.makedirs(FIGDIR, exist_ok=True)

# Reference limits drawn on the charts — match the engine configuration rather
# than assuming fixed values.
V_LO = float(os.environ.get("VOLTAGE_LOWER_PU", 0.95))
V_HI = float(os.environ.get("VOLTAGE_UPPER_PU", 1.05))
THERMAL = float(os.environ.get("THERMAL_LIMIT_PCT", 100.0))

plt.rcParams.update({"figure.dpi": 130, "font.size": 10, "axes.grid": True,
                     "grid.alpha": 0.3, "figure.autolayout": True})


# ---- scenario discovery (no hardcoded scenario list) ------------------------

def discover_scenarios():
    """Find every scenario with a voltages CSV; order by penetration if encoded."""
    scen = []
    for path in glob.glob(os.path.join(RESULTS, "*_voltages.csv")):
        base = os.path.basename(path)[: -len("_voltages.csv")]
        m = re.search(r"pen(\d+(?:\.\d+)?)", base)
        order = float(m.group(1)) if m else float("inf")
        scen.append((order, base))
    scen.sort(key=lambda x: (x[0], x[1]))
    return [base for _, base in scen]


def load_volts(base):
    return pd.read_csv(os.path.join(RESULTS, f"{base}_voltages.csv"))


def load_branches(base):
    path = os.path.join(RESULTS, f"{base}_branches.csv")
    return pd.read_csv(path) if os.path.exists(path) else None


def load_profile(base):
    path = os.path.join(PROFILES, f"{base}.csv")
    return pd.read_csv(path) if os.path.exists(path) else None


def label_for(base, df):
    """Scenario label from the data (DER penetration if present, else the name)."""
    if "der_penetration_percent" in df.columns:
        return f"{float(df['der_penetration_percent'].iloc[0]):g}% DER"
    return base


def step_hours(df):
    """Hours per timestep, inferred from consecutive timestamps (default 24/N)."""
    n = df["timestep"].nunique()
    try:
        one = df[df["bus_id"] == df["bus_id"].iloc[0]].sort_values("timestep")
        ts = pd.to_datetime(one["timestamp"].iloc[:2])
        dt = (ts.iloc[1] - ts.iloc[0]).total_seconds() / 3600.0
        if dt > 0:
            return dt
    except Exception:
        pass
    return 24.0 / n if n else 0.25


def hours_axis(df):
    n = df["timestep"].nunique()
    return np.arange(n) * step_hours(df)


def network_label(base):
    """Network id recorded in the profile CSV, else a generic label."""
    p = load_profile(base)
    if p is not None and "network_id" in p.columns and str(p["network_id"].iloc[0]):
        return str(p["network_id"].iloc[0])
    return "distribution feeder"


def save(fig, name):
    path = os.path.join(FIGDIR, name)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print("wrote", os.path.relpath(path, ROOT))


SCENARIOS = discover_scenarios()
if not SCENARIOS:
    raise SystemExit(
        f"No result CSVs found in {RESULTS}. Run scenarios and export results first."
    )

# Colour each discovered scenario from a perceptually-uniform map.
_cmap = plt.cm.viridis(np.linspace(0.0, 0.85, len(SCENARIOS)))
COLORS = {base: _cmap[i] for i, base in enumerate(SCENARIOS)}

# Representative scenarios for single-scenario figures, chosen by position:
MID = SCENARIOS[len(SCENARIOS) // 2]   # middle penetration
TOP = SCENARIOS[-1]                     # most-stressed (highest penetration)


# 1. Min & max system voltage over 24h, all scenarios -------------------------
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
for base in SCENARIOS:
    df = load_volts(base)
    g = df.groupby("timestep")["voltage_pu"]
    h = hours_axis(df)
    ax1.plot(h, g.min().values, color=COLORS[base], label=label_for(base, df))
    ax2.plot(h, g.max().values, color=COLORS[base], label=label_for(base, df))
for ax, lim, lbl in ((ax1, V_LO, f"{V_LO:g} lower limit"), (ax2, V_HI, f"{V_HI:g} upper limit")):
    ax.axhline(lim, ls="--", color="k", lw=1, label=lbl)
    ax.set_xlabel("Hour of day"); ax.set_ylabel("Voltage (p.u.)")
    ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 4)); ax.legend(fontsize=8)
ax1.set_title("Minimum system voltage")
ax2.set_title("Maximum system voltage")
save(fig, "01_voltage_envelope.png")

# 2. Voltage profile along feeder (per-bus min/mean/max), representative -------
fig, ax = plt.subplots(figsize=(9, 4))
df = load_volts(MID)
g = df.groupby("bus_id")["voltage_pu"]
buses = g.mean().index.values
ax.fill_between(buses, g.min().values, g.max().values, alpha=0.25,
                color=COLORS[MID], label="min–max range")
ax.plot(buses, g.mean().values, "o-", ms=3, color=COLORS[MID], label="mean")
ax.axhline(V_LO, ls="--", color="k", lw=1, label=f"{V_LO:g} limit")
ax.set_xlabel("Bus ID"); ax.set_ylabel("Voltage (p.u.)")
ax.set_title(f"Per-bus voltage profile along the {network_label(MID)} ({label_for(MID, df)})")
ax.legend(fontsize=8)
save(fig, "02_feeder_voltage_profile.png")

# 3. Voltage heatmap (bus x time), most-stressed scenario ---------------------
fig, ax = plt.subplots(figsize=(10, 4.5))
df = load_volts(TOP)
piv = df.pivot(index="bus_id", columns="timestep", values="voltage_pu")
total_h = df["timestep"].nunique() * step_hours(df)
bus_lo, bus_hi = int(df["bus_id"].min()), int(df["bus_id"].max())
im = ax.imshow(piv.values, aspect="auto", origin="lower", cmap="RdYlGn",
               vmin=round(V_LO - 0.05, 2), vmax=V_HI,
               extent=[0, total_h, bus_lo, bus_hi])
ax.set_xlabel("Hour of day"); ax.set_ylabel("Bus ID")
ax.set_title(f"Bus voltage heatmap over {total_h:g} h ({label_for(TOP, df)})")
fig.colorbar(im, ax=ax, label="Voltage (p.u.)")
save(fig, "03_voltage_heatmap.png")

# 4. Max branch loading over 24h ----------------------------------------------
fig, ax = plt.subplots(figsize=(9, 4))
for base in SCENARIOS:
    df = load_branches(base)
    if df is None:
        continue
    m = df.groupby("timestep")["loading_pct"].max().values
    ax.plot(np.arange(len(m)) * step_hours(df), m, color=COLORS[base],
            label=label_for(base, df))
ax.axhline(THERMAL, ls="--", color="k", lw=1, label=f"{THERMAL:g}% thermal limit")
ax.set_xlabel("Hour of day"); ax.set_ylabel("Max branch loading (%)")
ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 4))
ax.set_title("Maximum branch loading over 24 h")
ax.legend(fontsize=8)
save(fig, "04_branch_loading.png")

# 5. Feeder net load (duck curve) from profiles -------------------------------
fig, ax = plt.subplots(figsize=(9, 4))
for base in SCENARIOS:
    p = load_profile(base)
    if p is None:
        continue
    net = p.groupby("timestep")["net_load_kw"].sum().values
    ax.plot(np.arange(len(net)) * step_hours(p), net, color=COLORS[base],
            label=label_for(base, p))
ax.axhline(0, ls=":", color="k", lw=1)
ax.set_xlabel("Hour of day"); ax.set_ylabel("Aggregate net load (kW)")
ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 4))
ax.set_title("Feeder net load (duck curve) — negative = reverse power export")
ax.legend(fontsize=8)
save(fig, "05_net_load_duck_curve.png")

# 6. DER component breakdown (representative scenario) ------------------------
p = load_profile(MID)
if p is not None:
    fig, ax = plt.subplots(figsize=(9, 4))
    agg = p.groupby("timestep").sum(numeric_only=True)
    h = np.arange(len(agg)) * step_hours(p)
    ax.plot(h, agg["load_kw"].values, color="#555555", label="Building load")
    ax.plot(h, -agg["pv_kw"].values, color="#ff7f0e", label="PV (−, generation)")
    ax.plot(h, agg["ev_charge_kw"].values, color="#9467bd", label="EV charging")
    ax.plot(h, -agg["bess_power_kw"].values, color="#17becf",
            label="BESS (−discharge/+charge)")
    ax.plot(h, agg["net_load_kw"].values, color="k", lw=2, label="Net load")
    ax.axhline(0, ls=":", color="k", lw=1)
    ax.set_xlabel("Hour of day"); ax.set_ylabel("Aggregate power (kW)")
    ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 4))
    ax.set_title(f"Aggregate DER component profiles ({label_for(MID, load_volts(MID))})")
    ax.legend(fontsize=8, ncol=2)
    save(fig, "06_der_components.png")

# 7. EV aggregate charging -----------------------------------------------------
fig, ax = plt.subplots(figsize=(9, 4))
for base in SCENARIOS:
    p = load_profile(base)
    if p is None:
        continue
    ev = p.groupby("timestep")["ev_charge_kw"].sum().values
    ax.plot(np.arange(len(ev)) * step_hours(p), ev, color=COLORS[base],
            label=label_for(base, p))
ax.set_xlabel("Hour of day"); ax.set_ylabel("Aggregate EV charging (kW)")
ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 4))
ax.set_title("Aggregate EV charging demand")
ax.legend(fontsize=8)
save(fig, "07_ev_evening_charging.png")

# 8. Voltage violations per bus (most-stressed scenario) ----------------------
fig, ax = plt.subplots(figsize=(9, 4))
df = load_volts(TOP)
n_steps = df["timestep"].nunique()
bus_lo, bus_hi = int(df["bus_id"].min()), int(df["bus_id"].max())
vc = df[df["is_voltage_violation"]].groupby("bus_id").size().reindex(
    range(bus_lo, bus_hi + 1), fill_value=0)
ax.bar(vc.index, vc.values, color=COLORS[TOP])
ax.set_xlabel("Bus ID"); ax.set_ylabel(f"Violation count (of {n_steps} steps)")
ax.set_title(f"Under/over-voltage violations per bus ({label_for(TOP, df)})")
save(fig, "08_violations_per_bus.png")

print("\nAll figures written to", os.path.relpath(FIGDIR, ROOT))
