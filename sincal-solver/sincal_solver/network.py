"""Read-only network view for the solver.

The Simulation Engine owns network storage, import, and validation; a `build`
command carries the already-validated model dict over the bus. This class is
the solver-side view of that dict — the same derived accessors the OpenDSS
builders were written against (bus voltages, phases, transformer flags).
"""

# Phase node numbers for the three phases.
_PHASE_NODE = {"a": 1, "b": 2, "c": 3, "1": 1, "2": 2, "3": 3}


def normalize_phases(spec) -> list[int]:
    """Normalise a per-bus phase declaration to sorted OpenDSS node numbers.

    Accepts ``None``/``"abc"``/``"a"``/``"ac"`` or a list like ``[1, 3]`` and
    returns a sorted list drawn from ``[1, 2, 3]``. Anything empty or
    unrecognised falls back to a full three-phase connection ``[1, 2, 3]``.
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


class SolverNetwork:
    """In-memory view of a distribution network model dict."""

    def __init__(self, data: dict):
        self._data = data
        self.id: str = data["id"]
        self.name: str = data.get("name", self.id)
        self.base_voltage_kv: float = float(data["base_voltage_kv"])
        self.source_bus: int = int(data["source_bus"])
        self.buses: list[dict] = data["buses"]
        self.branches: list[dict] = data["branches"]

    @property
    def bus_ids(self) -> list[int]:
        return [int(b["bus_id"]) for b in self.buses]

    @property
    def load_bus_ids(self) -> list[int]:
        """All buses that carry a Load element (everything except the source)."""
        return [b for b in self.bus_ids if b != self.source_bus]

    def phase_nodes(self, bus_id: int) -> list[int]:
        """Phase node numbers a bus connects to (default full three-phase)."""
        for b in self.buses:
            if int(b["bus_id"]) == int(bus_id):
                return normalize_phases(b.get("phases"))
        return [1, 2, 3]

    def bus_base_kv(self, bus_id: int) -> float:
        """A bus's line-line base voltage (kV); per-bus base_kv or network base."""
        for b in self.buses:
            if int(b["bus_id"]) == int(bus_id):
                return float(b.get("base_kv") or self.base_voltage_kv)
        return self.base_voltage_kv

    def voltage_levels(self) -> list[float]:
        """Sorted distinct base voltages (kV) present in the network."""
        return sorted({round(self.bus_base_kv(b), 4) for b in self.bus_ids})

    def to_dict(self) -> dict:
        """The underlying model dict, as the Simulation Engine sent it."""
        return self._data

    @staticmethod
    def is_transformer(branch: dict) -> bool:
        return bool(branch.get("is_transformer"))

    @staticmethod
    def has_oltc(branch: dict) -> bool:
        return bool(branch.get("is_transformer")) and bool(branch.get("oltc"))
