from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import GitHubAppSettings, Settings


@pytest.mark.parametrize(
    "hosts",
    [
        [],
        ["*"],
        ["*.example.com"],
        ["https://example.com"],
        ["example.com:443"],
        ["example..com"],
        [" bad "],
    ],
)
def test_host_allowlist_rejects_unsafe_configuration(hosts: list[str]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"database_url": "unused", "app_allowed_hosts": hosts})


def test_host_allowlist_accepts_explicit_hosts_and_ipv6() -> None:
    hosts = ["example.com", "localhost", "127.0.0.1", "::1"]
    assert (
        Settings.model_validate(
            {"database_url": "unused", "app_allowed_hosts": hosts}
        ).app_allowed_hosts
        == hosts
    )


def test_host_allowlist_loads_json_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ALLOWED_HOSTS", '["example.com"]')
    assert Settings.model_validate({"database_url": "unused"}).app_allowed_hosts == ["example.com"]


def test_database_url_is_required() -> None:
    assert Settings.model_fields["database_url"].is_required()


def test_catalog_retention_defaults_are_three_days_for_memberships_and_seven_for_generations() -> (
    None
):
    settings = Settings.model_validate(
        {"database_url": "postgresql+psycopg://user:pass@localhost:5432/database"}
    )

    assert settings.catalog_membership_retention_days == 3
    assert settings.catalog_generation_retention_days == 7


def test_github_app_settings_accept_app_credentials() -> None:
    settings = GitHubAppSettings.model_validate(
        {
            "github_app_client_id": "Iv1.example",
            "github_app_private_key_path": Path("C:/private/path/github-app-private-key.pem"),
            "github_app_installation_id": 123,
        }
    )

    assert settings.github_app_client_id == "Iv1.example"
    assert settings.github_app_private_key_path.suffix == ".pem"
    assert settings.github_app_installation_id == 123
