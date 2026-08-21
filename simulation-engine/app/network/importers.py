"""Importers for external network-model formats -> the internal NetworkModel dict.

The QSTS engine needs buses (id, base voltage, base load) + branches (from/to,
R/X in ohms, rating, transformer flag), so the rich source formats are mapped
down to that:

  - PSS/E RAW  (.raw):  BUS, LOAD, BRANCH and 2-winding TRANSFORMER sections.
    Any revision -- the legacy v32/33 "0 / END OF ..." section terminators and
    the v34/35/36 "@!" labels + "BEGIN/END OF <X> DATA" markers are both read.
  - PSS/E RAWX (.rawx): the JSON variant of RAW.
  - CIM / CGMES (.xml): any CIM version (CIM14/15/16/17, CGMES CIM100). Reads
    ConnectivityNode/TopologicalNode, ACLineSegment, PowerTransformer(End),
    EnergyConsumer, BaseVoltage/VoltageLevel. Both the standard CGMES transformer
    (one PowerTransformerEnd per winding, each with its own terminal) and the
    simplified single-end-with-two-terminals form are handled; bus voltages come
    from container VoltageLevels, equipment BaseVoltage, or winding ratedU.

Each returns a dict ready for NetworkModel.from_dict() (which then validates
connectivity, ids, etc.).

Per-bus base voltages are preserved, so multi-voltage feeders (MV/LV) import
intact; 2-winding TRANSFORMER records are flagged ``is_transformer`` and become
OpenDSS Transformer elements between the two levels. PSS/E per-unit impedances
are converted to ohms on the from-bus base kV and the case SBASE; CIM impedances
are already ohms. Multi-winding transformers, DC links, and switching devices
are skipped.

Beyond topology and impedance, each format is read for as much of the plant
detail as it can express, because a file that loses it solves a different
network from the one it names:

  - Fixed transformer ratio. RAW and RAWX give it as WINDV1/WINDV2 (unit-coded
    by CW); CIM as the quotient of a winding's ``ratedU`` and its
    ``BaseVoltage.nominalVoltage``, multiplied by any ``RatioTapChanger``
    position. Both land in the branch's ``tap``.
  - Vector group. RAW's ANG1 phase shift and CIM's ``connectionKind`` pair both
    reduce to ``wye_wye`` or ``delta_wye``.
  - Thermal rating. RAW carries RATEA directly; CIM needs the
    ``OperationalLimit`` profile, where ``ApparentPowerLimit`` is read straight
    and ``CurrentLimit`` is converted once bus voltages are known. Only
    continuous limits count, not short-term ones.
  - Zero-sequence impedance, from ``ACLineSegment.r0``/``.x0``.
  - Per-bus phase connection, from ``Terminal.phases`` or
    ``EnergyConsumerPhase``, so a single-phase lateral stays single-phase and an
    unbalanced study reports a real voltage unbalance factor.

PSS/E is a positive-sequence format: it has no per-bus phase or zero-sequence
line data to read, so a RAW import is balanced three-phase by construction.
"""

import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque


class NetworkImportError(ValueError):
    """Raised when an external network file cannot be parsed."""


def _slug(name, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or "")).strip("_")[:64]
    return s or fallback


def _assemble(network_id, name, buses_raw: dict, branches_raw: list,
              sbase_mva: float, impedance_in_ohms: bool = False) -> dict:
    """Build the internal NetworkModel dict from parsed buses/branches.

    buses_raw: {bus_id: {base_kv, name, pl_mw, ql_mvar, ide, phases}}
    branches_raw: [{from, to, r, x, rate_mva, tap, connection, r0, x0}] with
    r/x/r0/x0 in pu (or ohms if impedance_in_ohms).

    ``tap``, ``connection``, ``phases`` and the zero-sequence pair are optional:
    a source format that cannot express them simply omits them and the model
    falls back to its documented defaults (unity tap, wye-wye, three phase,
    3x the positive-sequence impedance).
    """
    if not buses_raw:
        raise NetworkImportError("No buses found in the network file.")

    source = next((b for b, d in buses_raw.items() if d.get("ide") == 3), None)
    if source is None:
        source = min(buses_raw)
    base_kv = float(buses_raw[source].get("base_kv") or 1.0) or 1.0

    buses = []
    for bid, d in sorted(buses_raw.items()):
        bus = {
            "bus_id": int(bid),
            "name": d.get("name") or f"bus_{bid}",
            "base_kv": round(float(d.get("base_kv") or base_kv) or base_kv, 4),
            "base_load_kw": round(float(d.get("pl_mw", 0.0)) * 1000.0, 3),
            "base_load_kvar": round(float(d.get("ql_mvar", 0.0)) * 1000.0, 3),
        }
        # Optional WGS84 position (CIM GL profile / RAW substation records).
        if d.get("lat") is not None and d.get("lon") is not None:
            bus["lat"] = round(float(d["lat"]), 6)
            bus["lon"] = round(float(d["lon"]), 6)
        # Per-bus phase connection, for unbalanced studies. Only carried when
        # the file says something other than full three-phase, so a balanced
        # source format still imports byte-for-byte as it always did.
        phases = d.get("phases")
        if phases and str(phases).lower() not in ("abc", "abcn"):
            bus["phases"] = str(phases).lower()
        buses.append(bus)

    branches = []
    for i, br in enumerate(branches_raw, start=1):
        f, t = int(br["from"]), int(br["to"])
        if f == t or f not in buses_raw or t not in buses_raw:
            continue
        if impedance_in_ohms:
            scale = 1.0
        else:
            vkv = float(buses_raw.get(f, {}).get("base_kv") or base_kv)
            scale = (vkv * vkv) / sbase_mva if sbase_mva > 0 else 1.0
        r_ohm = float(br.get("r", 0.0)) * scale
        x_ohm = float(br.get("x", 0.0)) * scale
        rate_mva = float(br.get("rate_mva") or sbase_mva or 1.0) or 1.0
        entry = {
            "branch_id": i, "from_bus": f, "to_bus": t,
            "r_ohm": round(max(r_ohm, 0.0), 6), "x_ohm": round(max(x_ohm, 0.0), 6),
            "rating_kva": round(rate_mva * 1000.0, 1),
            "is_transformer": bool(br.get("is_transformer")),
        }
        if br.get("r0") is not None and br.get("x0") is not None:
            entry["r0_ohm"] = round(max(float(br["r0"]) * scale, 0.0), 6)
            entry["x0_ohm"] = round(max(float(br["x0"]) * scale, 0.0), 6)
        if entry["is_transformer"]:
            # 0.8 to 1.2 is the model's accepted tap band; anything outside it
            # is a misread field rather than a real tap, so it is dropped
            # instead of failing the whole import.
            tap = br.get("tap")
            if tap is not None and 0.8 <= float(tap) <= 1.2 and abs(float(tap) - 1.0) > 1e-9:
                entry["tap"] = round(float(tap), 6)
            if br.get("connection") in ("wye_wye", "delta_wye"):
                entry["connection"] = br["connection"]
        branches.append(entry)

    if not branches:
        raise NetworkImportError("No usable branches found in the network file.")

    return {
        "id": network_id, "name": name or network_id,
        "base_voltage_kv": base_kv, "source_bus": int(source),
        "buses": buses, "branches": branches,
    }


# ---------------------------------------------------------------------------
# PSS/E RAW (text)
# ---------------------------------------------------------------------------

def _raw_split(line: str) -> list[str]:
    """Comma-split a RAW line, keeping single-quoted fields intact."""
    out, cur, q = [], "", False
    for ch in line:
        if ch == "'":
            q = not q
        elif ch == "," and not q:
            out.append(cur.strip().strip("'").strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip().strip("'").strip())
    return out


def _safe_float(value):
    """``float(value)`` or ``None`` if it is blank/non-numeric (e.g. a name field)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _windv_pu(fields: list[str], cw: int, bus_kv):
    """A RAW winding record's WINDV, in per unit of that winding's bus base kV.

    ``fields`` is the winding line (WINDV, NOMV, ...) and ``cw`` the transformer
    record's winding-unit code:

      * CW 1 - WINDV is already per unit of the bus base voltage.
      * CW 2 - WINDV is in kV, so it is divided by the bus base.
      * CW 3 - WINDV is per unit of NOMV, the winding's own nominal, so the
        product is in kV and is then divided by the bus base.

    Returns ``None`` when the value is missing or the bus base is unknown, which
    leaves the branch on the model's unity-tap default.
    """
    windv = _safe_float(fields[0]) if fields else None
    if windv is None or windv <= 0:
        return None
    if cw == 1:
        return windv
    nomv = _safe_float(fields[1]) if len(fields) > 1 else None
    kv = windv * nomv if (cw == 3 and nomv) else windv
    base = float(bus_kv) if bus_kv else 0.0
    return (kv / base) if base > 0 else None


def _canon_raw_section(name) -> str | None:
    """Map a RAW section label to BUS/LOAD/BRANCH/TRANSFORMER/SUBSTATION, or None."""
    n = (name or "").upper().replace("-", " ")
    if "SUBSTATION" in n:        # v34+ substation records (site name + lat/long)
        return "SUBSTATION"
    if "NON TRANSFORMER" in n:   # "NON-TRANSFORMER BRANCH" (v34+) is a line section
        return "BRANCH"
    if "TRANSFORMER" in n:
        return "TRANSFORMER"
    if "BRANCH" in n:
        return "BRANCH"
    if "LOAD" in n:
        return "LOAD"
    if "BUS" in n:
        return "BUS"
    return None


def _raw_sections(lines: list[str]) -> dict[str, list[str]]:
    """Group RAW data lines into {BUS, LOAD, BRANCH, TRANSFORMER} record lists.

    Handles both RAW dialects:
      * v32/33: sections terminated by a bare ``0`` record that names the next
        section (``0 / END OF BUS DATA, BEGIN LOAD DATA``).
      * v34/35/36: explicit ``BEGIN <X> DATA`` / ``END OF <X> DATA`` markers
        (``@!`` label lines have already been stripped by ``_raw_clean``).
    """
    sections: dict[str, list[str]] = defaultdict(list)
    # v34+ is recognised by standalone "BEGIN <X> DATA" markers (a v32/33 file
    # only ever has "BEGIN" inside a "0 / END OF ... , BEGIN ..." terminator).
    is_v34 = any(re.match(r"BEGIN\s+.+\s+DATA", ln.strip(), re.I) for ln in lines)
    if is_v34:
        current = None
        for ln in lines[1:]:  # after header; title/unknown sections stay unselected
            s = ln.strip()
            begin = re.match(r"BEGIN\s+(.+?)\s+DATA", s, re.I)
            if begin:
                current = _canon_raw_section(begin.group(1))
                continue
            if re.match(r"END\s+OF\s+.+\s+DATA", s, re.I) or s in ("0", "Q"):
                current = None
                continue
            if current:
                sections[current].append(ln)
    else:
        current = "BUS"
        for ln in lines[3:]:  # skip header + 2 title lines
            # A terminator is a standalone "0" token; data like "0.01, 0.03" is not.
            if re.split(r"[,\s]+", ln.strip())[0] == "0":
                m = re.search(r"BEGIN\s+([A-Z][A-Z0-9 \-]*?)\s+DATA", ln, re.I)
                current = _canon_raw_section(m.group(1)) if m else None
                continue
            if current:
                sections[current].append(ln)
    return sections


def from_raw(text: str, network_id=None) -> dict:
    # Keep content lines; drop v34+ "@!" label/comment lines.
    lines = [ln.rstrip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("@")]
    if len(lines) < 4:
        raise NetworkImportError("RAW file too short.")
    header = _raw_split(lines[0])
    sbase = _safe_float(header[1] if len(header) > 1 else None) or 100.0

    sections = _raw_sections(lines)

    buses_raw: dict = {}
    for ln in sections.get("BUS", []):
        f = _raw_split(ln)
        if len(f) < 4 or _safe_float(f[0]) is None:
            continue
        bid = int(float(f[0]))
        buses_raw[bid] = {
            "name": f[1] if len(f) > 1 else None,
            "base_kv": float(f[2]) if f[2] else 1.0,
            "ide": int(float(f[3])) if f[3] else 1,
            "pl_mw": 0.0, "ql_mvar": 0.0,
        }

    for ln in sections.get("LOAD", []):
        f = _raw_split(ln)
        if len(f) < 7 or _safe_float(f[0]) is None:
            continue
        bid = int(float(f[0]))
        if bid in buses_raw and (f[2] in ("", "1")):  # status on (or unspecified)
            buses_raw[bid]["pl_mw"] += _safe_float(f[5]) or 0.0
            buses_raw[bid]["ql_mvar"] += _safe_float(f[6]) or 0.0

    branches_raw: list = []
    for ln in sections.get("BRANCH", []):
        f = _raw_split(ln)
        if len(f) < 5 or _safe_float(f[0]) is None or _safe_float(f[1]) is None:
            continue
        # The rating sits just after B: f[6] in v32/33 (RATEA), or f[7] in v34+
        # (a branch NAME field is inserted at f[6]). Scan the ratings region for
        # the first positive number so both layouts work.
        rate = next((v for v in (_safe_float(t) for t in f[6:9]) if v and v > 0), None)
        branches_raw.append({
            "from": abs(int(float(f[0]))), "to": abs(int(float(f[1]))),
            "r": _safe_float(f[3]) or 0.0, "x": _safe_float(f[4]) or 0.0,
            "rate_mva": rate,
        })

    # 2-winding transformers: 4-line records, K (3rd field) == 0.
    tlines = sections.get("TRANSFORMER", [])
    j = 0
    while j < len(tlines):
        first = _raw_split(tlines[j])
        k = int(float(first[2])) if len(first) > 2 and first[2] else 0
        span = 4 if k == 0 else 5  # 2-winding = 4 lines, 3-winding = 5 (skipped)
        if k == 0 and j + 3 < len(tlines):
            second = _raw_split(tlines[j + 1])   # R1-2, X1-2, SBASE1-2
            third = _raw_split(tlines[j + 2])    # WINDV1, NOMV1, ANG1, RATA1, ...
            fourth = _raw_split(tlines[j + 3])   # WINDV2, NOMV2
            f_bus = abs(int(float(first[0])))
            t_bus = abs(int(float(first[1])))
            # CW (field 4) says what unit WINDV is in. Both windings are put on
            # their own bus base so the ratio is per unit either way.
            cw = int(float(first[4])) if len(first) > 4 and first[4] else 1
            w1 = _windv_pu(third, cw, buses_raw.get(f_bus, {}).get("base_kv"))
            w2 = _windv_pu(fourth, cw, buses_raw.get(t_bus, {}).get("base_kv"))
            # PSS/E's off-nominal ratio is WINDV1 : WINDV2, and the low-voltage
            # winding voltage moves with WINDV2, so tap = WINDV2 / WINDV1 in the
            # model's convention (> 1 boosts the LV side).
            tap = (w2 / w1) if (w1 and w2) else None
            # ANG1 is the winding-1 phase shift in degrees. A non-zero shift is
            # a delta-star transformer; -30 is the Dyn11 convention.
            ang = _safe_float(third[2]) if len(third) > 2 else 0.0
            branches_raw.append({
                "from": f_bus, "to": t_bus,
                "r": _safe_float(second[0]) or 0.0, "x": _safe_float(second[1]) or 0.0,
                "rate_mva": _safe_float(third[3]) if len(third) > 3 else None,
                "is_transformer": True, "tap": tap,
                "connection": ("delta_wye" if ang and abs(ang) > 1e-6
                               else "wye_wye"),
            })
        j += span

    # Substation records (v34+): IS, 'NAME', LATI, LONG, SRG. When IS matches a
    # bus number (the convention our exports use), the site's WGS84 coordinates
    # become that bus's position.
    for ln in sections.get("SUBSTATION", []):
        f = _raw_split(ln)
        if len(f) < 4 or _safe_float(f[0]) is None:
            continue
        bid = int(float(f[0]))
        lat, lon = _safe_float(f[2]), _safe_float(f[3])
        if bid in buses_raw and lat is not None and lon is not None:
            buses_raw[bid].setdefault("lat", lat)
            buses_raw[bid].setdefault("lon", lon)

    return _assemble(
        _slug(network_id, "psse_raw"), network_id or "PSS/E RAW import",
        buses_raw, branches_raw, sbase,
    )


# ---------------------------------------------------------------------------
# PSS/E RAWX (JSON)
# ---------------------------------------------------------------------------

def _rawx_rows(section) -> list[dict]:
    if not isinstance(section, dict):
        return []
    fields, data = section.get("fields") or [], section.get("data") or []
    if data and not isinstance(data[0], list):  # single-record (e.g. caseid)
        return [dict(zip(fields, data))]
    return [dict(zip(fields, row)) for row in data]


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def from_rawx(text: str, network_id=None) -> dict:
    try:
        doc = json.loads(text)
    except Exception as exc:
        raise NetworkImportError(f"Invalid RAWX JSON: {exc}")
    net = doc.get("network", doc)

    caseid = _rawx_rows(net.get("caseid"))
    sbase = float(_first(caseid[0], "sbase", default=100.0)) if caseid else 100.0

    buses_raw: dict = {}
    for r in _rawx_rows(net.get("bus")):
        bid = int(_first(r, "ibus", "i", default=0))
        buses_raw[bid] = {
            "name": _first(r, "name"),
            "base_kv": float(_first(r, "baskv", "basekv", default=1.0) or 1.0),
            "ide": int(_first(r, "ide", default=1) or 1),
            "pl_mw": 0.0, "ql_mvar": 0.0,
        }
    for r in _rawx_rows(net.get("load")):
        bid = int(_first(r, "ibus", "i", default=0))
        if bid in buses_raw and int(_first(r, "stat", "status", default=1) or 1) == 1:
            buses_raw[bid]["pl_mw"] += float(_first(r, "pl", default=0.0) or 0.0)
            buses_raw[bid]["ql_mvar"] += float(_first(r, "ql", default=0.0) or 0.0)

    branches_raw: list = []
    for r in _rawx_rows(net.get("acline")):
        branches_raw.append({
            "from": abs(int(_first(r, "ibus", "i", default=0))),
            "to": abs(int(_first(r, "jbus", "j", default=0))),
            "r": float(_first(r, "rpu", "r", default=0.0) or 0.0),
            "x": float(_first(r, "xpu", "x", default=0.0) or 0.0),
            "rate_mva": _first(r, "rate1", "ratea"),
        })
    for r in _rawx_rows(net.get("transformer")):
        if int(_first(r, "kbus", "k", default=0) or 0) != 0:
            continue  # 3-winding, skipped
        f = abs(int(_first(r, "ibus", "i", default=0)))
        t = abs(int(_first(r, "jbus", "j", default=0)))
        # Same winding-ratio and phase-shift reading as the text RAW above.
        cw = int(_first(r, "cw", default=1) or 1)
        w1 = _windv_pu([str(_first(r, "windv1", default="")),
                        str(_first(r, "nomv1", default=""))],
                       cw, buses_raw.get(f, {}).get("base_kv"))
        w2 = _windv_pu([str(_first(r, "windv2", default="")),
                        str(_first(r, "nomv2", default=""))],
                       cw, buses_raw.get(t, {}).get("base_kv"))
        ang = float(_first(r, "ang1", default=0.0) or 0.0)
        branches_raw.append({
            "from": f, "to": t,
            "r": float(_first(r, "r1_2", "r12", default=0.0) or 0.0),
            "x": float(_first(r, "x1_2", "x12", default=0.0) or 0.0),
            "rate_mva": _first(r, "rate1_1", "wdg1rate1"),
            "is_transformer": True,
            "tap": (w2 / w1) if (w1 and w2) else None,
            "connection": "delta_wye" if abs(ang) > 1e-6 else "wye_wye",
        })

    return _assemble(
        _slug(network_id, "psse_rawx"), network_id or "PSS/E RAWX import",
        buses_raw, branches_raw, sbase,
    )


# ---------------------------------------------------------------------------
# CIM / CGMES (XML)
# ---------------------------------------------------------------------------

def _cim_id(ref: str | None) -> str | None:
    if not ref:
        return None
    return ref.lstrip("#").lstrip("_")


def from_cim(text: str, network_id=None) -> dict:
    """Parse a CIM / CGMES network (any version) into the internal model dict.

    Version-tolerant: any CIM namespace (CIM14/15/16/17, CGMES CIM100, ...), and
    both transformer encodings -- the standard CGMES form (a ``PowerTransformer``
    with two ``PowerTransformerEnd`` objects, each with its own terminal) and the
    simplified single-end-with-two-terminals form. Bus voltages come from a node's
    container ``VoltageLevel``, equipment ``BaseVoltage``, transformer-end
    ``ratedU``, or are inherited across lines. Loads read EQ ``.p/.q`` or SSH
    ``.pfixed/.qfixed``. Impedances are ohms.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise NetworkImportError(f"Invalid CIM XML: {exc}")

    # CIM namespace: prefer the 'cim' prefix, else any xmlns URI that mentions CIM.
    ns_map = dict(re.findall(r'xmlns:(\w+)="([^"]+)"', text))
    cim = ns_map.get("cim")
    if not cim:
        cands = [v for v in ns_map.values() if "cim" in v.lower()]
        cim = cands[0] if cands else ""
    if not cim:
        raise NetworkImportError("No CIM/CGMES namespace found in the XML.")
    RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

    def tag(local):
        return f"{{{cim}}}{local}"

    def rid(el):
        return _cim_id(el.get(f"{{{RDF}}}ID") or el.get(f"{{{RDF}}}about"))

    def ref(el, *locals_):
        for local in locals_:
            c = el.find(tag(local))
            if c is not None and c.get(f"{{{RDF}}}resource"):
                return _cim_id(c.get(f"{{{RDF}}}resource"))
        return None

    def num(el, *locals_):
        for local in locals_:
            c = el.find(tag(local))
            if c is not None and c.text:
                try:
                    return float(c.text)
                except ValueError:
                    pass
        return 0.0

    def name_of(el):
        c = el.find(tag("IdentifiedObject.name"))
        return c.text.strip() if c is not None and c.text and c.text.strip() else None

    def text_of(el, *locals_):
        """First non-empty child text, or an enum's rdf:resource fragment.

        CIM enumerations appear either as literal text (``<cim:Terminal.phases>
        ABC</...>``) or as a reference to the enumeration member
        (``rdf:resource="...PhaseCode.ABC"``); both are read.
        """
        for local in locals_:
            c = el.find(tag(local))
            if c is None:
                continue
            if c.text and c.text.strip():
                return c.text.strip().split(".")[-1]
            res = c.get(f"{{{RDF}}}resource")
            if res:
                return res.split(".")[-1].split("#")[-1]
        return None

    # BaseVoltage values, in the file's own voltage unit.
    base_v = {rid(e): num(e, "BaseVoltage.nominalVoltage")
              for e in root.iter(tag("BaseVoltage"))}

    # Unit-aware: CIM/CGMES files carry voltages in either volts (base CIM, e.g.
    # 11000) or kV (many ENTSO-E/utility exports, e.g. 11), and loads in either
    # watts (base CIM) or MW (CGMES SSH). Detect each from magnitude and
    # normalise to kV / MW. Any real MV/HV level is >= 1000 in volts but < 1000
    # in kV; any real feeder load is >= 1000 in watts but < 1000 in MW.
    _rated_u = [num(e, "PowerTransformerEnd.ratedU")
                for e in root.iter(tag("PowerTransformerEnd"))]
    _v_sample = [v for v in list(base_v.values()) + _rated_u if v > 0]
    _v_in_volts = bool(_v_sample) and max(_v_sample) >= 1000.0
    _pq_sample = []
    for ec in root.iter(tag("EnergyConsumer")):
        _pq_sample += [abs(num(ec, "EnergyConsumer.p", "EnergyConsumer.pfixed")),
                       abs(num(ec, "EnergyConsumer.q", "EnergyConsumer.qfixed"))]
    _p_in_watts = bool(_pq_sample) and max(_pq_sample) >= 1000.0

    def to_kv(v):
        return (v / 1000.0) if _v_in_volts else float(v)

    def to_mw(v):
        return (v / 1e6) if _p_in_watts else float(v)

    vlevel_kv, vlevel_sub = {}, {}
    for vl in root.iter(tag("VoltageLevel")):
        bv = ref(vl, "VoltageLevel.BaseVoltage")
        if bv in base_v and base_v[bv]:
            vlevel_kv[rid(vl)] = to_kv(base_v[bv])
        sub = ref(vl, "VoltageLevel.Substation")
        if sub:
            vlevel_sub[rid(vl)] = sub

    # Connectivity nodes -> sequential bus ids (TopologicalNode as a fallback for
    # bus-branch models that omit ConnectivityNodes).
    nodes_el = list(root.iter(tag("ConnectivityNode"))) or list(root.iter(tag("TopologicalNode")))
    node_ids = [rid(e) for e in nodes_el]
    # Prefer the human-readable IdentifiedObject.name (e.g. real busbar/building
    # labels) over the raw mRID for the bus name; fall back to the id.
    node_name = {rid(e): (name_of(e) or rid(e)) for e in nodes_el}
    bus_of = {nid: i + 1 for i, nid in enumerate(node_ids)}
    if not bus_of:
        raise NetworkImportError("No CIM ConnectivityNodes or TopologicalNodes found.")

    # Seed bus voltages from each node's container VoltageLevel (standard CGMES).
    known_kv: dict = {}
    for cn in root.iter(tag("ConnectivityNode")):
        cont = ref(cn, "ConnectivityNode.ConnectivityNodeContainer")
        if cont in vlevel_kv:
            known_kv[bus_of[rid(cn)]] = vlevel_kv[cont]

    # Terminals: equipment -> [node, ...]; terminal id -> node.
    term_node, equip_terms = {}, defaultdict(list)
    term_equip, term_phases = {}, {}
    for tm in root.iter(tag("Terminal")):
        node = ref(tm, "Terminal.ConnectivityNode", "Terminal.TopologicalNode")
        equip = ref(tm, "Terminal.ConductingEquipment")
        if node:
            term_node[rid(tm)] = node
        if equip:
            term_equip[rid(tm)] = equip
        if equip and node:
            equip_terms[equip].append(node)
        ph = text_of(tm, "Terminal.phases", "ACDCTerminal.phases")
        if ph:
            term_phases[rid(tm)] = ph

    buses_raw = {bid: {"name": node_name.get(nid, nid), "base_kv": known_kv.get(bid, 0.0),
                       "ide": 1, "pl_mw": 0.0, "ql_mvar": 0.0}
                 for nid, bid in bus_of.items()}

    def equip_kv(e):
        """Base kV of conducting equipment: direct BaseVoltage or its container's."""
        bv = ref(e, "ConductingEquipment.BaseVoltage")
        if bv in base_v and base_v[bv]:
            return to_kv(base_v[bv])
        cont = ref(e, "Equipment.EquipmentContainer", "Equipment.MemberOf_EquipmentContainer")
        return vlevel_kv.get(cont, 0.0)

    # Operational limits -> per-equipment MVA ratings. An OperationalLimitSet
    # hangs off a Terminal (or straight off the equipment), and carries
    # ApparentPowerLimit (MVA) and/or CurrentLimit (A) members. The lowest
    # "normal continuous" value wins; short-term limits are ignored, which is
    # what a thermal loading percentage should be measured against. Without
    # this every CIM branch falls back to the 100 MVA case base and reported
    # loadings are meaningless.
    limit_type_name = {rid(e): (name_of(e) or "").lower()
                       for e in root.iter(tag("OperationalLimitType"))}
    limit_type_dur = {rid(e): num(e, "OperationalLimitType.acceptableDuration")
                      for e in root.iter(tag("OperationalLimitType"))}
    set_equip = {}
    for ols in root.iter(tag("OperationalLimitSet")):
        term = ref(ols, "OperationalLimitSet.Terminal")
        eq = term_equip.get(term) or ref(ols, "OperationalLimitSet.Equipment")
        if eq:
            set_equip[rid(ols)] = eq

    def _is_continuous(lt):
        """A limit type is the continuous rating unless it names a duration."""
        if lt is None:
            return True
        if limit_type_dur.get(lt):
            return False
        return "short" not in limit_type_name.get(lt, "")

    equip_mva: dict = {}

    def _note_limit(eq, mva):
        if eq and mva and mva > 0:
            cur = equip_mva.get(eq)
            equip_mva[eq] = mva if cur is None else min(cur, mva)

    for lim in root.iter(tag("ApparentPowerLimit")):
        lt = ref(lim, "OperationalLimit.OperationalLimitType")
        if not _is_continuous(lt):
            continue
        eq = set_equip.get(ref(lim, "OperationalLimit.OperationalLimitSet"))
        _note_limit(eq, num(lim, "ApparentPowerLimit.value"))
    # Current limits need the equipment's voltage to become an MVA rating, so
    # they are resolved after the branch list is built and the voltages known.
    current_limits: list = []
    for lim in root.iter(tag("CurrentLimit")):
        lt = ref(lim, "OperationalLimit.OperationalLimitType")
        if not _is_continuous(lt):
            continue
        eq = set_equip.get(ref(lim, "OperationalLimit.OperationalLimitSet"))
        amps = num(lim, "CurrentLimit.value")
        if eq and amps > 0:
            current_limits.append((eq, amps))

    branches_raw: list = []

    # AC line segments -> line branches (endpoints remembered for GL positions).
    line_ends: dict = {}
    for e in root.iter(tag("ACLineSegment")):
        nodes = [n for n in equip_terms.get(rid(e), []) if n in bus_of]
        if len(nodes) < 2:
            continue
        f, t = bus_of[nodes[0]], bus_of[nodes[1]]
        line_ends[rid(e)] = (f, t)
        kv = equip_kv(e)
        if kv:
            known_kv.setdefault(f, kv)
            known_kv.setdefault(t, kv)
        entry = {"from": f, "to": t, "equip": rid(e),
                 "r": num(e, "ACLineSegment.r"), "x": num(e, "ACLineSegment.x"),
                 "rate_mva": equip_mva.get(rid(e)), "is_transformer": False}
        # Explicit zero-sequence impedance, when the file carries it. Without
        # it the model assumes 3x the positive sequence, which is a poor guess
        # for a cable and changes any unbalanced result.
        r0, x0 = num(e, "ACLineSegment.r0"), num(e, "ACLineSegment.x0")
        if r0 or x0:
            entry["r0"], entry["x0"] = r0, x0
        branches_raw.append(entry)

    # Transformers. Standard CGMES: a PowerTransformer grouping >=2 ends.
    ends_by_pt = defaultdict(list)
    for e in root.iter(tag("PowerTransformerEnd")):
        pt = ref(e, "PowerTransformerEnd.PowerTransformer", "TransformerEnd.PowerTransformer")
        ends_by_pt[pt].append(e)

    # RatioTapChanger, keyed by the transformer end it regulates. The ratio a
    # tap position contributes is 1 + (step - neutralStep) * increment / 100.
    rtc_by_end: dict = {}
    for rtc in root.iter(tag("RatioTapChanger")):
        end = ref(rtc, "RatioTapChanger.TransformerEnd", "TapChanger.TransformerEnd")
        if not end:
            continue
        inc = num(rtc, "RatioTapChanger.stepVoltageIncrement",
                  "TapChanger.stepVoltageIncrement")
        step = num(rtc, "TapChanger.step", "RatioTapChanger.step")
        neutral = num(rtc, "TapChanger.neutralStep", "RatioTapChanger.neutralStep")
        rtc_by_end[end] = 1.0 + (step - neutral) * inc / 100.0

    def end_ratio(e):
        """An end's voltage in per unit of its bus's nominal.

        ``ratedU`` is the winding's own rated voltage and ``BaseVoltage`` the
        system nominal it sits on, so their quotient is exactly the off-nominal
        ratio that winding contributes: an 11000/415 V transformer on a 400 V
        nominal LV level gives 1.0375 on the LV end and 1.0 on the HV end. Any
        RatioTapChanger on the end multiplies on top of that.
        """
        ru = num(e, "PowerTransformerEnd.ratedU")
        bv = ref(e, "TransformerEnd.BaseVoltage", "PowerTransformerEnd.BaseVoltage")
        nominal = base_v.get(bv) or 0.0
        ratio = (ru / nominal) if (ru and nominal) else 1.0
        return ratio * rtc_by_end.get(rid(e), 1.0)

    def end_kind(e):
        """``PowerTransformerEnd.connectionKind`` reduced to delta or wye."""
        kind = (text_of(e, "PowerTransformerEnd.connectionKind",
                        "TransformerEnd.connectionKind") or "").upper()
        return "D" if kind.startswith("D") else "Y"

    def winding_kv(e, node):
        """Bus nominal for a transformer end: BaseVoltage first, ratedU last.

        ratedU is the *winding* rating, which on a 415 V secondary differs from
        the 400 V system nominal, so it must not be preferred over an explicit
        BaseVoltage or the tap would be absorbed into the bus base and vanish.
        """
        bv = ref(e, "TransformerEnd.BaseVoltage", "PowerTransformerEnd.BaseVoltage")
        if bv in base_v and base_v[bv]:
            return to_kv(base_v[bv])
        ru = num(e, "PowerTransformerEnd.ratedU")
        return to_kv(ru) if ru else 0.0

    handled: set = set()
    for pt, ends in ends_by_pt.items():
        if pt is None or len(ends) < 2:
            continue
        # Node for each end: its own terminal, else the transformer's terminals.
        end_node = {}
        for e in ends:
            nd = term_node.get(ref(e, "TransformerEnd.Terminal", "PowerTransformerEnd.Terminal"))
            if nd in bus_of:
                end_node[rid(e)] = bus_of[nd]
        if len(set(end_node.values())) < 2:
            pt_nodes = [n for n in equip_terms.get(pt, []) if n in bus_of]
            if len(pt_nodes) >= 2:
                ordered = sorted(ends, key=lambda e: num(e, "TransformerEnd.endNumber",
                                                          "PowerTransformerEnd.endNumber") or 99)
                for e, nd in zip(ordered, (bus_of[pt_nodes[0]], bus_of[pt_nodes[1]])):
                    end_node[rid(e)] = nd
        if len(set(end_node.values())) < 2:
            continue
        # HV end (highest ratedU, else lowest endNumber) is the 'from' side.
        ranked = sorted(
            (e for e in ends if rid(e) in end_node),
            key=lambda e: (-num(e, "PowerTransformerEnd.ratedU"),
                           num(e, "TransformerEnd.endNumber", "PowerTransformerEnd.endNumber") or 99),
        )
        f, t = end_node[rid(ranked[0])], end_node[rid(ranked[-1])]
        if f == t:
            continue
        for e in ends:
            nd = end_node.get(rid(e))
            kv_end = winding_kv(e, nd)
            if kv_end and nd:
                known_kv.setdefault(nd, kv_end)
            handled.add(rid(e))
        kv = equip_kv(ranked[0])
        if kv:
            known_kv.setdefault(f, kv)
        hv_end, lv_end = ranked[0], ranked[-1]
        hv_ratio, lv_ratio = end_ratio(hv_end), end_ratio(lv_end)
        # ratedS is the winding rating; an operational limit on the transformer
        # or on either end overrides it when present.
        rated_s = max((num(e, "PowerTransformerEnd.ratedS") for e in ends),
                      default=0.0)
        limit = equip_mva.get(pt)
        for e in ends:
            if limit is None:
                limit = equip_mva.get(rid(e))
        branches_raw.append({
            "from": f, "to": t, "equip": pt,
            "r": sum(num(e, "PowerTransformerEnd.r") for e in ends),
            "x": sum(num(e, "PowerTransformerEnd.x") for e in ends),
            "rate_mva": limit if limit else (rated_s or None),
            "is_transformer": True,
            "tap": (lv_ratio / hv_ratio) if hv_ratio else None,
            "connection": ("delta_wye"
                           if end_kind(hv_end) == "D" and end_kind(lv_end) == "Y"
                           else "wye_wye"),
        })

    # Simplified form: a PowerTransformerEnd that itself carries both terminals.
    for e in root.iter(tag("PowerTransformerEnd")):
        if rid(e) in handled:
            continue
        nodes = [n for n in equip_terms.get(rid(e), []) if n in bus_of]
        if len(nodes) < 2:
            continue
        f, t = bus_of[nodes[0]], bus_of[nodes[1]]
        kv = equip_kv(e) or to_kv(num(e, "PowerTransformerEnd.ratedU"))
        if kv:
            known_kv.setdefault(f, kv)
        rated_s = num(e, "PowerTransformerEnd.ratedS")
        branches_raw.append({"from": f, "to": t, "equip": rid(e),
                             "r": num(e, "PowerTransformerEnd.r"), "x": num(e, "PowerTransformerEnd.x"),
                             "rate_mva": equip_mva.get(rid(e)) or rated_s or None,
                             "is_transformer": True,
                             "connection": ("delta_wye" if end_kind(e) == "D"
                                            else "wye_wye")})

    # Propagate voltages across non-transformer branches (buses without an explicit
    # voltage inherit a galvanically-connected neighbour's).
    line_adj: dict = defaultdict(list)
    for br in branches_raw:
        if not br["is_transformer"]:
            line_adj[br["from"]].append(br["to"])
            line_adj[br["to"]].append(br["from"])
    queue = deque(known_kv)
    while queue:
        b = queue.popleft()
        for nb in line_adj[b]:
            if nb not in known_kv:
                known_kv[nb] = known_kv[b]
                queue.append(nb)
    for bid, kv in known_kv.items():
        if bid in buses_raw:
            buses_raw[bid]["base_kv"] = kv

    # Current limits become MVA only now that every bus voltage is settled:
    # S = sqrt(3) * V * I. A branch that already has an apparent-power limit
    # keeps it; where both exist the tighter one wins.
    branch_by_equip = {br["equip"]: br for br in branches_raw if br.get("equip")}
    for eq, amps in current_limits:
        br = branch_by_equip.get(eq)
        if br is None:
            continue
        kv = known_kv.get(br["from"]) or known_kv.get(br["to"]) or 0.0
        if kv <= 0:
            continue
        mva = 3 ** 0.5 * kv * amps / 1000.0
        br["rate_mva"] = mva if not br.get("rate_mva") else min(br["rate_mva"], mva)

    # Energy consumers -> bus loads (EQ '.p/.q' or SSH '.pfixed/.qfixed', W/var)
    # and, where declared, the phase the board is connected to. The phase comes
    # from the consumer's Terminal.phases, or from EnergyConsumerPhase members
    # when the file spells the connection out per phase.
    ec_phases: dict = defaultdict(set)
    for ecp in root.iter(tag("EnergyConsumerPhase")):
        owner = ref(ecp, "EnergyConsumerPhase.EnergyConsumer")
        ph = text_of(ecp, "EnergyConsumerPhase.phase")
        if owner and ph:
            ec_phases[owner].add(ph.upper())
    term_of_equip: dict = defaultdict(list)
    for tid_, eq in term_equip.items():
        term_of_equip[eq].append(tid_)

    for ec in root.iter(tag("EnergyConsumer")):
        nodes = [n for n in equip_terms.get(rid(ec), []) if n in bus_of]
        if not nodes:
            continue
        bid = bus_of[nodes[0]]
        buses_raw[bid]["pl_mw"] += to_mw(num(ec, "EnergyConsumer.p", "EnergyConsumer.pfixed"))
        buses_raw[bid]["ql_mvar"] += to_mw(num(ec, "EnergyConsumer.q", "EnergyConsumer.qfixed"))
        letters = set(ec_phases.get(rid(ec), ()))
        if not letters:
            for tid_ in term_of_equip.get(rid(ec), ()):
                code = (term_phases.get(tid_) or "").upper()
                letters |= {ch for ch in code if ch in "ABC"}
        if letters:
            # A bus fed by several consumers takes the union of their phases.
            prev = {ch for ch in (buses_raw[bid].get("phases") or "").upper()
                    if ch in "ABC"}
            buses_raw[bid]["phases"] = "".join(sorted(letters | prev)).lower()

    # Slack: a bus carrying a network injection/source, else the lowest id.
    for inj in ("EnergySource", "ExternalNetworkInjection", "EquivalentInjection"):
        for e in root.iter(tag(inj)):
            nodes = [n for n in equip_terms.get(rid(e), []) if n in bus_of]
            if nodes:
                buses_raw[bus_of[nodes[0]]]["ide"] = 3

    # GL profile (geographic positions) -> per-bus WGS84 lat/lon.
    # Location.PowerSystemResources links a Location to a PSR; PositionPoints
    # (ordered by sequenceNumber) carry xPosition=longitude, yPosition=latitude.
    loc_psr = {rid(loc): ref(loc, "Location.PowerSystemResources")
               for loc in root.iter(tag("Location"))}
    psr_pts: dict = defaultdict(list)
    for pp in root.iter(tag("PositionPoint")):
        loc = ref(pp, "PositionPoint.Location")
        psr = loc_psr.get(loc)
        if psr is None:
            continue
        psr_pts[psr].append((num(pp, "PositionPoint.sequenceNumber") or 1,
                             num(pp, "PositionPoint.xPosition"),
                             num(pp, "PositionPoint.yPosition")))
    for psr in psr_pts:
        psr_pts[psr].sort()

    def set_pos(bid, lon, lat):
        if bid in buses_raw:
            buses_raw[bid].setdefault("lat", lat)
            buses_raw[bid].setdefault("lon", lon)

    # Most specific first: a line's route endpoints pin its own two buses, then
    # substation sites cover their voltage-level members, then any other located
    # equipment. (Order matters: street-side nodes share their kiosk's
    # VoltageLevel, so the substation rule must not claim them first.)
    # 1) Line routes -> first/last position point to the from/to bus.
    for eid, (f, t) in line_ends.items():
        pts = psr_pts.get(eid)
        if pts:
            set_pos(f, pts[0][1], pts[0][2])
            set_pos(t, pts[-1][1], pts[-1][2])
    # 2) Substation locations -> every bus whose VoltageLevel belongs to it.
    sub_pts = {psr: pts for psr, pts in psr_pts.items() if pts}
    for cn in root.iter(tag("ConnectivityNode")):
        cont = ref(cn, "ConnectivityNode.ConnectivityNodeContainer")
        sub = vlevel_sub.get(cont)
        if sub in sub_pts:
            _, lon, lat = sub_pts[sub][0]
            set_pos(bus_of.get(rid(cn)), lon, lat)
    # 3) Any other located equipment (e.g. an injection) -> its terminal buses.
    for psr, pts in psr_pts.items():
        for n in equip_terms.get(psr, []):
            if n in bus_of:
                set_pos(bus_of[n], pts[0][1], pts[0][2])

    return _assemble(
        _slug(network_id, "cim_import"), network_id or "CIM import",
        buses_raw, branches_raw, 100.0, impedance_in_ohms=True,
    )


# ---------------------------------------------------------------------------
# Native JSON
# ---------------------------------------------------------------------------

def from_json(text: str, network_id=None) -> dict:
    """Native format: returned as-is for NetworkModel.from_dict() to validate."""
    return json.loads(text)


# ---------------------------------------------------------------------------
# Pluggable importer registry
# ---------------------------------------------------------------------------
#
# Each network format is a parser ``(content: str, network_id) -> dict``
# registered by name. Adding a format (MATPOWER, OpenDSS, ...) is: write a
# parser, call ``register_format()`` — no edits to ``parse_network``. The
# dispatcher resolves the requested/​inferred format against the registry, so it
# mirrors the same plug-and-play pattern as the Load Engine's DER plugins and the
# KPI registry.

_IMPORTERS: dict[str, dict] = {}


def register_format(name: str, parser, *, description: str = "",
                    extensions: tuple[str, ...] = ()) -> None:
    """Register a network-format importer.

    Args:
        name: Canonical format token (e.g. "raw").
        parser: ``(content, network_id) -> NetworkModel dict``.
        description: Human-readable label for ``available_formats()``.
        extensions: Extra filename-extension/alias tokens that resolve to this
            format (e.g. "xml" -> "cim"). The canonical name is always accepted.
    """
    _IMPORTERS[name] = {
        "parser": parser,
        "description": description,
        "extensions": tuple(extensions),
    }


def supported_formats() -> tuple[str, ...]:
    """Canonical names of every registered importer."""
    return tuple(sorted(_IMPORTERS))


def available_formats() -> list[dict]:
    """Registered importers with descriptions + extensions (for API listing)."""
    return [
        {
            "name": n,
            "description": _IMPORTERS[n]["description"],
            "extensions": list(_IMPORTERS[n]["extensions"]),
        }
        for n in supported_formats()
    ]


def _resolve_format(token: str) -> str:
    """Map a format name or extension/alias to a canonical registered name."""
    if token in _IMPORTERS:
        return token
    for name, meta in _IMPORTERS.items():
        if token in meta["extensions"]:
            return name
    return token  # unresolved: caller raises a clear error


def _sniff_format(content: str) -> str:
    """Infer the format from the content when none is given."""
    stripped = content.lstrip()
    if stripped.startswith("{") and '"network"' in content:
        return "rawx"
    if stripped.startswith("<"):
        return "cim"
    if stripped.startswith("{"):
        return "json"
    if "new circuit." in content.lower():
        return "dss"
    return "raw"


def parse_network(content: str, fmt: str | None = None,
                  filename: str | None = None, network_id=None) -> dict:
    """Parse `content` into the internal NetworkModel dict.

    `fmt` is a registered format name/alias (or None to infer from the filename
    extension / content). 'json' is the native format (returned as-is for the
    registry to validate).
    """
    token = (fmt or "").lower().lstrip(".")
    if not token and filename:
        token = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not token:
        token = _sniff_format(content)

    name = _resolve_format(token)
    importer = _IMPORTERS.get(name)
    if importer is None:
        raise NetworkImportError(
            f"Unknown network format '{name}'. Use one of: {supported_formats()}."
        )
    return importer["parser"](content, network_id)


# ---------------------------------------------------------------------------
# OpenDSS master file (.dss) — a documented subset
# ---------------------------------------------------------------------------

_DSS_ARRAY_RE = re.compile(r"[\[\(]([^\]\)]*)[\]\)]")


def _dss_statements(content: str) -> list[str]:
    """Physical lines joined into logical statements (``~`` continuations)."""
    statements: list[str] = []
    for raw in content.replace("\r", "").split("\n"):
        line = raw.split("!", 1)[0].strip()   # strip trailing comments
        if not line or line.startswith("//"):
            continue
        if line.startswith("~") and statements:
            statements[-1] += " " + line[1:].strip()
        else:
            statements.append(line)
    return statements


def _dss_props(statement: str) -> dict[str, str]:
    """key=value properties of a statement (arrays kept as raw bracket text)."""
    props: dict[str, str] = {}
    # Protect array values (spaces inside brackets) before splitting on spaces.
    protected = _DSS_ARRAY_RE.sub(lambda m: "[" + m.group(1).replace(" ", "\x00") + "]", statement)
    for token in protected.split():
        if "=" in token:
            key, value = token.split("=", 1)
            props[key.strip().lower()] = value.replace("\x00", " ").strip()
    return props


def _dss_array(value: str) -> list[str]:
    inner = value.strip().strip("[]()")
    return [t for t in inner.replace(",", " ").split() if t]


def _dss_bus(token: str) -> str:
    """Bus name with any node suffixes stripped (``bus_007.1.2.3`` -> ``bus_007``)."""
    return token.split(".", 1)[0].strip().lower()


def from_dss(content: str, network_id=None) -> dict:
    """Import an OpenDSS master file — a documented subset.

    Supported statements: ``New Circuit`` (basekv, bus1), ``New Line`` (bus1,
    bus2, r1/x1 in ohms, optional length multiplier and normamps), ``New Load``
    (bus1, kw, kvar — accumulated per bus), and two-winding ``New Transformer``
    with ``buses=[..] kvs=[..] kvas=[..] xhl=..`` (becomes an
    ``is_transformer`` branch; the secondary bus takes the LV base). ``~``
    continuation lines and ``!`` comments are handled. NOT supported (skipped):
    linecode/geometry-based lines, per-winding ``wdg=`` transformer syntax,
    regulators/capacitors, and Redirect includes — files relying on them
    import partially and validation reports what is missing.
    """
    bus_ids: dict[str, int] = {}
    order: list[str] = []

    def bus_id(name: str) -> int:
        if name not in bus_ids:
            # Reuse a trailing number when the name carries one (bus_012 -> 12);
            # otherwise enumerate in encounter order.
            m = re.search(r"(\d+)$", name)
            wanted = int(m.group(1)) if m else None
            if wanted is None or wanted in bus_ids.values():
                wanted = max(bus_ids.values(), default=0) + 1
                while wanted in bus_ids.values():
                    wanted += 1
            bus_ids[name] = wanted
            order.append(name)
        return bus_ids[name]

    base_kv = None
    source_bus_name = None
    loads: dict[int, dict] = {}
    bus_kv: dict[int, float] = {}
    branches: list[dict] = []
    skipped_lines = 0

    for statement in _dss_statements(content):
        lower = statement.lower()
        if not lower.startswith("new "):
            continue
        head = lower.split(None, 2)[1]              # e.g. "circuit.ieee33"
        element = head.split(".", 1)[0]
        props = _dss_props(statement)

        if element == "circuit":
            base_kv = float(props.get("basekv", 0.0) or 0.0)
            source_bus_name = _dss_bus(props.get("bus1", "sourcebus"))
            bus_id(source_bus_name)
        elif element == "line":
            if "r1" not in props or "x1" not in props:
                skipped_lines += 1                   # linecode/geometry line
                continue
            f = bus_id(_dss_bus(props["bus1"]))
            t = bus_id(_dss_bus(props["bus2"]))
            length = float(props.get("length", 1.0) or 1.0)
            r_ohm = float(props["r1"]) * length
            x_ohm = float(props["x1"]) * length
            entry = {"branch_id": len(branches) + 1, "from_bus": f, "to_bus": t,
                     "r_ohm": round(r_ohm, 6), "x_ohm": round(x_ohm, 6)}
            if "normamps" in props and base_kv:
                entry["rating_kva"] = round(
                    float(props["normamps"]) * base_kv * 3 ** 0.5, 1)
            branches.append(entry)
        elif element == "load":
            b = bus_id(_dss_bus(props.get("bus1", "")))
            entry = loads.setdefault(b, {"kw": 0.0, "kvar": 0.0})
            entry["kw"] += float(props.get("kw", 0.0) or 0.0)
            entry["kvar"] += float(props.get("kvar", 0.0) or 0.0)
        elif element == "transformer":
            buses = [_dss_bus(t) for t in _dss_array(props.get("buses", ""))]
            kvs = [float(v) for v in _dss_array(props.get("kvs", ""))]
            kvas = [float(v) for v in _dss_array(props.get("kvas", ""))]
            if len(buses) != 2 or len(kvs) != 2:
                skipped_lines += 1                   # wdg= syntax / 3-winding
                continue
            f, t = bus_id(buses[0]), bus_id(buses[1])
            rating = kvas[0] if kvas else 1000.0
            hv = kvs[0]
            z_base = (hv * hv) / (rating / 1000.0) if rating else 1.0
            x_ohm = float(props.get("xhl", 5.0)) / 100.0 * z_base
            r_ohm = float(props.get("%loadloss", 0.0)) / 100.0 * z_base
            branches.append({
                "branch_id": len(branches) + 1, "from_bus": f, "to_bus": t,
                "r_ohm": round(r_ohm, 6), "x_ohm": round(x_ohm, 6),
                "rating_kva": round(rating, 1), "is_transformer": True,
            })
            bus_kv[f] = hv
            bus_kv[t] = kvs[1]

    if base_kv is None or source_bus_name is None:
        raise NetworkImportError(
            "No 'New Circuit' statement found — not a self-contained OpenDSS "
            "master file."
        )
    if skipped_lines:
        # Partial imports are surfaced, not silent: validation also catches any
        # resulting disconnection.
        import logging
        logging.getLogger(__name__).warning(
            "DSS import: skipped %d unsupported statement(s) "
            "(linecode/geometry lines or per-winding transformers).", skipped_lines)

    buses = []
    for name in order:
        bid = bus_ids[name]
        entry = {
            "bus_id": bid,
            "name": name,
            "base_load_kw": round(loads.get(bid, {}).get("kw", 0.0), 3),
            "base_load_kvar": round(loads.get(bid, {}).get("kvar", 0.0), 3),
        }
        if bid in bus_kv and abs(bus_kv[bid] - base_kv) > 1e-6:
            entry["base_kv"] = round(bus_kv[bid], 4)
        buses.append(entry)

    if not branches:
        raise NetworkImportError("No usable branches found in the DSS file.")

    return {
        "id": _slug(network_id, "dss_import"),
        "name": network_id or "OpenDSS import",
        "base_voltage_kv": base_kv,
        "source_bus": bus_ids[source_bus_name],
        "buses": buses,
        "branches": branches,
    }


register_format("json", from_json, description="Native NetworkModel JSON")
register_format("raw", from_raw, description="PSS/E RAW (text)")
register_format("rawx", from_rawx, description="PSS/E RAWX (JSON)")
register_format("cim", from_cim, description="CIM / CGMES (XML)", extensions=("xml",))
register_format(
    "dss", from_dss,
    description="OpenDSS master file (subset: Circuit, Line, Load, "
    "2-winding Transformer)",
)
