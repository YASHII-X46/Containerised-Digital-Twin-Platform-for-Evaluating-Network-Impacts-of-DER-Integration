"""Map the stack's network model dict onto PSS SINCAL database rows.

One module, used by two callers, so the mapping cannot drift:

  - the offline emitter in generate_swinburne_v6.py, which writes a project file
  - sincal-solver, which writes the same rows into its per-session project

It is vendored into sincal-solver rather than imported across services, because
that is how this repository already shares code (every service carries its own
copy of bus/), and the service image copies only its own package. A test asserts
the two copies are byte identical, which is a stronger guarantee than a
convention.

Schema discovery, not hardcoding: SINCAL's schema varies between releases, so
every write is filtered against the columns the target database actually has,
and a missing core table fails with a message naming it.

Verified against PSS SINCAL Platform 22.5, SQLite backend. See
SINCAL-SCHEMA-NOTES.md.

Standard library only.
"""
import math
import sqlite3

# The tables a network needs. Graphic tables are optional, the project still
# opens without them, it just has no drawing.
CORE_TABLES = ("VoltageLevel", "Node", "Element", "Terminal",
               "Line", "TwoWindingTransformer", "Load", "Infeeder")
GRAPHIC_TABLES = ("GraphicLayer", "GraphicNode", "GraphicElement", "GraphicTerminal")
# Written only when export_network(phases=True); a schema without it skips it.
OPTIONAL_TABLES = ("NeutralPointImp", "CalcParameter", "GraphicAreaTile",
                   "NetworkGroup", "GraphicBucklePoint", "GraphicText",
                   "GraphicObjectType", "DCInfeeder")
# Every table export_network fills, emptied first so a re-export is idempotent.
# CalcParameter is deliberately absent: it is one seeded row that is UPDATEd,
# not inserted, and GraphicLayer and GraphicAreaTile are the sheet SinDBCreate
# seeds, which the drawing points at rather than replaces.
WRITTEN_TABLES = ("VoltageLevel", "Node", "Element", "Terminal", "Line",
                  "TwoWindingTransformer", "Load", "Infeeder", "DCInfeeder",
                  "NeutralPointImp", "GraphicNode", "GraphicElement",
                  "GraphicTerminal", "GraphicText", "GraphicBucklePoint")

# SINCAL element type strings, as written into Element.Type.
TYPE_LINE = "Line"
TYPE_TRANSFORMER = "TwoWindingTransformer"
TYPE_LOAD = "Load"
TYPE_INFEEDER = "Infeeder"
TYPE_DCINFEEDER = "DCInfeeder"

# The ATC plant is 84 Trina 390 W panels behind three Sungrow SH10RT *hybrid*
# inverters, each with a BYD HVS10.2 battery on its DC bus (DCH5 Milestone
# Report #M7, Table 1). A hybrid inverter puts the array and the battery on one
# shared DC bus behind ONE AC connection, so the plant is a single DCInfeeder
# rated at the shared 30 kVA, not two independent infeeds that could each export
# 30 kVA. This mirrors the CIM, which carries one PowerElectronicsConnection
# with a PhotoVoltaicUnit and a BatteryUnit under it.
#
# Flag_DCtyp names the plant: 1 common, 3 battery storage, 7 photovoltaic
# system. There is no hybrid member, and 7 describes the generating half.
DC_TYPE_PHOTOVOLTAIC = 7
DC_TYPE_BATTERY = 3
# Flag_Lf 1 is "P and Q", the plain load-flow injection. The shipped samples use
# 9 because they drive dynamic macros, which this model does not need.
DC_LF_PQ = 1
# Flag_Connect 1 is a direct connection, with no dedicated transformer.
DC_CONNECT_DIRECT = 1
# GraphicElement.SymbolType for a converter, confirmed in Example Renewables.
SYMBOL_DCINFEEDER = 193

# A DCInfeeder is an INFEED: positive P injects into the network. That is the
# opposite of the Load convention used everywhere else in this mapping, where
# generation is written as negative load. The two must not be confused.
DER_SIGN_INJECTION = 1.0

# SinDBCreate seeds one network group, "Base Area". Nodes and elements must
# belong to it or SINCAL reports no power flow data for them.
GROUP_ID = 1

# Element.Flag_Input is a BIT MASK naming which per-method data sets have been
# entered for an element, not an enumeration. SINCAL refuses to build the
# network model for any element whose bit for the running method is clear:
# message 2534 "Power flow data has to be available to calculate the network
# model", and 2535 for supply sources, which need short circuit data too. This
# is the reason a fully populated project used to load every row and then solve
# nothing. Bit numbers are from Database-Description.chm, "Input Data State".
INPUT_SHORT_CIRCUIT = 1 << 0
INPUT_POWER_FLOW = 1 << 1
INPUT_ZERO_SEQ = 1 << 2
INPUT_NEGATIVE_SEQ = 1 << 3
# Every element here carries both short circuit and power flow data. Lines and
# transformers also carry zero-sequence data, so they claim that bit too;
# claiming it without writing the Z0 columns makes SINCAL warn "please check
# zero sequence data" and abandon the solve, so the two must move together.
FLAG_INPUT_BASE = INPUT_SHORT_CIRCUIT | INPUT_POWER_FLOW
FLAG_INPUT_WITH_Z0 = FLAG_INPUT_BASE | INPUT_ZERO_SEQ

# TwoWindingTransformer.Flag_Z0_Input 3 reads the R0/R1 and X0/X1 ratios. A
# Dyn11 distribution transformer's zero-sequence impedance seen from the
# earthed star secondary is close to its positive-sequence impedance, so the
# ratios are 1.0 -- the same assumption OpenDSS makes for a Dyn winding.
TX_Z0_INPUT_RATIOS = 3
TX_Z0_RATIO = 1.0

# Earth electrode resistance for a transformer star point, ohms. Only written
# on the unbalanced path. An Australian MEN installation is multiply earthed
# and lands around a tenth of an ohm overall; the exact figure matters little,
# but it cannot be zero (SINCAL message 2834).
MEN_EARTH_OHM = 0.1

# Infeeder.Flag_Lf selects what the supply source prescribes. 3 is "|vsrc| and
# delta", a voltage source behind its short circuit impedance, which is what
# makes the node a slack. 1 is "|I| and phi", a current source, which leaves the
# network with no slack at all.
INFEEDER_LF_SLACK = 3
# Infeeder.Flag_Typ 2 is "R/X and Sk2", matching the Sk2 / R_X pair written
# below; 1 would instead read the R and X columns, which are not written.
INFEEDER_TYP_SK = 2

# TwoWindingTransformer.VecGrp is an enumeration of named vector groups, not a
# clock number: 59 is DYN11 and 5 is YNYN0. Writing the clock number directly
# selects an unrelated group (11 is DZ1).
VECGRP_DYN11 = 59
VECGRP_YNYN0 = 5

# Terminal.Flag_Terminal is the phase connection: 1/2/3 are L1/L2/L3, 7 is
# L123. This is where a single-phase load is declared.
TERMINAL_PHASE = {1: 1, 2: 2, 3: 3}
TERMINAL_THREE_PHASE = 7

# Node.Flag_Type names the node's role in the network, 1 to 10; 0 is not a
# defined value. It drives how SINCAL groups the object in its own reports, so
# it is not cosmetic. The model supplies a role and this maps it; a model that
# supplies none leaves every node a plain node, as before.
NODE_TYPE_NODE = 1
NODE_TYPE_BUSBAR = 2
NODE_TYPE_JOINT = 3
NODE_TYPE_PRIMARY_SUBSTATION = 4
NODE_TYPE_SECONDARY_SUBSTATION = 5
NODE_TYPE_CUSTOMER_SUBSTATION = 7
ROLE_NODE_TYPE = {
    "zone_substation": NODE_TYPE_PRIMARY_SUBSTATION,
    "feeder_joint": NODE_TYPE_JOINT,
    "hv_switchboard": NODE_TYPE_BUSBAR,
    "customer_substation": NODE_TYPE_CUSTOMER_SUBSTATION,
    "public_substation": NODE_TYPE_SECONDARY_SUBSTATION,
    "board": NODE_TYPE_NODE,
}
# Roles whose node is drawn as a busbar rather than a point.
BUSBAR_ROLES = ("zone_substation", "hv_switchboard", "customer_substation",
                "public_substation")

# Node.Uul and Node.Ull are the voltage limits SINCAL checks against, in percent
# of nominal. AS 61000.3.100 allows 240 V +6 / -2 percent at the point of
# supply, and the stack's own planning band is 0.95 to 1.05 per unit, which is
# the wider of the two and the one every other solver in this repository is
# judged against. Using it here means SINCAL flags the same violations.
VOLTAGE_UPPER_LIMIT_PCT = 105.0
VOLTAGE_LOWER_LIMIT_PCT = 95.0

# Name is Text(50) and ShortName Text(8), both measured from the schema. The
# drawing label is fitted to this many characters: the brief proposes 24, but
# this model's zone names ("AMDC design and fabrication labs") do not survive
# 24 without losing the part that identifies them, and Name has room. Nothing is
# lost either way, because the full description goes to TextVal.
LABEL_CHARS = 32
SHORTNAME_CHARS = 8
# Node.Flag_Pos 2 selects latitude/longitude as the node position.
NODE_POS_LATLON = 2

# Load.Flag_LoadType 2 is constant P and Q, which is what the rest of the stack
# models; 1 would make every load a constant impedance and understate the
# voltage drop.
LOAD_TYPE_CONST_PQ = 2
# Load.Flag_Lf 1 is the "P and Q" input format, matching the columns written.
LOAD_LF_PQ = 1

# CalcParameter.Flag_LFmet is the power flow PROCEDURE, and it is what actually
# selects a balanced or an unbalanced solve. Calling Simulation.Start("ULF")
# does not switch it. The two procedures also write to different result tables:
# a symmetric run fills LFNodeResult, an unbalanced one fills ULFNodeResult.
LFMET_ADMITTANCE = 3           # symmetric, the default
LFMET_UNBALANCED_PHASES = 8    # unbalanced, solved in phase quantities

# CalcParameter.Flag_LFZ0 is how the zero-sequence (fourth conductor) network is
# built. SINCAL's own words: "Unbalanced loading with grounded neutral point or
# 1-phase loading against ground can only be carried out in networks with four
# conductors. The fourth conductor is specified via the zero system data."
#
#   1 Input data      2 Z0 equals Z1      3 Ze equals Zl
#   4 Z0 infinite     5 Z0 equals Z1 and neutral point
#
# 3 is the one used here: the neutral conductor is modelled with the same
# impedance as a phase conductor, which is exactly true of the four-core LV
# cable this network is built from, and it is the only mode whose neutral
# displacement grows along a run rather than sitting flat. It also agrees with
# OpenDSS: 0.2745 percent worst VUF against 0.272. Modes 2 and 5 converge in a
# quarter of the iterations but force Z0 = Z1, which discards the explicit
# r0/x0 on every line and overstates the unbalance by about a quarter (0.346
# percent on the same network). Mode 1 does not converge at all, whatever
# zero-sequence data the elements carry.
LFZ0_NEUTRAL_LIKE_PHASE = 3

# GraphicElement.SymbolType, the drawing symbol for each kind of element.
SYMBOL_INFEEDER = 11
SYMBOL_LOAD = 13
SYMBOL_LINE = 19
SYMBOL_TRANSFORMER = 20

# GraphicNode.SymType: 1 is a point circle, 3 a busbar that spans a distance.
# GraphicArea_ID names the sheet a graphic row is on; a row without one is on no
# sheet and is not drawn.
NODE_SYMBOL_CIRCLE = 1
NODE_SYMBOL_BUSBAR = 3
# NodeSize counts 0.25 mm steps and SymbolSize is a percentage. The samples use
# 4 for a point node, 0 for a busbar (its span carries the geometry) and 100 for
# symbols. Deriving these from the sheet gives sub-millimetre symbols that are
# in the database and invisible on the page.
NODE_SIZE_POINT = 4.0
NODE_SIZE_BUSBAR = 0.0
ELEMENT_SYMBOL_SIZE = 100
SYMBOL_DEF = 1


class SincalSchemaError(RuntimeError):
    """The target database does not carry a table or column the mapping needs."""


def discover(conn):
    """Return {table: {column, ...}} for the tables this mapping touches."""
    found = {}
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in CORE_TABLES if t not in names]
    if missing:
        raise SincalSchemaError(
            "SINCAL template is missing required table(s): %s. Create the "
            "project with SinDBCreate /DBSYS:SQLITE /TYPE:E, or point "
            "SINCAL_TEMPLATE at a project made in the SINCAL GUI."
            % ", ".join(missing))
    for t in CORE_TABLES + GRAPHIC_TABLES + OPTIONAL_TABLES:
        if t in names:
            found[t] = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)}
    return found


def _insert(conn, schema, table, row):
    """Insert a row, keeping only the columns this database actually has."""
    if table not in schema:
        return
    cols = [c for c in row if c in schema[table]]
    if not cols:
        return
    conn.execute("INSERT INTO %s (%s) VALUES (%s)"
                 % (table, ",".join(cols), ",".join("?" for _ in cols)),
                 [row[c] for c in cols])


def _bus_kv(net, bus_id):
    for b in net["buses"]:
        if int(b["bus_id"]) == int(bus_id):
            return float(b.get("base_kv") or net["base_voltage_kv"])
    return float(net["base_voltage_kv"])


_PHASE_CHAR = {"a": 1, "b": 2, "c": 3, "1": 1, "2": 2, "3": 3}


def terminal_phase(spec):
    """A bus 'phases' declaration -> Terminal.Flag_Terminal.

    Mirrors ``NetworkModel.normalize_phases``: anything absent, empty or
    three-phase becomes L123; a single named phase becomes L1/L2/L3. A
    two-phase declaration has no single SINCAL connection type, so it falls
    back to L123 rather than silently dropping a phase.
    """
    if spec is None:
        return TERMINAL_THREE_PHASE
    if isinstance(spec, (list, tuple)):
        nodes = sorted({_PHASE_CHAR[str(x).strip().lower()] for x in spec
                        if str(x).strip().lower() in _PHASE_CHAR})
    else:
        s = str(spec).strip().lower()
        nodes = sorted({_PHASE_CHAR[ch] for ch in s if ch in _PHASE_CHAR})
    if len(nodes) == 1:
        return TERMINAL_PHASE[nodes[0]]
    return TERMINAL_THREE_PHASE


def fit_label(text, limit=LABEL_CHARS):
    """Shorten a drawing label to fit, without inventing an abbreviation.

    Two steps, both reversible by eye: "and" becomes an ampersand, then trailing
    words are dropped until it fits. Nothing is truncated mid-word and no
    ellipsis is added, because the untruncated string is kept in the
    description field and the label only has to identify the object on a sheet.
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    text = text.replace(" and ", " & ")
    while len(text) > limit and " " in text:
        text = text.rsplit(" ", 1)[0]
    return text[:limit]


def _bus_identity(bus):
    """(ShortName, Name, description) for a bus."""
    bid = int(bus["bus_id"])
    short = ("bus_%d" % bid)[:SHORTNAME_CHARS]
    description = str(bus.get("name") or short)
    label = fit_label(bus.get("label") or description)
    return short, label, description


def _branch_identity(branch):
    """(ShortName, Name, description) for a line or transformer."""
    short = ("br_%s" % branch.get("branch_id"))[:SHORTNAME_CHARS]
    description = str(branch.get("name") or short)
    label = fit_label(branch.get("label") or description)
    return short, label, description


def _element_identity(prefix, bus, suffix):
    """(ShortName, Name, description) for plant hanging off a bus."""
    bid = int(bus["bus_id"])
    short = ("%s_%d" % (prefix, bid))[:SHORTNAME_CHARS]
    base = bus.get("label") or bus.get("name") or ("bus_%d" % bid)
    return short, fit_label("%s %s" % (base, suffix)), "%s %s" % (
        bus.get("name") or short, suffix)


def export_network(conn, net, with_graphics=True, phases=False):
    """Write a whole network model dict into an empty SINCAL project database.

    Bus ids are preserved as Node_ID and carried in Node.Name, so results map
    straight back onto the stack's bus numbering.

    ``phases`` selects an unbalanced project. It writes each bus's phase
    declaration onto its load terminal (Terminal.Flag_Terminal L1/L2/L3),
    earths every transformer star point so zero-sequence current has a return
    path, and sets the calculation to the unbalanced procedure with a
    four-conductor zero-sequence network. Results then land in ``ULFNodeResult``
    and ``ULFBranchResult`` rather than the symmetric tables, so read them with
    ``read_phase_voltages`` and ``read_max_vuf_pct``.

    It is off by default because the balanced project is what the solver bus
    asks SINCAL for, and a symmetric solve is cheaper and has no unbalance to
    report. Both settings are verified against the 48-bus Swinburne model.
    """
    schema = discover(conn)
    _clear(conn, schema)
    group_id = _write_groups(conn, schema, net)
    counts = {"VoltageLevel": 0, "Node": 0, "Line": 0,
              "TwoWindingTransformer": 0, "Load": 0, "Infeeder": 0,
              "DCInfeeder": 0}

    # One voltage level per distinct nominal voltage in the model.
    levels = sorted({round(_bus_kv(net, b["bus_id"]), 6) for b in net["buses"]},
                    reverse=True)
    level_id = {}
    for i, kv in enumerate(levels, start=1):
        level_id[kv] = i
        _insert(conn, schema, "VoltageLevel", {
            "VoltLevel_ID": i, "Variant_ID": 1, "Flag_Variant": 1,
            "Name": "%g kV" % kv, "ShortName": ("%g" % kv)[:8],
            "Un": kv, "Uop": kv, "f": 50.0,
        })
        counts["VoltageLevel"] += 1

    phase_of = {int(b["bus_id"]): (terminal_phase(b.get("phases")) if phases
                                   else TERMINAL_THREE_PHASE)
                for b in net["buses"]}

    for b in net["buses"]:
        bid = int(b["bus_id"])
        kv = round(_bus_kv(net, bid), 6)
        short, label, description = _bus_identity(b)
        row = {
            "Node_ID": bid, "Variant_ID": 1, "Flag_Variant": 1,
            # Three fields, three jobs: the id, the drawing label, and the full
            # description. Putting all three in Name is what captioned the
            # drawing with sentences and then truncated them at 50 characters.
            "Name": label, "ShortName": short, "TextVal": description,
            "VoltLevel_ID": level_id[kv], "Un": kv,
            "Flag_Type": ROLE_NODE_TYPE.get(b.get("role"), NODE_TYPE_NODE),
            "Group_ID": group_id.get(bid, GROUP_ID),
            # Voltage limits, so SINCAL reports violations by itself instead of
            # requiring a human to read 48 voltages.
            "Uul": VOLTAGE_UPPER_LIMIT_PCT, "Ull": VOLTAGE_LOWER_LIMIT_PCT,
        }
        if b.get("lat") is not None and b.get("lon") is not None:
            row.update({"Flag_Pos": NODE_POS_LATLON,
                        "lat": float(b["lat"]), "lon": float(b["lon"])})
        _insert(conn, schema, "Node", row)
        counts["Node"] += 1

    eid = 0
    tid = 0
    neutral_points = []
    # (element_id, symbol type, [(terminal_id, node_id), ...]) for the drawing.
    drawn = []
    for br in net["branches"]:
        eid += 1
        f, t = int(br["from_bus"]), int(br["to_bus"])
        kv_f = round(_bus_kv(net, f), 6)
        is_tx = bool(br.get("is_transformer"))
        etype = TYPE_TRANSFORMER if is_tx else TYPE_LINE
        br_short, br_label, br_description = _branch_identity(br)
        _insert(conn, schema, "Element", {
            "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
            "Type": etype, "Name": br_label, "ShortName": br_short,
            "Description": br_description[:50], "TextVal": br_description,
            "VoltLevel_ID": level_id[kv_f], "Flag_State": 1,
            "Flag_Input": FLAG_INPUT_WITH_Z0,
            # A branch belongs to the area its far end is in, so a submain sits
            # with its building rather than with the network above it.
            "Group_ID": group_id.get(t, group_id.get(f, GROUP_ID)),
        })
        # A branch is three-phase plant even where the board it feeds is
        # single-phase: only the load declares a phase.
        ends = []
        for no, node in ((1, f), (2, t)):
            tid += 1
            ends.append((tid, node))
            _insert(conn, schema, "Terminal", {
                "Terminal_ID": tid, "Variant_ID": 1, "Flag_Variant": 1,
                "Element_ID": eid, "Node_ID": node, "TerminalNo": no,
                "Flag_State": 1, "Flag_Switch": 0,
                "Flag_Terminal": TERMINAL_THREE_PHASE,
            })
        drawn.append((eid, SYMBOL_TRANSFORMER if is_tx else SYMBOL_LINE, ends))
        if is_tx:
            kv_t = round(_bus_kv(net, t), 6)
            sn_mva = float(br.get("rating_kva", 1000.0)) / 1000.0
            zbase = (kv_f * kv_f) / sn_mva if sn_mva > 0 else 1.0
            r, x = float(br.get("r_ohm", 0.0)), float(br.get("x_ohm", 0.0))
            # The fixed off-load tap is the winding ratio, so it belongs in the
            # rated secondary voltage: an 11 kV / 415 V transformer on a 400 V
            # nominal level is Un2 = 0.4 * 1.0375. SINCAL's `roh` is a tap
            # POSITION counted in `ukr` percent steps, not a per-unit ratio, so
            # writing the ratio there would silently do nothing.
            tap = float(br.get("tap", 1.0) or 1.0)
            row = {
                "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
                "Sn": sn_mva, "Un1": kv_f, "Un2": round(kv_t * tap, 9),
                "uk": round(math.hypot(r, x) / zbase * 100.0, 6),
                "ur": round(r / zbase * 100.0, 6),
                "VecGrp": (VECGRP_DYN11 if br.get("connection") == "delta_wye"
                           else VECGRP_YNYN0),
                # Flag_Lf 1 is the standard power-flow transformer model.
                "Flag_Lf": 1, "Flag_ur": 1,
                "Flag_ConNode": 2,
                "roh": 0.0, "rohl": 0.0, "rohu": 0.0, "ukr": 0.0,
                "Flag_Z0_Input": TX_Z0_INPUT_RATIOS,
                "R0_R1": TX_Z0_RATIO, "X0_X1": TX_Z0_RATIO,
                "i0": 0.5, "Vfe": 0.0,
                "Pvlk": round(r / zbase * 100.0 * sn_mva * 10.0, 6),
                "Smax": sn_mva,
            }
            if phases:
                # A single-phase load needs a zero-sequence return path, so the
                # star point is earthed through the MEN electrode resistance.
                # SINCAL rejects a zero impedance outright ("Neutral point
                # ground impedance is equal to zero"), so it must be > 0.
                stp_id = len(neutral_points) + 1
                neutral_points.append(stp_id)
                _insert(conn, schema, "NeutralPointImp", {
                    "Stp_ID": stp_id, "Variant_ID": 1, "Flag_Variant": 1,
                    "Name": ("npi_%d" % eid)[:50], "Flag_Type": 2,
                    "Element_ID": eid, "RE": MEN_EARTH_OHM, "XE": 0.0,
                    "Flag_Ground": 1, "RG": MEN_EARTH_OHM, "XG": 0.0,
                })
                row["Stp_ID2"] = stp_id
            _insert(conn, schema, TYPE_TRANSFORMER, row)
            counts["TwoWindingTransformer"] += 1
        else:
            length_km = float(br.get("length_m") or 0.0) / 1000.0
            # SINCAL carries line impedance per kilometre, so a zero-length
            # segment is given a nominal 1 m to keep the product finite.
            if length_km <= 0:
                length_km = 0.001
            kva = float(br.get("rating_kva", 0.0) or 0.0)
            _insert(conn, schema, TYPE_LINE, {
                "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
                "Un": kv_f, "l": round(length_km, 6), "fn": 50.0,
                "r": round(float(br.get("r_ohm", 0.0)) / length_km, 6),
                "x": round(float(br.get("x_ohm", 0.0)) / length_km, 6),
                "r0": round(float(br.get("r0_ohm", br.get("r_ohm", 0.0))) / length_km, 6),
                "x0": round(float(br.get("x0_ohm", br.get("x_ohm", 0.0))) / length_km, 6),
                "c": 0.0, "c0": 0.0, "Flag_LineTyp": 1,
                # Ith is the thermal limit current in kA; without it SINCAL
                # reports every line at zero utilisation.
                "Ith": round(kva / (math.sqrt(3) * kv_f) / 1000.0, 9) if kv_f else 0.0,
            })
            counts["Line"] += 1

    source = int(net.get("source_bus", 1))
    for b in net["buses"]:
        bid = int(b["bus_id"])
        kw = float(b.get("base_load_kw", 0.0) or 0.0)
        kvar = float(b.get("base_load_kvar", 0.0) or 0.0)
        if bid == source:
            eid += 1
            tid += 1
            inf_short, inf_label, inf_description = _element_identity(
                "inf", b, "grid infeed")
            _insert(conn, schema, "Element", {
                "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
                "Type": TYPE_INFEEDER, "Name": inf_label,
                "ShortName": inf_short, "Description": inf_description[:50],
                "TextVal": inf_description,
                "VoltLevel_ID": level_id[round(_bus_kv(net, bid), 6)],
                "Flag_State": 1, "Flag_Input": FLAG_INPUT_BASE,
                "Group_ID": group_id.get(bid, GROUP_ID),
            })
            _insert(conn, schema, "Terminal", {
                "Terminal_ID": tid, "Variant_ID": 1, "Flag_Variant": 1,
                "Element_ID": eid, "Node_ID": bid, "TerminalNo": 1,
                "Flag_State": 1, "Flag_Switch": 0,
                "Flag_Terminal": TERMINAL_THREE_PHASE,
            })
            drawn.append((eid, SYMBOL_INFEEDER, [(tid, bid)]))
            _insert(conn, schema, TYPE_INFEEDER, {
                "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
                # Flag_Lf 3 prescribes source voltage and angle, which is what
                # makes this node the slack. u is percent, delta is degrees.
                "Flag_Typ": INFEEDER_TYP_SK, "Flag_Lf": INFEEDER_LF_SLACK,
                "u": 100.0, "delta": 0.0, "phi": 0.0,
                "Sk2": 500.0, "Sk2max": 500.0, "Sk2min": 250.0,
                "R_X": 0.1, "R_Xmax": 0.1, "R_Xmin": 0.1,
                "cmax": 1.1, "cmin": 1.0, "cact": 1.0,
                "Ug": round(_bus_kv(net, bid), 9),
            })
            counts["Infeeder"] += 1
            continue
        if kw == 0.0 and kvar == 0.0:
            continue
        eid += 1
        tid += 1
        ld_short, ld_label, ld_description = _element_identity("load", b, "load")
        _insert(conn, schema, "Element", {
            "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
            "Type": TYPE_LOAD, "Name": ld_label, "ShortName": ld_short,
            "Description": ld_description[:50], "TextVal": ld_description,
            "VoltLevel_ID": level_id[round(_bus_kv(net, bid), 6)],
            "Flag_State": 1, "Flag_Input": FLAG_INPUT_BASE,
            "Group_ID": group_id.get(bid, GROUP_ID),
        })
        _insert(conn, schema, "Terminal", {
            "Terminal_ID": tid, "Variant_ID": 1, "Flag_Variant": 1,
            "Element_ID": eid, "Node_ID": bid, "TerminalNo": 1,
            "Flag_State": 1, "Flag_Switch": 0,
            "Flag_Terminal": phase_of.get(bid, TERMINAL_THREE_PHASE),
        })
        drawn.append((eid, SYMBOL_LOAD, [(tid, bid)]))
        # P and Q are megawatts and megavars in SINCAL.
        _insert(conn, schema, TYPE_LOAD, {
            "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
            "P": kw / 1000.0, "Q": kvar / 1000.0,
            "S": round(math.hypot(kw, kvar) / 1000.0, 9),
            "cosphi": round(kw / math.hypot(kw, kvar), 6) if (kw or kvar) else 1.0,
            "Flag_Load": 1, "Flag_LoadType": LOAD_TYPE_CONST_PQ,
            "Flag_Lf": LOAD_LF_PQ, "Ul": _bus_kv(net, bid),
        })
        counts["Load"] += 1

    eid, tid, der_drawn, der_count = _write_der(
        conn, schema, net, eid, tid, level_id, group_id, phase_of)
    drawn.extend(der_drawn)
    counts["DCInfeeder"] = der_count

    _write_calc_settings(conn, schema, phases)
    if with_graphics:
        roles = {int(b["bus_id"]): b.get("role") for b in net["buses"]}
        label_of = {}
        for br in net["branches"]:
            label_of[int(br["branch_id"])] = _branch_identity(br)[1]
        for element_id, symbol, ends in drawn:
            label_of.setdefault(element_id, "")
        for b in net["buses"]:
            bid = int(b["bus_id"])
            for element_id, symbol, ends in drawn:
                if len(ends) == 1 and ends[0][1] == bid and not label_of.get(element_id):
                    suffix = {SYMBOL_LOAD: "load", SYMBOL_INFEEDER: "grid infeed",
                              SYMBOL_DCINFEEDER: "PV and BESS"}.get(symbol, "plant")
                    label_of[element_id] = _element_identity(
                        "x", b, suffix)[1]
        sheets = _sheet_plan(net, drawn, roles)
        for sheet in sheets:
            _layout_sheet(sheet, net, roles, net.get("source_bus", 1))
        report = _write_sheets(conn, schema, net, sheets, roles, label_of)
        counts["Sheet"] = report["sheets"]
        counts["Label"] = report["labels"]
        # A label that could not be placed clear of everything else is reported
        # as a count rather than hidden, so a crowded sheet is visible in the
        # build output and can be asserted on.
        counts["LabelCollision"] = len(report["collisions"])
        if report["collisions"]:
            import logging
            logging.getLogger(__name__).warning(
                "SINCAL drawing: %d label(s) could not be placed clear: %s",
                len(report["collisions"]), report["collisions"][:6])
    conn.commit()
    return counts


def _clear(conn, schema):
    """Empty every table this module writes, so an export is idempotent.

    Exporting twice into one project is a normal thing to do -- the audit does
    it to run the balanced and the unbalanced study -- and without this the
    second write collides on a primary key. Clearing here rather than in the
    caller means the list of tables lives next to the code that fills them,
    instead of being a list each caller has to keep in step.
    """
    for table in WRITTEN_TABLES:
        if table in schema:
            conn.execute("DELETE FROM %s" % table)


def _write_der(conn, schema, net, eid, tid, level_id, group_id, phase_of):
    """Write the inverter-coupled plant a bus declares, one element per bus.

    The model puts a `der` block on the bus it connects to. Everything that
    constrains the network lives on the AC side, so the element is rated at the
    inverter and bounded by the plant it serves: the most it can export is the
    PV nameplate, and the most it can absorb is the battery's charging power.

    Returns the new element and terminal counters and the rows drawn.
    """
    drawn, count = [], 0
    if "DCInfeeder" not in schema:
        return eid, tid, drawn, count
    for b in net["buses"]:
        der = b.get("der")
        if not der:
            continue
        bid = int(b["bus_id"])
        kv = round(_bus_kv(net, bid), 6)
        eid += 1
        tid += 1
        short, label, description = _element_identity("der", b, "PV and BESS")
        _insert(conn, schema, "Element", {
            "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
            "Type": TYPE_DCINFEEDER, "Name": label, "ShortName": short,
            "Description": description[:50], "TextVal": description,
            "VoltLevel_ID": level_id[kv], "Flag_State": 1,
            "Flag_Input": FLAG_INPUT_BASE, "Group_ID": group_id.get(bid, GROUP_ID),
        })
        _insert(conn, schema, "Terminal", {
            "Terminal_ID": tid, "Variant_ID": 1, "Flag_Variant": 1,
            "Element_ID": eid, "Node_ID": bid, "TerminalNo": 1,
            "Flag_State": 1, "Flag_Switch": 0,
            "Flag_Terminal": phase_of.get(bid, TERMINAL_THREE_PHASE),
        })
        inverter_mva = float(der.get("inverter_kva", 0.0)) / 1000.0
        pv_mw = float(der.get("pv_kwp", 0.0)) / 1000.0
        bess_mw = float(der.get("bess_kw", 0.0)) / 1000.0
        _insert(conn, schema, TYPE_DCINFEEDER, {
            "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
            "Flag_DCtyp": DC_TYPE_PHOTOVOLTAIC, "Flag_Lf": DC_LF_PQ,
            "Flag_Connect": DC_CONNECT_DIRECT,
            "Sn_Inverter": round(inverter_mva, 9), "Ur_Inverter": kv,
            "Eta_Inverter": 100.0,
            # Shipped out of service, so the balanced and unbalanced baselines
            # are the same network with and without this element present.
            "P": 0.0, "Q": 0.0, "fP": 1.0, "fQ": 1.0,
            # The array is 32.76 kWp behind a 30 kVA inverter, a DC to AC ratio
            # of 1.09 that is ordinary practice. The inverter clips, so the most
            # the plant can ever put on the network is its inverter rating, not
            # its panel rating. Using the panel rating here would dispatch
            # 32.76 kW down a cable sized for 30 kVA and report it at 109
            # percent, which is an artefact of the model, not of the plant.
            "Pmax": round(min(pv_mw, inverter_mva), 9),
            "Pmin": round(-min(bess_mw, inverter_mva), 9),
            "Flag_LfLimit": 0,
            # The energy storage module is not licensed on this installation
            # (CheckLicense("EL", "ESP") returns 6), so the battery has no state
            # of charge here and this key stays unset. Its energy and its
            # charge window live in the CIM and in the scenario preset.
            "EnergyStorage_ID": 0,
        })
        drawn.append((eid, SYMBOL_DCINFEEDER, [(tid, bid)]))
        count += 1
    return eid, tid, drawn, count


def _write_groups(conn, schema, net):
    """One network group per area the model names, returning bus -> group id.

    SinDBCreate seeds a single group and 48 nodes in one group makes a project
    unnavigable, so the model's own grouping is used where it supplies one.
    Measured on this licence: groups do not count against the Xplore node cap,
    ten groups and 48 nodes solve exactly as one group and 48 nodes do.
    """
    names = []
    for b in net["buses"]:
        g = b.get("group")
        if g and g not in names:
            names.append(g)
    if "NetworkGroup" not in schema or not names:
        return {int(b["bus_id"]): GROUP_ID for b in net["buses"]}
    conn.execute("DELETE FROM NetworkGroup")
    ids = {}
    for i, name in enumerate(names, start=1):
        ids[name] = i
        _insert(conn, schema, "NetworkGroup", {
            "Group_ID": i, "Variant_ID": 1, "Flag_Variant": 1,
            "Name": str(name)[:50], "ShortName": str(name)[:SHORTNAME_CHARS],
        })
    return {int(b["bus_id"]): ids.get(b.get("group"), GROUP_ID)
            for b in net["buses"]}


def _write_calc_settings(conn, schema, phases):
    """Point the project's calculation settings at the right power flow.

    SinDBCreate seeds one CalcParameter row set to the symmetric procedure.
    Nothing about the model data switches it, and ``Simulation.Start("ULF")``
    does not either, so an unbalanced project that does not set this solves
    symmetrically or, once single-phase loads are declared, not at all.
    """
    if "CalcParameter" not in schema:
        return
    row = {"Flag_LFmet": (LFMET_UNBALANCED_PHASES if phases
                          else LFMET_ADMITTANCE)}
    if phases:
        row["Flag_LFZ0"] = LFZ0_NEUTRAL_LIKE_PHASE
    cols = [c for c in row if c in schema["CalcParameter"]]
    if cols:
        conn.execute("UPDATE CalcParameter SET %s"
                     % ", ".join("%s = ?" % c for c in cols),
                     [row[c] for c in cols])


# ---------------------------------------------------------------------------
# The drawing
# ---------------------------------------------------------------------------
#
# A SINCAL project opens on a blank sheet unless three tables agree: a
# GraphicNode per node, a GraphicElement per element, and a GraphicTerminal
# tying each element's symbol to its node's symbol. GraphicTerminal is the one
# that actually draws a connection, and GraphicBucklePoint is what bends it, so
# without either you get bare circles joined by nothing, or by diagonals.
#
# Geometry is in millimetres of page throughout this section and converted once
# at the write, because a 6 mm clearance is easier to reason about than 0.006 in
# a column labelled metres. See SINCAL-SCHEMA-NOTES.md section 8 for why those
# two units differ.

# Page furniture, all in millimetres.
SHEET_W_MM = 420.0                 # A3 landscape
SHEET_H_MM = 297.0
MARGIN_MM = 20.0
TITLE_BLOCK_MM = 16.0              # reserved strip along the bottom
FEEDER_PITCH_MM = 24.0             # spacing of parallel feeders
LAYER_PITCH_MM = 46.0              # vertical spacing between depths
STUB_MM = 15.0                     # how far a one-terminal symbol hangs off
BUSBAR_OVERHANG_MM = 10.0          # how far a bar runs past its outermost feeder
MIN_CLEARANCE_MM = 6.0             # between any two symbol centres

# GraphicText geometry, from the sample conventions.
LABEL_FONT = "Arial"
LABEL_FONT_STYLE = 16              # standard
LABEL_SIZE_NODE = 9.33
LABEL_SIZE_ELEMENT = 6.66
LABEL_OFFSET_MM = 3.0
# TextAlign, from the help: 0 left-up, 1 middle-up, 2 right-up, 3 left-middle,
# 4 middle-middle, 5 right-middle, 6 left-down, 7 middle-down, 8 right-down.
ALIGN_LEFT_MIDDLE = 3
ALIGN_RIGHT_MIDDLE = 5
ALIGN_MIDDLE_UP = 1
ALIGN_MIDDLE_DOWN = 7
# A conservative average glyph width as a fraction of the text height, used to
# estimate a label's box for the collision test.
GLYPH_WIDTH_EM = 0.55
# FontSize is a text height in 0.25 mm steps, the same unit NodeSize uses.
FONT_STEP_MM = 0.25


def mm(value):
    """Millimetres of page to the metres SINCAL stores coordinates in."""
    return round(value / 1000.0, 6)


class Sheet:
    """One drawing area: its nodes and elements, and where they sit."""

    def __init__(self, area_id, name):
        self.area_id = area_id
        self.name = name
        self.nodes = []            # bus ids, in drawing order
        self.elements = []         # (element_id, symbol, [(terminal_id, bus)])
        self.pos = {}              # bus -> (x, y) in mm
        self.span = {}             # bus -> (x1, x2) in mm for a busbar
        self.sym = {}              # element_id -> (x, y) in mm
        self.labels = []           # placed label boxes, for the collision test


def _sheet_plan(net, drawn, roles):
    """Split the network into sheets, one per network area the model names.

    An element is drawn on the sheet of the node it serves, except a transformer,
    which is drawn with the switchboard that feeds it. Every node an element
    touches is drawn on that element's sheet, so a node on the boundary between
    two areas appears on both. That is exactly what SINCAL's own multi-area
    projects do, and it is what lets a run continue across a sheet edge.
    """
    group_of = {int(b["bus_id"]): b.get("group") or "Network"
                for b in net["buses"]}
    branch_of = {}
    for br in net["branches"]:
        branch_of[int(br["branch_id"])] = br

    # Element id -> the group whose sheet it belongs on.
    element_group = {}
    for element_id, symbol, ends in drawn:
        br = branch_of.get(element_id)
        if br is not None and br.get("is_transformer"):
            element_group[element_id] = group_of.get(int(br["from_bus"]))
        elif br is not None:
            element_group[element_id] = group_of.get(int(br["to_bus"]))
        else:
            element_group[element_id] = group_of.get(ends[0][1])

    # Sheets follow the order the groups first appear, so they are stable.
    order = []
    for b in net["buses"]:
        g = group_of[int(b["bus_id"])]
        if g not in order:
            order.append(g)
    # The public network and the areas it feeds share one sheet: they are a
    # single 11 kV run and splitting them would put a transformer on its own.
    public = [g for g in order
              if all(roles.get(int(b["bus_id"])) != "hv_switchboard"
                     and roles.get(int(b["bus_id"])) != "customer_substation"
                     for b in net["buses"] if group_of[int(b["bus_id"])] == g)]
    utility = [g for g in order if g in public]
    grouped = [utility] + [[g] for g in order if g not in public]

    sheets, area_id = [], 0
    for groups in grouped:
        if not groups:
            continue
        area_id += 1
        title = " and ".join(groups) if len(groups) < 3 else groups[0]
        sheet = Sheet(area_id, title)
        seen = set()
        for element_id, symbol, ends in drawn:
            if element_group.get(element_id) not in groups:
                continue
            sheet.elements.append((element_id, symbol, ends))
            for _terminal_id, bus in ends:
                if bus not in seen:
                    seen.add(bus)
                    sheet.nodes.append(bus)
        if sheet.elements:
            sheets.append(sheet)
    return sheets


def _layout_sheet(sheet, net, roles, source_bus):
    """Place a sheet's nodes on a layered tree and its elements between them.

    Depth comes from the network's own direction of supply, so the sheet reads
    top to bottom the way power flows. Siblings are spread at the feeder pitch
    and a parent sits over the middle of its children, which keeps every
    connection either vertical or a single dogleg.
    """
    edges = {}
    for element_id, symbol, ends in sheet.elements:
        if len(ends) >= 2:
            a, b = ends[0][1], ends[1][1]
            edges.setdefault(a, []).append(b)
            edges.setdefault(b, []).append(a)

    on_sheet = set(sheet.nodes)
    # Root: whichever node is nearest the network source, so the sheet hangs
    # the same way up as the network does.
    depth_from_source = _depth_from(net, source_bus)
    root = min(sheet.nodes, key=lambda b: (depth_from_source.get(b, 1 << 30), b))

    depth, parent, order = {root: 0}, {}, [root]
    queue = [root]
    while queue:
        node = queue.pop(0)
        for nb in sorted(edges.get(node, ())):
            if nb in depth or nb not in on_sheet:
                continue
            depth[nb] = depth[node] + 1
            parent[nb] = node
            order.append(nb)
            queue.append(nb)
    for node in sheet.nodes:                     # anything unreachable
        depth.setdefault(node, 0)
        if node not in order:
            order.append(node)

    children = {}
    for node in order:
        if node in parent:
            children.setdefault(parent[node], []).append(node)

    # Leaves take consecutive slots at the feeder pitch; a parent centres over
    # its children. Walking deepest-first means children are placed before the
    # parent that has to average them.
    slot = [0]
    x_of = {}

    def place(node):
        kids = children.get(node, [])
        if not kids:
            x_of[node] = slot[0] * FEEDER_PITCH_MM
            slot[0] += 1
            return
        for kid in kids:
            place(kid)
        x_of[node] = sum(x_of[k] for k in kids) / float(len(kids))

    place(root)
    for node in order:
        x_of.setdefault(node, slot[0] * FEEDER_PITCH_MM)

    # Scale into the drawable area and centre it.
    usable_w = SHEET_W_MM - 2 * MARGIN_MM
    usable_h = SHEET_H_MM - 2 * MARGIN_MM - TITLE_BLOCK_MM
    width = (max(x_of.values()) - min(x_of.values())) or 1.0
    scale = min(1.0, usable_w / width)
    x0 = min(x_of.values())
    max_depth = max(depth.values()) or 1
    layer = min(LAYER_PITCH_MM, usable_h / max(max_depth, 1))
    left = MARGIN_MM + (usable_w - width * scale) / 2.0

    for node in sheet.nodes:
        x = left + (x_of[node] - x0) * scale
        y = SHEET_H_MM - MARGIN_MM - depth[node] * layer
        sheet.pos[node] = (x, y)
        kids = children.get(node, [])
        if kids and (roles.get(node) in BUSBAR_ROLES or len(kids) >= 3):
            xs = [left + (x_of[k] - x0) * scale for k in kids] + [x]
            sheet.span[node] = (min(xs) - BUSBAR_OVERHANG_MM,
                                max(xs) + BUSBAR_OVERHANG_MM)

    # Element symbols: a two-terminal element sits on the run between its two
    # nodes, a one-terminal element on a stub below its node.
    used_stubs = {}
    for element_id, symbol, ends in sheet.elements:
        if len(ends) >= 2:
            (x1, y1), (x2, y2) = sheet.pos[ends[0][1]], sheet.pos[ends[1][1]]
            child = ends[1][1] if parent.get(ends[1][1]) == ends[0][1] else ends[0][1]
            sheet.sym[element_id] = (sheet.pos[child][0], (y1 + y2) / 2.0)
        else:
            bus = ends[0][1]
            x, y = sheet.pos[bus]
            n = used_stubs.get(bus, 0)
            used_stubs[bus] = n + 1
            # A bus carrying more than one one-terminal element fans them out
            # sideways so their symbols do not land on top of each other.
            sheet.sym[element_id] = (x + n * FEEDER_PITCH_MM * 0.6, y - STUB_MM)
    return sheet


def _depth_from(net, source_bus):
    """Hop count from the network source, over the whole network."""
    adj = {}
    for br in net["branches"]:
        a, b = int(br["from_bus"]), int(br["to_bus"])
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    depth, queue = {int(source_bus): 0}, [int(source_bus)]
    while queue:
        node = queue.pop(0)
        for nb in adj.get(node, ()):
            if nb not in depth:
                depth[nb] = depth[node] + 1
                queue.append(nb)
    return depth


def _label_box(text, size, x, y, align):
    """A conservative bounding box in mm for a label, for the collision test."""
    h = size * FONT_STEP_MM
    w = len(str(text)) * GLYPH_WIDTH_EM * h
    if align == ALIGN_LEFT_MIDDLE:
        return (x, y - h / 2.0, x + w, y + h / 2.0)
    if align == ALIGN_RIGHT_MIDDLE:
        return (x - w, y - h / 2.0, x, y + h / 2.0)
    if align == ALIGN_MIDDLE_UP:
        return (x - w / 2.0, y, x + w / 2.0, y + h)
    return (x - w / 2.0, y - h, x + w / 2.0, y)


def _overlaps(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


_ALIGN_OFFSET = {
    ALIGN_LEFT_MIDDLE: (1.0, 0.0),
    ALIGN_RIGHT_MIDDLE: (-1.0, 0.0),
    ALIGN_MIDDLE_UP: (0.0, 1.0),
    ALIGN_MIDDLE_DOWN: (0.0, -1.0),
}


def _place_label(sheet, text, size, anchor, preferred):
    """Choose an offset for a label that clears everything already placed.

    The preferred side first, then the other three, then the offset steps out.
    Returns (align, dx, dy) in millimetres, or None when nothing fits, which is
    reported as a collision rather than silently hidden.
    """
    x, y = anchor
    tried = []
    for step in (1.0, 1.6, 2.4, 3.4, 4.6):
        for align in (preferred, ALIGN_MIDDLE_UP, ALIGN_MIDDLE_DOWN,
                      ALIGN_LEFT_MIDDLE, ALIGN_RIGHT_MIDDLE):
            ux, uy = _ALIGN_OFFSET[align]
            d = LABEL_OFFSET_MM * step
            key = (align, ux * d, uy * d)
            if key in tried:
                continue
            tried.append(key)
    for align, dx, dy in tried:
        box = _label_box(text, size, x + dx, y + dy, align)
        if box[0] < 2.0 or box[2] > SHEET_W_MM - 2.0:
            continue
        if box[1] < TITLE_BLOCK_MM or box[3] > SHEET_H_MM - 2.0:
            continue
        if any(_overlaps(box, other) for other in sheet.labels):
            continue
        sheet.labels.append(box)
        return align, dx, dy
    return None


def _write_sheets(conn, schema, net, sheets, roles, element_label):
    """Write every graphic table, for every sheet.

    Graphic ids are derived from the carrier's id and the sheet rather than
    counted up, so two builds of the same model produce the same rows in the
    same order.
    """
    if "GraphicNode" not in schema:
        return {"sheets": 0, "labels": 0, "collisions": []}

    try:
        conn.execute("DELETE FROM GraphicAreaTile")
    except sqlite3.Error:
        pass
    layer = 1
    row = conn.execute("SELECT GraphicLayer_ID FROM GraphicLayer LIMIT 1").fetchone()
    if row:
        layer = row[0]
    obj_type = None
    try:
        row = conn.execute("SELECT GraphicType_ID FROM GraphicObjectType "
                           "LIMIT 1").fetchone()
        obj_type = row[0] if row else None
    except sqlite3.Error:
        obj_type = None

    node_label = {int(b["bus_id"]): _bus_identity(b)[1] for b in net["buses"]}
    stride = len(net["buses"]) + len(net["branches"]) + 64
    state = {"text": 0, "labels": 0}
    collisions = []

    def add_text(size, align, dx, dy):
        state["text"] += 1
        _insert(conn, schema, "GraphicText", {
            "GraphicText_ID": state["text"], "Variant_ID": 1, "Flag_Variant": 1,
            "GraphicLayer_ID": layer, "Font": LABEL_FONT,
            "FontStyle": LABEL_FONT_STYLE, "FontSize": size,
            "TextAlign": align, "TextOrient": 0, "TextColor": 0,
            "Visible": 1, "AdjustAngle": 1, "Angle": 0.0,
            "Pos1": mm(dx), "Pos2": mm(dy), "RowTextNo": 0, "AngleTermNo": 1,
        })
        return state["text"]

    for sheet in sheets:
        _insert(conn, schema, "GraphicAreaTile", {
            "GraphicArea_ID": sheet.area_id, "Variant_ID": 1,
            "Name": ("%02d %s" % (sheet.area_id, sheet.name))[:50],
            # The sheet is stored in centimetres while the coordinates on it are
            # in metres, so this is the one place the two units meet.
            "AreaWidth": SHEET_W_MM / 10.0, "AreaHeight": SHEET_H_MM / 10.0,
            "GridWidth": 10, "GridHeight": 10,
            "ScalePaper": 1.0, "ScaleReal": 1.0, "Scale2": 1,
            "Pos": sheet.area_id, "TileIndex": "",
        })

        graphic_node = {}
        for bus in sheet.nodes:
            x, y = sheet.pos[bus]
            gn_id = sheet.area_id * stride + bus
            graphic_node[bus] = gn_id
            span = sheet.span.get(bus)
            text = node_label.get(bus, "bus_%d" % bus)
            anchor = ((span[0], y) if span else (x, y))
            placed = _place_label(sheet, text, LABEL_SIZE_NODE, anchor,
                                  ALIGN_RIGHT_MIDDLE if span else ALIGN_LEFT_MIDDLE)
            if placed is None:
                collisions.append(("node", bus, sheet.name))
                placed = (ALIGN_LEFT_MIDDLE, LABEL_OFFSET_MM, 0.0)
            else:
                state["labels"] += 1
            row = {
                "GraphicNode_ID": gn_id, "Variant_ID": 1, "Flag_Variant": 1,
                "GraphicArea_ID": sheet.area_id, "GraphicLayer_ID": layer,
                "Node_ID": bus,
                "GraphicText_ID1": add_text(LABEL_SIZE_NODE, *placed),
                "NodeStartX": mm(span[0] if span else x), "NodeStartY": mm(y),
                "NodeEndX": mm(span[1] if span else x), "NodeEndY": mm(y),
                "SymType": NODE_SYMBOL_BUSBAR if span else NODE_SYMBOL_CIRCLE,
                "NodeSize": NODE_SIZE_BUSBAR if span else NODE_SIZE_POINT,
            }
            if obj_type is not None:
                row["GraphicType_ID"] = obj_type
            _insert(conn, schema, "GraphicNode", row)

        for element_id, symbol, ends in sheet.elements:
            cx, cy = sheet.sym[element_id]
            ge_id = sheet.area_id * stride + element_id
            text = element_label.get(element_id, "")
            placed = _place_label(sheet, text, LABEL_SIZE_ELEMENT, (cx, cy),
                                  ALIGN_LEFT_MIDDLE)
            if placed is None:
                collisions.append(("element", element_id, sheet.name))
                placed = (ALIGN_LEFT_MIDDLE, LABEL_OFFSET_MM, 0.0)
            else:
                state["labels"] += 1
            row = {
                "GraphicElement_ID": ge_id, "Variant_ID": 1, "Flag_Variant": 1,
                "GraphicArea_ID": sheet.area_id, "GraphicLayer_ID": layer,
                "Element_ID": element_id,
                "GraphicText_ID1": add_text(LABEL_SIZE_ELEMENT, *placed),
                "SymbolType": symbol, "SymbolNo": 0, "SymbolDef": SYMBOL_DEF,
                "SymCenterX": mm(cx), "SymCenterY": mm(cy),
                "SymbolSize": ELEMENT_SYMBOL_SIZE,
            }
            if obj_type is not None:
                row["GraphicType_ID"] = obj_type
            _insert(conn, schema, "GraphicElement", row)

            for terminal_id, bus in ends:
                gt_id = sheet.area_id * stride + terminal_id
                x, y = sheet.pos[bus]
                span = sheet.span.get(bus)
                # A feeder meets a busbar under its own symbol rather than at
                # the bar's centre, which is what spreads ten feeders along the
                # ATC and AMDC switchboards instead of stacking them.
                ax = min(max(cx, span[0]), span[1]) if span else x
                _insert(conn, schema, "GraphicTerminal", {
                    "GraphicTerminal_ID": gt_id, "Variant_ID": 1,
                    "Flag_Variant": 1, "GraphicArea_ID": sheet.area_id,
                    "GraphicElement_ID": ge_id, "Terminal_ID": terminal_id,
                    "GraphicNode_ID": graphic_node[bus],
                    "PosX": mm(ax), "PosY": mm(y), "SwtType": 0,
                })
                # One buckle makes the run a dogleg: out of the symbol on its
                # own row, then square onto the node. Without it SINCAL draws a
                # single diagonal from the symbol to the node, which is where
                # the spaghetti in the old drawing came from.
                if abs(ax - cx) > 1e-9 and abs(y - cy) > 1e-9:
                    _insert(conn, schema, "GraphicBucklePoint", {
                        "GraphicPoint_ID": gt_id, "Variant_ID": 1,
                        "Flag_Variant": 1, "GraphicTerminal_ID": gt_id,
                        "NoPoint": 1, "PosX": mm(ax), "PosY": mm(cy),
                    })

    return {"sheets": len(sheets), "labels": state["labels"],
            "collisions": collisions}


def write_element_states(conn, updates, op_to_sign=None):
    """Apply per-step P and Q updates onto the Load rows written above.

    `updates` is the solver contract's list of {op, bus_id, kw, kvar}. PV and
    BESS discharge are negative load, EV and load are positive, which is how the
    stack's other solvers model them.
    """
    schema = discover(conn)
    sign = op_to_sign or {"load": 1.0, "pv": -1.0, "pv_q": -1.0,
                          "bess": -1.0, "ev": 1.0}
    by_bus = {}
    for u in updates:
        bus = int(u["bus_id"])
        s = sign.get(u.get("op", "load"), 1.0)
        p, q = by_bus.get(bus, (0.0, 0.0))
        if u.get("op") == "pv_q":
            q += s * float(u.get("kvar", 0.0) or 0.0)
        else:
            p += s * float(u.get("kw", 0.0) or 0.0)
            q += s * float(u.get("kvar", 0.0) or 0.0)
        by_bus[bus] = (p, q)
    applied = 0
    for bus, (kw, kvar) in by_bus.items():
        row = conn.execute(
            "SELECT e.Element_ID FROM Element e JOIN Terminal t "
            "ON t.Element_ID = e.Element_ID WHERE e.Type = ? AND t.Node_ID = ?",
            (TYPE_LOAD, bus)).fetchone()
        if row is None:
            continue
        conn.execute("UPDATE Load SET P = ?, Q = ? WHERE Element_ID = ?",
                     (kw / 1000.0, kvar / 1000.0, row[0]))
        applied += 1
    conn.commit()
    return applied


# ULF results are stored per phase set; Flag_Phase 7 is the L123 row, which is
# the one carrying all three conductors' values.
_ULF_ALL_PHASES = 7


def read_node_voltages(conn):
    """Per-unit node voltages keyed by bus id.

    Reads the symmetric result table, then the unbalanced one, so a caller does
    not have to know which procedure ran. An unbalanced result is reduced to the
    mean of the three phase magnitudes, which is what the OpenDSS adapter
    reports for the same quantity, so the two remain comparable.
    """
    out = {}
    try:
        rows = conn.execute("SELECT Node_ID, U_Un FROM LFNodeResult").fetchall()
    except sqlite3.Error:
        rows = []
    for node_id, u_un in rows:
        if u_un is not None:
            out[int(node_id)] = float(u_un) / 100.0
    if out:
        return out
    for node_id, phases in read_phase_voltages(conn).items():
        if phases:
            out[node_id] = sum(phases) / len(phases)
    return out


def read_phase_voltages(conn):
    """bus_id -> [U1, U2, U3] per unit, from an unbalanced result.

    Empty when the project was solved symmetrically, which is the honest
    answer: a symmetric solve has no per-phase voltages to report.
    """
    out = {}
    try:
        rows = conn.execute(
            "SELECT Node_ID, U1_Un, U2_Un, U3_Un FROM ULFNodeResult "
            "WHERE Flag_Phase = ?", (_ULF_ALL_PHASES,)).fetchall()
    except sqlite3.Error:
        return out
    for node_id, u1, u2, u3 in rows:
        mags = [float(u) / 100.0 for u in (u1, u2, u3) if u is not None]
        if mags:
            out[int(node_id)] = mags
    return out


def read_max_vuf_pct(conn):
    """Worst voltage unbalance factor across the network, in percent.

    The IEC definition: negative- over positive-sequence voltage magnitude,
    built from the per-phase magnitudes and angles SINCAL reports. Returns 0.0
    from a symmetric solve, which has no unbalance by construction.
    """
    try:
        rows = conn.execute(
            "SELECT U1, U2, U3, phi1, phi2, phi3 FROM ULFNodeResult "
            "WHERE Flag_Phase = ?", (_ULF_ALL_PHASES,)).fetchall()
    except sqlite3.Error:
        return 0.0
    a = complex(-0.5, 3 ** 0.5 / 2)          # 1 at 120 degrees
    worst = 0.0
    for u1, u2, u3, p1, p2, p3 in rows:
        if None in (u1, u2, u3, p1, p2, p3):
            continue
        v = [float(u) * complex(math.cos(math.radians(float(p))),
                                math.sin(math.radians(float(p))))
             for u, p in ((u1, p1), (u2, p2), (u3, p3))]
        pos = (v[0] + a * v[1] + a * a * v[2]) / 3.0
        neg = (v[0] + a * a * v[1] + a * v[2]) / 3.0
        if abs(pos) > 1e-9:
            worst = max(worst, abs(neg) / abs(pos) * 100.0)
    return worst


def read_branch_loadings(conn, branch_ids=None):
    """Branch utilisation in percent, keyed by the model's branch_id.

    Elements are written in branch order, so Element_ID equals branch_id for
    every line and transformer, and the loads and the infeeder are numbered
    after them. The symmetric table holds one row per terminal with ``S_Sn``,
    apparent power over rated power; the unbalanced one instead reports each
    conductor's current as a percentage of rating, so the worst of the three is
    that terminal's figure. Either way an element's utilisation is its worst
    terminal.
    """
    out = {}
    for sql, params in (
        ("SELECT e.Element_ID, MAX(b.S_Sn) FROM LFBranchResult b "
         "JOIN Terminal t ON t.Terminal_ID = b.Terminal1_ID "
         "JOIN Element e ON e.Element_ID = t.Element_ID "
         "WHERE e.Type IN (?, ?) GROUP BY e.Element_ID",
         (TYPE_LINE, TYPE_TRANSFORMER)),
        ("SELECT e.Element_ID, MAX(MAX(b.I1_In, b.I2_In, b.I3_In)) "
         "FROM ULFBranchResult b "
         "JOIN Terminal t ON t.Terminal_ID = b.Terminal1_ID "
         "JOIN Element e ON e.Element_ID = t.Element_ID "
         "WHERE e.Type IN (?, ?) AND b.Flag_Phase = ? GROUP BY e.Element_ID",
         (TYPE_LINE, TYPE_TRANSFORMER, _ULF_ALL_PHASES)),
    ):
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            rows = []
        for element_id, util in rows:
            if util is not None:
                out[int(element_id)] = float(util)
        if out:
            break
    if branch_ids is not None:
        return {b: out.get(int(b), 0.0) for b in branch_ids}
    return out


def read_power_summary(conn):
    """Total losses and delivered power, both in kW.

    LFPowDataResult splits losses into line and transformer copper terms; its
    ``Pl`` column stays zero in this schema, so the two are summed instead.
    Values are megawatts in the database.

    An unbalanced run writes one row per phase plus a whole-network row; the
    whole-network row is the one to read, so it is ordered first. A release
    whose result table has no ``Flag_Phase`` column only ever writes the one
    row, so the plain query is the fallback rather than an error.
    """
    row = None
    for sql, params in (
        ("SELECT Plline, Pltrans, Plfe, Pload FROM LFPowDataResult "
         "ORDER BY (Flag_Phase = ?) DESC", (_ULF_ALL_PHASES,)),
        ("SELECT Plline, Pltrans, Plfe, Pload FROM LFPowDataResult", ()),
    ):
        try:
            row = conn.execute(sql, params).fetchone()
            break
        except sqlite3.Error:
            continue
    if row is None:
        return {"losses_kw": 0.0, "total_kw": 0.0}
    line, trafo, iron, load = (float(v or 0.0) for v in row)
    return {"losses_kw": (line + trafo + iron) * 1000.0,
            "total_kw": load * 1000.0}
