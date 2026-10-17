"""Check the proxy-header behavior used behind the private Caddy network."""

from typing import cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import ASGIApp
from uvicorn._types import ASGI3Application
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


@pytest.mark.parametrize("trust_peers", [True, False])
def test_uvicorn_proxy_scheme_and_client_address(trust_peers: bool) -> None:
    probe = FastAPI()

    @probe.get("/")
    def index(request: Request) -> dict[str, str]:
        assert request.client is not None
        return {"scheme": request.url.scheme, "client": request.client.host}

    # Uvicorn uses TypedDict scopes; Starlette uses MutableMapping scopes.
    # Both implement ASGI, but their static type aliases are not assignable.
    app = ProxyHeadersMiddleware(
        cast(ASGI3Application, probe), trusted_hosts="*" if trust_peers else []
    )
    with TestClient(cast(ASGIApp, app)) as client:
        result = client.get(
            "/", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"}
        )
        assert result.json() == {
            "scheme": "https" if trust_peers else "http",
            "client": "203.0.113.7" if trust_peers else "testclient",
        }
