from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from RPCMS_SYNC_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="RPCMS_SYNC_", env_file=".env", extra="ignore")

    jwt_key: str = Field(min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_audience: str | None = None

    database_url: str = "sqlite:///./rpcms_offline_sync.db"

    guard_url: str = "http://localhost:8000"
    guard_api_key: str = Field(min_length=16)
    guard_timeout_seconds: float = 10.0
    # Used to derive the Duplicate Guard idempotency key from (partner, device, client record id)
    # so every retry of a record carries the same key.
    idempotency_salt: str = Field(min_length=16)

    # Role (a string in the JWT 'roles' claim) allowed to requeue FAILED records.
    admin_role: str = "sync-admin"
    # If set, SYNCED/REJECTED records older than this many days have their ProGres ID and auth
    # reference scrubbed. None (default) keeps everything: choose a retention policy before real data.
    retention_days: int | None = Field(default=None, ge=1)

    max_batch_records: int = 500
    max_clock_skew_seconds: int = 300  # capturedAt may be at most this far in the future
    max_attempts: int = 8
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 3600
    worker_batch_size: int = 100
    worker_poll_seconds: float = 5.0
