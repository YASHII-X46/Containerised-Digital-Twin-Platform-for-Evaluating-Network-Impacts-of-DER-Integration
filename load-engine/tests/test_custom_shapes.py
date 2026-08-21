"""Tests for custom PV/EV day-shapes (kind-aware custom-profile store)."""

import numpy as np
import pytest

from app.profiles.custom import CustomProfileError, CustomProfileStore
from app.profiles.generator import ProfileGenerator


def _bus(**over):
    base = {
        "bus_id": 2, "base_load_kw": 150.0, "base_load_kvar": 60.0,
        "customer_class": "res_detached_medium", "pv_capacity_kw": 50.0,
        "bess_config": None, "bess_capacity_kwh": 0.0,
        "ev_config": "level2_7kw", "ev_charge_rate_kw": 7.0,
    }
    base.update(over)
    return base


def _gen(bus, store=None, **cfg):
    config = {"bus_data": [bus], "timesteps": 96, "seed": 42, "custom_store": store}
    config.update(cfg)
    return ProfileGenerator(config)


class TestKindAwareStore:
    def test_save_and_list_kind(self, tmp_path):
        store = CustomProfileStore(str(tmp_path))
        saved = store.save("pv_day", [0, 1, 4, 2, 0], kind="pv")
        assert saved["kind"] == "pv"
        listed = {p["name"]: p["kind"] for p in store.list_profiles()}
        assert listed["pv_day"] == "pv"

    def test_kind_mismatch_raises(self, tmp_path):
        store = CustomProfileStore(str(tmp_path))
        store.save("pv_day", [0, 1, 0], kind="pv")
        with pytest.raises(CustomProfileError, match="pv shape"):
            store.get_shape("pv_day", 96, kind="load")

    def test_legacy_files_default_to_load(self, tmp_path):
        store = CustomProfileStore(str(tmp_path))
        store.save("old_shape", [1, 2, 1])  # no kind argument
        assert store.get_shape("old_shape", 96, kind="load").max() == 1.0

    def test_bad_kind_rejected(self, tmp_path):
        store = CustomProfileStore(str(tmp_path))
        with pytest.raises(CustomProfileError, match="kind"):
            store.save("x", [1, 2], kind="banana")


class TestCustomPvEvShapes:
    def test_pv_profile_shapes_output(self, tmp_path):
        store = CustomProfileStore(str(tmp_path))
        # Triangular measured PV day, peak at midday.
        vals = np.maximum(0.0, 1 - np.abs(np.arange(96) - 48) / 24.0)
        store.save("measured_pv", vals.tolist(), kind="pv")
        gen = _gen(_bus(), store=store, pv_profile="custom:measured_pv")
        p = gen.generate_all_profiles()[2]
        assert abs(float(p["pv_kw"].max()) - 50.0) < 1e-6      # capacity x peak(1.0)
        assert float(p["pv_kw"][0]) == 0.0                     # midnight zero
        np.testing.assert_allclose(p["pv_kw"], 50.0 * vals, atol=1e-9)

    def test_ev_profile_scaled_by_rate_and_fleet(self, tmp_path):
        store = CustomProfileStore(str(tmp_path))
        shape = np.zeros(96); shape[80:88] = 1.0               # 20:00-22:00 block
        store.save("depot_ev", shape.tolist(), kind="ev")
        gen = _gen(_bus(), store=store, ev_profile="depot_ev")
        p = gen.generate_all_profiles()[2]
        fleet = min(int(150.0 / 1.5), 400)                     # households_for_bus
        assert abs(float(p["ev_charge_kw"].max()) - 7.0 * fleet) < 1e-6
        assert float(p["ev_charge_kw"][:80].max()) == 0.0

    def test_wrong_kind_for_pv_profile_raises(self, tmp_path):
        store = CustomProfileStore(str(tmp_path))
        store.save("a_load", [1, 2, 1], kind="load")
        gen = _gen(_bus(), store=store, pv_profile="a_load")
        with pytest.raises(CustomProfileError, match="load shape"):
            gen.generate_all_profiles()
