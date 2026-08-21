"""Tests for BESS model."""

import numpy as np
import pytest

from app.profiles.bess_model import (
    BESS_CONFIGS,
    BESSModel,
    create_bess,
    degrade_soh,
    dispatch_bess,
    generate_self_consumption_schedule,
    generate_time_of_use_schedule,
)
from app.profiles.solar_model import generate_solar_profile


class TestBESSModel:
    def test_init_soc(self):
        bess = BESSModel(capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5, initial_soc=0.5)
        assert abs(bess.soc - 0.5) < 1e-6

    def test_discharge_reduces_soc(self):
        bess = BESSModel(capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5, initial_soc=0.5)
        initial_soc = bess.soc
        bess.step(3.0)  # discharge 3 kW
        assert bess.soc < initial_soc

    def test_charge_increases_soc(self):
        bess = BESSModel(capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5, initial_soc=0.5)
        initial_soc = bess.soc
        bess.step(-3.0)  # charge 3 kW
        assert bess.soc > initial_soc

    def test_min_soc_clamp(self):
        bess = BESSModel(
            capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5,
            initial_soc=0.15, min_soc=0.1
        )
        # Try to discharge a huge amount
        for _ in range(100):
            bess.step(5.0)
        assert bess.soc >= 0.1 - 1e-9

    def test_max_soc_clamp(self):
        bess = BESSModel(
            capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5,
            initial_soc=0.9, max_soc=0.95
        )
        # Try to charge a huge amount
        for _ in range(100):
            bess.step(-5.0)
        assert bess.soc <= 0.95 + 1e-9

    def test_efficiency_loss(self):
        bess = BESSModel(
            capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5,
            initial_soc=0.5, efficiency=0.90
        )
        initial_energy = bess.energy_kwh
        # Charge then discharge equal power for same duration
        bess.step(-2.0, 1.0)  # charge 2 kW for 1 hour
        bess.step(2.0, 1.0)   # discharge 2 kW for 1 hour
        # Should end up with less energy due to round-trip losses
        assert bess.energy_kwh < initial_energy

    def test_idle_preserves_soc(self):
        bess = BESSModel(capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5, initial_soc=0.5)
        initial_soc = bess.soc
        bess.step(0.0)
        assert abs(bess.soc - initial_soc) < 1e-9

    def test_self_consumption_charges_midday(self):
        load_kw = np.full(96, 2.0)
        pv_kw = generate_solar_profile(6.0, seed=42)
        bess = create_bess("powerwall_2", initial_soc=0.3)
        result = generate_self_consumption_schedule(load_kw, pv_kw, bess)
        # During midday (steps ~30-65), PV exceeds load → battery charges (negative power)
        midday = result["bess_power_kw"][35:60]
        assert np.any(midday < 0), "Battery should charge during midday excess PV"

    def test_self_consumption_discharges_evening(self):
        # Realistic load: low midday, high evening peak. A flat load would let the
        # greedy self-consumption strategy empty the battery as soon as PV drops in
        # the afternoon, leaving nothing for the evening — that is correct dispatch
        # behaviour, not an evening-discharge scenario. An evening-peaked load is
        # what actually exercises evening discharge.
        load_kw = np.full(96, 1.0)
        load_kw[72:88] = 6.0  # 18:00-22:00 evening peak
        pv_kw = generate_solar_profile(6.0, seed=42)
        bess = create_bess("powerwall_2", initial_soc=0.3)
        result = generate_self_consumption_schedule(load_kw, pv_kw, bess)
        # Evening (steps 72-88, ~18:00-22:00) — no PV, high load → battery discharges.
        evening = result["bess_power_kw"][72:88]
        assert np.any(evening > 0), "Battery should discharge during evening"

    def test_schedule_length_96(self):
        load_kw = np.ones(96) * 2.0
        pv_kw = np.zeros(96)
        bess = create_bess("generic_small", initial_soc=0.8)
        result = generate_self_consumption_schedule(load_kw, pv_kw, bess)
        assert len(result["bess_power_kw"]) == 96
        assert len(result["bess_soc"]) == 96

    def test_create_bess_from_config(self):
        for name in BESS_CONFIGS:
            bess = create_bess(name)
            assert bess.capacity_kwh == BESS_CONFIGS[name]["capacity_kwh"]

    def test_invalid_config_raises(self):
        with pytest.raises(ValueError):
            create_bess("nonexistent_battery")

    def test_asymmetric_named_config(self):
        bess = create_bess("hybrid_asym_3_6")
        assert bess.max_charge_kw == 3.3
        assert bess.max_discharge_kw == 6.6

    def test_create_bess_overrides_rates(self):
        bess = create_bess(
            "powerwall_2", capacity_kwh=20.0, max_charge_kw=2.0, max_discharge_kw=8.0
        )
        assert bess.capacity_kwh == 20.0
        assert bess.max_charge_kw == 2.0      # independent charge limit
        assert bess.max_discharge_kw == 8.0   # independent discharge limit


class TestBESSDegradation:
    def test_no_use_no_fade(self):
        assert degrade_soh(1.0, 0.0, 0.0) == 1.0

    def test_fade_increases_with_cycles(self):
        assert degrade_soh(1.0, 200.0, 1.0) < degrade_soh(1.0, 100.0, 1.0) < 1.0

    def test_fade_increases_with_time(self):
        assert degrade_soh(1.0, 0.0, 100.0) < degrade_soh(1.0, 0.0, 10.0) < 1.0

    def test_soh_floored_at_zero(self):
        assert degrade_soh(0.01, 1_000_000.0, 10_000.0) == 0.0

    def test_initial_soh_shrinks_usable_capacity(self):
        full = create_bess("powerwall_2", initial_soh=1.0)
        aged = create_bess("powerwall_2", initial_soh=0.8)
        assert abs(aged.capacity_kwh - 0.8 * full.capacity_kwh) < 1e-9


class TestBESSDispatchModes:
    def test_time_of_use_charges_and_discharges_in_windows(self):
        load_kw = np.full(96, 3.0)
        bess = create_bess("powerwall_2", initial_soc=0.5)
        res = generate_time_of_use_schedule(
            load_kw, bess, step_hours=0.25,
            charge_window=(1.0, 6.0), discharge_window=(17.0, 21.0),
        )
        power = res["bess_power_kw"]
        # Charge window 01:00-06:00 (steps 4-23): only charging or idle, never discharge.
        assert np.all(power[4:24] <= 1e-9) and np.any(power[4:24] < 0)
        # Discharge window 17:00-21:00 (steps 68-83): discharges.
        assert np.any(power[68:84] > 0)
        # Midday (12:00, step 48) is outside both windows: idle.
        assert abs(power[48]) < 1e-9

    def test_dispatch_bess_selects_mode(self):
        load_kw = np.full(96, 3.0)
        pv_kw = np.zeros(96)
        tou = dispatch_bess("time_of_use", load_kw, pv_kw,
                            create_bess("powerwall_2", initial_soc=0.5))
        self_c = dispatch_bess("self_consumption", load_kw, pv_kw,
                               create_bess("powerwall_2", initial_soc=0.5))
        assert not np.allclose(tou["bess_power_kw"], self_c["bess_power_kw"])

    def test_available_power_respects_timestep_duration(self):
        # SOC-limited availability must scale with the timestep length, not a
        # hardcoded 15-min step. A 1-hour step allows 1/4 the SOC-bound power.
        bess = BESSModel(
            capacity_kwh=10, max_charge_kw=100, max_discharge_kw=100,
            initial_soc=0.5, min_soc=0.1, max_soc=0.95,
        )
        # Usable energy = (0.5 - 0.1) * 10 = 4 kWh; headroom = (0.95-0.5)*10 = 4.5 kWh.
        assert abs(bess.available_discharge_kw(0.25) - 16.0) < 1e-9
        assert abs(bess.available_discharge_kw(1.0) - 4.0) < 1e-9
        assert abs(bess.available_charge_kw(0.25) - 18.0) < 1e-9
        assert abs(bess.available_charge_kw(1.0) - 4.5) < 1e-9

    def test_available_power_bounded_by_rating(self):
        # When SOC headroom is large, the power rating is the binding limit.
        bess = BESSModel(
            capacity_kwh=100, max_charge_kw=5, max_discharge_kw=5, initial_soc=0.5,
        )
        assert bess.available_discharge_kw(1.0) == 5.0
        assert bess.available_charge_kw(1.0) == 5.0
