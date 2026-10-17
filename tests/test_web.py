from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import Settings
from app.db.models import CatalogGeneration, GenerationRepository, Repository
from app.main import create_app

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def test_host_validation_rejects_unknown_host_without_querying_database() -> None:
    settings = Settings.model_validate(
        {
            "database_url": "postgresql+psycopg://unused@localhost/unused",
            "app_allowed_hosts": ["example.com"],
        }
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
        assert client.get("/", headers={"Host": "example.com"}).status_code == 200
        assert client.get("/", headers={"Host": "example.com:443"}).status_code == 200
        assert client.get("/", headers={"Host": "www.example.com"}).status_code == 400
        assert (
            client.get(
                "/", headers={"Host": "evil.example", "X-Forwarded-Host": "example.com"}
            ).status_code
            == 400
        )


def _repository(repository_id: int, *, topics: list[str] | None = None) -> Repository:
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
        topics=topics if topics is not None else ["testing"],
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def test_index_and_public_random_api_use_only_active_catalog(
    session_factory: sessionmaker[Session],
) -> None:
    topics = [f"topic-{index}" for index in range(10)]
    with session_factory() as session, session.begin():
        active = CatalogGeneration(status="active", created_at=NOW, activated_at=NOW)
        retired = CatalogGeneration(status="retired", created_at=NOW, activated_at=NOW)
        session.add_all([active, retired])
        session.flush()
        session.add_all([_repository(1, topics=topics), _repository(2)])
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
        initial_page = client.get("/")
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
        default_response = client.get("/api/random-repository")
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

    assert initial_page.status_code == 200
    assert initial_page.text.count('value="any"') == 3
    assert "GitHub Repository Finder" in initial_page.text
    assert "Search the catalog" not in initial_page.text
    assert "paced, repeatable crawl" not in initial_page.text
    assert "SET THE SIGNAL" not in initial_page.text
    assert "Last seen by this catalog" not in initial_page.text
    assert 'class="home-link" href="/"' in initial_page.text
    assert 'href="https://github.com/TanishTuteja/GH-Random"' in initial_page.text
    assert 'href="https://github.com/TanishTuteja/GH-Random/issues"' in initial_page.text
    assert "Found a bug? Have a feature request?" in initial_page.text
    assert 'class="relative-time local-time"' in page.text
    assert 'timeZoneName: "short"' in page.text
    assert "relativeDate" in page.text
    assert "ageInMilliseconds <= 604_800_000" in page.text
    assert "View on GitHub" in page.text
    assert "Find repo" in initial_page.text
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in initial_page.text
    assert 'id="discovery-form"' in initial_page.text
    assert "event.preventDefault()" in initial_page.text
    assert "history.replaceState" in initial_page.text
    assert "preserveViewport" in initial_page.text
    assert "window.scrollTo(scrollX, scrollY)" in initial_page.text
    assert 'classList.add("is-loading")' in initial_page.text
    assert 'content: "Finding a repository…"' in initial_page.text
    assert page.status_code == 200
    assert "octo/repository-1" in page.text
    assert "<dt>Stars</dt><dd>42</dd>" in page.text
    assert "★" not in page.text
    assert "topic-7" in page.text
    assert 'class="extra-topic" hidden>topic-8</li>' in page.text
    assert "+2 more" in page.text
    assert 'class="topic-toggle" type="button" aria-expanded="false"' in page.text
    assert "Show less" in page.text
    assert 'data-repository-id="1"' in page.text
    assert 'parameters.set("exclude_github_id", currentRepositoryId)' in initial_page.text
    assert 'name="pushed_bucket"' in page.text
    assert response.status_code == 200
    assert response.json()["github_id"] == 1
    assert response.json()["topics"] == topics
    assert response.headers["cache-control"] == "no-store"
    assert default_response.status_code == 200
    assert default_response.json()["github_id"] == 1
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
    assert "No catalogued repository matches these filters." in empty.json()["detail"]
