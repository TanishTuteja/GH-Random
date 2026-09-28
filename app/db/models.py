"""Persistent catalog models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Repository(Base):
    """The most recent catalog snapshot of a GitHub repository."""

    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("stargazer_count >= 0", name="ck_repositories_stargazer_count_nonnegative"),
        CheckConstraint("fork_count >= 0", name="ck_repositories_fork_count_nonnegative"),
    )

    github_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    owner_login: Mapped[str] = mapped_column(String(255))
    html_url: Mapped[str] = mapped_column(String(2_048))
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(100), index=True)
    stargazer_count: Mapped[int] = mapped_column(Integer, index=True)
    fork_count: Mapped[int] = mapped_column(Integer)
    is_archived: Mapped[bool] = mapped_column(Boolean)
    is_fork: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    github_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
