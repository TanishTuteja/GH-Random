"""Map GitHub repository-search payloads into catalog model instances."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from app.db.models import Repository


class GitHubRepositoryPayloadError(ValueError):
    """A GitHub repository-search item is missing data required by the catalog."""


def map_repository_payload(
    payload: Mapping[str, object],
    *,
    crawled_at: datetime,
) -> Repository:
    """Create an unsaved catalog record from one GitHub search-result item."""

    if crawled_at.tzinfo is None:
        raise GitHubRepositoryPayloadError("The crawl timestamp must include a timezone.")

    owner = payload.get("owner")
    if not isinstance(owner, Mapping):
        raise GitHubRepositoryPayloadError("GitHub repository payload has no valid owner.")

    seen_at = crawled_at.astimezone(timezone.utc)
    return Repository(
        github_id=_require_nonnegative_int(payload, "id"),
        full_name=_require_string(payload, "full_name"),
        name=_require_string(payload, "name"),
        owner_login=_require_string(owner, "login"),
        html_url=_require_string(payload, "html_url"),
        description=_optional_string(payload, "description"),
        language=_optional_string(payload, "language"),
        stargazer_count=_require_nonnegative_int(payload, "stargazers_count"),
        fork_count=_require_nonnegative_int(payload, "forks_count"),
        open_issues_count=_require_nonnegative_int(payload, "open_issues_count"),
        is_archived=_require_bool(payload, "archived"),
        is_fork=_require_bool(payload, "fork"),
        github_created_at=_parse_github_timestamp(_require_string(payload, "created_at")),
        github_updated_at=_parse_github_timestamp(_require_string(payload, "updated_at")),
        github_pushed_at=_optional_timestamp(payload, "pushed_at"),
        topics=_optional_topics(payload),
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )


def _require_string(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise GitHubRepositoryPayloadError(
            f"GitHub repository payload has no valid {field_name!r} string."
        )
    return value


def _optional_string(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubRepositoryPayloadError(
            f"GitHub repository payload has no valid optional {field_name!r} string."
        )
    return value


def _require_nonnegative_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GitHubRepositoryPayloadError(
            f"GitHub repository payload has no valid non-negative {field_name!r} integer."
        )
    return value


def _require_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise GitHubRepositoryPayloadError(
            f"GitHub repository payload has no valid {field_name!r} boolean."
        )
    return value


def _optional_timestamp(payload: Mapping[str, object], field_name: str) -> datetime | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubRepositoryPayloadError(
            f"GitHub repository payload has no valid optional {field_name!r} timestamp."
        )
    return _parse_github_timestamp(value)


def _optional_topics(payload: Mapping[str, object]) -> list[str]:
    value = payload.get("topics")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(topic, str) for topic in value):
        raise GitHubRepositoryPayloadError("GitHub repository payload has no valid topics list.")
    return value


def _parse_github_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GitHubRepositoryPayloadError(
            "GitHub repository payload contains an invalid timestamp."
        ) from error

    if parsed.tzinfo is None:
        raise GitHubRepositoryPayloadError(
            "GitHub repository payload contains a timestamp without a timezone."
        )

    return parsed.astimezone(timezone.utc)
