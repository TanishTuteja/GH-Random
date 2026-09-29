from dataclasses import dataclass

import httpx
import pytest

from app.github.search import GitHubSearchClient, GitHubSearchError, RequestPacer


@dataclass
class FakeTokenProvider:
    token: str = "installation-token"
    calls: int = 0

    def get_installation_access_token(self) -> str:
        self.calls += 1
        return self.token


def test_request_pacer_waits_five_seconds_between_request_starts() -> None:
    current_time = 100.0
    sleeps: list[float] = []

    def clock() -> float:
        return current_time

    def sleeper(seconds: float) -> None:
        nonlocal current_time
        sleeps.append(seconds)
        current_time += seconds

    pacer = RequestPacer(5.0, clock=clock, sleeper=sleeper)

    pacer.wait_for_next_request()
    current_time += 1.0
    pacer.wait_for_next_request()
    pacer.wait_for_next_request()

    assert sleeps == [4.0, 5.0]


def test_search_sends_the_query_and_keeps_sort_policy_explicit() -> None:
    observed_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_requests.append(request)
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [{"id": 123, "full_name": "octo/example"}],
            },
        )

    token_provider = FakeTokenProvider()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubSearchClient(
        token_provider,
        request_delay_seconds=5.0,
        http_client=http_client,
    )

    try:
        page = client.search_repositories(" language:Python stars:0..100 ", page=2, per_page=50)
    finally:
        http_client.close()

    assert page.total_count == 1
    assert page.repositories[0]["full_name"] == "octo/example"
    assert token_provider.calls == 1
    assert len(observed_requests) == 1

    request = observed_requests[0]
    assert request.url.path == "/search/repositories"
    assert dict(request.url.params) == {
        "q": "language:Python stars:0..100",
        "page": "2",
        "per_page": "50",
    }
    assert request.headers["authorization"] == "Bearer installation-token"


def test_search_accepts_an_explicit_sort_and_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params)["sort"] == "updated"
        assert dict(request.url.params)["order"] == "asc"
        return httpx.Response(
            200,
            json={"total_count": 0, "incomplete_results": False, "items": []},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubSearchClient(
        FakeTokenProvider(),
        request_delay_seconds=5.0,
        http_client=http_client,
    )

    try:
        result = client.search_repositories("language:Rust", sort="updated", order="asc")
    finally:
        http_client.close()

    assert result.repositories == ()


def test_search_rejects_order_without_sort() -> None:
    client = GitHubSearchClient(FakeTokenProvider(), request_delay_seconds=5.0)

    try:
        with pytest.raises(ValueError, match="requires an explicit sort"):
            client.search_repositories("language:Go", order="asc")
    finally:
        client.close()


def test_search_retries_an_explicit_github_rate_limit_with_retry_after() -> None:
    requests = 0
    rate_limit_sleeps: list[float] = []
    pacer_sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                403,
                headers={"Retry-After": "15"},
                json={"message": "You have exceeded a secondary rate limit."},
            )
        return httpx.Response(
            200,
            json={"total_count": 0, "incomplete_results": False, "items": []},
        )

    pacer = RequestPacer(
        5.0,
        clock=lambda: 100.0,
        sleeper=pacer_sleeps.append,
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubSearchClient(
        FakeTokenProvider(),
        request_delay_seconds=5.0,
        http_client=http_client,
        request_pacer=pacer,
        rate_limit_sleeper=rate_limit_sleeps.append,
    )

    try:
        result = client.search_repositories("language:Python")
    finally:
        http_client.close()

    assert result.repositories == ()
    assert requests == 2
    assert rate_limit_sleeps == [15.0]
    assert pacer_sleeps == [5.0]


def test_search_stops_after_the_configured_rate_limit_retry_count() -> None:
    requests = 0
    rate_limit_sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            429,
            json={"message": "API rate limit exceeded"},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubSearchClient(
        FakeTokenProvider(),
        request_delay_seconds=5.0,
        http_client=http_client,
        request_pacer=RequestPacer(5.0, sleeper=lambda _: None),
        rate_limit_retry_seconds=30.0,
        rate_limit_max_retries=1,
        rate_limit_sleeper=rate_limit_sleeps.append,
    )

    try:
        with pytest.raises(GitHubSearchError, match="API rate limit exceeded"):
            client.search_repositories("language:Python")
    finally:
        http_client.close()

    assert requests == 2
    assert rate_limit_sleeps == [30.0]


def test_search_retries_a_transient_transport_failure() -> None:
    requests = 0
    transport_sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("Connection dropped", request=request)
        return httpx.Response(
            200,
            json={"total_count": 0, "incomplete_results": False, "items": []},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubSearchClient(
        FakeTokenProvider(),
        request_delay_seconds=5.0,
        http_client=http_client,
        request_pacer=RequestPacer(5.0, sleeper=lambda _: None),
        transport_retry_seconds=30.0,
        transport_max_retries=1,
        transport_retry_sleeper=transport_sleeps.append,
    )

    try:
        result = client.search_repositories("language:Python")
    finally:
        http_client.close()

    assert result.repositories == ()
    assert requests == 2
    assert transport_sleeps == [30.0]
