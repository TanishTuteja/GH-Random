# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# uv 0.13.1, pinned to the multi-architecture image index.
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv

WORKDIR /app

RUN groupadd --gid 10001 gh-random \
    && useradd --uid 10001 --gid gh-random --create-home \
        --home-dir /home/gh-random --shell /usr/sbin/nologin gh-random

COPY pyproject.toml uv.lock .python-version README.md ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN uv sync --locked --no-default-groups --no-editable --no-cache \
    && chown --recursive gh-random:gh-random /app

USER gh-random

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
