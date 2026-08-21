"""Generic distribution network model — plug-and-play network support.

A network is described by a plain dict (usually loaded from JSON):

{
  "id": "my_feeder",                  # unique slug, used in API calls
  "name": "My feeder",                # human-readable label
  "base_voltage_kv": 11.0,            # default line-line base voltage
  "source_bus": 1,                    # slack/substation bus
  "buses": [
    {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0, "name": "sub"},
    {"bus_id": 9, "base_load_kw": 5.0, "base_kv": 0.4},   # per-bus voltage (LV)
    ...
  ],
  "branches": [
    {"branch_id": 1, "from_bus": 1, "to_bus": 2,
     "r_ohm": 0.30, "x_ohm": 0.15, "rating_kva": 3000},
    {"branch_id": 8, "from_bus": 2, "to_bus": 9, "is_transformer": true,
     "oltc": true, "r_ohm": 0.01, "x_ohm": 0.06, "rating_kva": 500},
    ...
  ]
}

Buses may declare their own ``base_kv`` (defaulting to ``base_voltage_kv``), so
multi-voltage feeders (MV/LV) are supported; a branch spanning two voltage
levels must set ``"is_transformer": true`` and is modelled as a transformer.

Optional branch fields:
  - lines: ``r0_ohm`` / ``x0_ohm`` — explicit zero-sequence impedance (defaults
    to 3x the positive-sequence values when omitted; affects unbalanced solves).
  - transformers: ``"oltc": true`` — an on-load tap changer regulating the
    secondary side; ``"connection"`` — ``"wye_wye"`` (default) or
    ``"delta_wye"`` (Dyn11-style); ``"tap"`` — fixed secondary off-load tap in
    per-unit (0.8-1.2; >1 boosts the LV voltage).

Any radial (or weakly meshed) network expressed this way can be simulated.
Drop a JSON file into NETWORKS_DIR or POST it to /networks and it becomes
selectable in the UI.
"""

import json
import logging
import os
import re
from collections import defaultdict

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

# OpenDSS phase node numbers for the three phases.
_PHASE_NODE = {"a": 1, "b": 2, "c": 3, "1": 1, "2": 2, "3": 3}


def normalize_phases(spec) -> list[int]:
    """Normalise a per-bus phase declaration to sorted OpenDSS node numbers.

    Accepts ``None``/``"abc"``/``"a"``/``"ac"`` or a list like ``[1, 3]`` and
    returns a sorted list drawn from ``[1, 2, 3]``. Anything empty or
    unrecognised falls back to a full three-phase connection ``[1, 2, 3]`` so a
    network without phase data behaves exactly as a balanced three-phase feeder.
    """
    if spec is None:
        return [1, 2, 3]
    if isinstance(spec, str):
        s = spec.strip().lower()
        if s in ("", "abc", "3", "three", "3p", "3ph"):
            return [1, 2, 3]
        nodes = [_PHASE_NODE[ch] for ch in s if ch in _PHASE_NODE]
        return sorted(set(nodes)) or [1, 2, 3]
    if isinstance(spec, (list, tuple)):
        nodes = [_PHASE_NODE[str(x).strip().lower()] for x in spec
                 if str(x).strip().lower() in _PHASE_NODE]
        return sorted(set(nodes)) or [1, 2, 3]
    return [1, 2, 3]


def _phases_recognised(spec) -> bool:
    """True if a provided ``phases`` value names at least one valid phase.

    ``None`` (no declaration) is accepted; a non-empty value that resolves to no
    a/b/c phase is rejected so typos like ``"xyz"`` surface on upload instead of
    silently defaulting to three-phase.
    """
    if spec is None:
        return True
    if isinstance(spec, str):
        return any(ch in _PHASE_NODE for ch in spec.strip().lower())
    if isinstance(spec, (list, tuple)):
        return any(str(x).strip().lower() in _PHASE_NODE for x in spec)
    return False


class NetworkValidationError(ValueError):
    """Raised when a network definition is structurally invalid."""


class NetworkModel:
    """In-memory representation of a distribution network."""

    def __init__(self, data: dict):
        self._data = data
        self.id: str = data["id"]
        self.name: str = data.get("name", self.id)
        self.base_voltage_kv: float = float(data["base_voltage_kv"])
        self.source_bus: int = int(data["source_bus"])
        self.buses: list[dict] = data["buses"]
        self.branches: list[dict] = data["branches"]

    # ---- derived views -------------------------------------------------

    @property
    def bus_ids(self) -> list[int]:
        return [int(b["bus_id"]) for b in self.buses]

    @property
    def branch_ids(self) -> list[int]:
        return [int(br["branch_id"]) for br in self.branches]

    @property
    def load_bus_ids(self) -> list[int]:
        """All buses that carry a Load element (everything except the source)."""
        return [b for b in self.bus_ids if b != self.source_bus]

    def phase_nodes(self, bus_id: int) -> list[int]:
        """Phase node numbers a bus connects to (default full three-phase).

        Used for unbalanced studies: a bus may declare ``"phases"`` (e.g. ``"a"``
        or ``[1, 3]``) to model a single-phase lateral. Balanced studies ignore
        this and treat every bus as three-phase.
        """
        for b in self.buses:
            if int(b["bus_id"]) == int(bus_id):
                return normalize_phases(b.get("phases"))
        return [1, 2, 3]

    def bus_base_kv(self, bus_id: int) -> float:
        """A bus's line-line base voltage (kV).

        A bus may declare its own ``base_kv`` for multi-voltage feeders (MV/LV);
        buses without one fall back to the network ``base_voltage_kv``.
        """
        for b in self.buses:
            if int(b["bus_id"]) == int(bus_id):
                return float(b.get("base_kv") or self.base_voltage_kv)
        return self.base_voltage_kv

    def voltage_levels(self) -> list[float]:
        """Sorted distinct base voltages (kV) present in the network."""
        return sorted({round(self.bus_base_kv(b), 4) for b in self.bus_ids})

    @staticmethod
    def is_transformer(branch: dict) -> bool:
        """Whether a branch is a transformer (vs an ordinary line)."""
        return bool(branch.get("is_transformer"))

    @staticmethod
    def has_oltc(branch: dict) -> bool:
        """Whether a transformer branch carries an on-load tap changer."""
        return bool(branch.get("is_transformer")) and bool(branch.get("oltc"))

    @property
    def num_buses(self) -> int:
        return len(self.buses)

    @property
    def num_branches(self) -> int:
        return len(self.branches)

    def base_loads(self) -> dict[int, dict]:
        """bus_id -> {base_load_kw, base_load_kvar} for non-source buses."""
        return {
            int(b["bus_id"]): {
                "base_load_kw": float(b.get("base_load_kw", 0.0)),
                "base_load_kvar": float(b.get("base_load_kvar", 0.0)),
            }
            for b in self.buses
            if int(b["bus_id"]) != self.source_bus
        }

    def to_dict(self) -> dict:
        return self._data

    # ---- construction / validation --------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "NetworkModel":
        validate_network_dict(data)
        return cls(data)

    @classmethod
    def from_json_file(cls, path: str) -> "NetworkModel":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def validate_network_dict(data: dict) -> None:
    """Validate a network definition dict; raises NetworkValidationError."""
    if not isinstance(data, dict):
        raise NetworkValidationError("Network definition must be a JSON object.")

    for key in ("id", "base_voltage_kv", "source_bus", "buses", "branches"):
        if key not in data:
            raise NetworkValidationError(f"Missing required field '{key}'.")

    if not _ID_RE.match(str(data["id"])):
        raise NetworkValidationError(
            "Field 'id' must be 1-64 chars of letters, digits, '_' or '-'."
        )

    try:
        base_kv = float(data["base_voltage_kv"])
    except (TypeError, ValueError):
        raise NetworkValidationError("'base_voltage_kv' must be a number.")
    if base_kv <= 0:
        raise NetworkValidationError("'base_voltage_kv' must be positive.")

    buses = data["buses"]
    branches = data["branches"]
    if not isinstance(buses, list) or len(buses) < 2:
        raise NetworkValidationError("'buses' must be a list with at least 2 buses.")
    if not isinstance(branches, list) or len(branches) < 1:
        raise NetworkValidationError("'branches' must be a non-empty list.")

    bus_ids: set[int] = set()
    for b in buses:
        if "bus_id" not in b:
            raise NetworkValidationError("Every bus needs a 'bus_id'.")
        try:
            bid = int(b["bus_id"])
        except (TypeError, ValueError):
            raise NetworkValidationError(f"bus_id '{b['bus_id']}' is not an integer.")
        if bid in bus_ids:
            raise NetworkValidationError(f"Duplicate bus_id {bid}.")
        if float(b.get("base_load_kw", 0.0)) < 0 or float(b.get("base_load_kvar", 0.0)) < 0:
            raise NetworkValidationError(f"Bus {bid}: base loads must be >= 0.")
        if "base_kv" in b and b["base_kv"] is not None:
            try:
                bkv = float(b["base_kv"])
            except (TypeError, ValueError):
                raise NetworkValidationError(f"Bus {bid}: 'base_kv' must be a number.")
            if bkv <= 0:
                raise NetworkValidationError(f"Bus {bid}: 'base_kv' must be positive.")
        if "phases" in b and not _phases_recognised(b["phases"]):
            raise NetworkValidationError(
                f"Bus {bid}: 'phases' must reference phases a/b/c (e.g. \"a\", "
                f"\"ac\", \"abc\" or [1, 3])."
            )
        bus_ids.add(bid)

    source = int(data["source_bus"])
    if source not in bus_ids:
        raise NetworkValidationError(f"source_bus {source} is not in the bus list.")

    # Per-bus base voltage (for the multi-voltage line/transformer check below).
    bus_kv = {
        int(b["bus_id"]): float(b.get("base_kv") or base_kv) for b in buses
    }

    branch_ids: set[int] = set()
    adjacency: dict[int, list[int]] = defaultdict(list)
    for br in branches:
        for key in ("branch_id", "from_bus", "to_bus", "r_ohm", "x_ohm"):
            if key not in br:
                raise NetworkValidationError(f"Every branch needs '{key}'.")
        brid = int(br["branch_id"])
        if brid in branch_ids:
            raise NetworkValidationError(f"Duplicate branch_id {brid}.")
        branch_ids.add(brid)
        f, t = int(br["from_bus"]), int(br["to_bus"])
        if f not in bus_ids or t not in bus_ids:
            raise NetworkValidationError(
                f"Branch {brid} references unknown bus ({f} -> {t})."
            )
        if f == t:
            raise NetworkValidationError(f"Branch {brid} connects bus {f} to itself.")
        if float(br["r_ohm"]) < 0 or float(br["x_ohm"]) < 0:
            raise NetworkValidationError(f"Branch {brid}: impedances must be >= 0.")
        for zkey in ("r0_ohm", "x0_ohm"):
            if br.get(zkey) is not None and float(br[zkey]) < 0:
                raise NetworkValidationError(
                    f"Branch {brid}: {zkey} must be >= 0 when given."
                )
        if float(br.get("rating_kva", 1.0)) <= 0:
            raise NetworkValidationError(f"Branch {brid}: rating_kva must be > 0.")
        # A line cannot span two voltage levels — that needs a transformer.
        if not br.get("is_transformer") and abs(bus_kv[f] - bus_kv[t]) > 1e-6:
            raise NetworkValidationError(
                f"Branch {brid} connects different voltage levels "
                f"({bus_kv[f]} kV to {bus_kv[t]} kV); mark it as a transformer "
                f'with "is_transformer": true.'
            )
        # An on-load tap changer only makes sense on a transformer.
        if br.get("oltc") and not br.get("is_transformer"):
            raise NetworkValidationError(
                f'Branch {brid}: "oltc" requires "is_transformer": true.'
            )
        # Vector group and fixed tap are transformer-only properties.
        if br.get("connection") is not None:
            if not br.get("is_transformer"):
                raise NetworkValidationError(
                    f'Branch {brid}: "connection" requires "is_transformer": true.'
                )
            if str(br["connection"]) not in ("wye_wye", "delta_wye"):
                raise NetworkValidationError(
                    f"Branch {brid}: 'connection' must be 'wye_wye' or "
                    f"'delta_wye' (Dyn11-style)."
                )
        if br.get("tap") is not None:
            if not br.get("is_transformer"):
                raise NetworkValidationError(
                    f'Branch {brid}: "tap" requires "is_transformer": true.'
                )
            try:
                tap = float(br["tap"])
            except (TypeError, ValueError):
                raise NetworkValidationError(f"Branch {brid}: 'tap' must be a number.")
            if not (0.8 <= tap <= 1.2):
                raise NetworkValidationError(
                    f"Branch {brid}: 'tap' must be within 0.8-1.2 per unit."
                )
        adjacency[f].append(t)
        adjacency[t].append(f)

    # Connectivity: every bus must be reachable from the source.
    seen = {source}
    stack = [source]
    while stack:
        for nb in adjacency[stack.pop()]:
            if nb not in seen:
                seen.add(nb)
                stack.append(nb)
    unreachable = sorted(bus_ids - seen)
    if unreachable:
        raise NetworkValidationError(
            f"Buses {unreachable} are not connected to source bus {source}."
        )


class NetworkRegistry:
    """Discovers and serves network models.

    The stack ships no networks; in production only the user directory is used.
    The optional built-in sources exist so callers (e.g. tests) can seed models:

      1. Models registered in code via ``register_builtin`` (none by default).
      2. JSON files in an optional ``builtin_dir`` (unused in production).
      3. JSON files in the user directory (NETWORKS_DIR). Drop a file in and
         it shows up; POST /networks writes here too.
    """

    def __init__(self, builtin_dir: str | None, user_dir: str | None):
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir
        self._builtin_models: dict[str, NetworkModel] = {}
        if user_dir:
            os.makedirs(user_dir, exist_ok=True)

    def register_builtin(self, model: NetworkModel) -> None:
        self._builtin_models[model.id] = model

    def _scan_dir(self, directory: str | None) -> dict[str, NetworkModel]:
        models: dict[str, NetworkModel] = {}
        if not directory or not os.path.isdir(directory):
            return models
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(directory, fname)
            try:
                model = NetworkModel.from_json_file(path)
                models[model.id] = model
            except (NetworkValidationError, json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping invalid network file %s: %s", path, exc)
        return models

    def _builtin_ids(self) -> set[str]:
        """Ids that ship with the app — code-registered or JSON in builtin_dir."""
        return set(self._builtin_models) | set(self._scan_dir(self._builtin_dir))

    def is_builtin(self, network_id: str) -> bool:
        return network_id in self._builtin_ids()

    def all_models(self) -> dict[str, NetworkModel]:
        """Full id -> model map; user files override built-ins on id clash."""
        models = dict(self._builtin_models)
        models.update(self._scan_dir(self._builtin_dir))
        models.update(self._scan_dir(self._user_dir))
        return models

    def list_networks(self) -> list[dict]:
        builtin = self._builtin_ids()
        out = []
        for model in self.all_models().values():
            out.append({
                "id": model.id,
                "name": model.name,
                "buses": model.num_buses,
                "branches": model.num_branches,
                "base_voltage_kv": model.base_voltage_kv,
                "voltage_levels": model.voltage_levels(),
                "source_bus": model.source_bus,
                "builtin": model.id in builtin,
            })
        return sorted(out, key=lambda n: (not n["builtin"], n["id"]))

    def get(self, network_id: str) -> NetworkModel:
        models = self.all_models()
        if network_id not in models:
            raise KeyError(
                f"Unknown network '{network_id}'. Available: {sorted(models)}"
            )
        return models[network_id]

    def resolve_default_id(self, preferred: str | None = None) -> str | None:
        """Pick a default network id without assuming any specific network exists.

        Returns ``preferred`` if it is registered; otherwise the first available
        network (built-ins first, then user networks), or ``None`` if the
        registry is empty. This keeps the stack plug-and-play: drop in any set of
        networks and a sensible default is chosen even if the configured one is
        missing.
        """
        listed = self.list_networks()
        ids = [n["id"] for n in listed]
        if preferred and preferred in ids:
            return preferred
        return ids[0] if ids else None

    def save(self, data: dict) -> NetworkModel:
        """Validate and persist a user network; returns the model."""
        model = NetworkModel.from_dict(data)
        if self.is_builtin(model.id):
            raise NetworkValidationError(
                f"'{model.id}' is a built-in network and cannot be overwritten."
            )
        if not self._user_dir:
            raise NetworkValidationError("No user networks directory configured.")
        path = os.path.join(self._user_dir, f"{model.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved user network '%s' to %s", model.id, path)
        return model

    def delete(self, network_id: str) -> None:
        if self.is_builtin(network_id):
            raise NetworkValidationError("Built-in networks cannot be deleted.")
        path = os.path.join(self._user_dir or "", f"{network_id}.json")
        if not os.path.exists(path):
            raise KeyError(f"User network '{network_id}' not found.")
        os.remove(path)
