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
OPTIONAL_TABLES = ("NeutralPointImp", "CalcParameter")

# SINCAL element type strings, as written into Element.Type.
TYPE_LINE = "Line"
TYPE_TRANSFORMER = "TwoWindingTransformer"
TYPE_LOAD = "Load"
TYPE_INFEEDER = "Infeeder"

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

# Node.Flag_Type 1 is a plain node (0 is not a defined value).
NODE_TYPE_NODE = 1
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
    counts = {"VoltageLevel": 0, "Node": 0, "Line": 0,
              "TwoWindingTransformer": 0, "Load": 0, "Infeeder": 0}

    # One voltage level per distinct nominal voltage in the model.
    levels = sorted({round(_bus_kv(net, b["bus_id"]), 6) for b in net["buses"]},
                    reverse=True)
    level_id = {}
    conn.execute("DELETE FROM VoltageLevel")
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
        row = {
            "Node_ID": bid, "Variant_ID": 1, "Flag_Variant": 1,
            "Name": ("bus_%d %s" % (bid, b.get("name", "")))[:50],
            "ShortName": ("b%d" % bid)[:8],
            "VoltLevel_ID": level_id[kv], "Flag_Type": NODE_TYPE_NODE, "Un": kv,
            "Group_ID": GROUP_ID,
        }
        if b.get("lat") is not None and b.get("lon") is not None:
            row.update({"Flag_Pos": NODE_POS_LATLON,
                        "lat": float(b["lat"]), "lon": float(b["lon"])})
        _insert(conn, schema, "Node", row)
        counts["Node"] += 1

    eid = 0
    tid = 0
    neutral_points = []
    for br in net["branches"]:
        eid += 1
        f, t = int(br["from_bus"]), int(br["to_bus"])
        kv_f = round(_bus_kv(net, f), 6)
        is_tx = bool(br.get("is_transformer"))
        etype = TYPE_TRANSFORMER if is_tx else TYPE_LINE
        _insert(conn, schema, "Element", {
            "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
            "Type": etype, "Name": ("br_%s %s" % (br.get("branch_id"), br.get("name", "")))[:50],
            "ShortName": ("br%s" % br.get("branch_id"))[:8],
            "VoltLevel_ID": level_id[kv_f], "Flag_State": 1,
            "Flag_Input": FLAG_INPUT_WITH_Z0, "Group_ID": GROUP_ID,
        })
        # A branch is three-phase plant even where the board it feeds is
        # single-phase: only the load declares a phase.
        for no, node in ((1, f), (2, t)):
            tid += 1
            _insert(conn, schema, "Terminal", {
                "Terminal_ID": tid, "Variant_ID": 1, "Flag_Variant": 1,
                "Element_ID": eid, "Node_ID": node, "TerminalNo": no,
                "Flag_State": 1, "Flag_Switch": 0,
                "Flag_Terminal": TERMINAL_THREE_PHASE,
            })
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
            _insert(conn, schema, "Element", {
                "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
                "Type": TYPE_INFEEDER, "Name": ("infeed_bus_%d" % bid)[:50],
                "ShortName": ("inf%d" % bid)[:8],
                "VoltLevel_ID": level_id[round(_bus_kv(net, bid), 6)],
                "Flag_State": 1, "Flag_Input": FLAG_INPUT_BASE,
                "Group_ID": GROUP_ID,
            })
            _insert(conn, schema, "Terminal", {
                "Terminal_ID": tid, "Variant_ID": 1, "Flag_Variant": 1,
                "Element_ID": eid, "Node_ID": bid, "TerminalNo": 1,
                "Flag_State": 1, "Flag_Switch": 0,
                "Flag_Terminal": TERMINAL_THREE_PHASE,
            })
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
        _insert(conn, schema, "Element", {
            "Element_ID": eid, "Variant_ID": 1, "Flag_Variant": 1,
            "Type": TYPE_LOAD, "Name": ("load_bus_%d" % bid)[:50],
            "ShortName": ("ld%d" % bid)[:8],
            "VoltLevel_ID": level_id[round(_bus_kv(net, bid), 6)],
            "Flag_State": 1, "Flag_Input": FLAG_INPUT_BASE,
            "Group_ID": GROUP_ID,
        })
        _insert(conn, schema, "Terminal", {
            "Terminal_ID": tid, "Variant_ID": 1, "Flag_Variant": 1,
            "Element_ID": eid, "Node_ID": bid, "TerminalNo": 1,
            "Flag_State": 1, "Flag_Switch": 0,
            "Flag_Terminal": phase_of.get(bid, TERMINAL_THREE_PHASE),
        })
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

    _write_calc_settings(conn, schema, phases)
    if with_graphics:
        _write_graphics(conn, schema, net)
    conn.commit()
    return counts


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


def _write_graphics(conn, schema, net):
    """Place nodes on the drawing from their WGS84 positions, so it opens drawn."""
    if "GraphicNode" not in schema:
        return
    pts = [(int(b["bus_id"]), b.get("lat"), b.get("lon"))
           for b in net["buses"] if b.get("lat") is not None]
    if not pts:
        return
    lats = [p[1] for p in pts]
    lons = [p[2] for p in pts]
    lat0, lon0 = min(lats), min(lons)
    # Metres east and north of the south-west corner, then millimetres on the
    # drawing at a fixed scale, which keeps the layout geographic.
    scale = 0.35
    layer = 1
    row = conn.execute("SELECT GraphicLayer_ID FROM GraphicLayer LIMIT 1").fetchone()
    if row:
        layer = row[0]
    for i, (bid, lat, lon) in enumerate(pts, start=1):
        y = (lat - lat0) * 111320.0 * scale
        x = (lon - lon0) * 111320.0 * math.cos(math.radians(lat)) * scale
        _insert(conn, schema, "GraphicNode", {
            "GraphicNode_ID": i, "Variant_ID": 1, "Flag_Variant": 1,
            "GraphicLayer_ID": layer, "Node_ID": bid,
            "NodeStartX": round(x, 3), "NodeStartY": round(y, 3),
            "NodeEndX": round(x + 8.0, 3), "NodeEndY": round(y, 3),
            "SymType": 1, "NodeSize": 8.0,
        })


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
