"""Editable MVP filter definitions shared by the crawler and public API."""

from dataclasses import dataclass

ANY_FILTER_ID = "any"


@dataclass(frozen=True)
class LanguageOption:
    id: str
    label: str
    github_qualifier: str


@dataclass(frozen=True)
class StarBucket:
    id: str
    label: str
    minimum_stars: int
    maximum_stars: int | None


@dataclass(frozen=True)
class PushedBucket:
    """An age range for GitHub's pushed_at timestamp, expressed in days.

    The lower bound is exclusive and the upper bound is inclusive. A missing
    lower bound means no minimum age; a missing upper bound means no maximum.
    """

    id: str
    label: str
    minimum_age_days: int | None
    maximum_age_days: int | None


SUPPORTED_LANGUAGES: tuple[LanguageOption, ...] = (
    LanguageOption("python", "Python", "Python"),
    LanguageOption("javascript", "JavaScript", "JavaScript"),
    LanguageOption("typescript", "TypeScript", "TypeScript"),
    LanguageOption("java", "Java", "Java"),
    LanguageOption("csharp", "C#", "C#"),
    LanguageOption("cplusplus", "C++", "C++"),
    LanguageOption("c", "C", "C"),
    LanguageOption("go", "Go", "Go"),
    LanguageOption("rust", "Rust", "Rust"),
    LanguageOption("ruby", "Ruby", "Ruby"),
)

# These options are only for local catalog filtering. The crawler must continue
# to query the concrete buckets above, not broad GitHub Search queries.
LANGUAGE_FILTER_OPTIONS: tuple[LanguageOption, ...] = (
    LanguageOption(ANY_FILTER_ID, "Any", ""),
    *SUPPORTED_LANGUAGES,
)

STAR_BUCKETS: tuple[StarBucket, ...] = (
    StarBucket("0-100", "0–100 stars", minimum_stars=0, maximum_stars=100),
    StarBucket("101-1k", "101–1,000 stars", minimum_stars=101, maximum_stars=1_000),
    StarBucket("1001-10k", "1,001–10,000 stars", minimum_stars=1_001, maximum_stars=10_000),
    StarBucket("10001-100k", "10,001–100,000 stars", minimum_stars=10_001, maximum_stars=100_000),
    StarBucket("100001-plus", "100,001+ stars", minimum_stars=100_001, maximum_stars=None),
)

STAR_FILTER_OPTIONS: tuple[StarBucket, ...] = (
    StarBucket(ANY_FILTER_ID, "Any", minimum_stars=0, maximum_stars=None),
    *STAR_BUCKETS,
)

PUSHED_BUCKETS: tuple[PushedBucket, ...] = (
    PushedBucket("pushed-within-week", "Pushed within 1 week", None, 7),
    PushedBucket("pushed-1-week-to-1-month", "Pushed 1 week–1 month ago", 7, 30),
    PushedBucket("pushed-1-month-to-1-year", "Pushed 1 month–1 year ago", 30, 365),
    PushedBucket("pushed-over-1-year", "Pushed over 1 year ago", 365, None),
)

PUSHED_FILTER_OPTIONS: tuple[PushedBucket, ...] = (
    PushedBucket(ANY_FILTER_ID, "Any", None, None),
    *PUSHED_BUCKETS,
)
