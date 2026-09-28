# GH Random

GH Random is a local-first GitHub repository discovery app. A separate crawler
will slowly collect GitHub Search API results into a local catalog; the web app
will choose random repositories only from that catalog, never during a user
request.

## Current milestone

The project currently contains the application foundation and centralized MVP
filter definitions. The database schema, crawler, and random-repository API
will be added in the next incremental steps.

## MVP filter configuration

The initial supported languages are Python, JavaScript, TypeScript, Java, C#,
C++, C, Go, Rust, and Ruby. Star ranges are non-overlapping:

- 0–100
- 101–1,000
- 1,001–10,000
- 10,001–100,000
- 100,001+

Activity ranges measure the age of GitHub's `updated_at` timestamp: within one
week, over one week through one month, over one month through one year, and
over one year.

All of these definitions live in `app/core/filters.py` so they can change
without altering crawler or API logic.

## Local setup

Requires Python 3.12 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

The initial health endpoint is available at `http://127.0.0.1:8000/health`.

Run the current automated tests with:

```powershell
pytest
```

Future milestones will add explicit database migration and manual crawl
commands to this document.
