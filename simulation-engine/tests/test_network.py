"""Tests for the pluggable network model and registry (DSS generation
tests live in opendss-solver/tests/test_dss_model.py)."""

import os

import pytest

from app.network.model import (
    NetworkModel,
    NetworkRegistry,
    NetworkValidationError,
    normalize_phases,
    validate_network_dict,
)

# The engine ships no built-in networks; tests treat the sample-model directory
# as a "built-in" source to exercise registry built-in/user semantics.
BUILTIN_NETWORKS_DIR = os.path.join(os.path.dirname(__file__), "data")


def _tiny_network(net_id="tiny3") -> dict:
    return {
        "id": net_id,
        "name": "Tiny 3-bus",
        "base_voltage_kv": 11.0,
        "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
            {"bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0},
            {"bus_id": 3, "base_load_kw": 50.0, "base_load_kvar": 20.0},
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
            {"branch_id": 2, "from_bus": 2, "to_bus": 3, "r_ohm": 0.4, "x_ohm": 0.2, "rating_kva": 2000},
        ],
    }


def _multivoltage_network(net_id="mv_lv") -> dict:
    """11 kV feeder with an 11/0.4 kV transformer down to an LV bus."""
    return {
        "id": net_id,
        "name": "MV/LV feeder",
        "base_voltage_kv": 11.0,
        "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},        # 11 kV slack
            {"bus_id": 2, "base_load_kw": 0.0, "base_load_kvar": 0.0},        # 11 kV
            {"bus_id": 3, "base_load_kw": 80.0, "base_load_kvar": 30.0, "base_kv": 0.4},  # LV
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
            {"branch_id": 2, "from_bus": 2, "to_bus": 3, "is_transformer": True,
             "r_ohm": 0.02, "x_ohm": 0.12, "rating_kva": 500},
        ],
    }


def test_per_bus_base_kv_and_voltage_levels():
    model = NetworkModel.from_dict(_multivoltage_network())
    assert model.bus_base_kv(1) == 11.0          # explicit network default
    assert model.bus_base_kv(3) == 0.4           # per-bus override
    assert model.bus_base_kv(99) == 11.0         # unknown bus -> default
    assert model.voltage_levels() == [0.4, 11.0]
    assert model.is_transformer(model.branches[1]) is True
    assert model.is_transformer(model.branches[0]) is False


def test_single_voltage_network_has_one_level():
    model = NetworkModel.from_dict(_tiny_network())
    assert model.voltage_levels() == [11.0]


def test_validation_rejects_negative_zero_sequence():
    bad = _tiny_network()
    bad["branches"][0]["r0_ohm"] = -0.1
    with pytest.raises(NetworkValidationError, match="r0_ohm"):
        validate_network_dict(bad)


def test_validation_rejects_bad_transformer_fields():
    for field, value, match in (
        ("connection", "zigzag", "connection"),
        ("tap", 1.5, "tap"),
    ):
        bad = _multivoltage_network()
        bad["branches"][1][field] = value
        with pytest.raises(NetworkValidationError, match=match):
            validate_network_dict(bad)
    # Both fields are transformer-only.
    for field, value in (("connection", "delta_wye"), ("tap", 1.05)):
        bad = _tiny_network()
        bad["branches"][0][field] = value
        with pytest.raises(NetworkValidationError, match="is_transformer"):
            validate_network_dict(bad)


def test_validation_rejects_oltc_on_a_line():
    bad = _tiny_network()
    bad["branches"][0]["oltc"] = True   # a plain line cannot carry a tap changer
    with pytest.raises(NetworkValidationError, match="oltc"):
        validate_network_dict(bad)


def test_validation_rejects_line_across_voltage_levels():
    bad = _multivoltage_network()
    bad["branches"][1]["is_transformer"] = False  # a line cannot span 11kV/0.4kV
    with pytest.raises(NetworkValidationError, match="different voltage levels"):
        validate_network_dict(bad)


def test_validation_rejects_bad_base_kv():
    bad = _multivoltage_network()
    bad["buses"][2]["base_kv"] = -1.0
    with pytest.raises(NetworkValidationError, match="base_kv"):
        validate_network_dict(bad)


def test_ieee33_builtin_model_loads_from_json(ieee33_network):
    model = ieee33_network  # loaded from app/networks/ieee33.json
    assert model.id == "ieee33"
    assert model.num_buses == 33
    assert model.num_branches == 32
    assert model.source_bus == 1
    assert len(model.load_bus_ids) == 32
    assert sorted(model.bus_ids) == list(range(1, 34))


def test_normalize_phases_and_phase_nodes():
    assert normalize_phases(None) == [1, 2, 3]
    assert normalize_phases("abc") == [1, 2, 3]
    assert normalize_phases("a") == [1]
    assert normalize_phases("ca") == [1, 3]
    assert normalize_phases([2, 3]) == [2, 3]
    assert normalize_phases("nonsense") == [1, 2, 3]  # falls back to three-phase

    net = _tiny_network()
    net["buses"][1]["phases"] = "b"
    model = NetworkModel.from_dict(net)
    assert model.phase_nodes(2) == [2]
    assert model.phase_nodes(3) == [1, 2, 3]


def test_validation_rejects_unrecognised_phases():
    bad = _tiny_network()
    bad["buses"][1]["phases"] = "xyz"
    with pytest.raises(NetworkValidationError, match="phases"):
        validate_network_dict(bad)


def test_validation_rejects_bad_networks():
    bad = _tiny_network()
    bad["branches"][1]["to_bus"] = 99  # unknown bus
    with pytest.raises(NetworkValidationError):
        validate_network_dict(bad)

    orphan = _tiny_network()
    orphan["buses"].append({"bus_id": 4, "base_load_kw": 10.0, "base_load_kvar": 4.0})
    with pytest.raises(NetworkValidationError, match="not connected"):
        validate_network_dict(orphan)

    dup = _tiny_network()
    dup["buses"].append({"bus_id": 2, "base_load_kw": 1.0, "base_load_kvar": 0.0})
    with pytest.raises(NetworkValidationError, match="Duplicate"):
        validate_network_dict(dup)


def test_registry_roundtrip(tmp_path):
    # Built-ins come from the JSON dir, not register_builtin().
    registry = NetworkRegistry(builtin_dir=BUILTIN_NETWORKS_DIR, user_dir=str(tmp_path))

    listed = registry.list_networks()
    assert any(n["id"] == "ieee33" and n["builtin"] for n in listed)

    registry.save(_tiny_network("user_net"))
    assert registry.get("user_net").num_buses == 3
    assert any(n["id"] == "user_net" and not n["builtin"] for n in registry.list_networks())

    with pytest.raises(NetworkValidationError):
        registry.save(_tiny_network("ieee33"))  # cannot shadow built-in
    with pytest.raises(NetworkValidationError):
        registry.delete("ieee33")  # cannot delete built-in

    registry.delete("user_net")
    assert "user_net" not in {n["id"] for n in registry.list_networks()}


def test_default_network_resolves_without_hardcoding(tmp_path):
    # Preferred network present -> used as-is.
    registry = NetworkRegistry(builtin_dir=BUILTIN_NETWORKS_DIR, user_dir=str(tmp_path))
    assert registry.resolve_default_id("ieee33") == "ieee33"
    # Preferred missing -> falls back to the first available network, not a
    # hardcoded id (so the stack works with any drop-in set of networks).
    assert registry.resolve_default_id("does_not_exist") in {
        n["id"] for n in registry.list_networks()
    }
    # No networks at all -> None rather than assuming one exists.
    empty = NetworkRegistry(builtin_dir=str(tmp_path / "none"), user_dir=str(tmp_path / "u"))
    assert empty.resolve_default_id("ieee33") is None
