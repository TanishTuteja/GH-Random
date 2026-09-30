"""Local-only random repository selection from the active catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.filters import (
    ANY_FILTER_ID,
    LANGUAGE_FILTER_OPTIONS,
    PUSHED_FILTER_OPTIONS,
    STAR_FILTER_OPTIONS,
    LanguageOption,
    PushedBucket,
    StarBucket,
)
from app.db.models import CatalogGeneration, GenerationRepository, Repository


class FilterSelectionError(ValueError):
    """A request named a language, star range, or pushed-time range not configured for the MVP."""


@dataclass(frozen=True)
class RepositoryFilterSelection:
    """The resolved filter objects for one random-repository request."""

    language: LanguageOption
    star_bucket: StarBucket
    pushed_bucket: PushedBucket


def resolve_filter_selection(
    *,
    language_id: str,
    star_bucket_id: str,
    pushed_bucket_id: str,
) -> RepositoryFilterSelection:
    """Resolve public filter IDs to the single configuration source of truth."""

    return RepositoryFilterSelection(
        language=_find_language(language_id),
        star_bucket=_find_star_bucket(star_bucket_id),
        pushed_bucket=_find_pushed_bucket(pushed_bucket_id),
    )


def find_random_active_repository(
    session: Session,
    selection: RepositoryFilterSelection,
    *,
    now: datetime,
    exclude_github_id: int | None = None,
) -> Repository | None:
    """Choose one matching active repository, preferring not to repeat an excluded one."""

    if now.tzinfo is None:
        raise ValueError("The repository-selection timestamp must include a timezone.")

    statement: Select[Repository] = (
        select(Repository)
        .join(GenerationRepository, GenerationRepository.github_id == Repository.github_id)
        .join(
            CatalogGeneration,
            CatalogGeneration.id == GenerationRepository.generation_id,
        )
        .where(
            CatalogGeneration.status == "active",
        )
    )

    if selection.language.id != ANY_FILTER_ID:
        statement = statement.where(Repository.language == selection.language.github_qualifier)

    if selection.star_bucket.id != ANY_FILTER_ID:
        statement = statement.where(
            Repository.stargazer_count >= selection.star_bucket.minimum_stars
        )

    if (
        selection.star_bucket.id != ANY_FILTER_ID
        and selection.star_bucket.maximum_stars is not None
    ):
        statement = statement.where(
            Repository.stargazer_count <= selection.star_bucket.maximum_stars
        )

    statement = _apply_pushed_time_filter(
        statement,
        selection.pushed_bucket,
        now=now.astimezone(timezone.utc),
    )

    if exclude_github_id is not None:
        alternative = session.scalar(
            statement.where(Repository.github_id != exclude_github_id)
            .order_by(func.random())
            .limit(1)
        )
        if alternative is not None:
            return alternative

    return session.scalar(statement.order_by(func.random()).limit(1))


def _find_language(option_id: str) -> LanguageOption:
    return cast(LanguageOption, _find_option(LANGUAGE_FILTER_OPTIONS, option_id, "language"))


def _find_star_bucket(option_id: str) -> StarBucket:
    return cast(StarBucket, _find_option(STAR_FILTER_OPTIONS, option_id, "star bucket"))


def _find_pushed_bucket(option_id: str) -> PushedBucket:
    return cast(
        PushedBucket,
        _find_option(PUSHED_FILTER_OPTIONS, option_id, "pushed-time bucket"),
    )


def _find_option(
    options: tuple[LanguageOption | StarBucket | PushedBucket, ...],
    option_id: str,
    option_name: str,
) -> LanguageOption | StarBucket | PushedBucket:
    for option in options:
        if option.id == option_id:
            return option
    raise FilterSelectionError(f"Unsupported {option_name}: {option_id!r}.")


def _apply_pushed_time_filter(
    statement: Select[Repository],
    pushed_bucket: PushedBucket,
    *,
    now: datetime,
) -> Select[Repository]:
    """Apply local timestamp bounds; lower age is exclusive, upper age inclusive."""

    if pushed_bucket.maximum_age_days is not None:
        newest_allowed_pushed_at = now - timedelta(days=pushed_bucket.maximum_age_days)
        statement = statement.where(Repository.github_pushed_at >= newest_allowed_pushed_at)

    if pushed_bucket.minimum_age_days is not None:
        oldest_excluded_pushed_at = now - timedelta(days=pushed_bucket.minimum_age_days)
        statement = statement.where(Repository.github_pushed_at < oldest_excluded_pushed_at)

    return statement
