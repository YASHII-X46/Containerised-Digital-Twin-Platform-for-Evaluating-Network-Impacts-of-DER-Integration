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

import math
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
             "TextVal", "VoltLevel_ID", "Flag_Type", "Un", "Group_ID",
             "Uul", "Ull", "Flag_Pos", "lat", "lon"],
    "Element": ["Element_ID", "Variant_ID", "Flag_Variant", "Type", "Name",
                "ShortName", "Description", "TextVal", "VoltLevel_ID",
                "Flag_State", "Flag_Input", "Group_ID"],
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
    "NetworkGroup": ["Group_ID", "Variant_ID", "Flag_Variant", "Name",
                     "ShortName"],
    "DCInfeeder": ["Element_ID", "Variant_ID", "Flag_Variant", "Flag_DCtyp",
                   "Flag_Lf", "Flag_Connect", "Sn_Inverter", "Ur_Inverter",
                   "Eta_Inverter", "P", "Q", "fP", "fQ", "Pmax", "Pmin",
                   "Flag_LfLimit", "EnergyStorage_ID"],
    "GraphicText": ["GraphicText_ID", "Variant_ID", "Flag_Variant",
                    "GraphicLayer_ID", "Font", "FontStyle", "FontSize",
                    "TextAlign", "TextOrient", "TextColor", "Visible",
                    "AdjustAngle", "Angle", "Pos1", "Pos2", "RowTextNo",
                    "AngleTermNo"],
    "GraphicBucklePoint": ["GraphicPoint_ID", "Variant_ID", "Flag_Variant",
                           "GraphicTerminal_ID", "NoPoint", "PosX", "PosY"],
    "GraphicLayer": ["GraphicLayer_ID"],
    "GraphicAreaTile": ["GraphicArea_ID", "Variant_ID", "Name", "AreaWidth",
                        "AreaHeight", "GridWidth", "GridHeight", "ScalePaper",
                        "ScaleReal", "Scale2", "Pos", "TileIndex"],
    "GraphicNode": ["GraphicNode_ID", "Variant_ID", "Flag_Variant",
                    "GraphicArea_ID", "GraphicLayer_ID", "GraphicType_ID",
                    "Node_ID", "GraphicText_ID1", "NodeStartX", "NodeStartY",
                    "NodeEndX", "NodeEndY", "SymType", "NodeSize"],
    "GraphicElement": ["GraphicElement_ID", "Variant_ID", "Flag_Variant",
                       "GraphicArea_ID", "GraphicLayer_ID", "GraphicType_ID",
                       "Element_ID", "GraphicText_ID1", "SymbolType",
                       "SymbolNo", "SymbolDef", "SymCenterX", "SymCenterY",
                       "SymbolSize"],
    "GraphicTerminal": ["GraphicTerminal_ID", "Variant_ID", "Flag_Variant",
                        "GraphicArea_ID", "GraphicElement_ID", "Terminal_ID",
                        "GraphicNode_ID", "PosX", "PosY", "SwtType"],
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
         "name": "supply transformer 11/0.4 kV 1250 kVA",
         "label": "TX main 1250 kVA",
         "r_ohm": 1.02, "x_ohm": 5.23, "rating_kva": 1250.0,
         "connection": "delta_wye", "tap": 1.0375},
        {"branch_id": 2, "from_bus": 2, "to_bus": 3,
         "name": "board B submain, 4c 35 mm2 Cu XLPE",
         "label": "Board B submain",
         "r_ohm": 0.02, "x_ohm": 0.01, "r0_ohm": 0.08, "x0_ohm": 0.03,
         "rating_kva": 242.5, "length_m": 40},
    ],
}


# The seeded sheet, in the centimetres GraphicAreaTile stores...
SHEET_W_CM, SHEET_H_CM = 42.0, 29.7
# ...and the metres the coordinates are actually in. Getting these two mixed up
# is what put the drawing a hundred times too far out to be on the page.
SHEET_W, SHEET_H = SHEET_W_CM / 100.0, SHEET_H_CM / 100.0

# A model whose three buses sit in a right angle: bus 1 to 2 is due north, bus 2
# to 3 due east, and the two spans are the same number of metres. A drawing that
# preserves geography must therefore place them the same distance apart on the
# page.
_LAT0, _DLAT = -37.8224, 0.0009
_DLON = _DLAT / math.cos(math.radians(abs(_LAT0 + _DLAT / 2)))
GEO_NETWORK = {
    "id": "geo", "name": "right angle", "base_voltage_kv": 11.0, "source_bus": 1,
    "buses": [
        {"bus_id": 1, "name": "grid supply point", "label": "Supply",
         "base_kv": 11.0, "role": "zone_substation", "group": "Supply",
         "base_load_kw": 0.0, "base_load_kvar": 0.0,
         "lat": _LAT0, "lon": 145.0380},
        {"bus_id": 2, "name": "lv main board", "label": "Main board",
         "base_kv": 0.4, "role": "customer_substation", "group": "Board",
         "base_load_kw": 300.0, "base_load_kvar": 100.0,
         "lat": _LAT0 + _DLAT, "lon": 145.0380},
        {"bus_id": 3, "name": "single phase board", "label": "Board B",
         "base_kv": 0.4, "role": "board", "group": "Board",
         "base_load_kw": 12.0, "base_load_kvar": 4.0, "phases": "b",
         "lat": _LAT0 + _DLAT, "lon": 145.0380 + _DLON},
    ],
    "branches": NETWORK["branches"],
}


def _fresh():
    """A stand-in project database, seeded the way SinDBCreate seeds one."""
    db = sqlite3.connect(":memory:")
    for table, cols in COLUMNS.items():
        db.execute("CREATE TABLE %s (%s)" % (table, ", ".join(cols)))
    db.execute("INSERT INTO GraphicLayer (GraphicLayer_ID) VALUES (1)")
    # SinDBCreate seeds one calculation-settings row on the symmetric default.
    db.execute("INSERT INTO CalcParameter (CalcParameter_ID, Variant_ID, "
               "Flag_LFmet, Flag_LFZ0, ITmax) VALUES (1, 1, 3, 1, 200)")
    # ...and one A3 sheet, in the drawing's own units.
    db.execute("INSERT INTO GraphicAreaTile (GraphicArea_ID, AreaWidth, "
               "AreaHeight) VALUES (1, %s, %s)" % (SHEET_W_CM, SHEET_H_CM))
    return db


def _drawn():
    """A fully drawn project, for the graphics tests."""
    db = _fresh()
    ss.export_network(db, GEO_NETWORK)
    return db


@pytest.fixture()
def conn():
    db = _fresh()
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
    load_element = one(conn, "Element", Type=ss.TYPE_LOAD, ShortName="load_3")
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
        conn, "Element", Type=ss.TYPE_LOAD, ShortName="load_2")["Element_ID"])
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


# --- the drawing ------------------------------------------------------------
#
# A project whose data is complete and solves can still open on an empty sheet.
# Two ways that happens, both of which these pin: nodes placed outside the sheet
# extent are not on the page at all, and node symbols with no GraphicElement or
# GraphicTerminal rows have nothing joining them.

def test_every_element_gets_a_symbol_on_the_drawing():
    """84 elements in the model, 84 symbols on the sheet."""
    db = _drawn()
    assert len(rows(db, "GraphicElement")) == len(rows(db, "Element"))


def test_every_terminal_ties_its_element_symbol_to_its_node_symbol():
    """This is what actually draws a connection; without it nothing joins up."""
    db = _drawn()
    gts = rows(db, "GraphicTerminal")
    assert len(gts) == len(rows(db, "Terminal"))
    node_ids = {g["GraphicNode_ID"] for g in rows(db, "GraphicNode")}
    elem_ids = {g["GraphicElement_ID"] for g in rows(db, "GraphicElement")}
    for gt in gts:
        assert gt["GraphicNode_ID"] in node_ids
        assert gt["GraphicElement_ID"] in elem_ids


def test_each_kind_of_element_gets_its_own_symbol():
    db = _drawn()
    by_symbol = {}
    for g in rows(db, "GraphicElement"):
        by_symbol.setdefault(g["SymbolType"], 0)
        by_symbol[g["SymbolType"]] += 1
    assert by_symbol[ss.SYMBOL_INFEEDER] == 1
    assert by_symbol[ss.SYMBOL_TRANSFORMER] == 1
    assert by_symbol[ss.SYMBOL_LINE] == 1
    assert by_symbol[ss.SYMBOL_LOAD] == 2


def test_nothing_is_drawn_outside_its_sheet():
    """The whole reason a populated project can look empty.

    The bound is each sheet's own extent in METRES, the unit coordinates are in,
    not the centimetres the sheet size is stored in. A hundred times looser and
    this would pass on a drawing nowhere near the page.
    """
    db = _drawn()
    for area in rows(db, "GraphicAreaTile"):
        w = area["AreaWidth"] / 100.0
        h = area["AreaHeight"] / 100.0
        aid = area["GraphicArea_ID"]
        for g in rows(db, "GraphicNode"):
            if g["GraphicArea_ID"] != aid:
                continue
            assert 0 <= g["NodeStartX"] <= w and 0 <= g["NodeEndX"] <= w, g
            assert 0 <= g["NodeStartY"] <= h, g
        for g in rows(db, "GraphicElement"):
            if g["GraphicArea_ID"] != aid:
                continue
            assert 0 <= g["SymCenterX"] <= w, g
            assert 0 <= g["SymCenterY"] <= h, g


def test_the_drawing_fills_a_useful_part_of_the_page():
    """A layout a hundred times too small is as unreadable as one too big."""
    db = _drawn()
    for area in rows(db, "GraphicAreaTile"):
        aid = area["GraphicArea_ID"]
        xs = [g["NodeStartX"] for g in rows(db, "GraphicNode")
              if g["GraphicArea_ID"] == aid]
        ys = [g["NodeStartY"] for g in rows(db, "GraphicNode")
              if g["GraphicArea_ID"] == aid]
        if len(xs) < 2:
            continue
        used = max(max(xs) - min(xs), max(ys) - min(ys))
        assert used > 0.15 * min(area["AreaWidth"] / 100.0,
                                 area["AreaHeight"] / 100.0)


def test_symbols_are_big_enough_to_see():
    """NodeSize counts 0.25 mm steps, so a value under 1 is sub-millimetre."""
    db = _drawn()
    for g in rows(db, "GraphicNode"):
        # A busbar carries its size in its span, so 0 is correct there.
        if g["SymType"] == ss.NODE_SYMBOL_CIRCLE:
            assert g["NodeSize"] >= 2.0, g
    assert all(g["SymbolSize"] >= 50 for g in rows(db, "GraphicElement"))


def test_a_point_node_does_not_span_a_distance():
    """Only a busbar has a start and an end apart; a point node is a point."""
    db = _drawn()
    for g in rows(db, "GraphicNode"):
        if g["SymType"] == ss.NODE_SYMBOL_CIRCLE:
            assert g["NodeStartX"] == g["NodeEndX"]
            assert g["NodeStartY"] == g["NodeEndY"]


def test_a_busbar_spans_far_enough_for_everything_that_lands_on_it():
    """A bar shorter than its feeders is a drawing that cannot be read."""
    db = _drawn()
    bars = [g for g in rows(db, "GraphicNode")
            if g["SymType"] == ss.NODE_SYMBOL_BUSBAR]
    assert bars, "the model has switchboards, so it should have busbars"
    for bar in bars:
        lo, hi = sorted((bar["NodeStartX"], bar["NodeEndX"]))
        landing = [t for t in rows(db, "GraphicTerminal")
                   if t["GraphicNode_ID"] == bar["GraphicNode_ID"]]
        for t in landing:
            assert lo - 1e-9 <= t["PosX"] <= hi + 1e-9, (bar, t)


def test_graphic_rows_name_a_sheet_that_exists():
    """A graphic row without a sheet is drawn nowhere."""
    db = _drawn()
    sheets = {a["GraphicArea_ID"] for a in rows(db, "GraphicAreaTile")}
    assert sheets
    for table in ("GraphicNode", "GraphicElement", "GraphicTerminal"):
        for g in rows(db, table):
            assert g["GraphicArea_ID"] in sheets, (table, g)


def test_every_carrier_points_at_a_text_row_that_exists():
    """With no text row SINCAL falls back to its own annotation block, which is
    the object's full name plus every parameter, and that is unreadable."""
    db = _drawn()
    ids = {t["GraphicText_ID"] for t in rows(db, "GraphicText")}
    assert ids
    for table in ("GraphicNode", "GraphicElement"):
        for g in rows(db, table):
            assert g["GraphicText_ID1"] in ids, (table, g)


def test_labels_are_one_font_at_two_sizes():
    db = _drawn()
    texts = rows(db, "GraphicText")
    assert {t["Font"] for t in texts} == {ss.LABEL_FONT}
    assert len({t["FontSize"] for t in texts}) <= 2
    assert all(t["Visible"] == 1 for t in texts)


def test_a_connection_is_never_a_diagonal():
    """Element symbol, then buckle points in order, then the attachment point.
    Every segment of that path has to be axis aligned."""
    db = _drawn()
    elements = {e["GraphicElement_ID"]: e for e in rows(db, "GraphicElement")}
    buckles = {}
    for b in rows(db, "GraphicBucklePoint"):
        buckles.setdefault(b["GraphicTerminal_ID"], []).append(b)
    for t in rows(db, "GraphicTerminal"):
        e = elements[t["GraphicElement_ID"]]
        path = [(e["SymCenterX"], e["SymCenterY"])]
        path += [(b["PosX"], b["PosY"]) for b in
                 sorted(buckles.get(t["GraphicTerminal_ID"], []),
                        key=lambda r: r["NoPoint"])]
        path.append((t["PosX"], t["PosY"]))
        for (x1, y1), (x2, y2) in zip(path, path[1:]):
            assert abs(x1 - x2) < 1e-9 or abs(y1 - y2) < 1e-9, (t, path)


def test_a_model_without_positions_still_draws():
    """The drawing is schematic, so it no longer needs a geographic position.

    The old geographic layout could not draw a network without lat and lon; a
    plain feeder with no survey data is exactly the case that has to work.
    """
    db = _fresh()
    plain = dict(NETWORK)
    plain["buses"] = [{k: v for k, v in b.items() if k not in ("lat", "lon")}
                      for b in NETWORK["buses"]]
    ss.export_network(db, plain)
    assert len(rows(db, "GraphicNode")) >= len(plain["buses"])
    assert len(rows(db, "GraphicElement")) == len(rows(db, "Element"))


def test_a_release_without_the_element_graphic_table_still_draws_nodes():
    db = _fresh()
    db.execute("DROP TABLE GraphicElement")
    ss.export_network(db, GEO_NETWORK)
    assert rows(db, "GraphicNode")


def test_exporting_twice_replaces_rather_than_duplicates():
    """The audit exports twice, balanced then unbalanced, into one project."""
    db = _fresh()
    ss.export_network(db, GEO_NETWORK)
    first = {t: len(rows(db, t)) for t in ss.WRITTEN_TABLES}
    ss.export_network(db, GEO_NETWORK, phases=True)
    assert {t: len(rows(db, t)) for t in ss.WRITTEN_TABLES
            if t != "NeutralPointImp"} == {t: n for t, n in first.items()
                                           if t != "NeutralPointImp"}


def test_a_re_export_switches_the_project_between_the_two_studies():
    db = _fresh()
    ss.export_network(db, GEO_NETWORK, phases=True)
    assert rows(db, "CalcParameter")[0]["Flag_LFmet"] == ss.LFMET_UNBALANCED_PHASES
    assert len(rows(db, "NeutralPointImp")) == 1
    ss.export_network(db, GEO_NETWORK, phases=False)
    assert rows(db, "CalcParameter")[0]["Flag_LFmet"] == ss.LFMET_ADMITTANCE
    assert rows(db, "NeutralPointImp") == []


def test_the_sheet_size_is_written_not_inherited():
    """The drawing sets its own page.

    The geographic layout used to scale itself to whatever page the project
    happened to carry. A schematic sizes the page to the drawing instead, so a
    project seeded at some other paper size is overwritten rather than obeyed.
    """
    db = _fresh()
    db.execute("UPDATE GraphicAreaTile SET AreaWidth = 9.8, AreaHeight = 9.6")
    ss.export_network(db, GEO_NETWORK)
    for area in rows(db, "GraphicAreaTile"):
        assert area["AreaWidth"] == pytest.approx(SHEET_W_CM)
        assert area["AreaHeight"] == pytest.approx(SHEET_H_CM)


# --- identity ---------------------------------------------------------------
#
# Three fields, three jobs. Doing all three with Name is what captioned the
# drawing with sentences and then truncated them at 50 characters.

def test_shortname_is_the_model_id_and_fits_its_column():
    db = _drawn()
    seen = set()
    for table in ("Node", "Element"):
        for r in rows(db, table):
            assert len(r["ShortName"]) <= 8, r
            assert r["ShortName"] not in seen, ("duplicate short name", r)
            seen.add(r["ShortName"])
    for r in rows(db, "Node"):
        assert r["ShortName"] == "bus_%d" % r["Node_ID"]


def test_name_is_a_drawable_label_not_a_sentence():
    db = _drawn()
    for table in ("Node", "Element"):
        for r in rows(db, table):
            assert len(r["Name"]) <= ss.LABEL_CHARS, r


def test_no_name_repeats_a_word_consecutively():
    """This is what produced ATC ATC101 and Sports Sports aquatic."""
    db = _drawn()
    for table in ("Node", "Element"):
        for r in rows(db, table):
            words = r["Name"].split()
            assert all(a.lower() != b.lower()
                       for a, b in zip(words, words[1:])), r


def test_no_name_contains_its_own_shortname():
    db = _drawn()
    for table in ("Node", "Element"):
        for r in rows(db, table):
            assert r["ShortName"] not in r["Name"], r


def test_the_full_description_is_kept_somewhere():
    """The label is short by design, so nothing may be lost in making it."""
    db = _drawn()
    for r in rows(db, "Node"):
        assert r["TextVal"], r
    for r in rows(db, "Element"):
        assert r["TextVal"] or r["Description"], r


# --- typing, grouping, limits ----------------------------------------------

def test_a_node_is_typed_by_the_role_the_model_gives_it():
    db = _drawn()
    by_id = {int(b["bus_id"]): b for b in GEO_NETWORK["buses"]}
    for r in rows(db, "Node"):
        role = by_id[r["Node_ID"]].get("role")
        assert r["Flag_Type"] == ss.ROLE_NODE_TYPE.get(role, ss.NODE_TYPE_NODE)
        assert r["Flag_Type"] != 0, "0 is not a member of the node type set"


def test_every_node_carries_voltage_limits():
    """Without them SINCAL cannot flag a voltage violation by itself."""
    db = _drawn()
    for r in rows(db, "Node"):
        assert r["Uul"] == ss.VOLTAGE_UPPER_LIMIT_PCT
        assert r["Ull"] == ss.VOLTAGE_LOWER_LIMIT_PCT


def test_the_model_grouping_is_carried_into_network_areas():
    db = _drawn()
    groups = {g["Name"]: g["Group_ID"] for g in rows(db, "NetworkGroup")}
    assert set(groups) == {b.get("group") for b in GEO_NETWORK["buses"]}
    for r in rows(db, "Node"):
        assert r["Group_ID"] in groups.values()


def test_a_model_with_no_grouping_falls_back_to_one_area():
    db = _fresh()
    plain = dict(GEO_NETWORK)
    plain["buses"] = [{k: v for k, v in b.items() if k != "group"}
                      for b in GEO_NETWORK["buses"]]
    ss.export_network(db, plain)
    assert all(r["Group_ID"] == ss.GROUP_ID for r in rows(db, "Node"))


# --- the DER ----------------------------------------------------------------

DER_NETWORK = dict(GEO_NETWORK)
DER_NETWORK["buses"] = [
    dict(b, der={"pv_kwp": 32.76, "inverter_kva": 30.0, "unit_count": 3,
                 "bess_kwh": 30.6, "bess_kw": 15.36})
    if b["bus_id"] == 3 else b
    for b in GEO_NETWORK["buses"]]


def _with_der():
    db = _fresh()
    ss.export_network(db, DER_NETWORK)
    return db


def test_the_der_reaches_sincal_at_all():
    """The project is about DER integration, and the DER used to be absent
    from the SINCAL description of the network entirely."""
    db = _with_der()
    assert len(rows(db, "DCInfeeder")) == 1
    assert one(db, "Element", Type=ss.TYPE_DCINFEEDER)["ShortName"] == "der_3"


def test_the_der_is_one_element_because_the_inverters_are_hybrid():
    """Sungrow SH10RT Hybrid inverters put the array and the battery on one
    shared DC bus behind a single AC connection (DCH5 report, Table 1). Two
    elements would imply two AC interfaces and would let the plant export
    twice its inverter rating."""
    db = _with_der()
    assert len(rows(db, "DCInfeeder")) == 1


def test_the_der_is_rated_at_its_inverter_not_its_panels():
    """32.76 kWp behind a 30 kVA inverter is a DC to AC ratio of 1.09, which
    is ordinary practice. The inverter clips, so the network never sees more
    than 30 kW however sunny it is."""
    db = _with_der()
    der = rows(db, "DCInfeeder")[0]
    assert der["Sn_Inverter"] == pytest.approx(0.030)
    assert der["Pmax"] == pytest.approx(0.030)


def test_the_battery_can_absorb_as_well_as_deliver():
    """A battery modelled as a generator that never charges is wrong."""
    db = _with_der()
    der = rows(db, "DCInfeeder")[0]
    assert der["Pmin"] == pytest.approx(-0.01536)
    assert der["Pmin"] < 0 < der["Pmax"]


def test_the_der_ships_out_of_service():
    """So the acceptance baseline is the same network with and without it."""
    db = _with_der()
    der = rows(db, "DCInfeeder")[0]
    assert der["P"] == 0.0 and der["Q"] == 0.0


def test_the_der_is_a_plain_load_flow_injection():
    db = _with_der()
    der = rows(db, "DCInfeeder")[0]
    assert der["Flag_Lf"] == ss.DC_LF_PQ == 1
    assert der["Flag_DCtyp"] == ss.DC_TYPE_PHOTOVOLTAIC == 7
    assert der["Flag_Connect"] == ss.DC_CONNECT_DIRECT


def test_the_battery_has_no_state_of_charge_and_that_is_deliberate():
    """CheckLicense EL ESP returns 6 on this licence, so the energy storage
    module is unavailable. The battery is a signed injection and nothing more,
    and its energy lives in the CIM and in the scenario preset."""
    db = _with_der()
    assert rows(db, "DCInfeeder")[0]["EnergyStorage_ID"] == 0


def test_the_der_gets_a_converter_symbol_of_its_own():
    db = _with_der()
    element = one(db, "Element", Type=ss.TYPE_DCINFEEDER)
    drawn = [g for g in rows(db, "GraphicElement")
             if g["Element_ID"] == element["Element_ID"]]
    assert drawn
    assert all(g["SymbolType"] == ss.SYMBOL_DCINFEEDER == 193 for g in drawn)


def test_a_model_without_der_writes_none():
    assert rows(_drawn(), "DCInfeeder") == []


# --- sheets -----------------------------------------------------------------

def test_every_element_is_drawn_exactly_once_across_the_sheets():
    db = _drawn()
    drawn = [g["Element_ID"] for g in rows(db, "GraphicElement")]
    assert sorted(drawn) == sorted(r["Element_ID"] for r in rows(db, "Element"))


def test_every_terminal_is_drawn_exactly_once_across_the_sheets():
    db = _drawn()
    drawn = [g["Terminal_ID"] for g in rows(db, "GraphicTerminal")]
    assert sorted(drawn) == sorted(
        r["Terminal_ID"] for r in rows(db, "Terminal"))


def test_an_element_has_both_its_terminals_on_its_own_sheet():
    """A run that leaves the page is a drawing nobody can follow."""
    db = _drawn()
    area_of = {g["GraphicElement_ID"]: g["GraphicArea_ID"]
               for g in rows(db, "GraphicElement")}
    for t in rows(db, "GraphicTerminal"):
        assert t["GraphicArea_ID"] == area_of[t["GraphicElement_ID"]], t


# --- determinism ------------------------------------------------------------

def test_two_builds_of_one_model_are_identical():
    """Ids are derived from the model, so nothing depends on iteration order."""
    def snapshot():
        db = _fresh()
        ss.export_network(db, DER_NETWORK)
        out = []
        for table in ss.WRITTEN_TABLES:
            try:
                out.append((table, rows(db, table)))
            except sqlite3.Error:
                pass
        db.close()
        return out

    assert snapshot() == snapshot()
