"""Tests for the SINCAL database mapping.

These run without PSS SINCAL: the mapping is exercised against a stand-in
SQLite database carrying the same table and column names the real schema has,
so the column choices and the flag values are pinned even on a machine with no
licence. What cannot be checked here is that SINCAL then *solves* the project;
that is what ``sample-networks/audit_models.py`` does against a real install.

The flag values are the whole point. SINCAL loads a project happily and then
declines to solve it when ``Element.Flag_Input`` is missing the power-flow bit
or ``Infeeder.Flag_Lf`` names a current source instead of a voltage source, and
neither mistake produces a parse error anywhere. See SINCAL-SCHEMA-NOTES.md.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sincal_solver import sincal_schema as ss

# Columns the mapping writes, per table. A stand-in database only needs these:
# ``export_network`` filters every write against the columns actually present,
# so a narrower table is exactly what a different SINCAL release looks like.
COLUMNS = {
    "VoltageLevel": ["VoltLevel_ID", "Variant_ID", "Flag_Variant", "Name",
                     "ShortName", "Un", "Uop", "f"],
    "Node": ["Node_ID", "Variant_ID", "Flag_Variant", "Name", "ShortName",
             "VoltLevel_ID", "Flag_Type", "Un", "Group_ID", "Flag_Pos",
             "lat", "lon"],
    "Element": ["Element_ID", "Variant_ID", "Flag_Variant", "Type", "Name",
                "ShortName", "VoltLevel_ID", "Flag_State", "Flag_Input",
                "Group_ID"],
    "Terminal": ["Terminal_ID", "Variant_ID", "Flag_Variant", "Element_ID",
                 "Node_ID", "TerminalNo", "Flag_State", "Flag_Switch",
                 "Flag_Terminal"],
    "Line": ["Element_ID", "Variant_ID", "Flag_Variant", "Un", "l", "fn",
             "r", "x", "r0", "x0", "c", "c0", "Flag_LineTyp", "Ith"],
    "TwoWindingTransformer": [
        "Element_ID", "Variant_ID", "Flag_Variant", "Sn", "Un1", "Un2", "uk",
        "ur", "VecGrp", "Flag_Lf", "Flag_ur", "Flag_ConNode", "roh", "rohl",
        "rohu", "ukr", "Flag_Z0_Input", "R0_R1", "X0_X1", "i0", "Vfe",
        "Pvlk", "Smax", "Stp_ID2"],
    "Load": ["Element_ID", "Variant_ID", "Flag_Variant", "P", "Q", "S",
             "cosphi", "Flag_Load", "Flag_LoadType", "Flag_Lf", "Ul"],
    "Infeeder": ["Element_ID", "Variant_ID", "Flag_Variant", "Flag_Typ",
                 "Flag_Lf", "u", "delta", "phi", "Sk2", "Sk2max", "Sk2min",
                 "R_X", "R_Xmax", "R_Xmin", "cmax", "cmin", "cact", "Ug"],
    "NeutralPointImp": ["Stp_ID", "Variant_ID", "Flag_Variant", "Name",
                        "Flag_Type", "Element_ID", "RE", "XE", "Flag_Ground",
                        "RG", "XG"],
    "CalcParameter": ["CalcParameter_ID", "Variant_ID", "Flag_LFmet",
                      "Flag_LFZ0", "ITmax"],
    "GraphicLayer": ["GraphicLayer_ID"],
    "GraphicNode": ["GraphicNode_ID", "Variant_ID", "Flag_Variant",
                    "GraphicLayer_ID", "Node_ID", "NodeStartX", "NodeStartY",
                    "NodeEndX", "NodeEndY", "SymType", "NodeSize"],
}

NETWORK = {
    "id": "t3", "name": "three-bus", "base_voltage_kv": 11.0, "source_bus": 1,
    "buses": [
        {"bus_id": 1, "name": "src", "base_kv": 11.0,
         "base_load_kw": 0.0, "base_load_kvar": 0.0,
         "lat": -37.8224, "lon": 145.0380},
        {"bus_id": 2, "name": "lv board", "base_kv": 0.4,
         "base_load_kw": 300.0, "base_load_kvar": 100.0},
        {"bus_id": 3, "name": "single phase board", "base_kv": 0.4,
         "base_load_kw": 12.0, "base_load_kvar": 4.0, "phases": "b"},
    ],
    "branches": [
        {"branch_id": 1, "from_bus": 1, "to_bus": 2, "is_transformer": True,
         "r_ohm": 1.02, "x_ohm": 5.23, "rating_kva": 1250.0,
         "connection": "delta_wye", "tap": 1.0375},
        {"branch_id": 2, "from_bus": 2, "to_bus": 3, "r_ohm": 0.02,
         "x_ohm": 0.01, "r0_ohm": 0.08, "x0_ohm": 0.03,
         "rating_kva": 242.5, "length_m": 40},
    ],
}


@pytest.fixture()
def conn():
    db = sqlite3.connect(":memory:")
    for table, cols in COLUMNS.items():
        db.execute("CREATE TABLE %s (%s)" % (table, ", ".join(cols)))
    db.execute("INSERT INTO GraphicLayer (GraphicLayer_ID) VALUES (1)")
    # SinDBCreate seeds one calculation-settings row on the symmetric default.
    db.execute("INSERT INTO CalcParameter (CalcParameter_ID, Variant_ID, "
               "Flag_LFmet, Flag_LFZ0, ITmax) VALUES (1, 1, 3, 1, 200)")
    yield db
    db.close()


def rows(conn, table):
    cur = conn.execute("SELECT * FROM %s" % table)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def one(conn, table, **where):
    for r in rows(conn, table):
        if all(r[k] == v for k, v in where.items()):
            return r
    raise AssertionError("no %s row matching %s" % (table, where))


# --- schema discovery -------------------------------------------------------

def test_discover_names_the_missing_table_rather_than_failing_obscurely():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE Node (Node_ID)")
    with pytest.raises(ss.SincalSchemaError) as exc:
        ss.discover(db)
    assert "VoltageLevel" in str(exc.value)
    db.close()


def test_a_column_the_release_lacks_is_dropped_not_written(conn):
    """A narrower schema must still take the model, minus what it cannot hold."""
    conn.execute("ALTER TABLE Line DROP COLUMN Ith")
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert "Ith" not in rows(conn, "Line")[0]
    assert len(rows(conn, "Line")) == 1


# --- the flags that decide whether SINCAL will solve at all -----------------

def test_flag_input_is_a_bit_mask_carrying_the_power_flow_bit(conn):
    """Bit 1 clear is why a fully populated project used to solve nothing."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    for row in rows(conn, "Element"):
        assert row["Flag_Input"] & ss.INPUT_POWER_FLOW, row["Name"]
        assert row["Flag_Input"] & ss.INPUT_SHORT_CIRCUIT, row["Name"]


def test_lines_and_transformers_claim_the_zero_sequence_bit(conn):
    """They write r0/x0 and the vector group, so the bit is honest."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    for etype in (ss.TYPE_LINE, ss.TYPE_TRANSFORMER):
        row = one(conn, "Element", Type=etype)
        assert row["Flag_Input"] & ss.INPUT_ZERO_SEQ


def test_loads_and_the_infeeder_do_not_claim_zero_sequence_data(conn):
    """Claiming a bit whose columns are unwritten makes SINCAL abandon the solve."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    for etype in (ss.TYPE_LOAD, ss.TYPE_INFEEDER):
        row = one(conn, "Element", Type=etype)
        assert not row["Flag_Input"] & ss.INPUT_ZERO_SEQ


def test_the_infeeder_is_a_voltage_source_so_the_network_has_a_slack(conn):
    """Flag_Lf 3 prescribes voltage and angle; 1 would be a current source."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    inf = rows(conn, "Infeeder")[0]
    assert inf["Flag_Lf"] == ss.INFEEDER_LF_SLACK == 3
    assert inf["u"] == 100.0
    assert inf["delta"] == 0.0


def test_the_infeeder_input_type_matches_the_columns_written(conn):
    """Flag_Typ 2 reads Sk2 and R_X, which is what the mapping supplies."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    inf = rows(conn, "Infeeder")[0]
    assert inf["Flag_Typ"] == ss.INFEEDER_TYP_SK == 2
    assert inf["Sk2"] and inf["R_X"]


def test_exactly_one_infeeder_and_it_sits_on_the_source_bus(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert len(rows(conn, "Infeeder")) == 1
    element_id = rows(conn, "Infeeder")[0]["Element_ID"]
    assert one(conn, "Terminal", Element_ID=element_id)["Node_ID"] == 1


def test_terminals_are_not_marked_as_separation_points(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert all(t["Flag_Switch"] == 0 for t in rows(conn, "Terminal"))
    assert all(t["Flag_State"] == 1 for t in rows(conn, "Terminal"))


def test_node_type_is_a_defined_value(conn):
    """0 is not in SINCAL's node-type enumeration; 1 is a plain node."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert all(n["Flag_Type"] == ss.NODE_TYPE_NODE == 1 for n in rows(conn, "Node"))


# --- the transformer --------------------------------------------------------

def test_the_off_load_tap_is_carried_in_the_rated_secondary_voltage(conn):
    """SINCAL's roh is a tap *position*, so a ratio written there does nothing."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    tx = rows(conn, "TwoWindingTransformer")[0]
    assert tx["Un1"] == pytest.approx(11.0)
    assert tx["Un2"] == pytest.approx(0.4 * 1.0375)
    assert tx["roh"] == 0.0


def test_a_unity_tap_leaves_the_rated_secondary_at_nominal(conn):
    net = dict(NETWORK)
    net["branches"] = [dict(NETWORK["branches"][0], tap=1.0),
                       NETWORK["branches"][1]]
    ss.export_network(conn, net, with_graphics=False)
    assert rows(conn, "TwoWindingTransformer")[0]["Un2"] == pytest.approx(0.4)


def test_the_vector_group_is_the_enumeration_member_not_the_clock_number(conn):
    """VecGrp 11 is DZ1; DYN11 is 59."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert rows(conn, "TwoWindingTransformer")[0]["VecGrp"] == ss.VECGRP_DYN11 == 59


def test_a_wye_wye_transformer_takes_the_earthed_star_group(conn):
    net = dict(NETWORK)
    net["branches"] = [dict(NETWORK["branches"][0], connection="wye_wye"),
                       NETWORK["branches"][1]]
    ss.export_network(conn, net, with_graphics=False)
    assert rows(conn, "TwoWindingTransformer")[0]["VecGrp"] == ss.VECGRP_YNYN0


def test_short_circuit_voltage_matches_the_branch_impedance(conn):
    """uk is |Z| on the transformer's own base, in percent."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    tx = rows(conn, "TwoWindingTransformer")[0]
    zbase = 11.0 ** 2 / 1.25
    assert tx["uk"] == pytest.approx((1.02 ** 2 + 5.23 ** 2) ** 0.5 / zbase * 100, rel=1e-6)
    assert tx["ur"] == pytest.approx(1.02 / zbase * 100, rel=1e-6)


# --- lines ------------------------------------------------------------------

def test_line_impedance_is_per_kilometre_over_the_real_route_length(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    line = rows(conn, "Line")[0]
    assert line["l"] == pytest.approx(0.040)
    assert line["r"] * line["l"] == pytest.approx(0.02, rel=1e-5)
    assert line["r0"] * line["l"] == pytest.approx(0.08, rel=1e-5)


def test_a_line_with_no_length_gets_a_nominal_one_metre(conn):
    """Zero length would divide by zero; the product still returns the ohms."""
    net = dict(NETWORK)
    net["branches"] = [NETWORK["branches"][0],
                       {k: v for k, v in NETWORK["branches"][1].items()
                        if k != "length_m"}]
    ss.export_network(conn, net, with_graphics=False)
    line = rows(conn, "Line")[0]
    assert line["l"] == pytest.approx(0.001)
    assert line["r"] * line["l"] == pytest.approx(0.02, rel=1e-5)


def test_the_thermal_limit_current_comes_from_the_rating(conn):
    """Without Ith every line reports zero utilisation."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    expected = 242.5 / (3 ** 0.5 * 0.4) / 1000.0        # kA at 0.4 kV
    assert rows(conn, "Line")[0]["Ith"] == pytest.approx(expected, rel=1e-6)


# --- loads ------------------------------------------------------------------

def test_loads_are_constant_power_not_constant_impedance(conn):
    """Flag_LoadType 1 would understate every voltage drop in the network."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    for load in rows(conn, "Load"):
        assert load["Flag_LoadType"] == ss.LOAD_TYPE_CONST_PQ == 2
        assert load["Flag_Lf"] == ss.LOAD_LF_PQ


def test_load_power_is_written_in_megawatts(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    load = one(conn, "Load", P=0.3)
    assert load["Q"] == pytest.approx(0.1)


def test_a_bus_with_no_load_gets_no_load_element(conn):
    """The source bus carries the infeeder and nothing else."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert len(rows(conn, "Load")) == 2


# --- phases -----------------------------------------------------------------

def test_terminal_phase_maps_a_declaration_to_a_connection_type():
    assert ss.terminal_phase("a") == 1
    assert ss.terminal_phase("b") == 2
    assert ss.terminal_phase("c") == 3
    assert ss.terminal_phase("abc") == ss.TERMINAL_THREE_PHASE
    assert ss.terminal_phase(None) == ss.TERMINAL_THREE_PHASE
    assert ss.terminal_phase([2]) == 2


def test_a_two_phase_declaration_falls_back_to_three_phase():
    """SINCAL has no L1+L3 connection type, so dropping a phase is not an option."""
    assert ss.terminal_phase("ac") == ss.TERMINAL_THREE_PHASE


def test_phases_are_not_written_by_default(conn):
    """The balanced project is the one SINCAL solves, so it is the default."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert all(t["Flag_Terminal"] == ss.TERMINAL_THREE_PHASE
               for t in rows(conn, "Terminal"))
    assert not rows(conn, "NeutralPointImp")


def test_phases_true_puts_the_declaration_on_the_load_terminal(conn):
    ss.export_network(conn, NETWORK, with_graphics=False, phases=True)
    load_element = one(conn, "Element", Type=ss.TYPE_LOAD, Name="load_bus_3")
    terminal = one(conn, "Terminal", Element_ID=load_element["Element_ID"])
    assert terminal["Flag_Terminal"] == 2                    # bus 3 declares "b"


def test_phases_true_leaves_plant_three_phase(conn):
    """The cable feeding a single-phase board is still three-phase plant."""
    ss.export_network(conn, NETWORK, with_graphics=False, phases=True)
    line = one(conn, "Element", Type=ss.TYPE_LINE)
    assert all(t["Flag_Terminal"] == ss.TERMINAL_THREE_PHASE
               for t in rows(conn, "Terminal")
               if t["Element_ID"] == line["Element_ID"])


def test_phases_true_earths_every_transformer_star_point(conn):
    """A single-phase load needs a zero-sequence return path to exist."""
    ss.export_network(conn, NETWORK, with_graphics=False, phases=True)
    npi = rows(conn, "NeutralPointImp")
    assert len(npi) == 1
    assert npi[0]["RE"] == ss.MEN_EARTH_OHM > 0
    assert one(conn, "TwoWindingTransformer")["Stp_ID2"] == npi[0]["Stp_ID"]


# --- per-step updates -------------------------------------------------------

def test_element_states_net_several_operations_onto_one_bus(conn):
    """PV and battery discharge are negative load; charging and demand positive."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    applied = ss.write_element_states(conn, [
        {"op": "load", "bus_id": 2, "kw": 300.0, "kvar": 100.0},
        {"op": "pv", "bus_id": 2, "kw": 50.0},
        {"op": "ev", "bus_id": 2, "kw": 7.0},
    ])
    assert applied == 1
    load = one(conn, "Load", Element_ID=one(
        conn, "Element", Type=ss.TYPE_LOAD, Name="load_bus_2")["Element_ID"])
    assert load["P"] == pytest.approx((300.0 - 50.0 + 7.0) / 1000.0)
    assert load["Q"] == pytest.approx(0.1)


def test_an_update_for_a_bus_with_no_load_element_is_skipped(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert ss.write_element_states(conn, [
        {"op": "load", "bus_id": 1, "kw": 5.0, "kvar": 1.0}]) == 0


# --- result readback --------------------------------------------------------

def test_result_readers_return_empty_when_the_project_has_not_solved(conn):
    """No result tables yet is the normal state, not an error."""
    assert ss.read_node_voltages(conn) == {}
    assert ss.read_branch_loadings(conn) == {}
    assert ss.read_power_summary(conn) == {"losses_kw": 0.0, "total_kw": 0.0}


def test_node_voltages_are_converted_from_percent_to_per_unit(conn):
    conn.execute("CREATE TABLE LFNodeResult (Node_ID, U_Un)")
    conn.executemany("INSERT INTO LFNodeResult VALUES (?, ?)",
                     [(1, 100.0), (2, 96.41), (3, None)])
    volts = ss.read_node_voltages(conn)
    assert volts == {1: 1.0, 2: pytest.approx(0.9641)}


def test_branch_loadings_take_the_worst_terminal_of_each_element(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    conn.execute("CREATE TABLE LFBranchResult (Terminal1_ID, S_Sn)")
    conn.executemany("INSERT INTO LFBranchResult VALUES (?, ?)",
                     [(1, 78.2), (2, 79.9), (3, 41.0), (4, 40.6)])
    loadings = ss.read_branch_loadings(conn)
    assert loadings[1] == pytest.approx(79.9)
    assert loadings[2] == pytest.approx(41.0)


def test_branch_loadings_fill_in_zero_for_a_branch_with_no_result(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    conn.execute("CREATE TABLE LFBranchResult (Terminal1_ID, S_Sn)")
    conn.execute("INSERT INTO LFBranchResult VALUES (1, 78.2)")
    assert ss.read_branch_loadings(conn, [1, 2]) == {1: pytest.approx(78.2), 2: 0.0}


def test_power_summary_sums_the_loss_terms_and_converts_to_kilowatts(conn):
    conn.execute("CREATE TABLE LFPowDataResult (Plline, Pltrans, Plfe, Pload)")
    conn.execute("INSERT INTO LFPowDataResult VALUES (0.06081, 0.038496, 0.0, 4.2314)")
    summary = ss.read_power_summary(conn)
    assert summary["losses_kw"] == pytest.approx(99.306, abs=1e-3)
    assert summary["total_kw"] == pytest.approx(4231.4)


# --- the vendoring guarantee ------------------------------------------------

def test_the_vendored_copy_is_byte_identical_to_the_generators():
    """One mapping, two callers. A drift here is a silent model difference.

    The generator in sample-networks and this service each carry a copy, the
    way every service in this repository carries its own ``bus/``. The copies
    are only safe while they are the same file.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    theirs = os.path.join(repo, "sample-networks", "sincal_schema.py")
    if not os.path.isfile(theirs):
        pytest.skip("sample-networks is not present in this checkout")
    mine = os.path.join(repo, "sincal-solver", "sincal_solver", "sincal_schema.py")
    with open(mine, "rb") as f:
        a = f.read()
    with open(theirs, "rb") as f:
        b = f.read()
    assert a == b, ("sincal_schema.py has drifted between sample-networks and "
                    "sincal-solver; copy one over the other.")


# --- calculation settings ---------------------------------------------------

def test_a_balanced_export_asks_for_the_symmetric_procedure(conn):
    ss.export_network(conn, NETWORK, with_graphics=False)
    row = rows(conn, "CalcParameter")[0]
    assert row["Flag_LFmet"] == ss.LFMET_ADMITTANCE == 3


def test_an_unbalanced_export_switches_the_power_flow_procedure(conn):
    """Flag_LFmet is what selects it; Simulation.Start("ULF") does not."""
    ss.export_network(conn, NETWORK, with_graphics=False, phases=True)
    row = rows(conn, "CalcParameter")[0]
    assert row["Flag_LFmet"] == ss.LFMET_UNBALANCED_PHASES == 8


def test_an_unbalanced_export_builds_the_fourth_conductor(conn):
    """Without a zero-sequence network a single-phase load cannot solve."""
    ss.export_network(conn, NETWORK, with_graphics=False, phases=True)
    assert rows(conn, "CalcParameter")[0]["Flag_LFZ0"] == ss.LFZ0_NEUTRAL_LIKE_PHASE


def test_a_balanced_export_leaves_the_zero_sequence_mode_alone(conn):
    """It has no bearing on a symmetric solve, so it is not touched."""
    ss.export_network(conn, NETWORK, with_graphics=False)
    assert rows(conn, "CalcParameter")[0]["Flag_LFZ0"] == 1


def test_calculation_settings_are_skipped_when_the_table_is_absent(conn):
    """An older release without CalcParameter must still take the model."""
    conn.execute("DROP TABLE CalcParameter")
    counts = ss.export_network(conn, NETWORK, with_graphics=False, phases=True)
    assert counts["Node"] == 3


# --- unbalanced result readback ---------------------------------------------

def _seed_ulf(conn, u1, u2, u3, p1=0.0, p2=-120.0, p3=120.0, node=2):
    conn.execute("CREATE TABLE IF NOT EXISTS ULFNodeResult "
                 "(Node_ID, Flag_Phase, U1, U2, U3, U1_Un, U2_Un, U3_Un, "
                 "phi1, phi2, phi3)")
    conn.execute("INSERT INTO ULFNodeResult VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (node, 7, u1, u2, u3, u1 / 0.231 * 100, u2 / 0.231 * 100,
                  u3 / 0.231 * 100, p1, p2, p3))


def test_phase_voltages_are_empty_after_a_symmetric_solve(conn):
    """A symmetric result has no per-phase voltages, and says so."""
    assert ss.read_phase_voltages(conn) == {}
    assert ss.read_max_vuf_pct(conn) == 0.0


def test_phase_voltages_are_read_from_the_unbalanced_table(conn):
    _seed_ulf(conn, 0.231, 0.229, 0.233)
    phases = ss.read_phase_voltages(conn)
    assert len(phases[2]) == 3
    assert phases[2][0] == pytest.approx(1.0, abs=1e-6)


def test_node_voltages_fall_back_to_the_unbalanced_table(conn):
    """A caller should not have to know which procedure ran."""
    _seed_ulf(conn, 0.231, 0.2287, 0.2333)
    volts = ss.read_node_voltages(conn)
    # The mean of the three phases, matching what the OpenDSS adapter reports.
    assert volts[2] == pytest.approx((1.0 + 0.99 + 1.01) / 3, abs=1e-4)


def test_the_symmetric_table_wins_when_both_are_present(conn):
    conn.execute("CREATE TABLE LFNodeResult (Node_ID, U_Un)")
    conn.execute("INSERT INTO LFNodeResult VALUES (2, 96.41)")
    _seed_ulf(conn, 0.231, 0.231, 0.231)
    assert ss.read_node_voltages(conn)[2] == pytest.approx(0.9641)


def test_a_balanced_set_of_phase_voltages_has_no_unbalance(conn):
    _seed_ulf(conn, 0.231, 0.231, 0.231)
    assert ss.read_max_vuf_pct(conn) == pytest.approx(0.0, abs=1e-9)


def test_vuf_is_the_negative_over_positive_sequence_ratio(conn):
    """A 1 percent dip on one phase gives a third of that as VUF."""
    _seed_ulf(conn, 0.231 * 0.99, 0.231, 0.231)
    # V2/V1 for a single-phase 1 percent depression is 0.01/3 of nominal.
    assert ss.read_max_vuf_pct(conn) == pytest.approx(100.0 / 3 * 0.01 / 0.99667,
                                                      rel=0.02)


def test_vuf_reports_the_worst_bus(conn):
    _seed_ulf(conn, 0.231, 0.231, 0.231, node=2)
    _seed_ulf(conn, 0.231 * 0.97, 0.231, 0.231, node=3)
    assert ss.read_max_vuf_pct(conn) > 0.9


def test_branch_loadings_fall_back_to_the_unbalanced_table(conn):
    """The unbalanced table reports per-conductor current, not S/Sn."""
    ss.export_network(conn, NETWORK, with_graphics=False, phases=True)
    conn.execute("CREATE TABLE ULFBranchResult "
                 "(Terminal1_ID, Flag_Phase, I1_In, I2_In, I3_In)")
    conn.executemany("INSERT INTO ULFBranchResult VALUES (?,?,?,?,?)",
                     [(1, 7, 70.0, 88.0, 71.0), (3, 7, 40.0, 41.0, 39.5)])
    loadings = ss.read_branch_loadings(conn)
    assert loadings[1] == pytest.approx(88.0)     # worst conductor wins
    assert loadings[2] == pytest.approx(41.0)


def test_power_summary_takes_the_whole_network_row(conn):
    """An unbalanced run writes one row per phase plus an L123 row."""
    conn.execute("CREATE TABLE LFPowDataResult "
                 "(Flag_Phase, Plline, Pltrans, Plfe, Pload)")
    conn.executemany("INSERT INTO LFPowDataResult VALUES (?,?,?,?,?)",
                     [(1, 0.02, 0.013, 0.0, 1.41), (2, 0.02, 0.013, 0.0, 1.41),
                      (3, 0.02, 0.013, 0.0, 1.41),
                      (7, 0.0608, 0.0385, 0.0, 4.2314)])
    summary = ss.read_power_summary(conn)
    assert summary["total_kw"] == pytest.approx(4231.4)
    assert summary["losses_kw"] == pytest.approx(99.3, abs=0.1)
