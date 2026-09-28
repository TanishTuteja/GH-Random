from pathlib import Path

from app.core.settings import GitHubAppSettings, Settings


def test_database_url_is_required() -> None:
    assert Settings.model_fields["database_url"].is_required()


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
