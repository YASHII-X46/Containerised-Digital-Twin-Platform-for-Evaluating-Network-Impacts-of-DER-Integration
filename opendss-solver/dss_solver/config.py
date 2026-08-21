from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BUS_PREFIX: str = "openfmb"
    # OpenFMB message-bus transport: "nats" (primary) or "loopback".
    BUS_TRANSPORT: str = "nats"
    NATS_URL: str = "nats://localhost:4222"
    # Working directory for generated OpenDSS files; each build gets its own
    # session subdirectory, so concurrent stacks never collide on files.
    DSS_DIR: str = "dss_work"

    model_config = {"env_prefix": ""}


settings = Settings()
