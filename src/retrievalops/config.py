from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from retrievalops import __version__


class Settings(BaseSettings):
    """Runtime configuration loaded from RETRIEVALOPS_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="RETRIEVALOPS_",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "retrievalops"
    service_version: str = __version__
    build_sha: str = Field(default="development", min_length=1)
    storage_root: Path = Path(".data/artifacts")
    database_url: str = "sqlite:///.data/retrievalops.db"
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(default=200, gt=0)
    sandbox_ttl_hours: int = Field(default=24, gt=0)
    mlflow_tracking_uri: str | None = None
    mlflow_artifact_root: Path = Path(".data/mlflow-artifacts")
    dependency_lock_hash: str = "development"
    otlp_traces_endpoint: str | None = None
    drift_min_approved_feedback: int = Field(default=3, ge=3)
    query_drift_threshold: float = Field(default=0.15, ge=0, le=1)
    allowed_origins: list[str] = Field(default_factory=list)
    sandbox_requests_per_minute: int = Field(default=120, gt=0, le=10_000)
    extraction_timeout_seconds: float = Field(default=30, gt=0, le=300)

    @model_validator(mode="after")
    def validate_lineage_configuration(self) -> "Settings":
        if self.mlflow_tracking_uri:
            if not _is_hex_digest(self.build_sha, minimum=7):
                raise ValueError("MLflow requires build_sha to be a hexadecimal commit digest")
            if not _is_hex_digest(self.dependency_lock_hash, minimum=64):
                raise ValueError("MLflow requires a SHA-256 dependency_lock_hash")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _is_hex_digest(value: str, *, minimum: int) -> bool:
    return minimum <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value
    )
