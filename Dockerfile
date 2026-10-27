# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# uv 0.13.5, pinned to the multi-architecture image index.
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv

WORKDIR /app

RUN groupadd --gid 10001 gh-random \
    && useradd --uid 10001 --gid gh-random --create-home \
        --home-dir /home/gh-random --shell /usr/sbin/nologin gh-random

COPY pyproject.toml uv.lock .python-version README.md ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# uv handles all installation. Remove the base image's unused pip/ensurepip
# copies, which otherwise retain their independent, outdated CA bundle.
RUN uv pip uninstall --system pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
    && uv sync --locked --no-default-groups --no-editable --no-cache \
    && chown --recursive gh-random:gh-random /app

USER gh-random

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
