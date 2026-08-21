from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BUS_PREFIX: str = "openfmb"
    BUS_TRANSPORT: str = "nats"
    NATS_URL: str = "nats://localhost:4222"
    VOLTAGE_LOWER_PU: float = 0.95
    VOLTAGE_UPPER_PU: float = 1.05

    model_config = {"env_prefix": ""}


settings = Settings()
