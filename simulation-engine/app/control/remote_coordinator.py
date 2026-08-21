"""Remote DR coordination over the OpenFMB/NATS command bus."""

import uuid

from fastapi import HTTPException

# Control-message reading keys mapped to dedicated OpenDSS elements. Any other
# reading is a custom control-plugin setpoint, applied generically below.
_KNOWN_CONTROL_KEYS = frozenset({
    "curtailment_kW", "evCurtailment_kW", "bessCharge_kW",
    "bessDischarge_kW", "pvReactive_kVAr",
})


def _request_ok(participant, service: str, action: str, payload: dict,
                timeout: float = 60.0) -> dict:
    evt = participant.request(service, action, payload, timeout=timeout)
    if evt is None:
        raise HTTPException(
            status_code=504,
            detail=f"Timed out waiting for {service}/{action} on the NATS bus.",
        )
    if evt.get("status") != "ok":
        detail = evt.get("payload", {}).get("error") or evt.get("payload") or evt
        raise HTTPException(status_code=502, detail=f"{service}/{action}: {detail}")
    return evt.get("payload") or {}


class RemoteCoordinator:
    """Coordinates QSTS control by calling external bus participants.

    The Simulation Engine keeps ownership of the OpenDSS solve. Prosumer shadow
    twins and the DR controller run as separate containers and are reached through
    OpenFMB command/event messages on NATS.
    """

    def __init__(
        self,
        participant,
        profiles: dict,
        mode: str,
        settings,
        topic_prefix: str = "openfmb",
        twin_config: dict | None = None,
        volt_watt=None,
        envelopes: dict | None = None,
    ):
        self._participant = participant
        self._profiles = profiles
        # Autonomous Volt-Watt curve (or None). Precedence is defined as:
        # the inverter's autonomous reduction applies first, then DR curtailment
        # on top of that reduced output — so commanding a curtailment never
        # silently undoes the standards response.
        self._volt_watt = volt_watt
        # Managed dynamic operating envelopes: per-site export-limit series.
        # The coordinator acts as the utility comms layer — it publishes each
        # site's current exportLimit_kW alongside its status, and the DR
        # controller's envelope plugin enforces it (store first, curtail rest).
        self._envelopes = envelopes or {}
        self.mode = mode
        self.topic_prefix = topic_prefix
        self.session_id = str(uuid.uuid4())
        self._summary: dict = {
            "mode": mode,
            "prosumer_twins": 0,
            "buses_curtailed": 0,
            "total_pv_curtailed_kwh": 0.0,
            "total_ev_deferred_kwh": 0.0,
            "total_pv_shared_kwh": 0.0,
            "messages": 0,
        }

        config = _request_ok(
            participant,
            "dr-controller",
            "configure",
            {
                "session_id": self.session_id,
                "mode": mode,
                "v_upper": settings.VOLTAGE_UPPER_PU,
                "v_lower": settings.VOLTAGE_LOWER_PU,
                "topic_prefix": topic_prefix,
            },
        )
        self.max_iterations = int(config.get("max_iterations", 4))
        self.mode = config.get("mode", mode)
        self._summary["mode"] = self.mode

        start_payload = {
            "session_id": self.session_id,
            "profiles": profiles,
            "mode": mode,
            "topic_prefix": topic_prefix,
        }
        # Forward optional twin configuration so the prosumer-shadow-twins service
        # applies it per session (selection thresholds / modelling assumptions).
        if twin_config:
            start_payload["config"] = twin_config
        started = _request_ok(
            participant,
            "prosumer-shadow-twins",
            "start",
            start_payload,
        )
        self._summary["prosumer_twins"] = int(started.get("prosumer_twins", 0))

    def coordinate(self, t: int, engine, step_hours: float, timestamp: str,
                   pv_base: dict | None = None) -> bool:
        """Run the closed-loop DR exchange for one timestep.

        ``pv_base`` optionally overrides the scheduled PV per bus — used when a
        dynamic-operating-envelope cap has already reduced a site's output, so
        DR curtailment applies to the capped value rather than re-inflating it.
        """
        applied_pv: dict[int, float] = {}
        applied_ev: dict[int, float] = {}
        applied_bess: dict[int, float] = {}
        applied_support: dict[int, float] = {}   # battery discharge (peak support)
        applied_q: dict[int, float] = {}         # commanded inverter reactive (kvar)
        # Setpoints from custom control plugins (any non-built-in control key),
        # applied generically as a per-bus net-load reduction.
        applied_other: dict[int, float] = {}
        converged = True

        for _ in range(self.max_iterations):
            voltages = engine.get_bus_voltages_pu()
            status_payload = _request_ok(
                self._participant,
                "prosumer-shadow-twins",
                "status",
                {
                    "session_id": self.session_id,
                    "timestep": t,
                    "voltages": voltages,
                    "step_hours": step_hours,
                    "timestamp": timestamp,
                    "topic_prefix": self.topic_prefix,
                },
            )
            statuses = status_payload.get("statuses", [])
            # Publish each site's operating-envelope limit with its status —
            # the utility-to-site-controller leg of managed DOE enforcement.
            if self._envelopes:
                for status in statuses:
                    try:
                        series = self._envelopes.get(int(status.get("bus_id")))
                    except (TypeError, ValueError):
                        series = None
                    if series is None:
                        continue
                    payload = status.setdefault("payload", {})
                    payload.setdefault("readings", {})["exportLimit_kW"] = round(
                        float(series[t]), 3
                    )
            control_payload = _request_ok(
                self._participant,
                "dr-controller",
                "control",
                {
                    "session_id": self.session_id,
                    "mode": self.mode,
                    "statuses": statuses,
                    "topic_prefix": self.topic_prefix,
                },
            )

            changed = self._accumulate(
                control_payload.get("controls", []),
                applied_pv,
                applied_ev,
                applied_bess,
                applied_other,
                applied_support,
                applied_q,
            )
            if not changed:
                break

            for bus_id, kw in applied_pv.items():
                base = self._expected(bus_id, t, "pv_kw")
                # An envelope cap (autonomous DOE) lowers the baseline first.
                if pv_base is not None and bus_id in pv_base:
                    base = pv_base[bus_id]
                # With autonomous Volt-Watt active, the inverter has already
                # reduced its output for the local voltage; DR curtailment
                # applies to that reduced output, not the scheduled value —
                # otherwise re-applying the full base would undo the response.
                if self._volt_watt is not None:
                    base *= self._volt_watt.factor(voltages.get(bus_id, 1.0))
                engine.update_pv(bus_id, max(0.0, base - kw))
            for bus_id, kw in applied_ev.items():
                base = self._expected(bus_id, t, "ev_charge_kw")
                engine.update_ev(bus_id, max(0.0, base - kw))
            for bus_id in set(applied_bess) | set(applied_support):
                base = self._expected(bus_id, t, "bess_power_kw")
                # Charge commands reduce battery output; support commands add
                # discharge on top (the two respond to opposite voltage signals).
                engine.update_bess(
                    bus_id,
                    base - applied_bess.get(bus_id, 0.0) + applied_support.get(bus_id, 0.0),
                )
            for bus_id, kvar in applied_q.items():
                engine.update_pv_reactive(bus_id, kvar)
            # Custom DER setpoints reduce the bus building load (which already
            # carries non-built-in DERs via `other_der_kw`). Positive = shed.
            for bus_id, shed_kw in applied_other.items():
                base_load = (
                    self._expected(bus_id, t, "load_kw")
                    + self._expected(bus_id, t, "other_der_kw")
                )
                base_kvar = self._expected(bus_id, t, "load_kvar")
                engine.update_load(bus_id, max(0.0, base_load - shed_kw), base_kvar)
            converged = engine.solve()

        final_voltages = engine.get_bus_voltages_pu()
        record = _request_ok(
            self._participant,
            "prosumer-shadow-twins",
            "record",
            {
                "session_id": self.session_id,
                "mode": self.mode,
                "timestep": t,
                "final_voltages": final_voltages,
                "step_hours": step_hours,
                "controls": {
                    str(bus_id): {
                        "curtailed_pv_kw": applied_pv.get(bus_id, 0.0),
                        "deferred_ev_kw": applied_ev.get(bus_id, 0.0),
                        "shared_kw": applied_bess.get(bus_id, 0.0),
                        "other_shed_kw": applied_other.get(bus_id, 0.0),
                        "support_kw": applied_support.get(bus_id, 0.0),
                    }
                    for bus_id in (
                        set(applied_pv) | set(applied_ev) | set(applied_bess)
                        | set(applied_other) | set(applied_support)
                    )
                },
            },
        )
        self._summary = record.get("summary", self._summary)
        self._summary.setdefault("mode", self.mode)
        return converged

    def _expected(self, bus_id: int, timestep: int, key: str) -> float:
        bus = self._profiles["buses"].get(bus_id) or self._profiles["buses"].get(str(bus_id))
        if not bus:
            return 0.0
        return float(bus["timeseries"][timestep].get(key, 0.0))

    @staticmethod
    def _accumulate(controls, applied_pv, applied_ev, applied_bess, applied_other,
                    applied_support, applied_q) -> bool:
        changed = False
        for control in controls:
            try:
                bus_id = int(control["bus_id"])
            except (KeyError, TypeError, ValueError):
                continue
            readings = control.get("readings", {})
            for target, key in (
                (applied_pv, "curtailment_kW"),
                (applied_ev, "evCurtailment_kW"),
                (applied_bess, "bessCharge_kW"),
                (applied_support, "bessDischarge_kW"),
            ):
                kw = float(readings.get(key, 0.0))
                if kw > target.get(bus_id, 0.0) + 1e-6:
                    target[bus_id] = kw
                    changed = True
            # Reactive is a signed setpoint (inject/absorb): track the latest
            # value rather than a running maximum.
            kvar = float(readings.get("pvReactive_kVAr", 0.0))
            if abs(kvar - applied_q.get(bus_id, 0.0)) > 1e-6:
                applied_q[bus_id] = kvar
                changed = True
            # Sum any custom control-plugin setpoints into one per-bus shed.
            custom = sum(
                float(v) for k, v in readings.items() if k not in _KNOWN_CONTROL_KEYS
            )
            if custom > applied_other.get(bus_id, 0.0) + 1e-6:
                applied_other[bus_id] = custom
                changed = True
        return changed

    def summary(self) -> dict:
        return self._summary

    def close(self) -> None:
        for service in ("prosumer-shadow-twins", "dr-controller"):
            try:
                _request_ok(
                    self._participant,
                    service,
                    "stop",
                    {"session_id": self.session_id},
                    timeout=10.0,
                )
            except HTTPException:
                pass
