from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BUS_PREFIX: str = "openfmb"
    PROFILES_DIR: str = "outputs/profiles"
    NETWORKS_DIR: str = "outputs/networks"
    # Preferred default network id (optional). Empty = no preference; the engine
    # uses the first available user-provided network. No network is assumed.
    DEFAULT_NETWORK: str = ""
    VOLTAGE_LOWER_PU: float = 0.95
    VOLTAGE_UPPER_PU: float = 1.05
    THERMAL_LIMIT_PCT: float = 100.0
    # Time-of-use tariff for the cost KPIs (AUD/kWh; representative Australian
    # residential TOU). Peak window on a 24-h clock.
    TARIFF_PEAK_RATE: float = 0.45
    TARIFF_OFFPEAK_RATE: float = 0.22
    TARIFF_FEED_IN_RATE: float = 0.05
    TARIFF_PEAK_START: float = 15.0
    TARIFF_PEAK_END: float = 21.0
    # Anytime rate for the built-in 'flat' tariff (AUD/kWh).
    FLAT_RATE: float = 0.30
    # Ambient air temperature for the transformer hot-spot/ageing KPI (deg C).
    TRANSFORMER_AMBIENT_C: float = 25.0
    # Grid emissions intensity for imported energy (kg CO2e per kWh).
    EMISSIONS_KG_PER_KWH: float = 0.60
    # Start the OpenFMB bus participant on boot (disabled in tests — no broker).
    BUS_ENABLED: bool = True
    # OpenFMB message-bus transport: "nats" (primary) or "loopback".
    BUS_TRANSPORT: str = "nats"
    NATS_PORT: int = 4222
    NATS_URL: str = "nats://localhost:4222"

    model_config = {"env_prefix": ""}


settings = Settings()
