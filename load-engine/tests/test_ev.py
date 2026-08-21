"""Tests for EV charging model."""

import numpy as np
import pytest

from app.profiles.ev_model import (
    EV_CONFIGS,
    create_ev_profile,
    generate_diversified_ev_profile,
    generate_ev_charging_profile,
)


class TestEVModel:
    def test_profile_length_96(self):
        result = generate_ev_charging_profile()
        assert len(result["ev_charge_kw"]) == 96
        assert len(result["ev_soc"]) == 96

    def test_all_values_non_negative(self):
        result = generate_ev_charging_profile()
        assert np.all(result["ev_charge_kw"] >= 0)

    def test_charging_starts_after_arrival(self):
        result = generate_ev_charging_profile(
            arrival_hour=18.0, seed=100
        )
        # Before 17:00 (index 68) there should be no charging
        # (allowing for 30-min jitter, so before 17:30 = index 70 should be mostly zero)
        assert np.all(result["ev_charge_kw"][:66] == 0)

    def test_no_charging_during_day(self):
        result = generate_ev_charging_profile(
            arrival_hour=18.0, departure_hour=7.0, seed=42
        )
        # Between 08:00 (index 32) and 17:00 (index 68) should be zero
        daytime = result["ev_charge_kw"][32:68]
        assert np.all(daytime == 0)

    def test_charge_stops_when_full(self):
        result = generate_ev_charging_profile(
            charge_rate_kw=7.0,
            battery_kwh=60.0,
            initial_soc=0.85,
            arrival_hour=18.0,
            seed=42,
        )
        # Only 5% (3 kWh) needed: 7kW * 0.25h = 1.75 kWh per step → ~2 steps
        charging_steps = np.sum(result["ev_charge_kw"] > 0)
        assert charging_steps <= 4  # Should stop quickly

    def test_energy_consumed_physically_reasonable(self):
        result = generate_ev_charging_profile(
            charge_rate_kw=7.0,
            battery_kwh=60.0,
            initial_soc=0.3,
            seed=42,
        )
        # From 30% to 90% = 36 kWh needed
        expected = 0.6 * 60.0
        assert abs(result["energy_consumed_kwh"] - expected) < 1.0

    def test_soc_follows_charging_order_across_midnight(self):
        # A session that plugs in at 22:00 charges past midnight, so the SOC
        # peak is reached in the early-morning continuation — not misattributed
        # to the 23:45 clock-end as the old clock-order cumsum did.
        res = generate_ev_charging_profile(
            charge_rate_kw=7.0, battery_kwh=60.0, initial_soc=0.3,
            arrival_hour=22.0, departure_hour=7.0, seed=42,
        )
        soc = res["ev_soc"]
        assert np.argmax(soc) < 40            # peak SOC in the early-morning hours
        assert soc.max() > 0.85               # reaches ~target
        assert soc[95] < soc.max() - 1e-6     # 23:45 is mid-session, below peak

    def test_arrival_jitter_with_seed(self):
        r1 = generate_ev_charging_profile(seed=10)
        r2 = generate_ev_charging_profile(seed=20)
        # Different seeds → different arrival jitter → different profiles
        assert not np.array_equal(r1["ev_charge_kw"], r2["ev_charge_kw"])

    def test_create_from_config(self):
        for name in EV_CONFIGS:
            result = create_ev_profile(name, seed=42)
            assert len(result["ev_charge_kw"]) == 96
            assert result["energy_consumed_kwh"] >= 0


class TestChargingModes:
    STEP_H = 24.0 / 96

    def test_uncontrolled_charges_from_arrival(self):
        ev = generate_ev_charging_profile(arrival_hour=18.0, mode="uncontrolled", seed=42)[
            "ev_charge_kw"
        ]
        # Charging begins in the evening, at full rate.
        assert ev[68:96].sum() > 0
        assert abs(ev.max() - 7.0) < 1e-9

    def test_offpeak_defers_to_the_timer_window(self):
        ev = generate_ev_charging_profile(
            arrival_hour=18.0, offpeak_start_hour=23.0, mode="offpeak", seed=42
        )["ev_charge_kw"]
        # No charging between arrival (18:00 = idx 72) and the 23:00 timer (idx 92)...
        assert ev[72:92].sum() == 0.0
        # ...then it charges once the off-peak window opens.
        assert ev[92:96].sum() > 0.0

    def test_smart_lowers_peak_and_spreads_charging(self):
        unc = generate_ev_charging_profile(mode="uncontrolled", seed=42)["ev_charge_kw"]
        smart = generate_ev_charging_profile(mode="smart", seed=42)["ev_charge_kw"]
        assert smart.max() < unc.max()                                   # flatter
        assert np.count_nonzero(smart) > np.count_nonzero(unc)            # wider

    def test_all_modes_deliver_the_same_energy(self):
        energies = {
            m: generate_ev_charging_profile(mode=m, seed=42)["ev_charge_kw"].sum() * self.STEP_H
            for m in ("uncontrolled", "offpeak", "smart")
        }
        # Same EV needs the same kWh regardless of when/how it charges (~12 kWh
        # for the default 0.7 -> 0.9 daily top-up on a 60 kWh battery).
        assert max(energies.values()) - min(energies.values()) < 1.0
        assert abs(energies["uncontrolled"] - 12.0) < 1.0

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            generate_ev_charging_profile(mode="bogus")

    def test_create_ev_profile_forwards_mode(self):
        smart = create_ev_profile("level2_7kw", seed=42, mode="smart")["ev_charge_kw"]
        unc = create_ev_profile("level2_7kw", seed=42, mode="uncontrolled")["ev_charge_kw"]
        assert smart.max() < unc.max()

    def test_diversified_forwards_mode(self):
        unc = generate_diversified_ev_profile(
            "level2_7kw", n_evs=32, seed=42, mode="uncontrolled"
        )["ev_charge_kw"]
        off = generate_diversified_ev_profile(
            "level2_7kw", n_evs=32, seed=42, mode="offpeak"
        )["ev_charge_kw"]
        assert not np.array_equal(unc, off)


class TestEVDiversity:
    def test_diversified_spreads_charging(self):
        rate = 7.0
        single = create_ev_profile("level2_7kw", seed=42)["ev_charge_kw"]
        diverse = generate_diversified_ev_profile("level2_7kw", n_evs=64, seed=42)[
            "ev_charge_kw"
        ]
        # Staggered arrivals/finishes spread charging over more timesteps and
        # replace the hard on/off block with gradual (partial-power) ramps. The
        # plateau can still reach full rate (uncontrolled overlap is realistic),
        # so the peak is not higher, not necessarily lower.
        assert diverse.max() <= single.max() + 1e-9
        assert np.count_nonzero(diverse) > np.count_nonzero(single)
        partial = lambda p: int(np.sum((p > 0.01) & (p < rate - 0.01)))
        assert partial(diverse) > 5 * partial(single)

    def test_diversified_conserves_per_charger_energy(self):
        single = create_ev_profile("level2_7kw", seed=42)
        diverse = generate_diversified_ev_profile("level2_7kw", n_evs=64, seed=42)
        # The mean-of-EVs keeps roughly one charger's daily energy (not N times).
        step_h = 24.0 / 96
        single_energy = single["ev_charge_kw"].sum() * step_h
        diverse_energy = diverse["ev_charge_kw"].sum() * step_h
        assert abs(diverse_energy - single_energy) < 8.0  # kWh, within SOC spread

    def test_diversified_is_deterministic(self):
        a = generate_diversified_ev_profile("level2_7kw", n_evs=32, seed=7)
        b = generate_diversified_ev_profile("level2_7kw", n_evs=32, seed=7)
        np.testing.assert_array_equal(a["ev_charge_kw"], b["ev_charge_kw"])
        c = generate_diversified_ev_profile("level2_7kw", n_evs=32, seed=8)
        assert not np.array_equal(a["ev_charge_kw"], c["ev_charge_kw"])

    def test_wider_sigma_spreads_more(self):
        narrow = generate_diversified_ev_profile(
            "level2_7kw", n_evs=64, seed=42, arrival_sigma_minutes=15
        )
        wide = generate_diversified_ev_profile(
            "level2_7kw", n_evs=64, seed=42, arrival_sigma_minutes=240
        )
        # A wider arrival spread charges over more timesteps and, once the spread
        # is comparable to the charge duration, lowers the coincident peak.
        assert np.count_nonzero(wide["ev_charge_kw"]) > np.count_nonzero(
            narrow["ev_charge_kw"]
        )
        assert wide["ev_charge_kw"].max() < narrow["ev_charge_kw"].max()

    def test_invalid_config_raises(self):
        with pytest.raises(ValueError):
            generate_diversified_ev_profile("nonexistent", n_evs=10)
