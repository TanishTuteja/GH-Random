"""CI-only replacement for app.cli in synthetic scheduler test images.

Never copied into the production Dockerfile; no credentials or GitHub traffic.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from app.core.filters import PUSHED_BUCKETS, STAR_BUCKETS, SUPPORTED_LANGUAGES
from app.core.settings import get_settings
from app.crawler.planning import build_crawl_search_plan
from app.crawler.service import CrawlService
from app.db.session import create_engine_from_url, create_session_factory
from app.github.search import RepositorySearchPage, RepositorySearchSort, SearchOrder


def plan(reference: datetime):
    return build_crawl_search_plan(
        SUPPORTED_LANGUAGES[:1], STAR_BUCKETS[:1], PUSHED_BUCKETS[:1], reference_time=reference
    )


class FakeSearcher:
    def search_repositories(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 100,
        sort: RepositorySearchSort | None = None,
        order: SearchOrder | None = None,
    ) -> RepositorySearchPage:
        mode = os.environ.get("GH_RANDOM_CI_CRAWL_MODE", "success")
        if mode == "hang":
            print(
                "CI crawler owns its database session; waiting for supervisor termination.",
                flush=True,
            )
            time.sleep(600)
        if mode == "fail":
            raise RuntimeError("Deliberate CI crawl failure")
        return RepositorySearchPage(total_count=0, incomplete_results=False, repositories=())


def main() -> int:
    assert sys.argv[1:] == ["crawl"], "Scheduled invocation must be a normal crawl, never --resume"
    engine = create_engine_from_url(get_settings().database_url)
    try:
        result = CrawlService(
            create_session_factory(engine), FakeSearcher(), plan_builder=plan
        ).run_full_crawl()
        print(f"CI crawl completed: {result.generation_id}", flush=True)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
