from dataclasses import dataclass

import httpx
import pytest

from app.github.search import GitHubSearchClient, RequestPacer


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
