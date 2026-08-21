"""Demand-response control law.

The control law itself is a registry of ``ControlPlugin`` devices
(see ``control_plugins``); ``DRController`` just supplies the per-bus parameters
and runs whatever plugins are installed. New controllable DER types are added by
registering a plugin — no edits here.
"""

from app.control_plugins import compute_setpoints


class DRController:
    """Volt-Watt PV + EV-deferral controller (driven by control plugins)."""

    def __init__(
        self,
        v_upper: float = 1.05,
        v_lower: float = 0.95,
        droop_band: float = 0.05,
        mode: str = "dr_only",
        max_iterations: int = 4,
        bess_max_soc: float = 0.95,
        bess_min_soc: float = 0.1,
        control_ev: bool = True,
        topic_prefix: str = "openfmb",
    ):
        if mode not in ("dr_only", "dr_p2p"):
            raise ValueError(f"Unknown DR mode '{mode}'.")
        if droop_band <= 0:
            raise ValueError("droop_band must be > 0.")
        self.v_upper = v_upper
        self.v_lower = v_lower
        self.droop_band = droop_band
        self.mode = mode
        self.max_iterations = max_iterations
        self.bess_max_soc = bess_max_soc
        self.bess_min_soc = bess_min_soc
        self.control_ev = control_ev
        self.topic_prefix = topic_prefix

    def control_for(self, readings: dict) -> dict:
        """Compute this bus's control setpoints by running the control plugins.

        Always returns the three built-in channels (PV curtailment, EV deferral,
        BESS charge); any extra setpoints a custom control plugin emits are passed
        through under their own keys.
        """
        setpoints = compute_setpoints(
            readings,
            v_upper=self.v_upper,
            v_lower=self.v_lower,
            droop_band=self.droop_band,
            mode=self.mode,
            control_ev=self.control_ev,
            bess_max_soc=self.bess_max_soc,
            bess_min_soc=self.bess_min_soc,
        )
        return {
            "pv_curtailment_kw": setpoints.pop("pv_curtailment_kw", 0.0),
            "ev_curtailment_kw": setpoints.pop("ev_curtailment_kw", 0.0),
            "bess_charge_kw": setpoints.pop("bess_charge_kw", 0.0),
            "bess_discharge_kw": setpoints.pop("bess_discharge_kw", 0.0),
            "pv_reactive_kvar": setpoints.pop("pv_reactive_kvar", 0.0),
            **setpoints,  # custom control-plugin outputs, carried through
        }
