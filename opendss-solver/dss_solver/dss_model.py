"""Generates OpenDSS .dss script files from any SolverNetwork.

Every study uses the full multi-phase circuit model (``Set CktModel=Multiphase``).
The ``solve_mode`` then decides how elements connect:

  - ``"balanced"``   -> every load and DER is a symmetric three-phase connection,
                        so the solve returns the balanced answer with each bus's
                        three phases identical.
  - ``"unbalanced"`` -> loads and DER connect to the phases each bus declares
                        (default three-phase), and intrinsically single-phase EV
                        chargers are placed on one phase, producing a genuine
                        multi-phase unbalanced solve.

The multi-phase model (rather than positive-sequence) is deliberate: in
positive-sequence mode OpenDSS reads a three-phase element's ``kW`` as a
*per-phase* value and triples it, which would inflate every load/PV/BESS/EV 3x.
"""

import os
from math import sqrt

from dss_solver.network import SolverNetwork

BALANCED = "balanced"
UNBALANCED = "unbalanced"
SOLVE_MODES = (BALANCED, UNBALANCED)

# Default zero-sequence impedance multiplier for lines that do not declare
# explicit r0_ohm/x0_ohm. Distribution feeders typically sit near Z0 ~ 2.5-3x
# Z1; using Z0 = Z1 (the old behaviour) understates neutral-path effects in
# unbalanced studies. Balanced (positive-sequence-symmetric) results are
# unaffected by Z0 either way.
Z0_DEFAULT_MULTIPLIER = 3.0


def normalize_solve_mode(solve_mode: str | None) -> str:
    """Coerce a solve-mode string to 'balanced' or 'unbalanced' (default balanced)."""
    mode = (solve_mode or BALANCED).strip().lower()
    return mode if mode in SOLVE_MODES else BALANCED


def _bus_phase_nodes(network: SolverNetwork, bus_id: int, solve_mode: str) -> list[int]:
    """Phases an element at ``bus_id`` connects to for the given solve mode.

    Balanced studies are always three-phase; unbalanced studies honour the bus's
    declared phases (default three-phase).
    """
    if normalize_solve_mode(solve_mode) == BALANCED:
        return [1, 2, 3]
    return network.phase_nodes(bus_id)


def _ev_phase_nodes(network: SolverNetwork, bus_id: int, solve_mode: str) -> list[int]:
    """Phase placement for an EV charger (an intrinsically single-phase load).

    Balanced: aggregated as a symmetric three-phase load. Unbalanced: a single
    phase — the bus's own phase if it is a single-phase lateral, otherwise one of
    the three phases chosen by bus id so chargers spread across a/b/c and create
    realistic phase unbalance.
    """
    if normalize_solve_mode(solve_mode) == BALANCED:
        return [1, 2, 3]
    bus_nodes = network.phase_nodes(bus_id)
    if len(bus_nodes) == 1:
        return bus_nodes
    return [bus_nodes[int(bus_id) % len(bus_nodes)]]


def _bus_terminal(bus_id: int, nodes: list[int]) -> str:
    """OpenDSS bus terminal spec, e.g. 'bus_007' (3-phase) or 'bus_007.1' (phase a)."""
    if nodes == [1, 2, 3]:
        return f"bus_{bus_id:03d}"
    return f"bus_{bus_id:03d}." + ".".join(str(n) for n in nodes)


def _element_kv(base_kv: float, nodes: list[int]) -> float:
    """Element kV base: line-line for multi-phase, line-neutral for single-phase."""
    return base_kv / sqrt(3) if len(nodes) == 1 else base_kv


def _transformer_dss(branch: dict, from_kv: float, to_kv: float) -> str:
    """A two-winding transformer between buses at different base voltages.

    The branch impedance (ohms, referred to the from-side base) is converted to
    the percent reactance / load-loss OpenDSS expects. The optional
    ``connection`` selects the vector group: ``wye_wye`` (default) or
    ``delta_wye`` — the delta-primary/wye-secondary (Dyn11-style) arrangement of
    Australian distribution transformers, which isolates zero sequence between
    the levels. The optional ``tap`` fixes the secondary winding's off-load tap
    (per unit; >1 boosts the LV voltage).
    """
    branch_id = int(branch["branch_id"])
    from_bus, to_bus = int(branch["from_bus"]), int(branch["to_bus"])
    rating_kva = float(branch.get("rating_kva") or 5000) or 5000.0
    z_base = (from_kv * from_kv) / (rating_kva / 1000.0)  # ohms on the from side
    x_pct = max(0.01, float(branch.get("x_ohm", 0.0)) / z_base * 100.0) if z_base > 0 else 5.0
    loadloss_pct = float(branch.get("r_ohm", 0.0)) / z_base * 100.0 if z_base > 0 else 0.0
    conns = "[delta, wye]" if branch.get("connection") == "delta_wye" else "[wye, wye]"
    taps = f" Taps=[1, {float(branch['tap']):.4f}]" if branch.get("tap") is not None else ""
    return (
        f"New Transformer.xfmr_{branch_id} Phases=3 Windings=2 "
        f"Buses=[bus_{from_bus:03d}, bus_{to_bus:03d}] "
        f"kVs=[{from_kv:.4f}, {to_kv:.4f}] kVAs=[{rating_kva}, {rating_kva}] "
        f"XHL={x_pct:.4f} %loadloss={loadloss_pct:.4f} Conns={conns}{taps}"
    )


def _oltc_dss(branch: dict, to_kv: float) -> str:
    """An on-load tap changer regulating the transformer's secondary side.

    Modelled as an OpenDSS RegControl on winding 2 holding 1.0 pu within a 2%
    band on a 120 V regulator base (the transformer's default +/-10% tap range,
    32 steps, applies). OpenDSS iterates the tap position inside each solve.
    """
    branch_id = int(branch["branch_id"])
    ptratio = (to_kv * 1000.0 / sqrt(3)) / 120.0  # LV line-neutral volts -> 120 V base
    return (
        f"New RegControl.reg_{branch_id} transformer=xfmr_{branch_id} winding=2 "
        f"vreg=120 band=2.4 ptratio={ptratio:.4f}"
    )


def generate_master_dss(
    network: SolverNetwork, output_dir: str = "app/dss", solve_mode: str = BALANCED
) -> str:
    """Creates a master.dss file for the given network.

    Supports multi-voltage feeders: each bus carries its own base voltage and
    branches flagged ``is_transformer`` become OpenDSS Transformer elements
    between the two levels (otherwise they are Lines at the from-bus voltage).

    ``solve_mode`` selects a balanced (symmetric three-phase) or unbalanced
    (per-bus phases) connection model. Returns the path of the created master.dss.
    """
    mode = normalize_solve_mode(solve_mode)
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "master.dss")
    source_kv = network.bus_base_kv(network.source_bus)

    lines = [
        "Clear",
        "",
        f"! Network: {network.name} (id={network.id})  solve_mode={mode}",
        f"New Circuit.{network.id} basekv={source_kv} pu=1.0 phases=3 "
        f"bus1=bus_{network.source_bus:03d}",
        # Always the full multi-phase model: balanced studies still get the
        # balanced answer (every element is a symmetric three-phase connection),
        # but a three-phase load's kW is read as the true total. Positive-sequence
        # mode would read it as per-phase and triple every load/PV/BESS/EV.
        "Set CktModel=Multiphase",
        "",
    ]

    for branch in network.branches:
        from_bus, to_bus = int(branch["from_bus"]), int(branch["to_bus"])
        if network.is_transformer(branch):
            to_kv = network.bus_base_kv(to_bus)
            lines.append(
                _transformer_dss(branch, network.bus_base_kv(from_bus), to_kv)
            )
            if network.has_oltc(branch):
                lines.append(_oltc_dss(branch, to_kv))
            continue
        bus_kv = network.bus_base_kv(from_bus)
        rating_kva = float(branch.get("rating_kva", 5000))
        normamps = rating_kva / (bus_kv * sqrt(3))
        r1, x1 = float(branch["r_ohm"]), float(branch["x_ohm"])
        # Zero-sequence: explicit per-branch values when given, otherwise the
        # documented default multiple of the positive-sequence impedance.
        r0 = float(branch["r0_ohm"]) if branch.get("r0_ohm") is not None else r1 * Z0_DEFAULT_MULTIPLIER
        x0 = float(branch["x0_ohm"]) if branch.get("x0_ohm") is not None else x1 * Z0_DEFAULT_MULTIPLIER
        lines.append(
            f"New Line.branch_{int(branch['branch_id'])} "
            f"Bus1=bus_{from_bus:03d} Bus2=bus_{to_bus:03d} "
            f"Length=1 Units=none "
            f"R1={r1} X1={x1} "
            f"R0={r0} X0={x0} "
            f"Normamps={normamps:.4f}"
        )

    lines.append("")

    for bus_id in network.load_bus_ids:
        nodes = _bus_phase_nodes(network, bus_id, mode)
        bus_kv = network.bus_base_kv(bus_id)
        lines.append(
            f"New Load.load_{bus_id:03d} Bus1={_bus_terminal(bus_id, nodes)} "
            f"Phases={len(nodes)} Conn=Wye Model=1 kV={_element_kv(bus_kv, nodes):.4f} "
            f"kW=0 kvar=0"
        )

    lines.append("")
    levels = ", ".join(f"{v}" for v in network.voltage_levels())
    lines.append(f"Set VoltageBases=[{levels}]")
    lines.append("CalcVoltageBases")
    lines.append("Solve")
    lines.append("")

    with open(filepath, "w") as f:
        f.write("\n".join(lines))

    return filepath


def generate_pv_dss(bus_id: int, capacity_kw: float, base_kv: float,
                    nodes: list[int] | None = None) -> str:
    """Returns an OpenDSS command string to add a PV generator at a bus."""
    nodes = nodes or [1, 2, 3]
    return (
        f"New Generator.pv_{bus_id:03d} Bus1={_bus_terminal(bus_id, nodes)} "
        f"Phases={len(nodes)} kV={_element_kv(base_kv, nodes):.4f} "
        f"kW={capacity_kw} PF=1 Model=1 Enabled=yes"
    )


def generate_bess_dss(bus_id: int, capacity_kw: float, base_kv: float,
                      nodes: list[int] | None = None) -> str:
    """Returns an OpenDSS command string to add a BESS generator at a bus.

    BESS is modelled as a Generator element that can inject or absorb power.
    Positive kW = discharging (injecting), negative kW = charging (absorbing).
    """
    nodes = nodes or [1, 2, 3]
    return (
        f"New Generator.bess_{bus_id:03d} Bus1={_bus_terminal(bus_id, nodes)} "
        f"Phases={len(nodes)} kV={_element_kv(base_kv, nodes):.4f} "
        f"kW=0 PF=1 Model=1 Enabled=yes"
    )


def generate_ev_load_dss(bus_id: int, charge_rate_kw: float, base_kv: float,
                         nodes: list[int] | None = None) -> str:
    """Returns an OpenDSS command string to add an EV load at a bus."""
    nodes = nodes or [1]
    return (
        f"New Load.ev_{bus_id:03d} Bus1={_bus_terminal(bus_id, nodes)} "
        f"Phases={len(nodes)} Conn=Wye Model=1 kV={_element_kv(base_kv, nodes):.4f} "
        f"kW=0 kvar=0"
    )


def generate_all_pv_dss(
    pv_config: list[dict], network: SolverNetwork, output_dir: str = "app/dss",
    solve_mode: str = BALANCED,
) -> str:
    """Creates a pv_systems.dss file with Generator elements for all PV buses.

    Args:
        pv_config: List of {"bus_id": int, "pv_capacity_kw": float}.
        network: The network the elements attach to.
        output_dir: Directory to write the file.
        solve_mode: 'balanced' (three-phase) or 'unbalanced' (per-bus phases).

    Returns the file path of the created pv_systems.dss.
    """
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "pv_systems.dss")

    lines = []
    for pv in pv_config:
        nodes = _bus_phase_nodes(network, pv["bus_id"], solve_mode)
        lines.append(
            generate_pv_dss(pv["bus_id"], pv["pv_capacity_kw"], network.bus_base_kv(pv["bus_id"]), nodes)
        )
    lines.append("")

    with open(filepath, "w") as f:
        f.write("\n".join(lines))

    return filepath


def generate_all_bess_dss(
    bess_config: list[dict], network: SolverNetwork, output_dir: str = "app/dss",
    solve_mode: str = BALANCED,
) -> str:
    """Creates a bess_systems.dss file with Generator elements for all BESS buses.

    Args:
        bess_config: List of {"bus_id": int, "bess_capacity_kwh": float}.
        network: The network the elements attach to.
        output_dir: Directory to write the file.
        solve_mode: 'balanced' (three-phase) or 'unbalanced' (per-bus phases).

    Returns the file path of the created bess_systems.dss.
    """
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "bess_systems.dss")

    lines = []
    for bess in bess_config:
        nodes = _bus_phase_nodes(network, bess["bus_id"], solve_mode)
        lines.append(
            generate_bess_dss(bess["bus_id"], bess["bess_capacity_kwh"],
                              network.bus_base_kv(bess["bus_id"]), nodes)
        )
    lines.append("")

    with open(filepath, "w") as f:
        f.write("\n".join(lines))

    return filepath


def generate_all_ev_dss(
    ev_config: list[dict], network: SolverNetwork, output_dir: str = "app/dss",
    solve_mode: str = BALANCED,
) -> str:
    """Creates an ev_loads.dss file with Load elements for all EV buses.

    Args:
        ev_config: List of {"bus_id": int, "ev_charge_rate_kw": float}.
        network: The network the elements attach to.
        output_dir: Directory to write the file.
        solve_mode: 'balanced' (three-phase) or 'unbalanced' (single phase).

    Returns the file path of the created ev_loads.dss.
    """
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "ev_loads.dss")

    lines = []
    for ev in ev_config:
        nodes = _ev_phase_nodes(network, ev["bus_id"], solve_mode)
        lines.append(
            generate_ev_load_dss(ev["bus_id"], ev["ev_charge_rate_kw"],
                                 network.bus_base_kv(ev["bus_id"]), nodes)
        )
    lines.append("")

    with open(filepath, "w") as f:
        f.write("\n".join(lines))

    return filepath
