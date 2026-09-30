from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
)


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables or a local .env file."""

    model_config = SETTINGS_CONFIG

    app_name: str = "GH Random"
    database_url: str
    github_search_delay_seconds: float = Field(default=5.0, gt=0)
    github_rate_limit_retry_seconds: float = Field(default=60.0, gt=0)
    github_rate_limit_max_retries: int = Field(default=2, ge=0)
    github_transport_retry_seconds: float = Field(default=60.0, gt=0)
    github_transport_max_retries: int = Field(default=2, ge=0)
    catalog_membership_retention_days: int = Field(default=3, ge=1)
    catalog_generation_retention_days: int = Field(default=7, ge=1)


class GitHubAppSettings(BaseSettings):
    """GitHub App credentials, loaded only by crawler code."""

    model_config = SETTINGS_CONFIG

    github_app_client_id: str = Field(min_length=1)
    github_app_private_key_path: Path
    github_app_installation_id: int | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # Values load from environment sources.


@lru_cache
def get_github_app_settings() -> GitHubAppSettings:
    return GitHubAppSettings()  # pyright: ignore[reportCallIssue]  # Values load from environment sources.
