from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BUS_PREFIX: str = "openfmb"
    DEFAULT_SEED: int = 42
    CUSTOM_PROFILES_DIR: str = "outputs/custom_profiles"
    # Start the OpenFMB bus participant on boot (broker required).
    BUS_ENABLED: bool = True
    # OpenFMB message-bus transport: "nats" (primary) or "loopback".
    BUS_TRANSPORT: str = "nats"
    NATS_PORT: int = 4222
    NATS_URL: str = "nats://localhost:4222"

    # Drop-in extensions: importable module names (comma-separated) and/or a
    # directory of *.py files that register DER plugins / archetypes / weather
    # providers at import. Loaded at startup; empty by default.
    DER_PLUGIN_MODULES: str = ""
    DER_PLUGINS_DIR: str = ""

    # Local CSV for the 'file' weather source: hourly rows "temp_C[,ghi_Wm2]"
    # (24 rows per day; wraps when shorter than the horizon).
    WEATHER_FILE: str = ""

    model_config = {"env_prefix": ""}


settings = Settings()
