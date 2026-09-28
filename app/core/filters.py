"""Editable MVP filter definitions shared by the crawler and public API."""

from dataclasses import dataclass


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
class ActivityBucket:
    """A range of ages for GitHub's updated_at timestamp, expressed in days.

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

STAR_BUCKETS: tuple[StarBucket, ...] = (
    StarBucket("0-100", "0–100 stars", minimum_stars=0, maximum_stars=100),
    StarBucket("101-1k", "101–1,000 stars", minimum_stars=101, maximum_stars=1_000),
    StarBucket("1001-10k", "1,001–10,000 stars", minimum_stars=1_001, maximum_stars=10_000),
    StarBucket("10001-100k", "10,001–100,000 stars", minimum_stars=10_001, maximum_stars=100_000),
    StarBucket("100001-plus", "100,001+ stars", minimum_stars=100_001, maximum_stars=None),
)

ACTIVITY_BUCKETS: tuple[ActivityBucket, ...] = (
    ActivityBucket("updated-within-week", "Updated within 1 week", None, 7),
    ActivityBucket("updated-1-week-to-1-month", "Updated 1 week–1 month ago", 7, 30),
    ActivityBucket("updated-1-month-to-1-year", "Updated 1 month–1 year ago", 30, 365),
    ActivityBucket("updated-over-1-year", "Updated over 1 year ago", 365, None),
)
