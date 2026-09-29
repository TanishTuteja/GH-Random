"""Manual operational commands for GH Random."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from app.core.settings import get_github_app_settings, get_settings
from app.crawler.service import CrawlService, CrawlSummary
from app.db.session import create_engine_from_url, create_session_factory
from app.github.app_auth import GitHubAppClient
from app.github.search import GitHubSearchClient

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the small, explicit command-line interface."""

    parser = argparse.ArgumentParser(
        prog="gh-random",
        description="Operate the local GH Random catalog.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "crawl",
        help="Run one complete, paced GitHub Search crawl and publish it on success.",
    )
    return parser


def run_crawl() -> CrawlSummary:
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
        )
        return service.run_full_crawl()
    finally:
        search_client.close()
        app_client.close()
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a command and return a shell-friendly exit code."""

    arguments = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if arguments.command == "crawl":
        try:
            summary = run_crawl()
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

    raise AssertionError(f"Unhandled command: {arguments.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
