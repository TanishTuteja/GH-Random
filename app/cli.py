"""Manual operational commands for GH Random."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.settings import get_github_app_settings, get_settings
from app.crawler.service import CrawlService, CrawlSummary
from app.db.models import CatalogGeneration, CrawlRun
from app.db.session import create_engine_from_url, create_session_factory
from app.github.app_auth import GitHubAppClient
from app.github.search import GitHubSearchClient

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogStatus:
    """A local operator's view of the active catalog and latest crawl."""

    active_generation_id: int | None
    active_generation_activated_at: datetime | None
    latest_run_id: int | None
    latest_run_status: str | None
    latest_run_started_at: datetime | None
    latest_run_completed_at: datetime | None
    latest_run_requests_made: int | None
    latest_run_repositories_seen: int | None
    latest_run_resume_count: int | None


def build_parser() -> argparse.ArgumentParser:
    """Build the small, explicit command-line interface."""

    parser = argparse.ArgumentParser(
        prog="gh-random",
        description="Operate the local GH Random catalog.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    crawl_parser = subcommands.add_parser(
        "crawl",
        help="Run one complete, paced GitHub Search crawl and publish it on success.",
    )
    crawl_parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue the latest failed crawl from its durable page checkpoint.",
    )
    subcommands.add_parser(
        "status",
        help="Show local catalog and latest-crawl status without a public web endpoint.",
    )
    return parser


def run_crawl(*, resume: bool = False) -> CrawlSummary:
    """Run the complete crawler using configuration from the environment."""

    settings = get_settings()
    github_app_settings = get_github_app_settings()
    engine = create_engine_from_url(settings.database_url)
    app_client = GitHubAppClient(github_app_settings)
    search_client = GitHubSearchClient(
        app_client,
        request_delay_seconds=settings.github_search_delay_seconds,
        rate_limit_retry_seconds=settings.github_rate_limit_retry_seconds,
        rate_limit_max_retries=settings.github_rate_limit_max_retries,
        transport_retry_seconds=settings.github_transport_retry_seconds,
        transport_max_retries=settings.github_transport_max_retries,
    )

    try:
        service = CrawlService(
            create_session_factory(engine),
            search_client,
            stale_membership_retention=timedelta(days=settings.catalog_membership_retention_days),
            generation_retention=timedelta(days=settings.catalog_generation_retention_days),
        )
        return service.resume_full_crawl() if resume else service.run_full_crawl()
    finally:
        search_client.close()
        app_client.close()
        engine.dispose()


def get_catalog_status() -> CatalogStatus:
    """Read local crawl progress without importing or contacting GitHub."""

    settings = get_settings()
    engine = create_engine_from_url(settings.database_url)

    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            active_generation = session.scalar(
                select(CatalogGeneration).where(CatalogGeneration.status == "active").limit(1)
            )
            latest_run = session.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1))

            return CatalogStatus(
                active_generation_id=(
                    active_generation.id if active_generation is not None else None
                ),
                active_generation_activated_at=(
                    active_generation.activated_at if active_generation is not None else None
                ),
                latest_run_id=latest_run.id if latest_run is not None else None,
                latest_run_status=latest_run.status if latest_run is not None else None,
                latest_run_started_at=latest_run.started_at if latest_run is not None else None,
                latest_run_completed_at=latest_run.completed_at if latest_run is not None else None,
                latest_run_requests_made=(
                    latest_run.requests_made if latest_run is not None else None
                ),
                latest_run_repositories_seen=(
                    latest_run.repositories_seen if latest_run is not None else None
                ),
                latest_run_resume_count=latest_run.resume_count if latest_run is not None else None,
            )
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a command and return a shell-friendly exit code."""

    arguments = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if arguments.command == "crawl":
        try:
            summary = run_crawl(resume=True) if arguments.resume else run_crawl()
        except Exception:
            LOGGER.exception(
                "Full catalog crawl failed; the previous active catalog remains available."
            )
            return 1

        print(
            "Crawl complete: "
            f"generation={summary.generation_id}, "
            f"requests={summary.requests_made}, "
            f"repositories={summary.repositories_seen}, "
            f"sort={summary.ordering.sort} {summary.ordering.order}"
        )
        return 0

    if arguments.command == "status":
        status = get_catalog_status()
        active_generation = (
            str(status.active_generation_id) if status.active_generation_id is not None else "none"
        )
        latest_run = str(status.latest_run_id) if status.latest_run_id is not None else "none"
        run_status = status.latest_run_status or "none"
        requests_made = (
            str(status.latest_run_requests_made)
            if status.latest_run_requests_made is not None
            else "none"
        )
        repositories_seen = (
            str(status.latest_run_repositories_seen)
            if status.latest_run_repositories_seen is not None
            else "none"
        )
        resume_count = (
            str(status.latest_run_resume_count)
            if status.latest_run_resume_count is not None
            else "none"
        )
        active_at = _format_datetime(status.active_generation_activated_at)
        started_at = _format_datetime(status.latest_run_started_at)
        completed_at = _format_datetime(status.latest_run_completed_at)
        print(
            "Catalog status: "
            f"active_generation={active_generation}, "
            f"active_at={active_at}, "
            f"latest_run={latest_run}, "
            f"run_status={run_status}, "
            f"started_at={started_at}, "
            f"completed_at={completed_at}, "
            f"resumes={resume_count}, "
            f"requests={requests_made}, "
            f"repositories={repositories_seen}"
        )
        return 0

    raise AssertionError(f"Unhandled command: {arguments.command!r}")


def _format_datetime(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "none"


if __name__ == "__main__":
    raise SystemExit(main())
