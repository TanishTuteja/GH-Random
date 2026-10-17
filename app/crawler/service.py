"""Offline full-crawl orchestration with active/staging publication."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
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
    fingerprint_crawl_search_plan,
)
from app.db.models import CatalogGeneration, CrawlRun, GenerationRepository, Repository
from app.db.operations import exclusive_catalog_operation
from app.github.search import RepositorySearchPage, RepositorySearchSort, SearchOrder

DEFAULT_STALE_MEMBERSHIP_RETENTION = timedelta(days=3)
DEFAULT_GENERATION_RETENTION = timedelta(days=7)


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


@dataclass(frozen=True)
class CrawlContinuation:
    """Persisted position required to continue one exact crawl plan."""

    run_id: int
    generation_id: int
    ordering: SearchOrdering
    plan_reference_at: datetime
    plan_fingerprint: str
    next_work_item_index: int
    next_page: int
    current_work_item_page_count: int | None


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
        stale_membership_retention: timedelta = DEFAULT_STALE_MEMBERSHIP_RETENTION,
        generation_retention: timedelta = DEFAULT_GENERATION_RETENTION,
    ) -> None:
        if stale_membership_retention <= timedelta():
            raise ValueError("Catalog membership retention must be positive.")
        if generation_retention <= timedelta():
            raise ValueError("Catalog generation retention must be positive.")

        self._session_factory = session_factory
        self._searcher = searcher
        self._clock = clock
        self._plan_builder = plan_builder or self._default_plan
        self._logger = logger or logging.getLogger(__name__)
        self._stale_membership_retention = stale_membership_retention
        self._generation_retention = generation_retention

    def run_full_crawl(self) -> CrawlSummary:
        """Run all configured queries and publish only if every request succeeds."""

        return self._run_exclusively(resume=False)

    def resume_full_crawl(self) -> CrawlSummary:
        """Continue the latest failed/interrupted crawl under exclusive ownership."""

        return self._run_exclusively(resume=True)

    def _run_exclusively(self, *, resume: bool) -> CrawlSummary:
        engine = self._session_factory.kw.get("bind")
        if not isinstance(engine, Engine):
            raise ValueError("CrawlService requires an engine-bound session factory")
        with engine.connect() as connection, exclusive_catalog_operation(connection):
            # All reads, checkpoints, publication, recovery, and failure recording
            # use the physical DB connection that owns the lock. Do not mutate the
            # public service's factory: simultaneous callers must contend normally.
            worker = CrawlService(
                sessionmaker(bind=connection, autoflush=False, expire_on_commit=False),
                self._searcher,
                clock=self._clock,
                plan_builder=self._plan_builder,
                logger=self._logger,
                stale_membership_retention=self._stale_membership_retention,
                generation_retention=self._generation_retention,
            )
            worker._recover_abandoned_crawls()
            return worker._resume_full_crawl() if resume else worker._run_full_crawl()

    def _recover_abandoned_crawls(self) -> None:
        """Exclusive ownership proves no cooperating previous crawler is alive."""

        recovered_ids: list[int] = []
        with self._session_factory() as session, session.begin():
            abandoned = session.scalars(select(CrawlRun).where(CrawlRun.status == "running")).all()
            for run in abandoned:
                generation = session.get(CatalogGeneration, run.generation_id)
                if generation is None or generation.status != "staging":
                    raise RuntimeError(
                        "Running crawl has inconsistent generation state; inspect it"
                    )
                run.status = "failed"
                run.completed_at = self._current_time()
                run.error_message = "Previous crawler lost ownership before completion."
                generation.status = "failed"
                recovered_ids.append(run.id)
        for run_id in recovered_ids:
            self._logger.warning("Recovered abandoned crawl run %s", run_id)

    def _run_full_crawl(self) -> CrawlSummary:
        """Execute a new plan after ownership and recovery are established."""

        started_at = self._current_time()
        work_items = self._plan_builder(started_at)
        continuation = self._begin_crawl(started_at, work_items)
        return self._continue_crawl(continuation, work_items, seen_repository_ids=set())

    def _resume_full_crawl(self) -> CrawlSummary:
        """Continue the latest failed crawl from its persisted next-page cursor."""

        continuation = self._load_resumable_crawl()
        work_items = self._plan_builder(continuation.plan_reference_at)
        if fingerprint_crawl_search_plan(work_items) != continuation.plan_fingerprint:
            raise RuntimeError(
                "The configured crawl plan differs from the failed crawl and cannot be resumed."
            )

        self._mark_crawl_running(continuation)
        seen_repository_ids = self._seen_repository_ids(
            continuation.generation_id,
            since=continuation.plan_reference_at,
        )
        return self._continue_crawl(continuation, work_items, seen_repository_ids)

    def _continue_crawl(
        self,
        continuation: CrawlContinuation,
        work_items: tuple[CrawlSearchWorkItem, ...],
        seen_repository_ids: set[int],
    ) -> CrawlSummary:
        """Fetch pages from a persisted cursor and publish only after completion."""

        if continuation.next_work_item_index > len(work_items):
            raise RuntimeError("The crawl checkpoint is beyond the configured plan.")

        try:
            for work_item_index in range(continuation.next_work_item_index, len(work_items)):
                work_item = work_items[work_item_index]
                if work_item_index == continuation.next_work_item_index:
                    page_number = continuation.next_page
                    page_count = continuation.current_work_item_page_count
                else:
                    page_number = 1
                    page_count = None

                if page_number == 1:
                    first_page = self._search(work_item, continuation.ordering, page=1)
                    page_count = calculate_search_page_count(first_page.total_count)
                    self._ingest_page(
                        continuation.run_id,
                        continuation.generation_id,
                        first_page,
                        seen_repository_ids,
                        next_work_item_index=(
                            work_item_index + 1 if page_count <= 1 else work_item_index
                        ),
                        next_page=1 if page_count <= 1 else 2,
                        current_work_item_page_count=None if page_count <= 1 else page_count,
                        seen_at=self._current_time(),
                    )
                    page_number = 2

                if page_count is None:
                    raise RuntimeError("The crawl checkpoint is missing the current page count.")

                for page_number in range(page_number, page_count + 1):
                    page = self._search(work_item, continuation.ordering, page=page_number)
                    self._ingest_page(
                        continuation.run_id,
                        continuation.generation_id,
                        page,
                        seen_repository_ids,
                        next_work_item_index=(
                            work_item_index + 1 if page_number == page_count else work_item_index
                        ),
                        next_page=1 if page_number == page_count else page_number + 1,
                        current_work_item_page_count=(
                            None if page_number == page_count else page_count
                        ),
                        seen_at=self._current_time(),
                    )

                self._logger.info(
                    "Crawl progress: %s / %s combinations; %s unique repositories in staging",
                    work_item_index + 1,
                    len(work_items),
                    len(seen_repository_ids),
                )

            return self._publish_crawl(
                continuation.run_id,
                continuation.generation_id,
                continuation.ordering,
                seen_repository_ids,
                completed_at=self._current_time(),
            )
        except Exception as error:
            try:
                self._fail_crawl(
                    continuation.run_id,
                    continuation.generation_id,
                    error,
                    failed_at=self._current_time(),
                )
            except Exception:
                self._logger.error(
                    "Could not record crawl failure; the next exclusive owner will recover it"
                )
            raise

    def _default_plan(self, reference_time: datetime) -> tuple[CrawlSearchWorkItem, ...]:
        return build_crawl_search_plan(
            SUPPORTED_LANGUAGES,
            STAR_BUCKETS,
            PUSHED_BUCKETS,
            reference_time=reference_time,
        )

    def _begin_crawl(
        self,
        started_at: datetime,
        work_items: tuple[CrawlSearchWorkItem, ...],
    ) -> CrawlContinuation:
        with self._session_factory() as session, session.begin():
            running_crawl = session.scalar(
                select(CrawlRun).where(CrawlRun.status == "running").limit(1)
            )
            if running_crawl is not None:
                raise RuntimeError("A crawl is already running.")

            ordering = self._next_ordering(session)
            plan_fingerprint = fingerprint_crawl_search_plan(work_items)
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
                plan_reference_at=started_at,
                plan_fingerprint=plan_fingerprint,
                next_work_item_index=0,
                next_page=1,
                current_work_item_page_count=None,
                resume_count=0,
            )
            session.add(crawl_run)
            session.flush()
            return CrawlContinuation(
                run_id=crawl_run.id,
                generation_id=generation.id,
                ordering=ordering,
                plan_reference_at=started_at,
                plan_fingerprint=plan_fingerprint,
                next_work_item_index=crawl_run.next_work_item_index,
                next_page=crawl_run.next_page,
                current_work_item_page_count=crawl_run.current_work_item_page_count,
            )

    def _load_resumable_crawl(self) -> CrawlContinuation:
        with self._session_factory() as session:
            running_crawl = session.scalar(
                select(CrawlRun).where(CrawlRun.status == "running").limit(1)
            )
            if running_crawl is not None:
                raise RuntimeError("A crawl is already running.")

            crawl_run = session.scalar(
                select(CrawlRun)
                .where(CrawlRun.status == "failed")
                .order_by(CrawlRun.id.desc())
                .limit(1)
            )
            if crawl_run is None:
                raise RuntimeError("There is no failed crawl available to resume.")
            if crawl_run.plan_reference_at is None or crawl_run.plan_fingerprint is None:
                raise RuntimeError(
                    "This crawl failed before durable checkpoints were introduced and cannot "
                    "be resumed exactly. Start a new crawl."
                )

            generation = session.get(CatalogGeneration, crawl_run.generation_id)
            if generation is None or generation.status != "failed":
                raise RuntimeError("The failed crawl does not have a resumable staging generation.")

            return CrawlContinuation(
                run_id=crawl_run.id,
                generation_id=crawl_run.generation_id,
                ordering=self._ordering_from_run(crawl_run),
                plan_reference_at=crawl_run.plan_reference_at,
                plan_fingerprint=crawl_run.plan_fingerprint,
                next_work_item_index=crawl_run.next_work_item_index,
                next_page=crawl_run.next_page,
                current_work_item_page_count=crawl_run.current_work_item_page_count,
            )

    def _mark_crawl_running(self, continuation: CrawlContinuation) -> None:
        with self._session_factory() as session, session.begin():
            running_crawl = session.scalar(
                select(CrawlRun).where(CrawlRun.status == "running").limit(1)
            )
            if running_crawl is not None:
                raise RuntimeError("A crawl is already running.")

            crawl_run = session.get(CrawlRun, continuation.run_id)
            generation = session.get(CatalogGeneration, continuation.generation_id)
            if crawl_run is None or generation is None:
                raise RuntimeError("The failed crawl no longer exists.")
            if crawl_run.status != "failed" or generation.status != "failed":
                raise RuntimeError("The failed crawl is no longer resumable.")

            crawl_run.status = "running"
            crawl_run.completed_at = None
            crawl_run.error_message = None
            crawl_run.resume_count += 1
            generation.status = "staging"

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

    @staticmethod
    def _ordering_from_run(crawl_run: CrawlRun) -> SearchOrdering:
        return SearchOrdering(
            id=f"{crawl_run.sort_key}-{crawl_run.sort_order}",
            sort=cast(RepositorySearchSort, crawl_run.sort_key),
            order=cast(SearchOrder, crawl_run.sort_order),
        )

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
        next_work_item_index: int,
        next_page: int,
        current_work_item_page_count: int | None,
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
            crawl_run.next_work_item_index = next_work_item_index
            crawl_run.next_page = next_page
            crawl_run.current_work_item_page_count = current_work_item_page_count

    def _publish_crawl(
        self,
        run_id: int,
        generation_id: int,
        ordering: SearchOrdering,
        seen_repository_ids: set[int],
        *,
        completed_at: datetime,
    ) -> CrawlSummary:
        membership_cutoff = completed_at - self._stale_membership_retention
        generation_cutoff = completed_at - self._generation_retention

        with self._session_factory() as session, session.begin():
            crawl_run = session.get(CrawlRun, run_id)
            generation = session.get(CatalogGeneration, generation_id)
            if crawl_run is None or generation is None:
                raise RuntimeError("The completed crawl state no longer exists.")

            session.execute(
                delete(GenerationRepository).where(
                    GenerationRepository.generation_id == generation_id,
                    GenerationRepository.last_seen_at < membership_cutoff,
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

            self._prune_expired_catalog_history(session, cutoff=generation_cutoff)

            return CrawlSummary(
                run_id=run_id,
                generation_id=generation_id,
                ordering=ordering,
                requests_made=crawl_run.requests_made,
                repositories_seen=crawl_run.repositories_seen,
            )

    @staticmethod
    def _prune_expired_catalog_history(session: Session, *, cutoff: datetime) -> None:
        """Remove expired non-live snapshots, then metadata no remaining snapshot references."""

        session.execute(
            delete(CatalogGeneration).where(
                CatalogGeneration.status.in_(("retired", "failed")),
                CatalogGeneration.created_at < cutoff,
            )
        )
        session.execute(
            delete(Repository).where(
                ~select(GenerationRepository.github_id)
                .where(GenerationRepository.github_id == Repository.github_id)
                .exists()
            )
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

    def _seen_repository_ids(self, generation_id: int, *, since: datetime) -> set[int]:
        with self._session_factory() as session:
            return set(
                session.scalars(
                    select(GenerationRepository.github_id).where(
                        GenerationRepository.generation_id == generation_id,
                        GenerationRepository.last_seen_at >= since,
                    )
                )
            )

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
