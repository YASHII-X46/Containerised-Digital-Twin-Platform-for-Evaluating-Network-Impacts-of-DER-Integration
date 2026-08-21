"""Importer tests for the plant detail beyond topology and impedance.

A file that parses but silently drops a transformer tap, a thermal rating or a
per-bus phase describes a different network from the one it names, and the
difference shows up as a wrong answer rather than an error. These tests pin the
four things that used to be lost:

  * the fixed transformer ratio (RAW WINDV, CIM ratedU against BaseVoltage),
  * the vector group (RAW ANG1, CIM connectionKind),
  * the thermal rating (RAW RATEA, CIM OperationalLimit),
  * the per-bus phase connection and the zero-sequence line impedance (CIM
    only; PSS/E is positive sequence and cannot express either).
"""

import json

import pytest

from app.network.importers import from_cim, from_raw, from_rawx
from app.network.model import NetworkModel

# --- fixtures ---------------------------------------------------------------
#
# One two-bus network, an 11 kV source feeding a 0.4 kV board through a Dyn11
# transformer whose secondary is rated 415 V on a 400 V nominal system. That
# ratio, 1.0375, is the number every one of these tests is really about.

TAP = 1.0375

# CW 1: WINDV is already per unit of the bus base voltage.
RAW_CW1 = """0, 100.00, 33, 0, 1, 50.00
TITLE
TITLE
1, 'SRC', 11.000, 3, 1, 1, 1, 1.0, 0.0
2, 'LV', 0.400, 1, 1, 1, 1, 1.0, 0.0
0 / END OF BUS DATA, BEGIN LOAD DATA
2, '1', 1, 1, 1, 0.100, 0.033
0 / END OF LOAD DATA, BEGIN GENERATOR DATA
0 / END OF GENERATOR DATA, BEGIN BRANCH DATA
0 / END OF BRANCH DATA, BEGIN TRANSFORMER DATA
1, 2, 0, '1', 1, 1, 1, 0.0, 0.0, 2, 'TX', 1, 1, 1.0
0.842975, 4.322314, 100.00
1.00000, 11.000, -30.000, 1.250, 1.250, 1.250, 0, 0
1.03750, 0.400
0 / END OF TRANSFORMER DATA, BEGIN AREA DATA
0 / END OF AREA DATA
"""

# CW 2: WINDV is in kV, so the winding reads 0.415 against a 0.4 kV bus.
RAW_CW2 = RAW_CW1.replace(
    "1, 2, 0, '1', 1, 1, 1, 0.0, 0.0, 2, 'TX', 1, 1, 1.0",
    "1, 2, 0, '1', 2, 1, 1, 0.0, 0.0, 2, 'TX', 1, 1, 1.0",
).replace(
    "1.00000, 11.000, -30.000, 1.250, 1.250, 1.250, 0, 0\n1.03750, 0.400",
    "11.00000, 11.000, -30.000, 1.250, 1.250, 1.250, 0, 0\n0.41500, 0.400",
)

# CW 3: WINDV is per unit of NOMV, the winding's own nominal.
RAW_CW3 = RAW_CW1.replace(
    "1, 2, 0, '1', 1, 1, 1, 0.0, 0.0, 2, 'TX', 1, 1, 1.0",
    "1, 2, 0, '1', 3, 1, 1, 0.0, 0.0, 2, 'TX', 1, 1, 1.0",
).replace(
    "1.00000, 11.000, -30.000, 1.250, 1.250, 1.250, 0, 0\n1.03750, 0.400",
    "1.00000, 11.000, -30.000, 1.250, 1.250, 1.250, 0, 0\n1.03750, 0.400",
)

# A star-star transformer: ANG1 zero, no phase shift.
RAW_YY = RAW_CW1.replace("-30.000", "0.000")

RAWX = json.dumps({"network": {
    "caseid": {"fields": ["ic", "sbase", "rev"], "data": [0, 100.0, 35]},
    "bus": {"fields": ["ibus", "name", "baskv", "ide"],
            "data": [[1, "SRC", 11.0, 3], [2, "LV", 0.4, 1]]},
    "load": {"fields": ["ibus", "loadid", "stat", "pl", "ql"],
             "data": [[2, "1", 1, 0.1, 0.033]]},
    "transformer": {
        "fields": ["ibus", "jbus", "kbus", "cw", "r1_2", "x1_2",
                   "windv1", "nomv1", "windv2", "nomv2", "ang1", "rate1_1"],
        "data": [[1, 2, 0, 1, 0.008, 0.043, 1.0, 11.0, 1.0375, 0.4, -30.0, 1.25]]},
}})


def _cim(lv_rated_u="415", conn_hv="D", conn_lv="Yn", limits=True,
         phases="AN", r0=True, rtc=""):
    """A two-bus CGMES fragment with the pieces each test needs turned on."""
    limit_xml = """
  <cim:OperationalLimitType rdf:ID="olt_n"><cim:IdentifiedObject.name>Normal continuous</cim:IdentifiedObject.name><cim:OperationalLimitType.acceptableDuration>0</cim:OperationalLimitType.acceptableDuration></cim:OperationalLimitType>
  <cim:OperationalLimitType rdf:ID="olt_s"><cim:IdentifiedObject.name>Short term</cim:IdentifiedObject.name><cim:OperationalLimitType.acceptableDuration>900</cim:OperationalLimitType.acceptableDuration></cim:OperationalLimitType>
  <cim:OperationalLimitSet rdf:ID="ols_l"><cim:OperationalLimitSet.Terminal rdf:resource="#t_l_a"/></cim:OperationalLimitSet>
  <cim:ApparentPowerLimit rdf:ID="apl_n"><cim:OperationalLimit.OperationalLimitSet rdf:resource="#ols_l"/><cim:OperationalLimit.OperationalLimitType rdf:resource="#olt_n"/><cim:ApparentPowerLimit.value>0.2425</cim:ApparentPowerLimit.value></cim:ApparentPowerLimit>
  <cim:ApparentPowerLimit rdf:ID="apl_s"><cim:OperationalLimit.OperationalLimitSet rdf:resource="#ols_l"/><cim:OperationalLimit.OperationalLimitType rdf:resource="#olt_s"/><cim:ApparentPowerLimit.value>0.9000</cim:ApparentPowerLimit.value></cim:ApparentPowerLimit>
""" if limits else ""
    z0 = ('<cim:ACLineSegment.r0>0.40</cim:ACLineSegment.r0>'
          '<cim:ACLineSegment.x0>0.28</cim:ACLineSegment.x0>') if r0 else ""
    ph = ("<cim:Terminal.phases>%s</cim:Terminal.phases>" % phases) if phases else ""
    return """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:cim="http://iec.ch/TC57/CIM100#">
  <cim:BaseVoltage rdf:ID="bv_mv"><cim:BaseVoltage.nominalVoltage>11000</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage>
  <cim:BaseVoltage rdf:ID="bv_lv"><cim:BaseVoltage.nominalVoltage>400</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage>
  <cim:Substation rdf:ID="sub"/>
  <cim:VoltageLevel rdf:ID="vl_mv"><cim:VoltageLevel.Substation rdf:resource="#sub"/><cim:VoltageLevel.BaseVoltage rdf:resource="#bv_mv"/></cim:VoltageLevel>
  <cim:VoltageLevel rdf:ID="vl_lv"><cim:VoltageLevel.Substation rdf:resource="#sub"/><cim:VoltageLevel.BaseVoltage rdf:resource="#bv_lv"/></cim:VoltageLevel>
  <cim:ConnectivityNode rdf:ID="n1"><cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#vl_mv"/></cim:ConnectivityNode>
  <cim:ConnectivityNode rdf:ID="n2"><cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#vl_lv"/></cim:ConnectivityNode>
  <cim:ConnectivityNode rdf:ID="n3"><cim:ConnectivityNode.ConnectivityNodeContainer rdf:resource="#vl_lv"/></cim:ConnectivityNode>
  <cim:EnergySource rdf:ID="src"/>
  <cim:Terminal rdf:ID="t_src"><cim:Terminal.ConductingEquipment rdf:resource="#src"/><cim:Terminal.ConnectivityNode rdf:resource="#n1"/></cim:Terminal>
  <cim:PowerTransformer rdf:ID="tx"><cim:Equipment.EquipmentContainer rdf:resource="#vl_mv"/></cim:PowerTransformer>
  <cim:PowerTransformerEnd rdf:ID="tx_1"><cim:PowerTransformerEnd.PowerTransformer rdf:resource="#tx"/><cim:TransformerEnd.endNumber>1</cim:TransformerEnd.endNumber><cim:PowerTransformerEnd.ratedU>11000</cim:PowerTransformerEnd.ratedU><cim:PowerTransformerEnd.ratedS>1.25</cim:PowerTransformerEnd.ratedS><cim:PowerTransformerEnd.r>1.02</cim:PowerTransformerEnd.r><cim:PowerTransformerEnd.x>5.23</cim:PowerTransformerEnd.x><cim:PowerTransformerEnd.connectionKind>{conn_hv}</cim:PowerTransformerEnd.connectionKind><cim:TransformerEnd.Terminal rdf:resource="#t_tx_hv"/><cim:TransformerEnd.BaseVoltage rdf:resource="#bv_mv"/></cim:PowerTransformerEnd>
  <cim:PowerTransformerEnd rdf:ID="tx_2"><cim:PowerTransformerEnd.PowerTransformer rdf:resource="#tx"/><cim:TransformerEnd.endNumber>2</cim:TransformerEnd.endNumber><cim:PowerTransformerEnd.ratedU>{lv_rated_u}</cim:PowerTransformerEnd.ratedU><cim:PowerTransformerEnd.ratedS>1.25</cim:PowerTransformerEnd.ratedS><cim:PowerTransformerEnd.r>0</cim:PowerTransformerEnd.r><cim:PowerTransformerEnd.x>0</cim:PowerTransformerEnd.x><cim:PowerTransformerEnd.connectionKind>{conn_lv}</cim:PowerTransformerEnd.connectionKind><cim:TransformerEnd.Terminal rdf:resource="#t_tx_lv"/><cim:TransformerEnd.BaseVoltage rdf:resource="#bv_lv"/></cim:PowerTransformerEnd>
  <cim:Terminal rdf:ID="t_tx_hv"><cim:Terminal.ConductingEquipment rdf:resource="#tx"/><cim:Terminal.ConnectivityNode rdf:resource="#n1"/></cim:Terminal>
  <cim:Terminal rdf:ID="t_tx_lv"><cim:Terminal.ConductingEquipment rdf:resource="#tx"/><cim:Terminal.ConnectivityNode rdf:resource="#n2"/></cim:Terminal>
  {rtc}
  <cim:ACLineSegment rdf:ID="l"><cim:ACLineSegment.r>0.13</cim:ACLineSegment.r><cim:ACLineSegment.x>0.09</cim:ACLineSegment.x>{z0}<cim:ConductingEquipment.BaseVoltage rdf:resource="#bv_lv"/></cim:ACLineSegment>
  <cim:Terminal rdf:ID="t_l_a"><cim:Terminal.ConductingEquipment rdf:resource="#l"/><cim:Terminal.ConnectivityNode rdf:resource="#n2"/></cim:Terminal>
  <cim:Terminal rdf:ID="t_l_b"><cim:Terminal.ConductingEquipment rdf:resource="#l"/><cim:Terminal.ConnectivityNode rdf:resource="#n3"/></cim:Terminal>
  <cim:EnergyConsumer rdf:ID="ec3"><cim:EnergyConsumer.p>100000</cim:EnergyConsumer.p><cim:EnergyConsumer.q>33000</cim:EnergyConsumer.q></cim:EnergyConsumer>
  <cim:Terminal rdf:ID="t_ec3"><cim:Terminal.ConductingEquipment rdf:resource="#ec3"/><cim:Terminal.ConnectivityNode rdf:resource="#n3"/>{ph}</cim:Terminal>
{limits}
</rdf:RDF>
""".format(lv_rated_u=lv_rated_u, conn_hv=conn_hv, conn_lv=conn_lv,
           limits=limit_xml, z0=z0, ph=ph, rtc=rtc)


def _tx(net):
    return next(b for b in net["branches"] if b.get("is_transformer"))


def _line(net):
    return next(b for b in net["branches"] if not b.get("is_transformer"))


# --- PSS/E RAW: the winding ratio -------------------------------------------

@pytest.mark.parametrize("text,label", [(RAW_CW1, "CW1"), (RAW_CW2, "CW2"),
                                        (RAW_CW3, "CW3")])
def test_raw_reads_the_fixed_tap_in_every_winding_unit(text, label):
    """WINDV means three different things by CW; all three give the same tap."""
    tx = _tx(from_raw(text))
    assert tx["tap"] == pytest.approx(TAP, abs=1e-6), label


def test_raw_tap_direction_boosts_the_low_voltage_side():
    """WINDV2 above WINDV1 raises the LV winding, so the tap must exceed 1."""
    assert _tx(from_raw(RAW_CW1))["tap"] > 1.0


def test_raw_unity_tap_is_omitted_rather_than_written_as_one():
    """A transformer with no off-nominal ratio carries no 'tap' key at all."""
    text = RAW_CW1.replace("1.03750, 0.400", "1.00000, 0.400")
    assert "tap" not in _tx(from_raw(text))


def test_raw_reads_the_vector_group_from_the_phase_shift():
    assert _tx(from_raw(RAW_CW1))["connection"] == "delta_wye"


def test_raw_zero_phase_shift_is_a_star_star_transformer():
    assert _tx(from_raw(RAW_YY))["connection"] == "wye_wye"


def test_raw_transformer_record_shorter_than_four_lines_is_skipped():
    """A truncated record must not be read as a transformer with a bogus tap.

    The fixture gains a third bus on a real line so the file still has a branch
    once the broken transformer is dropped; otherwise the import fails for the
    unrelated reason that nothing at all is left.
    """
    text = (RAW_CW1
            .replace("0 / END OF BUS DATA",
                     "3, 'MV2', 11.000, 1, 1, 1, 1, 1.0, 0.0\n0 / END OF BUS DATA")
            .replace("0 / END OF GENERATOR DATA, BEGIN BRANCH DATA",
                     "0 / END OF GENERATOR DATA, BEGIN BRANCH DATA\n"
                     "1, 3, '1', 0.01, 0.02, 0.0, 5.0")
            .replace("1.03750, 0.400\n", ""))
    net = from_raw(text)
    assert not [b for b in net["branches"] if b.get("is_transformer")]
    assert len(net["branches"]) == 1


def test_rawx_reads_the_tap_and_the_vector_group():
    tx = _tx(from_rawx(RAWX))
    assert tx["tap"] == pytest.approx(TAP, abs=1e-6)
    assert tx["connection"] == "delta_wye"


# --- CIM: the winding ratio -------------------------------------------------

def test_cim_derives_the_tap_from_rated_u_against_nominal_voltage():
    """415 V rated on a 400 V nominal level is a 1.0375 ratio, not a 415 V bus."""
    tx = _tx(from_cim(_cim()))
    assert tx["tap"] == pytest.approx(TAP, abs=1e-6)


def test_cim_rated_u_does_not_move_the_bus_nominal_voltage():
    """The LV bus stays on its BaseVoltage; otherwise the tap would vanish."""
    net = from_cim(_cim())
    lv = next(b for b in net["buses"] if b["bus_id"] == 2)
    assert lv["base_kv"] == pytest.approx(0.4)


def test_cim_matched_rated_u_leaves_no_tap():
    assert "tap" not in _tx(from_cim(_cim(lv_rated_u="400")))


def test_cim_ratio_tap_changer_multiplies_the_winding_ratio():
    """A tap changer on the LV end composes with its rated ratio."""
    rtc = ('<cim:RatioTapChanger rdf:ID="rtc">'
           '<cim:RatioTapChanger.TransformerEnd rdf:resource="#tx_2"/>'
           '<cim:RatioTapChanger.stepVoltageIncrement>2.5</cim:RatioTapChanger.stepVoltageIncrement>'
           '<cim:TapChanger.neutralStep>0</cim:TapChanger.neutralStep>'
           '<cim:TapChanger.step>1</cim:TapChanger.step></cim:RatioTapChanger>')
    tx = _tx(from_cim(_cim(lv_rated_u="400", rtc=rtc)))
    assert tx["tap"] == pytest.approx(1.025, abs=1e-6)


def test_cim_reads_the_vector_group_from_connection_kind():
    assert _tx(from_cim(_cim()))["connection"] == "delta_wye"


def test_cim_star_star_transformer_is_wye_wye():
    assert _tx(from_cim(_cim(conn_hv="Yn")))["connection"] == "wye_wye"


def test_a_tap_outside_the_models_band_is_dropped_not_carried():
    """A misread field must not produce a network the model refuses to load."""
    text = RAW_CW1.replace("1.03750, 0.400", "1.90000, 0.400")
    tx = _tx(from_raw(text))
    assert "tap" not in tx
    NetworkModel.from_dict(from_raw(text))    # still valid


# --- CIM: thermal ratings ---------------------------------------------------

def test_cim_reads_the_continuous_rating_from_operational_limits():
    """242.5 kVA is the continuous limit; the 900 kVA short-term one is not."""
    assert _line(from_cim(_cim()))["rating_kva"] == pytest.approx(242.5)


def test_cim_without_operational_limits_falls_back_to_the_case_base():
    """No limit profile means no rating information, and that must be visible."""
    assert _line(from_cim(_cim(limits=False)))["rating_kva"] == pytest.approx(100000.0)


def test_cim_current_limit_becomes_an_apparent_power_rating():
    """A file carrying only amps still yields a usable kVA rating."""
    amps = 350.0
    text = _cim(limits=False).replace(
        "</rdf:RDF>",
        '<cim:OperationalLimitType rdf:ID="olt_n"><cim:IdentifiedObject.name>Normal</cim:IdentifiedObject.name></cim:OperationalLimitType>'
        '<cim:OperationalLimitSet rdf:ID="ols_l"><cim:OperationalLimitSet.Terminal rdf:resource="#t_l_a"/></cim:OperationalLimitSet>'
        '<cim:CurrentLimit rdf:ID="cl_n"><cim:OperationalLimit.OperationalLimitSet rdf:resource="#ols_l"/>'
        '<cim:OperationalLimit.OperationalLimitType rdf:resource="#olt_n"/>'
        '<cim:CurrentLimit.value>%s</cim:CurrentLimit.value></cim:CurrentLimit></rdf:RDF>' % amps)
    expected = 3 ** 0.5 * 0.4 * amps          # kVA at the 0.4 kV LV level
    assert _line(from_cim(text))["rating_kva"] == pytest.approx(expected, rel=1e-4)


def test_cim_transformer_rating_falls_back_to_rated_s():
    """A transformer with no limit set is still rated by its winding."""
    assert _tx(from_cim(_cim()))["rating_kva"] == pytest.approx(1250.0)


# --- CIM: phases and zero sequence ------------------------------------------

def test_cim_reads_a_single_phase_board_from_terminal_phases():
    net = from_cim(_cim(phases="AN"))
    assert next(b for b in net["buses"] if b["bus_id"] == 3)["phases"] == "a"


def test_cim_reads_a_single_phase_board_from_energy_consumer_phase():
    """EnergyConsumerPhase is authoritative even when the terminal says ABCN."""
    text = _cim(phases="ABCN").replace(
        '<cim:Terminal rdf:ID="t_ec3">',
        '<cim:EnergyConsumerPhase rdf:ID="ecp3">'
        '<cim:EnergyConsumerPhase.EnergyConsumer rdf:resource="#ec3"/>'
        '<cim:EnergyConsumerPhase.phase>C</cim:EnergyConsumerPhase.phase>'
        '</cim:EnergyConsumerPhase>'
        '<cim:Terminal rdf:ID="t_ec3">')
    net = from_cim(text)
    assert next(b for b in net["buses"] if b["bus_id"] == 3)["phases"] == "c"


def test_cim_three_phase_board_declares_no_phases_key():
    """ABCN is the default, so it is not written and nothing downstream changes."""
    net = from_cim(_cim(phases="ABCN"))
    assert "phases" not in next(b for b in net["buses"] if b["bus_id"] == 3)


def test_cim_phase_enumeration_by_resource_reference_is_read():
    """CIM enums appear as text or as an rdf:resource; both must work."""
    text = _cim(phases=None).replace(
        '<cim:Terminal.ConnectivityNode rdf:resource="#n3"/>',
        '<cim:Terminal.ConnectivityNode rdf:resource="#n3"/>'
        '<cim:Terminal.phases rdf:resource="http://iec.ch/TC57/CIM100#PhaseCode.BN"/>')
    net = from_cim(text)
    assert next(b for b in net["buses"] if b["bus_id"] == 3)["phases"] == "b"


def test_cim_reads_explicit_zero_sequence_line_impedance():
    line = _line(from_cim(_cim()))
    assert line["r0_ohm"] == pytest.approx(0.40)
    assert line["x0_ohm"] == pytest.approx(0.28)


def test_cim_without_zero_sequence_omits_it_so_the_model_default_applies():
    assert "r0_ohm" not in _line(from_cim(_cim(r0=False)))


# --- what PSS/E genuinely cannot carry --------------------------------------

def test_raw_carries_no_phase_or_zero_sequence_data():
    """Positive-sequence format: absence here is correct, not a parser bug."""
    net = from_raw(RAW_CW1)
    assert not any("phases" in b for b in net["buses"])
    assert not any("r0_ohm" in b for b in net["branches"])


# --- everything still validates ---------------------------------------------

@pytest.mark.parametrize("net", [
    from_raw(RAW_CW1), from_raw(RAW_CW2), from_raw(RAW_CW3),
    from_rawx(RAWX), from_cim(_cim()), from_cim(_cim(limits=False)),
])
def test_every_imported_network_passes_model_validation(net):
    assert NetworkModel.from_dict(net).num_buses >= 2
