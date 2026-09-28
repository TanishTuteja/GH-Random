from datetime import datetime, timezone

import pytest

from app.core.filters import PUSHED_BUCKETS, STAR_BUCKETS, SUPPORTED_LANGUAGES
from app.crawler.mapper import GitHubRepositoryPayloadError, map_repository_payload
from app.crawler.planning import (
    CRAWL_SORT_CYCLE,
    build_crawl_search_plan,
    build_star_qualifier,
    calculate_search_page_count,
)

REFERENCE_TIME = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def test_crawl_plan_covers_all_two_hundred_filter_combinations() -> None:
    plan = build_crawl_search_plan(
        SUPPORTED_LANGUAGES,
        STAR_BUCKETS,
        PUSHED_BUCKETS,
        reference_time=REFERENCE_TIME,
    )

    assert len(plan) == 200
    assert len({item.query for item in plan}) == len(plan)
    assert any(
        item.language.id == "python"
        and item.star_bucket.id == "0-100"
        and item.pushed_bucket.id == "pushed-over-1-year"
        and item.query
        == "language:Python stars:0..100 pushed:<2025-09-29 archived:false fork:false"
        for item in plan
    )


def test_sort_cycle_contains_the_four_configured_orderings() -> None:
    assert [(item.sort, item.order) for item in CRAWL_SORT_CYCLE] == [
        ("updated", "desc"),
        ("stars", "desc"),
        ("updated", "asc"),
        ("stars", "asc"),
    ]


def test_page_count_is_capped_at_ten_pages() -> None:
    assert calculate_search_page_count(0) == 0
    assert calculate_search_page_count(100) == 1
    assert calculate_search_page_count(101) == 2
    assert calculate_search_page_count(100_000) == 10


def test_open_ended_star_bucket_uses_greater_than_or_equal_qualifier() -> None:
    assert build_star_qualifier(STAR_BUCKETS[-1]) == "stars:>=100001"


def test_maps_github_search_result_to_unsaved_repository() -> None:
    repository = map_repository_payload(
        {
            "id": 123,
            "full_name": "octo/example",
            "name": "example",
            "owner": {"login": "octo"},
            "html_url": "https://github.com/octo/example",
            "description": "An example repository.",
            "language": "Python",
            "stargazers_count": 42,
            "forks_count": 3,
            "open_issues_count": 4,
            "archived": False,
            "fork": False,
            "created_at": "2025-01-02T03:04:05Z",
            "updated_at": "2026-09-28T10:00:00Z",
            "pushed_at": "2026-09-27T10:00:00Z",
            "topics": ["example"],
        },
        crawled_at=REFERENCE_TIME,
    )

    assert repository.github_id == 123
    assert repository.full_name == "octo/example"
    assert repository.owner_login == "octo"
    assert repository.open_issues_count == 4
    assert repository.github_pushed_at == datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    assert repository.topics == ["example"]


def test_mapper_rejects_payload_without_owner_login() -> None:
    with pytest.raises(GitHubRepositoryPayloadError, match="login"):
        map_repository_payload(
            {
                "id": 123,
                "full_name": "octo/example",
                "name": "example",
                "owner": {},
                "html_url": "https://github.com/octo/example",
                "stargazers_count": 42,
                "forks_count": 3,
                "open_issues_count": 4,
                "archived": False,
                "fork": False,
                "created_at": "2025-01-02T03:04:05Z",
                "updated_at": "2026-09-28T10:00:00Z",
            },
            crawled_at=REFERENCE_TIME,
        )
