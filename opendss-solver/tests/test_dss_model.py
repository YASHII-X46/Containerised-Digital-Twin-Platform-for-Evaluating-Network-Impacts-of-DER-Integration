"""Tests for OpenDSS .dss generation (moved from the Simulation Engine —
model *validation* stays sim-side; DSS text generation is solver-side)."""

from dss_solver.dss_model import generate_all_ev_dss, generate_master_dss
from dss_solver.network import SolverNetwork, normalize_phases


def _tiny_network(net_id="tiny3") -> dict:
    return {
        "id": net_id,
        "name": "Tiny 3-bus",
        "base_voltage_kv": 11.0,
        "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
            {"bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0},
            {"bus_id": 3, "base_load_kw": 50.0, "base_load_kvar": 20.0},
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
            {"branch_id": 2, "from_bus": 2, "to_bus": 3, "r_ohm": 0.4, "x_ohm": 0.2, "rating_kva": 2000},
        ],
    }


def _multivoltage_network(net_id="mv_lv") -> dict:
    """11 kV feeder with an 11/0.4 kV transformer down to an LV bus."""
    return {
        "id": net_id,
        "name": "MV/LV feeder",
        "base_voltage_kv": 11.0,
        "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},        # 11 kV slack
            {"bus_id": 2, "base_load_kw": 0.0, "base_load_kvar": 0.0},        # 11 kV
            {"bus_id": 3, "base_load_kw": 80.0, "base_load_kvar": 30.0, "base_kv": 0.4},  # LV
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
            {"branch_id": 2, "from_bus": 2, "to_bus": 3, "is_transformer": True,
             "r_ohm": 0.02, "x_ohm": 0.12, "rating_kva": 500},
        ],
    }


def test_master_dss_emits_transformer_and_voltage_bases(tmp_path):
    model = SolverNetwork(_multivoltage_network())
    text = open(generate_master_dss(model, str(tmp_path))).read()
    # The transformer branch is a Transformer, the other is a Line.
    assert "New Transformer.xfmr_2" in text
    assert "kVs=[11.0000, 0.4000]" in text
    assert text.count("New Line.branch_") == 1
    # Both voltage levels are declared so OpenDSS bases each bus correctly.
    assert "Set VoltageBases=[0.4, 11.0]" in text
    # The LV load is created at 0.4 kV, not the 11 kV default.
    assert "New Load.load_003" in text and "kV=0.4000" in text


def test_zero_sequence_defaults_to_3x_positive(tmp_path):
    model = SolverNetwork(_tiny_network())
    text = open(generate_master_dss(model, str(tmp_path))).read()
    # Branch 1: r1=0.3, x1=0.15 -> default Z0 = 3x.
    assert "R1=0.3 X1=0.15 R0=0.8999999999999999" in text or "R0=0.9" in text
    assert "X0=0.44999999999999996" in text or "X0=0.45" in text


def test_explicit_zero_sequence_honoured(tmp_path):
    net = _tiny_network()
    net["branches"][0]["r0_ohm"] = 1.11
    net["branches"][0]["x0_ohm"] = 0.55
    text = open(generate_master_dss(SolverNetwork(net), str(tmp_path))).read()
    assert "R0=1.11 X0=0.55" in text


def test_delta_wye_connection_and_fixed_tap(tmp_path):
    net = _multivoltage_network()
    net["branches"][1]["connection"] = "delta_wye"
    net["branches"][1]["tap"] = 1.05
    text = open(generate_master_dss(SolverNetwork(net), str(tmp_path))).read()
    assert "Conns=[delta, wye]" in text
    assert "Taps=[1, 1.0500]" in text


def test_default_connection_stays_wye_wye(tmp_path):
    text = open(generate_master_dss(SolverNetwork(_multivoltage_network()), str(tmp_path))).read()
    assert "Conns=[wye, wye]" in text
    assert "Taps=" not in text


def test_oltc_emits_regcontrol(tmp_path):
    net = _multivoltage_network()
    net["branches"][1]["oltc"] = True
    text = open(generate_master_dss(SolverNetwork(net), str(tmp_path))).read()
    assert "New RegControl.reg_2 transformer=xfmr_2 winding=2" in text
    assert "vreg=120 band=2.4" in text
    # ptratio maps the 0.4 kV line-neutral voltage onto the 120 V regulator base.
    assert f"ptratio={0.4 * 1000 / (3 ** 0.5) / 120:.4f}" in text


def test_no_oltc_no_regcontrol(tmp_path):
    text = open(generate_master_dss(SolverNetwork(_multivoltage_network()), str(tmp_path))).read()
    assert "RegControl" not in text


def test_master_dss_is_generated_for_any_network(tmp_path):
    model = SolverNetwork(_tiny_network())
    path = generate_master_dss(model, str(tmp_path))
    text = open(path).read()
    assert "New Circuit.tiny3" in text
    assert text.count("New Line.branch_") == 2
    # Loads are created for every bus except the slack/substation bus.
    assert text.count("New Load.load_") == 2
    assert "basekv=11.0" in text


def test_balanced_mode_emits_symmetric_three_phase(tmp_path):
    model = SolverNetwork(_tiny_network())
    text = open(generate_master_dss(model, str(tmp_path), "balanced")).read()
    # Balanced studies use the full multi-phase model, NOT positive-sequence:
    # the latter reads a three-phase load's kW as per-phase and triples it.
    assert "Set CktModel=Multiphase" in text
    assert "Positive" not in text
    # Every load is a symmetric three-phase wye connection.
    assert text.count("New Load.load_") == 2
    assert "Phases=3 Conn=Wye" in text
    assert "bus_002." not in text  # no per-phase node spec in balanced mode


def test_unbalanced_mode_honours_single_phase_bus(tmp_path):
    net = _tiny_network()
    net["buses"][2]["phases"] = "a"  # bus 3 is a single-phase (phase a) lateral
    text = open(generate_master_dss(SolverNetwork(net), str(tmp_path), "unbalanced")).read()
    assert "Set CktModel=Multiphase" in text
    # Bus 2 stays three-phase; bus 3 is connected single-phase on node 1 (phase a).
    assert "New Load.load_002 Bus1=bus_002 Phases=3" in text
    assert "New Load.load_003 Bus1=bus_003.1 Phases=1" in text


def test_ev_is_single_phase_when_unbalanced(tmp_path):
    model = SolverNetwork(_tiny_network())
    ev = [{"bus_id": 3, "ev_charge_rate_kw": 7.0}]
    balanced = open(generate_all_ev_dss(ev, model, str(tmp_path), "balanced")).read()
    unbalanced = open(generate_all_ev_dss(ev, model, str(tmp_path), "unbalanced")).read()
    assert "Phases=3" in balanced              # aggregated symmetric load
    assert "ev_003 Bus1=bus_003." in unbalanced and "Phases=1" in unbalanced


def test_normalize_phases_and_phase_nodes():
    assert normalize_phases(None) == [1, 2, 3]
    assert normalize_phases("abc") == [1, 2, 3]
    assert normalize_phases("a") == [1]
    assert normalize_phases("ca") == [1, 3]
    assert normalize_phases([2, 3]) == [2, 3]
    assert normalize_phases("nonsense") == [1, 2, 3]  # falls back to three-phase

    net = _tiny_network()
    net["buses"][1]["phases"] = "b"
    model = SolverNetwork(net)
    assert model.phase_nodes(2) == [2]
    assert model.phase_nodes(3) == [1, 2, 3]
