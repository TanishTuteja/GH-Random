"""FastAPI web application for local GitHub repository discovery."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.catalog.random_repository import (
    FilterSelectionError,
    find_random_active_repository,
    resolve_filter_selection,
)
from app.core.filters import PUSHED_BUCKETS, STAR_BUCKETS, SUPPORTED_LANGUAGES
from app.core.settings import Settings, get_settings
from app.db.models import Repository
from app.db.session import create_engine_from_url, create_session_factory

APP_DIRECTORY = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIRECTORY / "templates"))


class FilterOptionResponse(BaseModel):
    id: str
    label: str


class FilterOptionsResponse(BaseModel):
    languages: list[FilterOptionResponse]
    star_buckets: list[FilterOptionResponse]
    pushed_buckets: list[FilterOptionResponse]


class RandomRepositoryResponse(BaseModel):
    github_id: int
    full_name: str
    name: str
    owner_login: str
    html_url: str
    description: str | None
    language: str | None
    stargazer_count: int
    fork_count: int
    open_issues_count: int
    github_updated_at: datetime
    github_pushed_at: datetime | None
    topics: list[str]
    last_seen_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> FastAPI:
    """Create the web application with an optional injectable database for tests."""

    runtime_settings = settings or get_settings()
    owned_engine: Engine | None = None
    if session_factory is None:
        owned_engine = create_engine_from_url(runtime_settings.database_url)
        session_factory = create_session_factory(owned_engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(title=runtime_settings.app_name, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=APP_DIRECTORY / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> object:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "languages": SUPPORTED_LANGUAGES,
                "star_buckets": STAR_BUCKETS,
                "pushed_buckets": PUSHED_BUCKETS,
            },
        )

    @app.get("/health", tags=["system"])
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/filter-options", response_model=FilterOptionsResponse, tags=["catalog"])
    def filter_options() -> FilterOptionsResponse:
        return FilterOptionsResponse(
            languages=[
                FilterOptionResponse(id=item.id, label=item.label) for item in SUPPORTED_LANGUAGES
            ],
            star_buckets=[
                FilterOptionResponse(id=item.id, label=item.label) for item in STAR_BUCKETS
            ],
            pushed_buckets=[
                FilterOptionResponse(id=item.id, label=item.label) for item in PUSHED_BUCKETS
            ],
        )

    @app.get("/api/random-repository", response_model=RandomRepositoryResponse, tags=["catalog"])
    def random_repository(
        language: str = Query(default=SUPPORTED_LANGUAGES[0].id),
        star_bucket: str = Query(default=STAR_BUCKETS[0].id),
        pushed_bucket: str = Query(default=PUSHED_BUCKETS[0].id),
    ) -> RandomRepositoryResponse:
        try:
            selection = resolve_filter_selection(
                language_id=language,
                star_bucket_id=star_bucket,
                pushed_bucket_id=pushed_bucket,
            )
        except FilterSelectionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        with session_factory() as session:
            repository = find_random_active_repository(
                session,
                selection,
                now=clock(),
            )

        if repository is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No active catalog repository matches these filters. "
                    "Run a successful full crawl, or choose different filters."
                ),
            )
        return _repository_response(repository)

    return app


def _repository_response(repository: Repository) -> RandomRepositoryResponse:
    return RandomRepositoryResponse(
        github_id=repository.github_id,
        full_name=repository.full_name,
        name=repository.name,
        owner_login=repository.owner_login,
        html_url=repository.html_url,
        description=repository.description,
        language=repository.language,
        stargazer_count=repository.stargazer_count,
        fork_count=repository.fork_count,
        open_issues_count=repository.open_issues_count,
        github_updated_at=repository.github_updated_at,
        github_pushed_at=repository.github_pushed_at,
        topics=repository.topics or [],
        last_seen_at=repository.last_seen_at,
    )


app = create_app()
