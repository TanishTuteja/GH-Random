"""Persistent catalog, crawl, and active-generation models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Repository(Base):
    """Canonical metadata for a GitHub repository seen by any crawl."""

    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("stargazer_count >= 0", name="ck_repositories_stargazer_count_nonnegative"),
        CheckConstraint("fork_count >= 0", name="ck_repositories_fork_count_nonnegative"),
        CheckConstraint(
            "open_issues_count >= 0",
            name="ck_repositories_open_issues_count_nonnegative",
        ),
    )

    github_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    owner_login: Mapped[str] = mapped_column(String(255))
    html_url: Mapped[str] = mapped_column(String(2_048))
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(100), index=True)
    stargazer_count: Mapped[int] = mapped_column(Integer, index=True)
    fork_count: Mapped[int] = mapped_column(Integer)
    open_issues_count: Mapped[int] = mapped_column(Integer, default=0)
    is_archived: Mapped[bool] = mapped_column(Boolean)
    is_fork: Mapped[bool] = mapped_column(Boolean)
    github_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    github_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    github_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    topics: Mapped[list[str] | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CatalogGeneration(Base):
    """A complete repository-membership snapshot, either active or being built."""

    __tablename__ = "catalog_generations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staging', 'active', 'retired', 'failed')",
            name="ck_catalog_generations_status",
        ),
        Index(
            "uq_catalog_generations_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrawlRun(Base):
    """An audit record for one full crawl and the generation it constructs."""

    __tablename__ = "crawl_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_crawl_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="CASCADE"),
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(20), index=True)
    sort_key: Mapped[str] = mapped_column(String(50))
    sort_order: Mapped[str] = mapped_column(String(4))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    repositories_seen: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    plan_reference_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64))
    next_work_item_index: Mapped[int] = mapped_column(Integer, default=0)
    next_page: Mapped[int] = mapped_column(Integer, default=1)
    current_work_item_page_count: Mapped[int | None] = mapped_column(Integer)
    resume_count: Mapped[int] = mapped_column(Integer, default=0)


class GenerationRepository(Base):
    """Membership and freshness for one repository within one generation."""

    __tablename__ = "generation_repositories"

    generation_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    github_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.github_id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
