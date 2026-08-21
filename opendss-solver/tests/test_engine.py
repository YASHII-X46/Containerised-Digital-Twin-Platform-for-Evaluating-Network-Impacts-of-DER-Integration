"""Tests for the OpenDSS engine wrapper (real power flow on the IEEE 33-bus)."""

from dss_solver.dss_model import generate_master_dss
from dss_solver.engine import OpenDSSEngine
from dss_solver.network import SolverNetwork


def _mv_lv_network() -> SolverNetwork:
    """11 kV feeder stepping down through an 11/0.4 kV transformer to an LV bus."""
    return SolverNetwork({
        "id": "mv_lv_solve",
        "name": "MV/LV solve",
        "base_voltage_kv": 11.0,
        "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
            {"bus_id": 2, "base_load_kw": 0.0, "base_load_kvar": 0.0},
            {"bus_id": 3, "base_load_kw": 0.0, "base_load_kvar": 0.0, "base_kv": 0.4},
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
            {"branch_id": 2, "from_bus": 2, "to_bus": 3, "is_transformer": True,
             "r_ohm": 0.02, "x_ohm": 0.12, "rating_kva": 500},
        ],
    })


def test_oltc_regulates_lv_voltage(tmp_path):
    """A heavily loaded LV bus sags without an OLTC; the tap changer lifts it."""
    def build(oltc: bool) -> SolverNetwork:
        return SolverNetwork({
            "id": f"oltc_{oltc}", "name": "OLTC test", "base_voltage_kv": 11.0,
            "source_bus": 1,
            "buses": [
                {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
                {"bus_id": 2, "base_load_kw": 0.0, "base_load_kvar": 0.0},
                {"bus_id": 3, "base_load_kw": 0.0, "base_load_kvar": 0.0, "base_kv": 0.4},
            ],
            "branches": [
                {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 2.0, "x_ohm": 1.5, "rating_kva": 2000},
                # ~2% R, ~6% X on the 500 kVA / 11 kV base (z_base = 242 ohm).
                {"branch_id": 2, "from_bus": 2, "to_bus": 3, "is_transformer": True,
                 "oltc": oltc, "r_ohm": 4.84, "x_ohm": 14.52, "rating_kva": 500},
            ],
        })

    voltages = {}
    for oltc in (False, True):
        net = build(oltc)
        engine = OpenDSSEngine(generate_master_dss(net, str(tmp_path)), net)
        engine.update_load(3, 350.0, 120.0)   # heavy LV load -> secondary sag
        assert engine.solve() is True
        voltages[oltc] = engine.get_bus_voltages_pu()[3]

    assert voltages[False] < 0.99             # sags without regulation
    assert voltages[True] > voltages[False]   # the tap changer lifts the LV side
    assert abs(voltages[True] - 1.0) < 0.02   # held inside the 2% regulator band


def test_vuf_zero_balanced_positive_unbalanced(tmp_path, ieee33_network):
    """VUF reads ~0 on a balanced solve and clearly >0 with a single-phase load."""
    net = ieee33_network
    master = generate_master_dss(net, str(tmp_path), "unbalanced")
    engine = OpenDSSEngine(master, net)

    engine.update_load(2, 300.0, 100.0)          # three-phase, balanced
    assert engine.solve()
    assert engine.get_max_vuf_pct() < 0.05

    # Pile the same power onto one phase of bus 18 via its (single-phase
    # capable) EV element pattern — emulate with a direct one-phase load edit.
    import opendssdirect as dss
    dss.Text.Command("New Load.unbal_18 Bus1=bus_018.1 Phases=1 Conn=Wye "
                     "Model=1 kV=7.31 kW=250 kvar=80")
    assert engine.solve()
    assert engine.get_max_vuf_pct() > 0.5        # genuine unbalance registered


def test_fixed_tap_boosts_lv_voltage_and_delta_wye_solves(tmp_path):
    """A +5% secondary tap lifts the LV bus; the Dyn11-style group still solves."""
    def build(tap):
        return SolverNetwork({
            "id": f"tap_{str(tap).replace('.', '_')}", "name": "Tap test",
            "base_voltage_kv": 11.0, "source_bus": 1,
            "buses": [
                {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
                {"bus_id": 2, "base_load_kw": 0.0, "base_load_kvar": 0.0, "base_kv": 0.4},
            ],
            "branches": [
                {"branch_id": 1, "from_bus": 1, "to_bus": 2, "is_transformer": True,
                 "connection": "delta_wye", "tap": tap,
                 "r_ohm": 4.84, "x_ohm": 14.52, "rating_kva": 500},
            ],
        })

    voltages = {}
    for tap in (1.0, 1.05):
        net = build(tap)
        engine = OpenDSSEngine(generate_master_dss(net, str(tmp_path)), net)
        engine.update_load(2, 200.0, 70.0)
        assert engine.solve() is True
        voltages[tap] = engine.get_bus_voltages_pu()[2]

    assert voltages[1.05] > voltages[1.0] + 0.03   # ~5% boost from the fixed tap


def test_multivoltage_network_solves_with_transformer(tmp_path):
    network = _mv_lv_network()
    engine = OpenDSSEngine(generate_master_dss(network, str(tmp_path)), network)

    # A modest LV load downstream of the transformer.
    engine.update_load(3, 120.0, 40.0)
    assert engine.solve() is True

    voltages = engine.get_bus_voltages_pu()
    # Per-unit voltages are referenced to each bus's own base, so the 0.4 kV LV
    # bus sits near 1.0 pu — proving it is correctly based, not 11/0.4 = 27x off.
    assert abs(voltages[1] - 1.0) < 0.02
    assert all(0.9 < v < 1.05 for v in voltages.values())

    # The transformer branch reports a loading (read as a Transformer, not a Line).
    loadings = engine.get_branch_loadings_pct()
    assert set(loadings) == {1, 2}
    assert loadings[2] > 0.0


def test_balanced_load_power_is_total_not_per_phase(tmp_path, ieee33_network):
    """A balanced 3-phase load draws its stated total kW, not 3x it.

    Guards against positive-sequence mode, where OpenDSS reads a three-phase
    element's kW as a per-phase value and triples every load/PV/BESS/EV.
    """
    import opendssdirect as dss

    engine = OpenDSSEngine(generate_master_dss(ieee33_network, str(tmp_path)), ieee33_network)
    engine.update_load(2, 100.0, 0.0)
    assert engine.solve() is True
    dss.Circuit.SetActiveElement("Load.load_002")
    measured_kw = sum(dss.CktElement.Powers()[0::2])
    assert abs(measured_kw - 100.0) < 1.0   # ~100 kW, not ~300 kW


def test_engine_compiles_solves_and_reports(tmp_path, ieee33_network):
    master = generate_master_dss(ieee33_network, str(tmp_path))
    engine = OpenDSSEngine(master, ieee33_network)

    # Apply a load and solve.
    engine.update_load(2, 100.0, 60.0)
    assert engine.solve() is True

    voltages = engine.get_bus_voltages_pu()
    assert len(voltages) == 33
    # Slack bus held at ~1.0 pu; all buses in a sane range.
    assert abs(voltages[1] - 1.0) < 0.02
    assert all(0.5 < v < 1.5 for v in voltages.values())

    loadings = engine.get_branch_loadings_pct()
    assert len(loadings) == 32
    assert all(ld >= 0.0 for ld in loadings.values())

    assert engine.get_total_losses_kw() >= 0.0


def test_reset_restores_state(tmp_path, ieee33_network):
    master = generate_master_dss(ieee33_network, str(tmp_path))
    engine = OpenDSSEngine(master, ieee33_network)
    engine.update_load(2, 500.0, 300.0)
    engine.solve()
    engine.reset()
    # After reset all loads are back to zero -> near-flat profile.
    engine.solve()
    voltages = engine.get_bus_voltages_pu()
    assert all(v > 0.98 for v in voltages.values())
