"""User-supplied custom profiles (plug-and-play).

A custom profile is a named daily shape uploaded through the API or stored as a
JSON file in CUSTOM_PROFILES_DIR. File format:

    {"name": "bakery", "kind": "load", "description": "...", "values": [0.31, ...]}

``kind`` says what the shape drives: "load" (a customer class, referenced as
customer_class "custom:<name>"), "pv" (a measured PV day, selected via the
scenario's ``pv_profile``), or "ev" (a per-charger EV demand day, selected via
``ev_profile``). Files without a kind are load shapes (backward compatible).

Values are normalised to per-unit of peak on save, so any kW series works as
input. Shapes can have any length; they are resampled to the requested
timestep count at generation time.
"""

import json
import os
import re

import numpy as np

CUSTOM_PREFIX = "custom:"

PROFILE_KINDS = ("load", "pv", "ev")

_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


class CustomProfileError(ValueError):
    """Raised for invalid custom-profile names or data."""


def resample_shape(values: np.ndarray, timesteps: int) -> np.ndarray:
    """Resample a daily shape to `timesteps` points (linear, periodic day)."""
    values = np.asarray(values, dtype=float)
    n = len(values)
    if n == timesteps:
        return values.copy()
    # Sample positions as fractions of the day; wrap the first point so the
    # interpolation is periodic across midnight.
    src_x = np.linspace(0.0, 1.0, n, endpoint=False)
    dst_x = np.linspace(0.0, 1.0, timesteps, endpoint=False)
    return np.interp(dst_x, np.append(src_x, 1.0), np.append(values, values[0]))


class CustomProfileStore:
    """Directory-backed registry of custom load shapes."""

    def __init__(self, directory: str):
        self._dir = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, name: str) -> str:
        # Validate on every path build (not just save) so a crafted
        # customer_class like "custom:../../x" cannot read or delete files
        # outside the profiles directory.
        if not _NAME_RE.match(name or ""):
            raise CustomProfileError(
                "Profile name must be 1-64 chars of letters, digits, '_' or '-'."
            )
        return os.path.join(self._dir, f"{name}.json")

    def list_profiles(self) -> list[dict]:
        out = []
        if not os.path.isdir(self._dir):
            return out
        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self._dir, fname), encoding="utf-8") as f:
                    data = json.load(f)
                values = data.get("values", [])
                out.append({
                    "name": data["name"],
                    "kind": data.get("kind", "load"),
                    "description": data.get("description", ""),
                    "points": len(values),
                    "peak_pu": round(float(max(values)), 4) if values else 0.0,
                })
            except (json.JSONDecodeError, KeyError, OSError, ValueError):
                continue
        return out

    def exists(self, name: str) -> bool:
        return os.path.exists(self._path(name))

    def get_shape(self, name: str, timesteps: int, kind: str | None = None) -> np.ndarray:
        """Return the named shape resampled to `timesteps` per-unit points.

        When ``kind`` is given, the stored profile must be of that kind — so a
        PV day cannot silently be used as a load class (or vice versa).
        """
        path = self._path(name)
        if not os.path.exists(path):
            raise CustomProfileError(
                f"Unknown custom profile '{name}'. "
                f"Available: {[p['name'] for p in self.list_profiles()]}"
            )
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        stored_kind = data.get("kind", "load")
        if kind is not None and stored_kind != kind:
            raise CustomProfileError(
                f"Custom profile '{name}' is a {stored_kind} shape, not {kind}."
            )
        values = np.asarray(data["values"], dtype=float)
        return np.clip(resample_shape(values, timesteps), 0.0, 1.0)

    def save(self, name: str, values: list[float], description: str = "",
             kind: str = "load") -> dict:
        """Validate, normalise to per-unit of peak, and persist a profile."""
        if not _NAME_RE.match(name or ""):
            raise CustomProfileError(
                "Profile name must be 1-64 chars of letters, digits, '_' or '-'."
            )
        if kind not in PROFILE_KINDS:
            raise CustomProfileError(
                f"Profile kind must be one of {list(PROFILE_KINDS)}."
            )
        arr = np.asarray(values, dtype=float)
        if arr.ndim != 1 or len(arr) < 2:
            raise CustomProfileError("Profile needs at least 2 numeric values.")
        if not np.all(np.isfinite(arr)):
            raise CustomProfileError("Profile values must all be finite numbers.")
        if np.any(arr < 0):
            raise CustomProfileError("Profile values must be >= 0 (kW or per-unit).")
        peak = float(arr.max())
        if peak <= 0:
            raise CustomProfileError("Profile peak must be greater than 0.")
        normalised = (arr / peak).tolist()

        data = {"name": name, "kind": kind, "description": description, "values": normalised}
        with open(self._path(name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return {"name": name, "kind": kind, "points": len(normalised), "description": description}

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not os.path.exists(path):
            raise CustomProfileError(f"Unknown custom profile '{name}'.")
        os.remove(path)


def parse_csv_values(csv_text: str) -> list[float]:
    """Extract a value series from pasted/uploaded CSV text.

    Accepts one number per line, a single comma-separated line, or two-column
    time,value rows (with or without a header line).
    """
    rows: list[list[float]] = []
    for raw_line in csv_text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        numerics: list[float] = []
        for cell in line.split(","):
            cell = cell.strip()
            if cell == "":
                continue
            try:
                numerics.append(float(cell))
            except ValueError:
                continue
        if numerics:
            rows.append(numerics)

    if len(rows) == 1:
        # Single line: treat every numeric cell as one sample.
        values = rows[0]
    else:
        # Multi-line: one sample per row — the last numeric cell, so
        # "time,value" exports take the value column.
        values = [r[-1] for r in rows]

    if len(values) < 2:
        raise CustomProfileError(
            "Could not parse at least 2 numeric values from the CSV text."
        )
    return values
