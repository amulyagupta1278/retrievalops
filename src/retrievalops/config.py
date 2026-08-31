from functools import lru_cache
from pathlib import Path

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
