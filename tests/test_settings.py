from pathlib import Path

from app.core.settings import GitHubAppSettings, Settings


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
