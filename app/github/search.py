"""GitHub repository-search client with conservative request pacing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from time import monotonic, sleep
from typing import Literal, Protocol

import httpx

from app.github.app_auth import GITHUB_API_BASE_URL, GITHUB_API_VERSION

RepositorySearchSort = Literal["stars", "forks", "help-wanted-issues", "updated"]
SearchOrder = Literal["asc", "desc"]
SUPPORTED_SORTS: frozenset[str] = frozenset({"stars", "forks", "help-wanted-issues", "updated"})
SUPPORTED_ORDERS: frozenset[str] = frozenset({"asc", "desc"})


class InstallationTokenProvider(Protocol):
    """Supplies a valid GitHub App installation access token."""

    def get_installation_access_token(self) -> str: ...


class GitHubSearchError(RuntimeError):
    """GitHub repository search could not be completed safely."""


@dataclass(frozen=True)
class RepositorySearchPage:
    """One page returned by GitHub's repository-search endpoint."""

    total_count: int
    incomplete_results: bool
    repositories: tuple[Mapping[str, object], ...]


class RequestPacer:
    """Serializes requests so their start times are at least a fixed interval apart."""

    def __init__(
        self,
        minimum_delay_seconds: float,
        *,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if minimum_delay_seconds <= 0:
            raise ValueError("The GitHub search delay must be greater than zero.")

        self._minimum_delay_seconds = minimum_delay_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._next_request_at: float | None = None
        self._lock = Lock()

    def wait_for_next_request(self) -> None:
        """Wait until the next GitHub search request is allowed to begin."""

        with self._lock:
            now = self._clock()
            if self._next_request_at is not None:
                wait_seconds = max(0.0, self._next_request_at - now)
                if wait_seconds > 0:
                    self._sleeper(wait_seconds)

            self._next_request_at = self._clock() + self._minimum_delay_seconds


class GitHubSearchClient:
    """Search public repositories using an installation access token."""

    def __init__(
        self,
        token_provider: InstallationTokenProvider,
        *,
        request_delay_seconds: float,
        http_client: httpx.Client | None = None,
        request_pacer: RequestPacer | None = None,
        api_base_url: str = GITHUB_API_BASE_URL,
    ) -> None:
        if request_delay_seconds <= 0:
            raise ValueError("The GitHub search delay must be greater than zero.")

        self._token_provider = token_provider
        self._http_client = http_client or httpx.Client(timeout=10.0)
        self._owns_http_client = http_client is None
        self._request_pacer = request_pacer or RequestPacer(request_delay_seconds)
        self._api_base_url = api_base_url.rstrip("/")

    def close(self) -> None:
        """Close the internally created HTTP client, if any."""

        if self._owns_http_client:
            self._http_client.close()

    def search_repositories(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 100,
        sort: RepositorySearchSort | None = None,
        order: SearchOrder | None = None,
    ) -> RepositorySearchPage:
        """Search a page of repositories without choosing crawler rotation policy."""

        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("A GitHub repository search query is required.")
        if page < 1:
            raise ValueError("GitHub repository-search page must be at least 1.")
        if not 1 <= per_page <= 100:
            raise ValueError("GitHub repository-search per_page must be between 1 and 100.")
        if sort is not None and sort not in SUPPORTED_SORTS:
            raise ValueError("Unsupported GitHub repository-search sort value.")
        if order is not None and order not in SUPPORTED_ORDERS:
            raise ValueError("Unsupported GitHub repository-search order value.")
        if sort is None and order is not None:
            raise ValueError("GitHub repository-search order requires an explicit sort.")

        parameters: dict[str, str | int] = {
            "q": normalized_query,
            "page": page,
            "per_page": per_page,
        }
        if sort is not None:
            parameters["sort"] = sort
        if order is not None:
            parameters["order"] = order

        token = self._token_provider.get_installation_access_token()
        self._request_pacer.wait_for_next_request()

        try:
            response = self._http_client.get(
                f"{self._api_base_url}/search/repositories",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
                params=parameters,
            )
        except httpx.HTTPError as error:
            raise GitHubSearchError("The request to GitHub repository search failed.") from error

        if response.is_error:
            raise GitHubSearchError(
                f"GitHub repository search failed with HTTP status {response.status_code}."
            )

        try:
            response_data = response.json()
        except ValueError as error:
            raise GitHubSearchError("GitHub repository search returned non-JSON data.") from error

        return self._parse_search_page(response_data)

    @staticmethod
    def _parse_search_page(response_data: object) -> RepositorySearchPage:
        if not isinstance(response_data, Mapping):
            raise GitHubSearchError("GitHub repository search returned an invalid response.")

        total_count = response_data.get("total_count")
        incomplete_results = response_data.get("incomplete_results")
        items = response_data.get("items")
        if (
            not isinstance(total_count, int)
            or isinstance(total_count, bool)
            or not isinstance(incomplete_results, bool)
            or not isinstance(items, list)
        ):
            raise GitHubSearchError("GitHub repository search response omitted expected fields.")

        repositories: list[Mapping[str, object]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise GitHubSearchError("GitHub repository search returned an invalid repository.")

            repository: dict[str, object] = {}
            for key, value in item.items():
                if not isinstance(key, str):
                    raise GitHubSearchError(
                        "GitHub repository search returned a repository with an invalid field name."
                    )
                repository[key] = value
            repositories.append(repository)

        return RepositorySearchPage(
            total_count=total_count,
            incomplete_results=incomplete_results,
            repositories=tuple(repositories),
        )
