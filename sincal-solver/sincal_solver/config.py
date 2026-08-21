from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BUS_PREFIX: str = "openfmb"
    # OpenFMB message-bus transport: "nats" (primary) or "loopback".
    BUS_TRANSPORT: str = "nats"
    NATS_URL: str = "nats://localhost:4222"
    # Working directory for per-session SINCAL project databases.
    SINCAL_WORK_DIR: str = "sincal_work"
    # PSS SINCAL COM automation ProgID (the load-flow simulation server).
    # Empty means auto-discover: the adapter probes the versioned ProgID
    # (Sincal.Simulation.28 for platform 22.5) and then the unsuffixed one.
    SINCAL_PROGID: str = ""
    # Optional path to an EMPTY .sin project (its sibling "<name>_files"
    # folder is cloned per session). Leave unset to have the adapter create
    # each project with SinDBCreate instead, which needs no committed artefact.
    SINCAL_TEMPLATE: str = ""
    # SINCAL's own empty-project creator, used when SINCAL_TEMPLATE is unset.
    SINCAL_DBCREATE: str = (
        r"C:\Program Files\Siemens\PSS SINCAL Platform 22.5\Bin\SinDBCreate.exe")

    model_config = {"env_prefix": ""}


settings = Settings()
