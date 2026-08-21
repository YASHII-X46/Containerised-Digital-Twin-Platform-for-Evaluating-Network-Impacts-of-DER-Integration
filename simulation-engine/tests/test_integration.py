"""Integration tests for the Simulation Engine API (via FastAPI TestClient)."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app, run_simulation
from app.models.schemas import SimulationRequest


def test_health_and_network():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        net = client.get("/networks/ieee33").json()
        assert len(net["buses"]) == 33
        assert len(net["branches"]) == 32


def test_networks_registry_endpoints():
    with TestClient(app) as client:
        listing = client.get("/networks").json()
        ids = {n["id"] for n in listing["networks"]}
        assert "ieee33" in ids
        assert listing["default"] == "ieee33"

        detail = client.get("/networks/ieee33").json()
        assert len(detail["buses"]) == 33
        assert len(detail["branches"]) == 32

        assert client.get("/networks/no_such_net").status_code == 404


def test_network_upload_and_delete():
    body = {
        "id": "pytest_net",
        "name": "pytest network",
        "base_voltage_kv": 11.0,
        "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
            {"bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0},
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2,
             "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/networks", json=body)
        assert resp.status_code == 201, resp.text
        assert "pytest_net" in {n["id"] for n in client.get("/networks").json()["networks"]}
        assert client.delete("/networks/pytest_net").status_code == 200

        bad = dict(body, id="bad_net", branches=[
            {"branch_id": 1, "from_bus": 1, "to_bus": 99,
             "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 2000},
        ])
        assert client.post("/networks", json=bad).status_code == 400


def test_run_simulation_core(sample_profiles, solver_bus):
    with TestClient(app):
        # The power flow runs in the solver container; tests wire the real
        # opendss-solver service onto the loopback bus (see conftest).
        app.state.bus = solver_bus
        body = run_simulation(SimulationRequest(
            profiles=sample_profiles,
            network_id="ieee33",
        )).model_dump()
        assert body["status"] == "completed"
        assert body["network_id"] == "ieee33"
        assert body["solver"] == "opendss"
        assert body["total_timesteps"] == 2
        assert body["converged_timesteps"] == 2
        assert body["buses_with_pv"] == 2
        assert body["buses_with_bess"] == 1
        assert body["buses_with_ev"] == 1
        assert len(body["bus_voltage_summary"]) == 33
        assert len(body["branch_loading_summary"]) == 32


def test_unknown_solver_rejected(sample_profiles, solver_bus):
    with TestClient(app):
        app.state.bus = solver_bus
        with pytest.raises(HTTPException) as exc:
            run_simulation(SimulationRequest(
                profiles=sample_profiles,
                network_id="ieee33",
                solver="powerfactory",
            ))
        assert exc.value.status_code == 400
        assert "Unknown solver" in exc.value.detail


def test_simulate_without_bus_is_503(sample_profiles):
    """No bus participant -> the solver container is unreachable by design."""
    with TestClient(app):
        app.state.bus = None
        with pytest.raises(HTTPException) as exc:
            run_simulation(SimulationRequest(
                profiles=sample_profiles,
                network_id="ieee33",
            ))
        assert exc.value.status_code == 503


def test_simulate_missing_profiles_rejected():
    with pytest.raises(ValidationError):
        SimulationRequest(network_id="ieee33")


def test_simulate_unknown_network_raises_404(sample_profiles):
    with TestClient(app):
        with pytest.raises(HTTPException) as exc:
            run_simulation(SimulationRequest(
                profiles=sample_profiles,
                network_id="ghost_network",
            ))
        assert exc.value.status_code == 404
