"""Offline full-crawl orchestration with active/staging publication."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.filters import PUSHED_BUCKETS, STAR_BUCKETS, SUPPORTED_LANGUAGES
from app.crawler.mapper import map_repository_payload
from app.crawler.planning import (
    CRAWL_SORT_CYCLE,
    SEARCH_PAGE_SIZE,
    CrawlSearchWorkItem,
    SearchOrdering,
    build_crawl_search_plan,
    calculate_search_page_count,
)
from app.db.models import CatalogGeneration, CrawlRun, GenerationRepository, Repository
from app.github.search import RepositorySearchPage, RepositorySearchSort, SearchOrder

STALE_MEMBERSHIP_RETENTION = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RepositorySearcher(Protocol):
    """The repository-search operation needed by a full crawl."""

    def search_repositories(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 100,
        sort: RepositorySearchSort | None = None,
        order: SearchOrder | None = None,
    ) -> RepositorySearchPage: ...


@dataclass(frozen=True)
class CrawlSummary:
    """The observable result of a completed full crawl."""

    run_id: int
    generation_id: int
    ordering: SearchOrdering
    requests_made: int
    repositories_seen: int


class CrawlService:
    """Build and atomically publish complete repository catalog generations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        searcher: RepositorySearcher,
        *,
        clock: Callable[[], datetime] = _utc_now,
        plan_builder: Callable[[datetime], tuple[CrawlSearchWorkItem, ...]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._searcher = searcher
        self._clock = clock
        self._plan_builder = plan_builder or self._default_plan
        self._logger = logger or logging.getLogger(__name__)

    def run_full_crawl(self) -> CrawlSummary:
        """Run all configured queries and publish only if every request succeeds."""

        started_at = self._current_time()
        ordering, run_id, generation_id = self._begin_crawl(started_at)
        seen_repository_ids: set[int] = set()

        try:
            work_items = self._plan_builder(started_at)
            for position, work_item in enumerate(work_items, start=1):
                first_page = self._search(work_item, ordering, page=1)
                self._ingest_page(
                    run_id,
                    generation_id,
                    first_page,
                    seen_repository_ids,
                    seen_at=self._current_time(),
                )

                page_count = calculate_search_page_count(first_page.total_count)
                for page_number in range(2, page_count + 1):
                    page = self._search(work_item, ordering, page=page_number)
                    self._ingest_page(
                        run_id,
                        generation_id,
                        page,
                        seen_repository_ids,
                        seen_at=self._current_time(),
                    )

                self._logger.info(
                    "Crawl progress: %s / %s combinations; %s unique repositories in staging",
                    position,
                    len(work_items),
                    len(seen_repository_ids),
                )

            return self._publish_crawl(
                run_id,
                generation_id,
                ordering,
                seen_repository_ids,
                completed_at=self._current_time(),
            )
        except Exception as error:
            self._fail_crawl(run_id, generation_id, error, failed_at=self._current_time())
            raise

    def _default_plan(self, reference_time: datetime) -> tuple[CrawlSearchWorkItem, ...]:
        return build_crawl_search_plan(
            SUPPORTED_LANGUAGES,
            STAR_BUCKETS,
            PUSHED_BUCKETS,
            reference_time=reference_time,
        )

    def _begin_crawl(self, started_at: datetime) -> tuple[SearchOrdering, int, int]:
        with self._session_factory() as session, session.begin():
            running_crawl = session.scalar(
                select(CrawlRun).where(CrawlRun.status == "running").limit(1)
            )
            if running_crawl is not None:
                raise RuntimeError("A crawl is already running.")

            ordering = self._next_ordering(session)
            generation = CatalogGeneration(
                status="staging",
                created_at=started_at,
                activated_at=None,
            )
            session.add(generation)
            session.flush()

            active_generation = session.scalar(
                select(CatalogGeneration).where(CatalogGeneration.status == "active").limit(1)
            )
            if active_generation is not None:
                active_memberships = session.scalars(
                    select(GenerationRepository).where(
                        GenerationRepository.generation_id == active_generation.id
                    )
                )
                session.add_all(
                    GenerationRepository(
                        generation_id=generation.id,
                        github_id=membership.github_id,
                        last_seen_at=membership.last_seen_at,
                    )
                    for membership in active_memberships
                )

            crawl_run = CrawlRun(
                generation_id=generation.id,
                status="running",
                sort_key=ordering.sort,
                sort_order=ordering.order,
                started_at=started_at,
                completed_at=None,
                requests_made=0,
                repositories_seen=0,
                error_message=None,
            )
            session.add(crawl_run)
            session.flush()
            return ordering, crawl_run.id, generation.id

    def _next_ordering(self, session: Session) -> SearchOrdering:
        previous_run = session.scalar(
            select(CrawlRun)
            .where(CrawlRun.status == "completed")
            .order_by(CrawlRun.completed_at.desc())
            .limit(1)
        )
        if previous_run is None:
            return CRAWL_SORT_CYCLE[0]

        for index, ordering in enumerate(CRAWL_SORT_CYCLE):
            if ordering.sort == previous_run.sort_key and ordering.order == previous_run.sort_order:
                return CRAWL_SORT_CYCLE[(index + 1) % len(CRAWL_SORT_CYCLE)]

        return CRAWL_SORT_CYCLE[0]

    def _search(
        self,
        work_item: CrawlSearchWorkItem,
        ordering: SearchOrdering,
        *,
        page: int,
    ) -> RepositorySearchPage:
        return self._searcher.search_repositories(
            work_item.query,
            page=page,
            per_page=SEARCH_PAGE_SIZE,
            sort=ordering.sort,
            order=ordering.order,
        )

    def _ingest_page(
        self,
        run_id: int,
        generation_id: int,
        search_page: RepositorySearchPage,
        seen_repository_ids: set[int],
        *,
        seen_at: datetime,
    ) -> None:
        incoming_repositories = [
            map_repository_payload(repository, crawled_at=seen_at)
            for repository in search_page.repositories
        ]
        github_ids = [repository.github_id for repository in incoming_repositories]

        with self._session_factory() as session, session.begin():
            crawl_run = session.get(CrawlRun, run_id)
            if crawl_run is None:
                raise RuntimeError("The current crawl run no longer exists.")

            existing_repositories = self._repositories_by_id(session, github_ids)
            existing_memberships = self._memberships_by_id(session, generation_id, github_ids)

            for incoming in incoming_repositories:
                existing = existing_repositories.get(incoming.github_id)
                if existing is None:
                    session.add(incoming)
                else:
                    self._update_repository_metadata(existing, incoming)

            session.flush()

            for incoming in incoming_repositories:
                membership = existing_memberships.get(incoming.github_id)
                if membership is None:
                    session.add(
                        GenerationRepository(
                            generation_id=generation_id,
                            github_id=incoming.github_id,
                            last_seen_at=seen_at,
                        )
                    )
                else:
                    membership.last_seen_at = seen_at

                seen_repository_ids.add(incoming.github_id)

            crawl_run.requests_made += 1
            crawl_run.repositories_seen = len(seen_repository_ids)

    def _publish_crawl(
        self,
        run_id: int,
        generation_id: int,
        ordering: SearchOrdering,
        seen_repository_ids: set[int],
        *,
        completed_at: datetime,
    ) -> CrawlSummary:
        cutoff = completed_at - STALE_MEMBERSHIP_RETENTION

        with self._session_factory() as session, session.begin():
            crawl_run = session.get(CrawlRun, run_id)
            generation = session.get(CatalogGeneration, generation_id)
            if crawl_run is None or generation is None:
                raise RuntimeError("The completed crawl state no longer exists.")

            session.execute(
                delete(GenerationRepository).where(
                    GenerationRepository.generation_id == generation_id,
                    GenerationRepository.last_seen_at < cutoff,
                )
            )

            active_generation = session.scalar(
                select(CatalogGeneration).where(CatalogGeneration.status == "active").limit(1)
            )
            if active_generation is not None:
                active_generation.status = "retired"
                session.flush()

            generation.status = "active"
            generation.activated_at = completed_at
            crawl_run.status = "completed"
            crawl_run.completed_at = completed_at
            crawl_run.repositories_seen = len(seen_repository_ids)
            session.flush()

            return CrawlSummary(
                run_id=run_id,
                generation_id=generation_id,
                ordering=ordering,
                requests_made=crawl_run.requests_made,
                repositories_seen=crawl_run.repositories_seen,
            )

    def _fail_crawl(
        self,
        run_id: int,
        generation_id: int,
        error: BaseException,
        *,
        failed_at: datetime,
    ) -> None:
        with self._session_factory() as session, session.begin():
            crawl_run = session.get(CrawlRun, run_id)
            generation = session.get(CatalogGeneration, generation_id)
            if crawl_run is None or generation is None:
                return

            crawl_run.status = "failed"
            crawl_run.completed_at = failed_at
            crawl_run.error_message = self._safe_error_message(error)
            generation.status = "failed"

    def _repositories_by_id(
        self,
        session: Session,
        github_ids: Iterable[int],
    ) -> dict[int, Repository]:
        ids = tuple(github_ids)
        if not ids:
            return {}

        return {
            repository.github_id: repository
            for repository in session.scalars(
                select(Repository).where(Repository.github_id.in_(ids))
            )
        }

    def _memberships_by_id(
        self,
        session: Session,
        generation_id: int,
        github_ids: Iterable[int],
    ) -> dict[int, GenerationRepository]:
        ids = tuple(github_ids)
        if not ids:
            return {}

        return {
            membership.github_id: membership
            for membership in session.scalars(
                select(GenerationRepository).where(
                    GenerationRepository.generation_id == generation_id,
                    GenerationRepository.github_id.in_(ids),
                )
            )
        }

    @staticmethod
    def _update_repository_metadata(existing: Repository, incoming: Repository) -> None:
        existing.full_name = incoming.full_name
        existing.name = incoming.name
        existing.owner_login = incoming.owner_login
        existing.html_url = incoming.html_url
        existing.description = incoming.description
        existing.language = incoming.language
        existing.stargazer_count = incoming.stargazer_count
        existing.fork_count = incoming.fork_count
        existing.open_issues_count = incoming.open_issues_count
        existing.is_archived = incoming.is_archived
        existing.is_fork = incoming.is_fork
        existing.github_created_at = incoming.github_created_at
        existing.github_updated_at = incoming.github_updated_at
        existing.github_pushed_at = incoming.github_pushed_at
        existing.topics = incoming.topics
        existing.last_seen_at = incoming.last_seen_at

    def _current_time(self) -> datetime:
        current_time = self._clock()
        if current_time.tzinfo is None:
            raise ValueError("The crawl clock must return an aware datetime.")
        return current_time.astimezone(timezone.utc)

    @staticmethod
    def _safe_error_message(error: BaseException) -> str:
        message = str(error).strip() or error.__class__.__name__
        return message[:500]
