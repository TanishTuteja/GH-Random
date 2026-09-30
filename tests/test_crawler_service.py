from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.filters import PUSHED_BUCKETS, STAR_BUCKETS, SUPPORTED_LANGUAGES
from app.crawler.planning import CrawlSearchWorkItem
from app.crawler.service import CrawlService
from app.db.models import CatalogGeneration, CrawlRun, GenerationRepository, Repository
from app.github.search import RepositorySearchPage, RepositorySearchSort, SearchOrder

STARTED_AT = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
WORK_ITEM = CrawlSearchWorkItem(
    language=SUPPORTED_LANGUAGES[0],
    star_bucket=STAR_BUCKETS[0],
    pushed_bucket=PUSHED_BUCKETS[0],
    query="language:Python stars:0..100 pushed:>=2026-09-22 archived:false fork:false",
)


class FakeRepositorySearcher:
    def __init__(self, responses: list[RepositorySearchPage | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, int, int, RepositorySearchSort | None, SearchOrder | None]] = []

    def search_repositories(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 100,
        sort: RepositorySearchSort | None = None,
        order: SearchOrder | None = None,
    ) -> RepositorySearchPage:
        self.calls.append((query, page, per_page, sort, order))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _plan(_: datetime) -> tuple[CrawlSearchWorkItem, ...]:
    return (WORK_ITEM,)


def _payload(
    repository_id: int,
    *,
    full_name: str | None = None,
) -> dict[str, object]:
    repository_name = f"repository-{repository_id}"
    return {
        "id": repository_id,
        "full_name": full_name or f"octo/{repository_name}",
        "name": repository_name,
        "owner": {"login": "octo"},
        "html_url": f"https://github.com/octo/repository-{repository_id}",
        "description": "A repository used in a crawler test.",
        "language": "Python",
        "stargazers_count": 42,
        "forks_count": 3,
        "open_issues_count": 4,
        "archived": False,
        "fork": False,
        "created_at": "2025-01-02T03:04:05Z",
        "updated_at": "2026-09-28T10:00:00Z",
        "pushed_at": "2026-09-27T10:00:00Z",
        "topics": ["test"],
    }


def _page(
    *repositories: dict[str, object],
    total_count: int | None = None,
) -> RepositorySearchPage:
    return RepositorySearchPage(
        total_count=total_count if total_count is not None else len(repositories),
        incomplete_results=False,
        repositories=tuple(repositories),
    )


def _service(
    session_factory: sessionmaker[Session],
    searcher: FakeRepositorySearcher,
    current_time: datetime,
) -> CrawlService:
    return CrawlService(
        session_factory,
        searcher,
        clock=lambda: current_time,
        plan_builder=_plan,
    )


def test_successful_crawls_publish_staging_and_rotate_sort_order(
    session_factory: sessionmaker[Session],
) -> None:
    first_searcher = FakeRepositorySearcher([_page(_payload(1))])
    first_summary = _service(session_factory, first_searcher, STARTED_AT).run_full_crawl()

    second_searcher = FakeRepositorySearcher([_page(_payload(2))])
    second_summary = _service(
        session_factory,
        second_searcher,
        STARTED_AT + timedelta(hours=1),
    ).run_full_crawl()

    assert (first_summary.ordering.sort, first_summary.ordering.order) == ("updated", "desc")
    assert (second_summary.ordering.sort, second_summary.ordering.order) == ("stars", "desc")
    assert first_searcher.calls == [
        (WORK_ITEM.query, 1, 100, "updated", "desc"),
    ]
    assert second_searcher.calls == [
        (WORK_ITEM.query, 1, 100, "stars", "desc"),
    ]

    with session_factory() as session:
        generations = list(
            session.scalars(select(CatalogGeneration).order_by(CatalogGeneration.id))
        )
        active_generation = session.scalar(
            select(CatalogGeneration).where(CatalogGeneration.status == "active")
        )
        active_memberships = list(
            session.scalars(
                select(GenerationRepository).where(
                    GenerationRepository.generation_id == second_summary.generation_id
                )
            )
        )
        completed_runs = list(
            session.scalars(
                select(CrawlRun).where(CrawlRun.status == "completed").order_by(CrawlRun.id)
            )
        )

    assert [generation.status for generation in generations] == ["retired", "active"]
    assert active_generation is not None
    assert active_generation.id == second_summary.generation_id
    assert {membership.github_id for membership in active_memberships} == {1, 2}
    assert [(run.sort_key, run.sort_order) for run in completed_runs] == [
        ("updated", "desc"),
        ("stars", "desc"),
    ]


def test_failed_crawl_keeps_prior_active_generation(
    session_factory: sessionmaker[Session],
) -> None:
    initial_summary = _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(1))]),
        STARTED_AT,
    ).run_full_crawl()

    with pytest.raises(RuntimeError, match="GitHub is unavailable"):
        _service(
            session_factory,
            FakeRepositorySearcher([RuntimeError("GitHub is unavailable")]),
            STARTED_AT + timedelta(hours=1),
        ).run_full_crawl()

    with session_factory() as session:
        active_generation = session.scalar(
            select(CatalogGeneration).where(CatalogGeneration.status == "active")
        )
        failed_generation = session.scalar(
            select(CatalogGeneration).where(CatalogGeneration.status == "failed")
        )
        failed_run = session.scalar(select(CrawlRun).where(CrawlRun.status == "failed"))
        active_memberships = list(
            session.scalars(
                select(GenerationRepository).where(
                    GenerationRepository.generation_id == initial_summary.generation_id
                )
            )
        )

    assert active_generation is not None
    assert active_generation.id == initial_summary.generation_id
    assert failed_generation is not None
    assert failed_run is not None
    assert failed_run.error_message == "GitHub is unavailable"
    assert [membership.github_id for membership in active_memberships] == [1]


def test_next_normal_crawl_starts_after_a_failed_crawl(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(RuntimeError, match="GitHub is unavailable"):
        _service(
            session_factory,
            FakeRepositorySearcher([RuntimeError("GitHub is unavailable")]),
            STARTED_AT,
        ).run_full_crawl()

    summary = _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(2))]),
        STARTED_AT + timedelta(days=1),
    ).run_full_crawl()

    with session_factory() as session:
        runs = list(session.scalars(select(CrawlRun).order_by(CrawlRun.id)))
        generations = list(
            session.scalars(select(CatalogGeneration).order_by(CatalogGeneration.id))
        )

    assert summary.run_id == 2
    assert (summary.ordering.sort, summary.ordering.order) == ("updated", "desc")
    assert [run.status for run in runs] == ["failed", "completed"]
    assert [generation.status for generation in generations] == ["failed", "active"]


def test_successful_crawl_prunes_seeded_memberships_not_seen_for_over_three_days(
    session_factory: sessionmaker[Session],
) -> None:
    _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(1))]),
        STARTED_AT,
    ).run_full_crawl()

    summary = _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(2))]),
        STARTED_AT + timedelta(days=3, seconds=1),
    ).run_full_crawl()

    with session_factory() as session:
        active_memberships = list(
            session.scalars(
                select(GenerationRepository)
                .where(GenerationRepository.generation_id == summary.generation_id)
                .order_by(GenerationRepository.github_id)
            )
        )

    assert [membership.github_id for membership in active_memberships] == [2]


def test_successful_crawl_prunes_expired_history_and_orphaned_repositories(
    session_factory: sessionmaker[Session],
) -> None:
    _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(1))]),
        STARTED_AT,
    ).run_full_crawl()

    with pytest.raises(RuntimeError, match="GitHub is unavailable"):
        _service(
            session_factory,
            FakeRepositorySearcher([RuntimeError("GitHub is unavailable")]),
            STARTED_AT + timedelta(hours=1),
        ).run_full_crawl()

    summary = _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(3))]),
        STARTED_AT + timedelta(days=8),
    ).run_full_crawl()

    with session_factory() as session:
        generations = list(
            session.scalars(select(CatalogGeneration).order_by(CatalogGeneration.id))
        )
        runs = list(session.scalars(select(CrawlRun).order_by(CrawlRun.id)))
        memberships = list(session.scalars(select(GenerationRepository)))
        repositories = list(session.scalars(select(Repository).order_by(Repository.github_id)))

    assert [(generation.id, generation.status) for generation in generations] == [
        (summary.generation_id, "active")
    ]
    assert [run.generation_id for run in runs] == [summary.generation_id]
    assert [(membership.generation_id, membership.github_id) for membership in memberships] == [
        (summary.generation_id, 3)
    ]
    assert [repository.github_id for repository in repositories] == [3]


def test_generation_created_at_exactly_at_retention_cutoff_is_kept(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(RuntimeError, match="GitHub is unavailable"):
        _service(
            session_factory,
            FakeRepositorySearcher([RuntimeError("GitHub is unavailable")]),
            STARTED_AT,
        ).run_full_crawl()

    summary = _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(2))]),
        STARTED_AT + timedelta(days=7),
    ).run_full_crawl()

    with session_factory() as session:
        generations = list(
            session.scalars(select(CatalogGeneration).order_by(CatalogGeneration.id))
        )

    assert [(generation.id, generation.status) for generation in generations] == [
        (1, "failed"),
        (summary.generation_id, "active"),
    ]


def test_crawl_fetches_every_useful_page_for_a_combination(
    session_factory: sessionmaker[Session],
) -> None:
    searcher = FakeRepositorySearcher(
        [
            _page(_payload(1), total_count=201),
            _page(_payload(2), total_count=201),
            _page(_payload(3), total_count=201),
        ]
    )

    summary = _service(session_factory, searcher, STARTED_AT).run_full_crawl()

    assert [call[1] for call in searcher.calls] == [1, 2, 3]
    assert summary.requests_made == 3
    assert summary.repositories_seen == 3

    with session_factory() as session:
        repositories = list(session.scalars(select(Repository).order_by(Repository.github_id)))

    assert [repository.github_id for repository in repositories] == [1, 2, 3]


def test_failed_crawl_resumes_from_the_next_persisted_page(
    session_factory: sessionmaker[Session],
) -> None:
    failing_searcher = FakeRepositorySearcher(
        [
            _page(_payload(1), total_count=201),
            RuntimeError("GitHub is unavailable"),
        ]
    )
    service = _service(session_factory, failing_searcher, STARTED_AT)

    with pytest.raises(RuntimeError, match="GitHub is unavailable"):
        service.run_full_crawl()

    with session_factory() as session:
        failed_run = session.scalar(select(CrawlRun).where(CrawlRun.status == "failed"))

    assert failed_run is not None
    assert failed_run.requests_made == 1
    assert failed_run.next_work_item_index == 0
    assert failed_run.next_page == 2
    assert failed_run.current_work_item_page_count == 3
    assert failed_run.plan_fingerprint is not None

    resuming_searcher = FakeRepositorySearcher(
        [
            _page(_payload(2), total_count=201),
            _page(_payload(3), total_count=201),
        ]
    )
    summary = _service(session_factory, resuming_searcher, STARTED_AT).resume_full_crawl()

    assert [call[1] for call in resuming_searcher.calls] == [2, 3]
    assert summary.requests_made == 3
    assert summary.repositories_seen == 3

    with session_factory() as session:
        completed_run = session.get(CrawlRun, summary.run_id)
        active_generation = session.scalar(
            select(CatalogGeneration).where(CatalogGeneration.status == "active")
        )

    assert completed_run is not None
    assert completed_run.resume_count == 1
    assert completed_run.status == "completed"
    assert active_generation is not None
    assert active_generation.id == summary.generation_id


def test_resume_rejects_a_changed_crawl_plan(
    session_factory: sessionmaker[Session],
) -> None:
    service = _service(
        session_factory,
        FakeRepositorySearcher([RuntimeError("GitHub is unavailable")]),
        STARTED_AT,
    )

    with pytest.raises(RuntimeError, match="GitHub is unavailable"):
        service.run_full_crawl()

    changed_work_item = CrawlSearchWorkItem(
        language=SUPPORTED_LANGUAGES[1],
        star_bucket=STAR_BUCKETS[0],
        pushed_bucket=PUSHED_BUCKETS[0],
        query="language:JavaScript stars:0..100 pushed:>=2026-09-22 archived:false fork:false",
    )
    changed_service = CrawlService(
        session_factory,
        FakeRepositorySearcher([]),
        clock=lambda: STARTED_AT,
        plan_builder=lambda _: (changed_work_item,),
    )

    with pytest.raises(RuntimeError, match="configured crawl plan differs"):
        changed_service.resume_full_crawl()

    with session_factory() as session:
        failed_run = session.scalar(select(CrawlRun).where(CrawlRun.status == "failed"))
        failed_generation = session.scalar(
            select(CatalogGeneration).where(CatalogGeneration.status == "failed")
        )

    assert failed_run is not None
    assert failed_generation is not None


def test_resume_rejects_a_pre_checkpoint_failed_crawl(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, session.begin():
        generation = CatalogGeneration(status="failed", created_at=STARTED_AT, activated_at=None)
        session.add(generation)
        session.flush()
        session.add(
            CrawlRun(
                generation_id=generation.id,
                status="failed",
                sort_key="updated",
                sort_order="desc",
                started_at=STARTED_AT,
                completed_at=STARTED_AT,
                requests_made=83,
                repositories_seen=4_634,
                error_message="GitHub is unavailable",
                plan_reference_at=STARTED_AT,
                plan_fingerprint=None,
                next_work_item_index=0,
                next_page=1,
                current_work_item_page_count=None,
                resume_count=0,
            )
        )

    with pytest.raises(RuntimeError, match="before durable checkpoints"):
        _service(session_factory, FakeRepositorySearcher([]), STARTED_AT).resume_full_crawl()


def test_crawl_accepts_a_reused_mutable_repository_full_name(
    session_factory: sessionmaker[Session],
) -> None:
    reused_full_name = "octo/transferred-repository"
    _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(1, full_name=reused_full_name))]),
        STARTED_AT,
    ).run_full_crawl()

    summary = _service(
        session_factory,
        FakeRepositorySearcher([_page(_payload(2, full_name=reused_full_name))]),
        STARTED_AT + timedelta(hours=1),
    ).run_full_crawl()

    with session_factory() as session:
        repositories = list(session.scalars(select(Repository).order_by(Repository.github_id)))
        memberships = list(
            session.scalars(
                select(GenerationRepository).where(
                    GenerationRepository.generation_id == summary.generation_id
                )
            )
        )

    assert [repository.github_id for repository in repositories] == [1, 2]
    assert {repository.full_name for repository in repositories} == {reused_full_name}
    assert {membership.github_id for membership in memberships} == {1, 2}
