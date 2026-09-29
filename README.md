# GH Random

GH Random discovers random GitHub repositories from a local catalog. GitHub is
contacted only by a manual, paced crawler. Browser requests read from the last
successfully published catalog generation, so browsing remains quick and
predictable even while another crawl is running.

## How it works

One full crawl executes every combination of:

- 10 programming languages
- 5 non-overlapping star ranges
- 4 GitHub pushed-time ranges

That is 200 distinct GitHub Search queries before pagination. Each query reads
up to 10 pages of 100 repositories. Search request starts are spaced by at
least five seconds, including pagination, so a complete crawl can take a
substantial amount of time.

The crawler builds a staging catalog. If every request succeeds, it atomically
makes staging active; if one fails, the prior active catalog remains visible.
The web application never calls GitHub to satisfy a random-repository request.

The initial filters are:

- Languages: Python, JavaScript, TypeScript, Java, C#, C++, C, Go, Rust, Ruby
- Stars: 0–100, 101–1,000, 1,001–10,000, 10,001–100,000, 100,001+
- Pushed: within one week, one week to one month, one month to one year, over
  one year

The authoritative definitions live in app/core/filters.py.

## Local setup

Requires Python 3.12 or newer.

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
~~~

Fill the copied .env file with the GitHub App values you created:

~~~dotenv
DATABASE_URL=sqlite:///./gh_random.db
GITHUB_SEARCH_DELAY_SECONDS=5
GITHUB_APP_CLIENT_ID=your-client-id
GITHUB_APP_PRIVATE_KEY_PATH=C:/secure/location/your-app.private-key.pem
# Optional when the App has exactly one installation:
# GITHUB_APP_INSTALLATION_ID=12345678
~~~

Do not put the private key in the repository. The default SQLite URL is suitable
for local development; set DATABASE_URL to a PostgreSQL URL when deploying.

## Run it

Apply the database migrations before the first crawl:

~~~powershell
alembic upgrade head
~~~

Start a complete catalog crawl:

~~~powershell
python -m app.cli crawl
~~~

On success, start the web application:

~~~powershell
uvicorn app.main:app --reload
~~~

Open http://127.0.0.1:8000. Before the first successful crawl, the page
explains that no active matching catalog exists. It does not silently query
GitHub as a fallback.

## Verification

~~~powershell
pytest
ruff format --check app tests
ruff check app tests
pyright
~~~

The test suite covers query-plan coverage, five-second request pacing,
GitHub-App authentication behavior, repository mapping, generation publishing,
failure retention, random filtering, the API, and the rendered start page.

## Deployment notes

Run the same migrations against the deployment database, provide all settings
as environment variables or deployment secrets, and run the crawler separately
from the web server. A crawler failure is safe for users because it cannot
replace the existing active catalog.
