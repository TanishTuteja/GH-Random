import re
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path

from pydantic import Field, field_validator
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
    app_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1", "testserver"], min_length=1
    )
    github_search_delay_seconds: float = Field(default=5.0, gt=0)
    github_rate_limit_retry_seconds: float = Field(default=60.0, gt=0)
    github_rate_limit_max_retries: int = Field(default=2, ge=0)
    github_transport_retry_seconds: float = Field(default=60.0, gt=0)
    github_transport_max_retries: int = Field(default=2, ge=0)
    catalog_membership_retention_days: int = Field(default=3, ge=1)
    catalog_generation_retention_days: int = Field(default=7, ge=1)

    @field_validator("app_allowed_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        """Accept exact hostnames/IPs, never URLs, ports or wildcard host checks."""
        for host in hosts:
            try:
                ip_address(host)
            except ValueError:
                if len(host) > 253 or not all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in host.split(".")
                ):
                    raise ValueError("Allowed hosts must be lowercase exact hostnames or IPs")
        return hosts


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
