from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.catalog.random_repository import (
    FilterSelectionError,
    RepositoryFilterSelection,
    find_random_active_repository,
    resolve_filter_selection,
)
from app.db.base import Base
from app.db.models import CatalogGeneration, GenerationRepository, Repository

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine)
    engine.dispose()


def _repository(
    repository_id: int,
    *,
    language: str = "Python",
    stars: int = 50,
    pushed_at: datetime = NOW - timedelta(days=3),
) -> Repository:
    return Repository(
        github_id=repository_id,
        full_name=f"octo/repository-{repository_id}",
        name=f"repository-{repository_id}",
        owner_login="octo",
        html_url=f"https://github.com/octo/repository-{repository_id}",
        description=None,
        language=language,
        stargazer_count=stars,
        fork_count=1,
        open_issues_count=2,
        is_archived=False,
        is_fork=False,
        github_created_at=NOW - timedelta(days=600),
        github_updated_at=NOW - timedelta(days=1),
        github_pushed_at=pushed_at,
        topics=[],
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def _selection(pushed_bucket_id: str) -> RepositoryFilterSelection:
    return resolve_filter_selection(
        language_id="python",
        star_bucket_id="0-100",
        pushed_bucket_id=pushed_bucket_id,
    )


def test_selects_only_active_repositories_matching_every_filter(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, session.begin():
        active = CatalogGeneration(status="active", created_at=NOW, activated_at=NOW)
        retired = CatalogGeneration(status="retired", created_at=NOW, activated_at=NOW)
        session.add_all([active, retired])
        session.flush()
        session.add_all(
            [
                _repository(1),
                _repository(2, language="Rust"),
                _repository(3, stars=101),
                _repository(4, pushed_at=NOW - timedelta(days=8)),
                _repository(5),
            ]
        )
        session.add_all(
            [
                GenerationRepository(
                    generation_id=active.id,
                    github_id=1,
                    last_seen_at=NOW,
                ),
                GenerationRepository(
                    generation_id=active.id,
                    github_id=2,
                    last_seen_at=NOW,
                ),
                GenerationRepository(
                    generation_id=active.id,
                    github_id=3,
                    last_seen_at=NOW,
                ),
                GenerationRepository(
                    generation_id=active.id,
                    github_id=4,
                    last_seen_at=NOW,
                ),
                GenerationRepository(
                    generation_id=retired.id,
                    github_id=5,
                    last_seen_at=NOW,
                ),
            ]
        )

    with session_factory() as session:
        repository = find_random_active_repository(
            session,
            _selection("pushed-within-week"),
            now=NOW,
        )

    assert repository is not None
    assert repository.github_id == 1


def test_pushed_time_boundaries_are_inclusive_at_the_newer_end(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, session.begin():
        active = CatalogGeneration(status="active", created_at=NOW, activated_at=NOW)
        session.add(active)
        session.flush()
        session.add_all(
            [
                _repository(1, pushed_at=NOW - timedelta(days=7)),
                _repository(2, pushed_at=NOW - timedelta(days=7, seconds=1)),
            ]
        )
        session.add_all(
            [
                GenerationRepository(
                    generation_id=active.id,
                    github_id=1,
                    last_seen_at=NOW,
                ),
                GenerationRepository(
                    generation_id=active.id,
                    github_id=2,
                    last_seen_at=NOW,
                ),
            ]
        )

    with session_factory() as session:
        within_week = find_random_active_repository(
            session,
            _selection("pushed-within-week"),
            now=NOW,
        )
        one_week_to_month = find_random_active_repository(
            session,
            _selection("pushed-1-week-to-1-month"),
            now=NOW,
        )

    assert within_week is not None
    assert within_week.github_id == 1
    assert one_week_to_month is not None
    assert one_week_to_month.github_id == 2


def test_rejects_unknown_filter_identifiers() -> None:
    with pytest.raises(FilterSelectionError, match="Unsupported language"):
        resolve_filter_selection(
            language_id="kotlin",
            star_bucket_id="0-100",
            pushed_bucket_id="pushed-within-week",
        )
