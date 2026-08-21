"""Tests for the external network-model importers (PSS/E RAW/RAWX, CIM)."""

import json

import pytest

from app.network import importers
from app.network.importers import (
    NetworkImportError,
    available_formats,
    from_cim,
    from_dss,
    from_raw,
    from_rawx,
    parse_network,
    register_format,
    supported_formats,
)
from app.network.model import NetworkModel

RAWX = json.dumps({"network": {
    "caseid": {"fields": ["ic", "sbase", "rev"], "data": [0, 100.0, 35]},
    "bus": {"fields": ["ibus", "name", "baskv", "ide"],
            "data": [[1, "SUB", 12.66, 3], [2, "B2", 12.66, 1], [3, "B3", 12.66, 1]]},
    "load": {"fields": ["ibus", "loadid", "stat", "pl", "ql"],
             "data": [[2, "1", 1, 1.0, 0.4], [3, "1", 1, 0.5, 0.2]]},
    "acline": {"fields": ["ibus", "jbus", "ckt", "rpu", "xpu", "rate1"],
               "data": [[1, 2, "1", 0.01, 0.02, 5.0]]},
    "transformer": {"fields": ["ibus", "jbus", "kbus", "r1_2", "x1_2", "rate1_1"],
                    "data": [[2, 3, 0, 0.01, 0.03, 3.0]]},
}})

RAW = """0, 100.00, 35, 0, 1, 60.00
TITLE LINE 1
TITLE LINE 2
1, 'SUB', 12.66, 3, 1, 1, 1, 1.0, 0.0
2, 'B2', 12.66, 1, 1, 1, 1, 1.0, 0.0
3, 'B3', 12.66, 1, 1, 1, 1, 1.0, 0.0
0 / END OF BUS DATA, BEGIN LOAD DATA
2, '1', 1, 1, 1, 1.0, 0.4
3, '1', 1, 1, 1, 0.5, 0.2
0 / END OF LOAD DATA, BEGIN GENERATOR DATA
0 / END OF GENERATOR DATA, BEGIN BRANCH DATA
1, 2, '1', 0.01, 0.02, 0.0, 5.0
0 / END OF BRANCH DATA, BEGIN TRANSFORMER DATA
2, 3, 0, '1', 1, 1, 1, 0.0, 0.0, 1, 'XF', 1
0.01, 0.03, 100.0
1.0, 12.66, 0.0, 3.0
1.0, 12.66
0 / END OF TRANSFORMER DATA, BEGIN AREA DATA
0 / END OF AREA DATA
"""

CIM = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:cim="http://iec.ch/TC57/2013/CIM-schema-cim16#">
  <cim:BaseVoltage rdf:ID="bv1"><cim:BaseVoltage.nominalVoltage>12660</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage>
  <cim:ConnectivityNode rdf:ID="n1"/>
  <cim:ConnectivityNode rdf:ID="n2"/>
  <cim:ConnectivityNode rdf:ID="n3"/>
  <cim:ACLineSegment rdf:ID="l1"><cim:ACLineSegment.r>0.1</cim:ACLineSegment.r><cim:ACLineSegment.x>0.2</cim:ACLineSegment.x><cim:ConductingEquipment.BaseVoltage rdf:resource="#bv1"/></cim:ACLineSegment>
  <cim:ACLineSegment rdf:ID="l2"><cim:ACLineSegment.r>0.15</cim:ACLineSegment.r><cim:ACLineSegment.x>0.25</cim:ACLineSegment.x></cim:ACLineSegment>
  <cim:Terminal rdf:ID="t1"><cim:Terminal.ConductingEquipment rdf:resource="#l1"/><cim:Terminal.ConnectivityNode rdf:resource="#n1"/></cim:Terminal>
  <cim:Terminal rdf:ID="t2"><cim:Terminal.ConductingEquipment rdf:resource="#l1"/><cim:Terminal.ConnectivityNode rdf:resource="#n2"/></cim:Terminal>
  <cim:Terminal rdf:ID="t3"><cim:Terminal.ConductingEquipment rdf:resource="#l2"/><cim:Terminal.ConnectivityNode rdf:resource="#n2"/></cim:Terminal>
  <cim:Terminal rdf:ID="t4"><cim:Terminal.ConductingEquipment rdf:resource="#l2"/><cim:Terminal.ConnectivityNode rdf:resource="#n3"/></cim:Terminal>
  <cim:EnergyConsumer rdf:ID="ec1"><cim:EnergyConsumer.p>100000</cim:EnergyConsumer.p><cim:EnergyConsumer.q>40000</cim:EnergyConsumer.q></cim:EnergyConsumer>
  <cim:Terminal rdf:ID="t5"><cim:Terminal.ConductingEquipment rdf:resource="#ec1"/><cim:Terminal.ConnectivityNode rdf:resource="#n2"/></cim:Terminal>
</rdf:RDF>
"""


def _check_three_bus(model_dict):
    """Each importer should produce a valid 3-bus / 2-branch radial network."""
    model = NetworkModel.from_dict(model_dict)  # validates ids + connectivity
    assert model.num_buses == 3
    assert model.num_branches == 2
    assert model.source_bus == 1
    return model


def test_rawx_import():
    d = from_rawx(RAWX, "psse_case")
    m = _check_three_bus(d)
    assert m.base_voltage_kv == 12.66
    loads = {b["bus_id"]: b["base_load_kw"] for b in d["buses"]}
    assert loads[2] == 1000.0 and loads[3] == 500.0   # PL MW -> kW


def test_raw_import():
    d = from_raw(RAW, "psse_case")
    _check_three_bus(d)
    # ohm conversion: zbase = 12.66^2 / 100 = 1.602; line 1-2 r = 0.01 * zbase
    line = next(b for b in d["branches"] if b["from_bus"] == 1)
    assert abs(line["r_ohm"] - 0.01 * (12.66 ** 2 / 100)) < 1e-4


def test_cim_import():
    d = from_cim(CIM, "cim_case")
    m = _check_three_bus(d)
    # CIM impedances are already ohms.
    line = next(b for b in d["branches"] if b["from_bus"] == 1)
    assert line["r_ohm"] == 0.1 and line["x_ohm"] == 0.2
    loads = {b["bus_id"]: b["base_load_kw"] for b in d["buses"]}
    assert loads[2] == 100.0   # EnergyConsumer.p 100000 W -> 100 kW
    assert m.base_voltage_kv == 12.66


# PSS/E RAW v34+ dialect: "@!" label lines, "BEGIN/END OF <X> DATA" markers, a
# branch NAME field before the ratings, and a trailing "Q" record.
RAW_V36 = """@!IC, SBASE, REV, XFRRAT, NXFRAT, BASFRQ
0, 100.00, 36, 0, 1, 50.00
PSS/E 36.5 TEST CASE
SECOND TITLE LINE
@! Bus data
BEGIN BUS DATA
@!  I,'NAME',BASKV,IDE
1, 'SLACK', 12.66, 3, 1, 1, 1, 1.0, 0.0
2, 'B2', 12.66, 1, 1, 1, 1, 1.0, 0.0
3, 'B3', 0.400, 1, 1, 1, 1, 1.0, 0.0
END OF BUS DATA
BEGIN LOAD DATA
2, '1', 1, 1, 1, 1.000, 0.400, 0,0,0,0,1,1,0
3, '1', 1, 1, 1, 0.500, 0.200, 0,0,0,0,1,1,0
END OF LOAD DATA
BEGIN BRANCH DATA
1, 2, '1', 0.01, 0.02, 0.0, 'FEEDER A', 5.0, 5.0, 5.0
END OF BRANCH DATA
BEGIN TRANSFORMER DATA
2, 3, 0, '1', 1, 1, 1, 0.0, 0.0, 2, 'XF', 1, 1, 1.0
0.01, 0.03, 100.0
1.0, 12.66, 0.0, 3.0
1.0, 0.40
END OF TRANSFORMER DATA
Q
"""

# Standard CGMES (CIM100 namespace): a PowerTransformer with two ends each with
# its own terminal + ratedU, node voltages from container VoltageLevels, an
# SSH-style pfixed/qfixed load, and an ExternalNetworkInjection as the slack.
CGMES = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:cim="http://iec.ch/TC57/CIM100#">
  <cim:BaseVoltage rdf:ID="bvHV"><cim:BaseVoltage.nominalVoltage>11000</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage>
  <cim:BaseVoltage rdf:ID="bvLV"><cim:BaseVoltage.nominalVoltage>400</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage>
  <cim:VoltageLevel rdf:ID="vlHV"><cim:VoltageLevel.BaseVoltage rdf:resource="#bvHV"/></cim:VoltageLevel>
  <cim:VoltageLevel rdf:ID="vlLV"><cim:VoltageLevel.BaseVoltage rdf:resource="#bvLV"/></cim:VoltageLevel>
  <cim:ConnectivityNode rdf:ID="nHV"><cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#vlHV"/></cim:ConnectivityNode>
  <cim:ConnectivityNode rdf:ID="nLV"><cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#vlLV"/></cim:ConnectivityNode>
  <cim:ExternalNetworkInjection rdf:ID="grid"/>
  <cim:PowerTransformer rdf:ID="pt1"/>
  <cim:PowerTransformerEnd rdf:ID="end1"><cim:PowerTransformerEnd.PowerTransformer rdf:resource="#pt1"/><cim:TransformerEnd.endNumber>1</cim:TransformerEnd.endNumber><cim:PowerTransformerEnd.ratedU>11000</cim:PowerTransformerEnd.ratedU><cim:PowerTransformerEnd.r>1.21</cim:PowerTransformerEnd.r><cim:PowerTransformerEnd.x>6.05</cim:PowerTransformerEnd.x><cim:TransformerEnd.Terminal rdf:resource="#tEnd1"/></cim:PowerTransformerEnd>
  <cim:PowerTransformerEnd rdf:ID="end2"><cim:PowerTransformerEnd.PowerTransformer rdf:resource="#pt1"/><cim:TransformerEnd.endNumber>2</cim:TransformerEnd.endNumber><cim:PowerTransformerEnd.ratedU>400</cim:PowerTransformerEnd.ratedU><cim:PowerTransformerEnd.r>0</cim:PowerTransformerEnd.r><cim:PowerTransformerEnd.x>0</cim:PowerTransformerEnd.x><cim:TransformerEnd.Terminal rdf:resource="#tEnd2"/></cim:PowerTransformerEnd>
  <cim:Terminal rdf:ID="tEnd1"><cim:Terminal.ConnectivityNode rdf:resource="#nHV"/></cim:Terminal>
  <cim:Terminal rdf:ID="tEnd2"><cim:Terminal.ConnectivityNode rdf:resource="#nLV"/></cim:Terminal>
  <cim:Terminal rdf:ID="tInj"><cim:Terminal.ConductingEquipment rdf:resource="#grid"/><cim:Terminal.ConnectivityNode rdf:resource="#nHV"/></cim:Terminal>
  <cim:EnergyConsumer rdf:ID="ld"><cim:EnergyConsumer.pfixed>50000</cim:EnergyConsumer.pfixed><cim:EnergyConsumer.qfixed>16000</cim:EnergyConsumer.qfixed></cim:EnergyConsumer>
  <cim:Terminal rdf:ID="tLd"><cim:Terminal.ConductingEquipment rdf:resource="#ld"/><cim:Terminal.ConnectivityNode rdf:resource="#nLV"/></cim:Terminal>
</rdf:RDF>
"""


def test_raw_v36_import():
    """PSS/E v34/35/36 dialect (BEGIN/END markers, @! lines) parses like v33."""
    d = from_raw(RAW_V36, "psse36_case")
    m = _check_three_bus(d)
    assert m.source_bus == 1                       # IDE==3 slack
    assert m.voltage_levels() == [0.4, 12.66]      # per-bus base kV preserved
    loads = {b["bus_id"]: b["base_load_kw"] for b in d["buses"]}
    assert loads[2] == 1000.0 and loads[3] == 500.0
    xfmr = [b for b in d["branches"] if b["is_transformer"]]
    assert len(xfmr) == 1 and {xfmr[0]["from_bus"], xfmr[0]["to_bus"]} == {2, 3}


def test_cim_cgmes_standard_transformer():
    """Standard CGMES (CIM100 ns, two-end transformer, container kV, SSH load)."""
    d = from_cim(CGMES, "cgmes_case")
    m = NetworkModel.from_dict(d)
    assert m.num_buses == 2 and m.num_branches == 1
    assert m.voltage_levels() == [0.4, 11.0]       # from container VoltageLevels
    assert m.source_bus == 1                        # ExternalNetworkInjection bus
    br = d["branches"][0]
    assert br["is_transformer"] and br["r_ohm"] == 1.21 and br["x_ohm"] == 6.05
    loads = {b["bus_id"]: b["base_load_kw"] for b in d["buses"]}
    assert loads[2] == 50.0                          # pfixed 50000 W -> 50 kW


def test_cim_unit_aware_kv_and_mw():
    """A CGMES file expressed in kV (ratedU/nominalVoltage) and MW (loads) reads
    the same as the equivalent volts/watts file - the importer normalises units."""
    kv_mw = (CGMES
             .replace("<cim:BaseVoltage.nominalVoltage>11000</cim:BaseVoltage.nominalVoltage>",
                      "<cim:BaseVoltage.nominalVoltage>11</cim:BaseVoltage.nominalVoltage>")
             .replace("<cim:BaseVoltage.nominalVoltage>400</cim:BaseVoltage.nominalVoltage>",
                      "<cim:BaseVoltage.nominalVoltage>0.4</cim:BaseVoltage.nominalVoltage>")
             .replace("<cim:PowerTransformerEnd.ratedU>11000</cim:PowerTransformerEnd.ratedU>",
                      "<cim:PowerTransformerEnd.ratedU>11</cim:PowerTransformerEnd.ratedU>")
             .replace("<cim:PowerTransformerEnd.ratedU>400</cim:PowerTransformerEnd.ratedU>",
                      "<cim:PowerTransformerEnd.ratedU>0.4</cim:PowerTransformerEnd.ratedU>")
             .replace("<cim:EnergyConsumer.pfixed>50000</cim:EnergyConsumer.pfixed>",
                      "<cim:EnergyConsumer.pfixed>0.05</cim:EnergyConsumer.pfixed>")
             .replace("<cim:EnergyConsumer.qfixed>16000</cim:EnergyConsumer.qfixed>",
                      "<cim:EnergyConsumer.qfixed>0.016</cim:EnergyConsumer.qfixed>"))
    d = from_cim(kv_mw, "cgmes_kv_mw")
    m = NetworkModel.from_dict(d)
    assert m.voltage_levels() == [0.4, 11.0]         # kV read as kV
    br = d["branches"][0]
    assert br["is_transformer"] and br["r_ohm"] == 1.21 and br["x_ohm"] == 6.05
    loads = {b["bus_id"]: b["base_load_kw"] for b in d["buses"]}
    assert loads[2] == 50.0                          # 0.05 MW -> 50 kW


def test_cim_gl_positions():
    """GL-profile Location/PositionPoints become per-bus WGS84 lat/lon."""
    gl = CGMES.replace("</rdf:RDF>", """
  <cim:CoordinateSystem rdf:ID="crs"><cim:CoordinateSystem.crsUrn>urn:ogc:def:crs:EPSG::4326</cim:CoordinateSystem.crsUrn></cim:CoordinateSystem>
  <cim:Location rdf:ID="loc_grid"><cim:Location.CoordinateSystem rdf:resource="#crs"/><cim:Location.PowerSystemResources rdf:resource="#grid"/></cim:Location>
  <cim:PositionPoint rdf:ID="pp1"><cim:PositionPoint.Location rdf:resource="#loc_grid"/><cim:PositionPoint.sequenceNumber>1</cim:PositionPoint.sequenceNumber><cim:PositionPoint.xPosition>145.054000</cim:PositionPoint.xPosition><cim:PositionPoint.yPosition>-37.828000</cim:PositionPoint.yPosition></cim:PositionPoint>
</rdf:RDF>""")
    d = from_cim(gl, "cgmes_gl")
    b1 = next(b for b in d["buses"] if b["bus_id"] == 1)
    assert b1["lat"] == -37.828 and b1["lon"] == 145.054
    # unlocated buses simply carry no lat/lon keys
    b2 = next(b for b in d["buses"] if b["bus_id"] == 2)
    assert "lat" not in b2


def test_raw_substation_geo():
    """v34+ SUBSTATION DATA records (IS = bus number) set per-bus lat/lon."""
    geo = RAW_V36.replace("Q\n", """BEGIN SUBSTATION DATA
1, 'SLACK', -37.828000, 145.054000, 0.0
3, 'B3', -37.822600, 145.038400, 0.0
END OF SUBSTATION DATA
Q
""")
    d = from_raw(geo, "raw_geo")
    buses = {b["bus_id"]: b for b in d["buses"]}
    assert buses[1]["lat"] == -37.828 and buses[1]["lon"] == 145.054
    assert buses[3]["lat"] == -37.8226 and buses[3]["lon"] == 145.0384
    assert "lat" not in buses[2]                     # no record -> no keys


def test_parse_network_infers_format():
    assert parse_network(RAWX, network_id="x")["source_bus"] == 1          # rawx by content
    assert parse_network(CIM, network_id="x")["source_bus"] == 1           # xml by content
    assert parse_network(RAW, filename="case.raw", network_id="x")["source_bus"] == 1
    native = parse_network('{"id":"n","base_voltage_kv":11,"source_bus":1,"buses":[],"branches":[]}',
                           fmt="json")
    assert native["base_voltage_kv"] == 11


def test_bad_inputs_raise():
    with pytest.raises(NetworkImportError):
        from_rawx("not json", "x")
    with pytest.raises(NetworkImportError):
        from_cim("<rdf:RDF></rdf:RDF>", "x")   # no nodes
    with pytest.raises(NetworkImportError):
        parse_network("anything", fmt="bogus")


def test_builtin_formats_registered():
    assert set(supported_formats()) == {"json", "raw", "rawx", "cim", "dss"}
    formats = {f["name"]: f for f in available_formats()}
    assert set(formats) == {"json", "raw", "rawx", "cim", "dss"}
    # Extensions are surfaced so the UI can build its file-picker dynamically.
    assert "xml" in formats["cim"]["extensions"]
    assert all("description" in f for f in formats.values())


def test_a_custom_format_is_picked_up():
    """Registering a new importer makes parse_network use it — no dispatcher edits."""
    native = '{"id":"n","base_voltage_kv":11,"source_bus":1,"buses":[],"branches":[]}'

    def from_demo(content, network_id=None):
        return json.loads(content)

    saved = dict(importers._IMPORTERS)
    try:
        register_format("demo", from_demo, description="Demo", extensions=("dmo",))
        assert "demo" in supported_formats()
        # By canonical name and by registered extension alias.
        assert parse_network(native, fmt="demo")["base_voltage_kv"] == 11
        assert parse_network(native, filename="case.dmo")["base_voltage_kv"] == 11
    finally:
        importers._IMPORTERS.clear()
        importers._IMPORTERS.update(saved)


DSS = """Clear
! A small MV/LV feeder in OpenDSS master form.
New Circuit.demo basekv=11.0 pu=1.0 phases=3 bus1=sourcebus
New Line.l1 Bus1=sourcebus Bus2=mid_2 Length=1 Units=none R1=0.4 X1=0.3 Normamps=100
New Line.l2 Bus1=mid_2 Bus2=lv_3
~ R1=0.02 X1=0.01 Normamps=50
New Transformer.tx1 Phases=3 Windings=2 Buses=[mid_2, lv_4] kVs=[11.0, 0.4]
~ kVAs=[500, 500] XHL=6.0 %loadloss=1.0
New Load.ld3 Bus1=lv_3.1.2.3 Phases=3 kV=11.0 kW=120 kvar=45
New Load.ld3b Bus1=lv_3 Phases=3 kV=11.0 kW=30 kvar=10
New Load.ld4 Bus1=lv_4 Phases=3 kV=0.4 kW=60 kvar=20
Set VoltageBases=[0.4, 11.0]
Solve
"""


def test_dss_import_subset():
    d = from_dss(DSS, "demo_dss")
    m = NetworkModel.from_dict(d)
    assert m.base_voltage_kv == 11.0
    assert m.num_buses == 4 and m.num_branches == 3
    # ~ continuation folded into the transformer; secondary carries the LV base.
    tx = [b for b in d["branches"] if b.get("is_transformer")]
    assert len(tx) == 1 and tx[0]["rating_kva"] == 500.0
    assert m.bus_base_kv(tx[0]["to_bus"]) == 0.4
    # XHL 6% on the 11 kV / 500 kVA base -> x = 0.06 * 242 = 14.52 ohm.
    assert abs(tx[0]["x_ohm"] - 14.52) < 0.01
    # Loads accumulate per bus; node suffixes are stripped.
    loads = {b["name"]: b["base_load_kw"] for b in d["buses"]}
    assert loads["lv_3"] == 150.0 and loads["lv_4"] == 60.0
    # Trailing digits in bus names become the bus ids.
    names = {b["bus_id"]: b["name"] for b in d["buses"]}
    assert names[2] == "mid_2" and names[4] == "lv_4"


def test_dss_roundtrip_of_generated_master(tmp_path):
    """The solver's generated master.dss re-imports into an equivalent model.

    Cross-service check: generation lives in the opendss-solver container,
    importing lives here — the sibling checkout supplies the generator.
    """
    dss_model = pytest.importorskip(
        "dss_solver.dss_model",
        reason="requires the sibling opendss-solver checkout",
    )
    generate_master_dss = dss_model.generate_master_dss

    src = NetworkModel.from_dict({
        "id": "rt", "name": "roundtrip", "base_voltage_kv": 11.0, "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
            {"bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0},
            {"bus_id": 3, "base_load_kw": 50.0, "base_load_kvar": 20.0, "base_kv": 0.4},
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
            {"branch_id": 2, "from_bus": 2, "to_bus": 3, "is_transformer": True,
             "r_ohm": 2.42, "x_ohm": 10.89, "rating_kva": 500},
        ],
    })
    text = open(generate_master_dss(src, str(tmp_path))).read()
    d = from_dss(text, "rt2")
    m = NetworkModel.from_dict(d)
    assert m.num_buses == 3 and m.num_branches == 2
    assert m.source_bus == 1
    tx = [b for b in d["branches"] if b.get("is_transformer")]
    assert len(tx) == 1
    assert m.bus_base_kv(tx[0]["to_bus"]) == 0.4
    # Impedance survives the ohm -> percent -> ohm conversion.
    assert abs(tx[0]["x_ohm"] - 10.89) < 0.05


def test_dss_inferred_by_filename_and_content():
    assert parse_network(DSS, filename="feeder.dss", network_id="x")["source_bus"] == 1
    assert parse_network(DSS, network_id="x")["base_voltage_kv"] == 11.0  # sniffed


def test_dss_without_circuit_raises():
    with pytest.raises(NetworkImportError, match="New Circuit"):
        from_dss("New Line.l1 bus1=a bus2=b r1=1 x1=1", "x")
