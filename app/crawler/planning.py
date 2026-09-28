"""Pure query planning for full GitHub repository crawls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

from app.core.filters import LanguageOption, PushedBucket, StarBucket
from app.github.search import RepositorySearchSort, SearchOrder

MAX_SEARCH_PAGES = 10
SEARCH_PAGE_SIZE = 100


@dataclass(frozen=True)
class SearchOrdering:
    """A GitHub search ordering used for one complete crawl."""

    id: str
    sort: RepositorySearchSort
    order: SearchOrder


CRAWL_SORT_CYCLE: tuple[SearchOrdering, ...] = (
    SearchOrdering("updated-desc", "updated", "desc"),
    SearchOrdering("stars-desc", "stars", "desc"),
    SearchOrdering("updated-asc", "updated", "asc"),
    SearchOrdering("stars-asc", "stars", "asc"),
)


@dataclass(frozen=True)
class CrawlSearchWorkItem:
    """One language/star/pushed-time query, with no implied scheduling order."""

    language: LanguageOption
    star_bucket: StarBucket
    pushed_bucket: PushedBucket
    query: str


def build_crawl_search_plan(
    languages: tuple[LanguageOption, ...],
    star_buckets: tuple[StarBucket, ...],
    pushed_buckets: tuple[PushedBucket, ...],
    *,
    reference_time: datetime,
) -> tuple[CrawlSearchWorkItem, ...]:
    """Build all 200 configured search combinations for one crawl."""

    return tuple(
        CrawlSearchWorkItem(
            language=language,
            star_bucket=star_bucket,
            pushed_bucket=pushed_bucket,
            query=build_repository_search_query(
                language,
                star_bucket,
                pushed_bucket,
                reference_time=reference_time,
            ),
        )
        for language in languages
        for star_bucket in star_buckets
        for pushed_bucket in pushed_buckets
    )


def build_repository_search_query(
    language: LanguageOption,
    star_bucket: StarBucket,
    pushed_bucket: PushedBucket,
    *,
    reference_time: datetime,
) -> str:
    """Build a conservative GitHub query for one configured filter combination."""

    qualifiers = [
        f"language:{language.github_qualifier}",
        build_star_qualifier(star_bucket),
        *build_pushed_qualifiers(pushed_bucket, reference_time=reference_time),
        "archived:false",
        "fork:false",
    ]
    return " ".join(qualifiers)


def build_star_qualifier(star_bucket: StarBucket) -> str:
    """Convert a configured inclusive star bucket into GitHub search syntax."""

    if star_bucket.maximum_stars is None:
        return f"stars:>={star_bucket.minimum_stars}"

    return f"stars:{star_bucket.minimum_stars}..{star_bucket.maximum_stars}"


def build_pushed_qualifiers(
    pushed_bucket: PushedBucket,
    *,
    reference_time: datetime,
) -> tuple[str, ...]:
    """Build date-granular GitHub pushed qualifiers for one configured age range."""

    if reference_time.tzinfo is None:
        raise ValueError("The crawl reference time must include a timezone.")

    reference_date = reference_time.astimezone(timezone.utc).date()
    minimum_date = (
        reference_date - timedelta(days=pushed_bucket.minimum_age_days)
        if pushed_bucket.minimum_age_days is not None
        else None
    )
    maximum_date = (
        reference_date - timedelta(days=pushed_bucket.maximum_age_days)
        if pushed_bucket.maximum_age_days is not None
        else None
    )

    qualifiers: list[str] = []
    if maximum_date is not None:
        qualifiers.append(f"pushed:>={maximum_date.isoformat()}")
    if minimum_date is not None:
        qualifiers.append(f"pushed:<{minimum_date.isoformat()}")
    return tuple(qualifiers)


def calculate_search_page_count(total_count: int) -> int:
    """Return the useful page count for one GitHub search response, capped at 10."""

    if total_count < 0:
        raise ValueError("GitHub repository-search total_count cannot be negative.")

    return min(ceil(total_count / SEARCH_PAGE_SIZE), MAX_SEARCH_PAGES)
