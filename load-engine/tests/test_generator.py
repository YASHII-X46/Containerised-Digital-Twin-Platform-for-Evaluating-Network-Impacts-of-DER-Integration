"""Tests for profile generator."""

import numpy as np

from app.profiles.generator import ProfileGenerator, build_profiles_payload
from tests.ieee33_data import default_bus_data


def _make_config(
    der_penetration_percent: float = 100,
    seed: int = 42,
    pv_buses: list[int] | None = None,
    bess_penetration: float = 0.3,
    ev_penetration: float = 0.2,
    season: str = "summer",
) -> dict:
    return {
        "bus_data": default_bus_data(
            der_penetration_percent,
            pv_buses=pv_buses,
            bess_penetration=bess_penetration,
            ev_penetration=ev_penetration,
        ),
        "seed": seed,
        "timesteps": 96,
        "resolution_minutes": 15,
        "season": season,
    }


class TestProfileGenerator:
    def test_returns_33_buses(self):
        gen = ProfileGenerator(_make_config())
        profiles = gen.generate_all_profiles()
        assert len(profiles) == 33

    def test_bus_has_required_keys(self):
        gen = ProfileGenerator(_make_config())
        profiles = gen.generate_all_profiles()
        required = {
            "bus_id", "customer_class", "load_kw", "load_kvar",
            "pv_kw", "net_load_kw", "timestamps",
            "bess_power_kw", "bess_soc", "bess_capacity_kwh", "ev_charge_kw",
        }
        for p in profiles.values():
            assert required.issubset(p.keys())

    def test_array_lengths(self):
        gen = ProfileGenerator(_make_config())
        profiles = gen.generate_all_profiles()
        for p in profiles.values():
            assert len(p["load_kw"]) == 96
            assert len(p["load_kvar"]) == 96
            assert len(p["pv_kw"]) == 96
            assert len(p["net_load_kw"]) == 96
            assert len(p["bess_power_kw"]) == 96
            assert len(p["bess_soc"]) == 96
            assert len(p["ev_charge_kw"]) == 96
            assert len(p["timestamps"]) == 96

    def test_net_load_equals_load_minus_pv(self):
        gen = ProfileGenerator(_make_config(bess_penetration=0.0, ev_penetration=0.0))
        profiles = gen.generate_all_profiles()
        bus = profiles[2]
        np.testing.assert_allclose(
            bus["net_load_kw"], bus["load_kw"] - bus["pv_kw"], atol=1e-9
        )

    def test_bus1_substation_is_zero(self):
        gen = ProfileGenerator(_make_config())
        profiles = gen.generate_all_profiles()
        bus1 = profiles[1]
        assert np.all(bus1["load_kw"] == 0)
        assert np.all(bus1["pv_kw"] == 0)

    def test_summary_reasonable(self):
        gen = ProfileGenerator(_make_config())
        gen.generate_all_profiles()
        summary = gen.get_summary()
        assert summary["total_buses"] == 33
        assert summary["peak_total_load_kw"] > 0
        assert summary["buses_with_pv"] > 0

    def test_determinism(self):
        gen1 = ProfileGenerator(_make_config(seed=42))
        gen2 = ProfileGenerator(_make_config(seed=42))
        p1 = gen1.generate_all_profiles()
        p2 = gen2.generate_all_profiles()
        for bus_id in p1:
            np.testing.assert_array_equal(p1[bus_id]["load_kw"], p2[bus_id]["load_kw"])

    def test_pv_penetration_zero(self):
        gen = ProfileGenerator(_make_config(der_penetration_percent=0))
        gen.generate_all_profiles()
        summary = gen.get_summary()
        assert summary["buses_with_pv"] == 0

    def test_bess_buses_subset_of_pv_buses(self):
        config = _make_config(bess_penetration=0.5)
        bus_data = config["bus_data"]
        pv_bus_ids = {b["bus_id"] for b in bus_data if b["pv_capacity_kw"] > 0}
        bess_bus_ids = {b["bus_id"] for b in bus_data if b["bess_config"] is not None}
        assert bess_bus_ids.issubset(pv_bus_ids)

    def test_ev_buses_subset_of_residential_buses(self):
        config = _make_config(ev_penetration=0.5)
        bus_data = config["bus_data"]
        residential_ids = {b["bus_id"] for b in bus_data if 2 <= b["bus_id"] <= 12}
        ev_bus_ids = {b["bus_id"] for b in bus_data if b["ev_config"] is not None}
        assert ev_bus_ids.issubset(residential_ids)

    def test_bess_penetration_zero(self):
        gen = ProfileGenerator(_make_config(bess_penetration=0.0))
        profiles = gen.generate_all_profiles()
        for p in profiles.values():
            assert p["bess_capacity_kwh"] == 0.0

    def test_ev_penetration_zero(self):
        config = _make_config(ev_penetration=0.0)
        bus_data = config["bus_data"]
        for b in bus_data:
            assert b["ev_charge_rate_kw"] == 0.0

    def test_net_load_formula(self):
        gen = ProfileGenerator(_make_config(bess_penetration=1.0, ev_penetration=1.0))
        profiles = gen.generate_all_profiles()
        for bus_id, p in profiles.items():
            expected = p["load_kw"] - p["pv_kw"] + p["ev_charge_kw"] - p["bess_power_kw"]
            np.testing.assert_allclose(p["net_load_kw"], expected, atol=1e-9)

    def test_all_buses_use_archetypes(self):
        bus_data = default_bus_data()
        for b in bus_data:
            if b["bus_id"] >= 2:
                assert b["customer_class"].startswith("res_")

    def test_multi_day_horizon_concatenates(self):
        base = _make_config(bess_penetration=0.3, ev_penetration=0.2)
        one = ProfileGenerator(base).generate_all_profiles()
        three = ProfileGenerator({**base, "days": 3}).generate_all_profiles()
        bus = next(b for b, p in one.items() if np.any(p["load_kw"] > 0))  # a load bus
        assert len(three[bus]["load_kw"]) == 3 * len(one[bus]["load_kw"])
        assert len(three[bus]["timestamps"]) == 3 * 96
        # Each day has its own seed, so day 0 differs from day 1.
        day0, day1 = three[bus]["load_kw"][:96], three[bus]["load_kw"][96:192]
        assert not np.allclose(day0, day1)

    def test_single_day_matches_default(self):
        # days=1 must reproduce the legacy single-day output exactly.
        base = _make_config()
        a = ProfileGenerator(base).generate_all_profiles()
        b = ProfileGenerator({**base, "days": 1}).generate_all_profiles()
        bus = next(iter(a))
        np.testing.assert_array_equal(a[bus]["load_kw"], b[bus]["load_kw"])

    def test_bess_soc_carries_across_days(self):
        gen = ProfileGenerator(
            {**_make_config(bess_penetration=1.0, ev_penetration=0.0), "days": 2})
        profiles = gen.generate_all_profiles()
        bus = next(b for b, p in profiles.items() if p["bess_capacity_kwh"] > 0)
        soc = profiles[bus]["bess_soc"]
        # The first step of day 2 (index 96) continues from the last of day 1
        # (index 95) — at most one timestep of SoC change, not a reset to 0.5.
        assert abs(soc[96] - soc[95]) < 0.15

    def test_bess_ages_over_multi_day_horizon(self):
        cfg = {**_make_config(bess_penetration=1.0, ev_penetration=0.0), "days": 10}
        ten = ProfileGenerator(cfg).generate_all_profiles()
        bus = next(b for b, p in ten.items() if p["bess_capacity_kwh"] > 0)
        assert ten[bus]["bess_soh"] < 1.0           # health falls over the horizon
        assert ten[bus]["bess_cycles"] > 0.0        # cycles accumulate
        one = ProfileGenerator({**cfg, "days": 1}).generate_all_profiles()
        # One day ages less than ten days, and accumulates fewer cycles.
        assert one[bus]["bess_soh"] > ten[bus]["bess_soh"]
        assert one[bus]["bess_cycles"] < ten[bus]["bess_cycles"]

    def test_weekend_days_differ_from_weekdays(self):
        # The horizon starts on a Monday; days 5-6 are the weekend and should
        # differ from a weekday for an occupancy-driven (commercial) bus.
        bus_data = default_bus_data()
        bus_data = [dict(b) for b in bus_data]
        for b in bus_data:
            if b["bus_id"] == 2:
                b["customer_class"] = "com_small_office"
        cfg = {"bus_data": bus_data, "seed": 42, "timesteps": 96,
               "resolution_minutes": 15, "season": "summer", "days": 7}
        profiles = ProfileGenerator(cfg).generate_all_profiles()
        load = profiles[2]["load_kw"]
        monday = load[0:96]
        saturday = load[5 * 96:6 * 96]  # day index 5 = Saturday
        assert not np.allclose(monday, saturday)

    def test_multi_day_payload_metadata(self):
        base = _make_config()
        gen = ProfileGenerator({**base, "days": 2})
        profiles = gen.generate_all_profiles()
        payload = build_profiles_payload(
            profiles, base["bus_data"], "s", 42, 100.0, 15, days=2)
        assert payload["metadata"]["days"] == 2
        assert payload["metadata"]["timesteps"] == 192
        any_bus = next(iter(payload["buses"].values()))
        assert len(any_bus["timeseries"]) == 192

    def test_bess_dispatch_mode_changes_schedule(self):
        base = _make_config(bess_penetration=1.0, ev_penetration=0.0)
        p_sc = ProfileGenerator(
            {**base, "bess_dispatch_mode": "self_consumption"}
        ).generate_all_profiles()
        p_tou = ProfileGenerator(
            {**base, "bess_dispatch_mode": "time_of_use"}
        ).generate_all_profiles()
        bess_bus = next(bid for bid, p in p_sc.items() if p["bess_capacity_kwh"] > 0)
        assert not np.allclose(
            p_sc[bess_bus]["bess_power_kw"], p_tou[bess_bus]["bess_power_kw"]
        )

    def test_per_bus_charge_discharge_assigned(self):
        gen = ProfileGenerator(_make_config(bess_penetration=1.0))
        gen.generate_all_profiles()
        # The auto-assigner populates separate charge/discharge limits per bus.
        bess_bus = next(b for b in gen.bus_data if b["bess_config"])
        assert "bess_max_charge_kw" in bess_bus
        assert "bess_max_discharge_kw" in bess_bus
