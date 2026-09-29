from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import Settings
from app.db.models import CatalogGeneration, GenerationRepository, Repository
from app.main import create_app

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _repository(repository_id: int) -> Repository:
    return Repository(
        github_id=repository_id,
        full_name=f"octo/repository-{repository_id}",
        name=f"repository-{repository_id}",
        owner_login="octo",
        html_url=f"https://github.com/octo/repository-{repository_id}",
        description="A web API test repository.",
        language="Python",
        stargazer_count=42,
        fork_count=3,
        open_issues_count=4,
        is_archived=False,
        is_fork=False,
        github_created_at=NOW - timedelta(days=400),
        github_updated_at=NOW - timedelta(days=1),
        github_pushed_at=NOW - timedelta(days=3),
        topics=["testing"],
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def test_index_and_public_random_api_use_only_active_catalog(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, session.begin():
        active = CatalogGeneration(status="active", created_at=NOW, activated_at=NOW)
        retired = CatalogGeneration(status="retired", created_at=NOW, activated_at=NOW)
        session.add_all([active, retired])
        session.flush()
        session.add_all([_repository(1), _repository(2)])
        session.flush()
        session.add_all(
            [
                GenerationRepository(
                    generation_id=active.id,
                    github_id=1,
                    last_seen_at=NOW,
                ),
                GenerationRepository(
                    generation_id=retired.id,
                    github_id=2,
                    last_seen_at=NOW,
                ),
            ]
        )

    app = create_app(
        Settings.model_validate(
            {"database_url": "postgresql+psycopg://gh_random@127.0.0.1/unused"}
        ),
        session_factory,
        clock=lambda: NOW,
    )
    with TestClient(app) as client:
        page = client.get(
            "/",
            params={
                "discover": "true",
                "language": "python",
                "star_bucket": "0-100",
                "pushed_bucket": "pushed-within-week",
            },
        )
        response = client.get(
            "/api/random-repository",
            params={
                "language": "python",
                "star_bucket": "0-100",
                "pushed_bucket": "pushed-within-week",
            },
        )
        disabled_routes = {
            path: client.get(path).status_code
            for path in [
                "/health",
                "/api/filter-options",
                "/docs",
                "/redoc",
                "/openapi.json",
                "/static/styles.css",
            ]
        }

    assert page.status_code == 200
    assert "octo/repository-1" in page.text
    assert 'name="pushed_bucket"' in page.text
    assert response.status_code == 200
    assert response.json()["github_id"] == 1
    assert response.json()["topics"] == ["testing"]
    assert response.headers["cache-control"] == "no-store"
    assert set(disabled_routes.values()) == {404}


def test_public_random_api_reports_invalid_or_empty_requests(
    session_factory: sessionmaker[Session],
) -> None:
    app = create_app(
        Settings.model_validate(
            {"database_url": "postgresql+psycopg://gh_random@127.0.0.1/unused"}
        ),
        session_factory,
        clock=lambda: NOW,
    )

    with TestClient(app) as client:
        invalid = client.get("/api/random-repository", params={"language": "kotlin"})
        empty = client.get("/api/random-repository")

    assert invalid.status_code == 422
    assert "Unsupported language" in invalid.json()["detail"]
    assert empty.status_code == 404
    assert "No active catalog repository" in empty.json()["detail"]
