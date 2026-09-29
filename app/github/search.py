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
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 60.0
DEFAULT_RATE_LIMIT_MAX_RETRIES = 2
DEFAULT_TRANSPORT_RETRY_SECONDS = 60.0
DEFAULT_TRANSPORT_MAX_RETRIES = 2


class InstallationTokenProvider(Protocol):
    """Supplies a valid GitHub App installation access token."""

    def get_installation_access_token(self) -> str: ...


class GitHubSearchError(RuntimeError):
    """GitHub repository search could not be completed safely."""


class GitHubSearchTransportError(GitHubSearchError):
    """A transient network or timeout failure occurred during a GitHub search request."""


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
        rate_limit_retry_seconds: float = DEFAULT_RATE_LIMIT_RETRY_SECONDS,
        rate_limit_max_retries: int = DEFAULT_RATE_LIMIT_MAX_RETRIES,
        rate_limit_sleeper: Callable[[float], None] = sleep,
        transport_retry_seconds: float = DEFAULT_TRANSPORT_RETRY_SECONDS,
        transport_max_retries: int = DEFAULT_TRANSPORT_MAX_RETRIES,
        transport_retry_sleeper: Callable[[float], None] = sleep,
        api_base_url: str = GITHUB_API_BASE_URL,
    ) -> None:
        if request_delay_seconds <= 0:
            raise ValueError("The GitHub search delay must be greater than zero.")
        if rate_limit_retry_seconds <= 0:
            raise ValueError("The GitHub rate-limit retry delay must be greater than zero.")
        if rate_limit_max_retries < 0:
            raise ValueError("The GitHub rate-limit retry count cannot be negative.")
        if transport_retry_seconds <= 0:
            raise ValueError("The GitHub transport retry delay must be greater than zero.")
        if transport_max_retries < 0:
            raise ValueError("The GitHub transport retry count cannot be negative.")

        self._token_provider = token_provider
        self._http_client = http_client or httpx.Client(timeout=10.0)
        self._owns_http_client = http_client is None
        self._request_pacer = request_pacer or RequestPacer(request_delay_seconds)
        self._rate_limit_retry_seconds = rate_limit_retry_seconds
        self._rate_limit_max_retries = rate_limit_max_retries
        self._rate_limit_sleeper = rate_limit_sleeper
        self._transport_retry_seconds = transport_retry_seconds
        self._transport_max_retries = transport_max_retries
        self._transport_retry_sleeper = transport_retry_sleeper
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
        for attempt in range(self._rate_limit_max_retries + 1):
            response = self._send_with_transport_retries(token, parameters)

            if self._is_rate_limited(response):
                if attempt == self._rate_limit_max_retries:
                    raise self._response_error(response)
                self._rate_limit_sleeper(self._rate_limit_delay(response))
                continue

            if response.is_error:
                raise self._response_error(response)

            try:
                response_data = response.json()
            except ValueError as error:
                raise GitHubSearchError(
                    "GitHub repository search returned non-JSON data."
                ) from error

            return self._parse_search_page(response_data)

        raise AssertionError("The bounded rate-limit retry loop exited unexpectedly.")

    def _send_with_transport_retries(
        self,
        token: str,
        parameters: Mapping[str, str | int],
    ) -> httpx.Response:
        for attempt in range(self._transport_max_retries + 1):
            self._request_pacer.wait_for_next_request()
            try:
                return self._send_search_request(token, parameters)
            except GitHubSearchTransportError:
                if attempt == self._transport_max_retries:
                    raise
                self._transport_retry_sleeper(self._transport_retry_seconds)

        raise AssertionError("The bounded transport retry loop exited unexpectedly.")

    def _send_search_request(
        self,
        token: str,
        parameters: Mapping[str, str | int],
    ) -> httpx.Response:
        try:
            return self._http_client.get(
                f"{self._api_base_url}/search/repositories",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
                params=parameters,
            )
        except httpx.TransportError as error:
            raise GitHubSearchTransportError(
                "The request to GitHub repository search failed."
            ) from error

    def _rate_limit_delay(self, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return self._rate_limit_retry_seconds

        try:
            delay = float(retry_after)
        except ValueError:
            return self._rate_limit_retry_seconds
        return delay if delay > 0 else self._rate_limit_retry_seconds

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True

        try:
            response_data = response.json()
        except ValueError:
            return False
        return (
            isinstance(response_data, Mapping)
            and isinstance(response_data.get("message"), str)
            and "rate limit" in response_data["message"].lower()
        )

    @staticmethod
    def _response_error(response: httpx.Response) -> GitHubSearchError:
        message = ""
        try:
            response_data = response.json()
        except ValueError:
            response_data = None
        if isinstance(response_data, Mapping) and isinstance(response_data.get("message"), str):
            message = f": {response_data['message'][:300]}"

        return GitHubSearchError(
            f"GitHub repository search failed with HTTP status {response.status_code}{message}"
        )

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
