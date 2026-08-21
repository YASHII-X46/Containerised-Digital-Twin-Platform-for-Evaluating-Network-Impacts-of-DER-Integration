"""Integration tests for the Load Engine v5.0 API."""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app, run_generation
from app.models.schemas import SimulationRequest
from app.profiles.custom import CustomProfileError
from tests.ieee33_data import IEEE33_NETWORK_BUSES as NB


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def generate(body: dict) -> tuple[dict, dict]:
    response, profiles = run_generation(SimulationRequest(**body))
    return response.model_dump(), profiles


@pytest.mark.anyio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "load-engine"
    assert data["version"] == "5.0.0"


@pytest.mark.anyio
async def test_generate_default_config(client):
    data, _ = generate({"seed": 42, "network_buses": NB})
    assert data["total_buses"] == 33
    assert data["timesteps"] == 96
    assert len(data["bus_summaries"]) == 33
    assert data["peak_total_load_kw"] > 0
    assert "buses_with_bess" in data
    assert "total_bess_capacity_kwh" in data
    assert "buses_with_ev" in data


@pytest.mark.anyio
async def test_generate_with_der_options(client):
    data, _ = generate({
        "seed": 42,
        "network_buses": NB,
        "bess_penetration": 0.5,
        "ev_penetration": 0.4,
        "bess_config": "powerwall_2",
        "ev_config": "level2_7kw",
        "season": "summer",
    })
    assert data["buses_with_bess"] > 0
    assert data["total_bess_capacity_kwh"] > 0
    assert data["buses_with_ev"] > 0


@pytest.mark.anyio
async def test_generate_custom_bus_data(client):
    bus_data = [
        {"bus_id": 1, "base_load_kw": 100, "base_load_kvar": 40, "customer_class": "res_detached_small", "pv_capacity_kw": 0},
        {"bus_id": 2, "base_load_kw": 200, "base_load_kvar": 80, "customer_class": "res_detached_large", "pv_capacity_kw": 50},
        {"bus_id": 3, "base_load_kw": 150, "base_load_kvar": 60, "customer_class": "res_townhouse", "pv_capacity_kw": 30},
    ]
    data, _ = generate({"bus_data": bus_data, "network_buses": NB})
    assert data["total_buses"] == 3
    classes = {b["bus_id"]: b["customer_class"] for b in data["bus_summaries"]}
    assert classes[1] == "res_detached_small"
    assert classes[2] == "res_detached_large"
    assert classes[3] == "res_townhouse"


@pytest.mark.anyio
async def test_determinism(client):
    data1, _ = generate({"seed": 42, "network_buses": NB})
    data2, _ = generate({"seed": 42, "network_buses": NB})
    assert data1 == data2


@pytest.mark.anyio
async def test_different_seeds(client):
    data1, _ = generate({"seed": 42, "network_buses": NB})
    data2, _ = generate({"seed": 99, "network_buses": NB})
    assert data1["peak_total_load_kw"] != data2["peak_total_load_kw"]


@pytest.mark.anyio
async def test_pv_penetration_zero(client):
    data, _ = generate({"der_penetration_percent": 0, "network_buses": NB})
    assert data["buses_with_pv"] == 0
    assert data["peak_total_pv_kw"] == 0.0


@pytest.mark.anyio
async def test_archetypes_endpoint(client):
    resp = await client.get("/archetypes")
    assert resp.status_code == 200
    data = resp.json()
    assert "res_detached_small" in data
    assert "res_detached_large" in data
    assert "peak_kw_summer" in data["res_detached_small"]


@pytest.mark.anyio
async def test_bess_configs_endpoint(client):
    resp = await client.get("/bess-configs")
    assert resp.status_code == 200
    data = resp.json()
    assert "powerwall_2" in data
    assert "capacity_kwh" in data["powerwall_2"]


@pytest.mark.anyio
async def test_ev_configs_endpoint(client):
    resp = await client.get("/ev-configs")
    assert resp.status_code == 200
    data = resp.json()
    assert "level2_7kw" in data
    assert "charge_rate_kw" in data["level2_7kw"]


@pytest.mark.anyio
async def test_invalid_request(client):
    with pytest.raises(ValidationError):
        SimulationRequest(der_penetration_percent=600)


@pytest.mark.anyio
async def test_generate_for_custom_network(client):
    """Auto-assignment over an arbitrary (non-IEEE33) network."""
    network_buses = [
        {"bus_id": 1, "base_load_kw": 0.0},
        {"bus_id": 2, "base_load_kw": 150.0},
        {"bus_id": 3, "base_load_kw": 200.0},
        {"bus_id": 4, "base_load_kw": 100.0},
        {"bus_id": 5, "base_load_kw": 250.0},
        {"bus_id": 6, "base_load_kw": 120.0},
    ]
    data, _ = generate({
        "scenario_name": "custom_net_test",
        "network_id": "example_radial_6bus",
        "network_buses": network_buses,
        "der_penetration_percent": 80,
        "export_csv": False,
    })
    assert data["total_buses"] == 6
    assert data["network_id"] == "example_radial_6bus"
    assert data["buses_with_pv"] == 5  # all load buses by default
    # PV sized to 80% of the 820 kW total base load
    assert abs(data["total_pv_capacity_kw"] - 0.8 * 820.0) < 1.0


@pytest.mark.anyio
async def test_custom_profile_roundtrip(client):
    """Upload a custom shape, use it on a bus, then delete it."""
    values = [0.2] * 40 + [1.0] * 16 + [0.2] * 40  # boxy midday peak, 96 pts
    up = await client.post("/profiles/custom", json={
        "name": "pytest_shape",
        "description": "test profile",
        "values": values,
    })
    assert up.status_code == 201, up.text
    assert up.json()["customer_class"] == "custom:pytest_shape"

    listing = await client.get("/profiles/custom")
    assert "pytest_shape" in [p["name"] for p in listing.json()["profiles"]]

    bus_data = [
        {"bus_id": 1, "base_load_kw": 0, "base_load_kvar": 0, "customer_class": "res_detached_small"},
        {"bus_id": 2, "base_load_kw": 100, "base_load_kvar": 40, "customer_class": "custom:pytest_shape"},
    ]
    data, _ = generate({
        "bus_data": bus_data, "network_buses": NB, "export_csv": False,
    })
    summary = {b["bus_id"]: b for b in data["bus_summaries"]}
    assert summary[2]["customer_class"] == "custom:pytest_shape"
    assert abs(summary[2]["peak_load_kw"] - 100.0) < 1e-6

    assert (await client.delete("/profiles/custom/pytest_shape")).status_code == 200
    assert (await client.delete("/profiles/custom/pytest_shape")).status_code == 404


@pytest.mark.anyio
async def test_unknown_custom_profile_is_400(client):
    bus_data = [
        {"bus_id": 2, "base_load_kw": 100, "base_load_kvar": 40,
         "customer_class": "custom:does_not_exist"},
    ]
    with pytest.raises(CustomProfileError):
        generate({"bus_data": bus_data, "network_buses": NB})


@pytest.mark.anyio
async def test_winter_pv_lower_than_summer(client):
    """Season now drives PV: a winter scenario yields less PV than summer."""
    common = {"seed": 42, "network_buses": NB, "export_csv": False}
    summer, _ = generate({**common, "season": "summer"})
    winter, _ = generate({**common, "season": "winter"})
    assert winter["peak_total_pv_kw"] < summer["peak_total_pv_kw"]


@pytest.mark.anyio
async def test_non_default_resolution(client):
    """Hourly resolution (24 steps) — previously crashed with fixed-96 shapes."""
    data, _ = generate({
        "timesteps": 24,
        "resolution_minutes": 60,
        "network_buses": NB,
        "export_csv": False,
    })
    assert data["timesteps"] == 24
    assert data["resolution_minutes"] == 60


@pytest.mark.anyio
async def test_ev_charging_mode_accepted_and_validated(client):
    """ev_charging_mode is selectable and validated."""
    generate({
        "seed": 42, "network_buses": NB, "export_csv": False,
        "ev_penetration": 0.4, "ev_charging_mode": "smart",
    })
    with pytest.raises(ValidationError):
        SimulationRequest(ev_charging_mode="bogus")


@pytest.mark.anyio
async def test_inconsistent_day_length_rejected(client):
    """timesteps × resolution_minutes must span exactly one 24-h day."""
    with pytest.raises(ValidationError):
        SimulationRequest(
            timesteps=96, resolution_minutes=60,  # = 96 h, not a day
            network_buses=NB, export_csv=False,
        )


@pytest.mark.anyio
async def test_custom_profile_traversal_name_rejected(client):
    """A crafted custom: name must not escape the profiles directory."""
    bus_data = [
        {"bus_id": 2, "base_load_kw": 100, "base_load_kvar": 40,
         "customer_class": "custom:../../../etc/passwd"},
    ]
    with pytest.raises(CustomProfileError):
        generate({"bus_data": bus_data, "network_buses": NB})
