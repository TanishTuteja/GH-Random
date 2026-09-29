# GH Random

GH Random discovers random GitHub repositories from a local catalog. GitHub is
contacted only by a manual, paced crawler. Browser requests read from the last
successfully published catalog generation, so browsing remains quick and
predictable even while another crawl is running.

## Architecture and catalog behavior

GH Random has three independently started project services:

```text
Browser → FastAPI web application → PostgreSQL ← crawler → GitHub Search API
```

| Service | Responsibility | Start command |
| --- | --- | --- |
| PostgreSQL | Stores repository metadata, crawl runs, and catalog generations. | `docker compose up --detach database` |
| FastAPI web application | Serves the index page and random-repository API from the active local catalog. | `uvicorn app.main:app --reload` |
| Crawler | Authenticates as the GitHub App and builds a new staging catalog. | `python -m app.cli crawl` |

The browser is an end-user client, not another project service. The web
application and crawler both require PostgreSQL, but they do not start each
other. The crawler alone contacts GitHub; the web application never does.

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

## First-time local setup

Requires Python 3.12 or newer, Docker Desktop, and a configured GitHub App
installation and private key.

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
~~~

Edit the copied `.env` file before starting Docker. It is ignored by Git. Fill
it with a local PostgreSQL password and the GitHub App values you created:

~~~dotenv
POSTGRES_DB=gh_random
POSTGRES_USER=gh_random
POSTGRES_PASSWORD=choose-a-local-development-password
POSTGRES_PORT=5432
DATABASE_URL=postgresql+psycopg://gh_random:choose-a-local-development-password@127.0.0.1:5432/gh_random
TEST_DATABASE_URL=postgresql+psycopg://gh_random:choose-a-local-development-password@127.0.0.1:5432/gh_random_test
GITHUB_SEARCH_DELAY_SECONDS=5
GITHUB_RATE_LIMIT_RETRY_SECONDS=60
GITHUB_RATE_LIMIT_MAX_RETRIES=2
GITHUB_TRANSPORT_RETRY_SECONDS=60
GITHUB_TRANSPORT_MAX_RETRIES=2
GITHUB_APP_CLIENT_ID=your-client-id
GITHUB_APP_PRIVATE_KEY_PATH=C:/secure/location/your-app.private-key.pem
# Optional when the App has exactly one installation:
# GITHUB_APP_INSTALLATION_ID=12345678
~~~

Do not put the private key or any real deployment password in the repository.
Unless a command says otherwise, the commands below assume the virtual
environment is active; run `.\.venv\Scripts\Activate.ps1` in each new terminal.

## 1. Database service

Start PostgreSQL after saving `.env`:

~~~powershell
docker compose up --detach database
docker compose ps
~~~

The container binds PostgreSQL only to `127.0.0.1:5432`. Its named Docker volume
persists the application database and isolated test database across container
restarts.

Apply database migrations before running the crawler or web application:

~~~powershell
alembic upgrade head
alembic current
~~~

Useful database commands:

~~~powershell
docker compose logs --follow database
docker compose down
~~~

`docker compose down` preserves database data. Do not use
`docker compose down --volumes` unless you deliberately want to delete all local
PostgreSQL data.

### Optional local pgAdmin interface

pgAdmin is a separate, local-only browser interface for inspecting tables,
editing rows, and running SQL. Before starting it, set a local pgAdmin login in
`.env`:

~~~dotenv
PGADMIN_DEFAULT_EMAIL=your-local-email@example.test
PGADMIN_DEFAULT_PASSWORD=choose-a-local-pgadmin-password
PGADMIN_PORT=5050
~~~

Start the optional admin service and open http://127.0.0.1:5050:

~~~powershell
docker compose --profile admin up --detach pgadmin
~~~

Log in with the pgAdmin email and password above, then register the local
PostgreSQL server with these connection values:

| Field | Value |
| --- | --- |
| Host name/address | `database` |
| Port | `5432` |
| Maintenance database | `gh_random` |
| Username | `gh_random` |
| Password | The local `POSTGRES_PASSWORD` from `.env` |

pgAdmin runs in a container, but its HTTP port is mapped only to the host's
loopback interface. It reaches PostgreSQL through Docker Compose's private
network as `database:5432`; the FastAPI process reaches the same PostgreSQL
server through the host mapping at `127.0.0.1:5432`. Do not expose pgAdmin to
the public internet or ship it with the production application.

Stop the optional UI without stopping PostgreSQL:

~~~powershell
docker compose --profile admin stop pgadmin
~~~

## 2. Crawler service

Start one full crawl in a terminal:

~~~powershell
python -m app.cli crawl
~~~

It performs the 200 configured filter combinations and retrieves up to ten
pages for each. Search request starts are at least five seconds apart, so a
worst-case crawl can take roughly three hours. Do not start a second crawl while
one is running.

Check the catalog and crawler state from another terminal:

~~~powershell
python -m app.cli status
~~~

If a checkpoint-enabled crawl fails after its bounded request retries, continue
the same staging generation without repeating successfully stored pages:

~~~powershell
python -m app.cli crawl --resume
~~~

Resume verifies that the filter plan has not changed and uses the original
crawl's date boundaries and GitHub sort order. It is intentionally explicit:
neither Docker nor the web application starts a crawl automatically.

The two rate-limit settings only apply after GitHub explicitly returns a
rate-limit response. The transport settings apply only to a network or timeout
failure. Normal requests remain five seconds apart.

## 3. Web application service

Start the FastAPI development server in another terminal:

~~~powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
~~~

Open http://127.0.0.1:8000. The web application can run while a crawler runs;
it continues to serve the previous active catalog until a replacement is fully
published. Before the first successful crawl, there is no active catalog and it
cannot return a repository.

The only public routes are:

| Route | Purpose |
| --- | --- |
| `GET /` | Server-rendered repository discovery page. |
| `GET /api/random-repository` | JSON random repository from the active local catalog. |

The API accepts `language`, `star_bucket`, and `pushed_bucket` IDs. For example:

~~~powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/random-repository?language=python&star_bucket=0-100&pushed_bucket=pushed-within-week"
~~~

FastAPI documentation and OpenAPI routes, health checks, crawler-status routes,
and filter-configuration routes are deliberately not public.

## Normal local workflow

1. Start the database: `docker compose up --detach database`.
2. Apply any newly added migration: `alembic upgrade head`.
3. Start the web application whenever you want to use or test the UI.
4. Run a crawler when you want to build or refresh the catalog.

Steps 3 and 4 can run at the same time. The crawler needs GitHub credentials and
internet access; the web application needs only the database.

## Schema migrations

Alembic records the schema revision applied to each database. A fresh database
runs every migration in order when `alembic upgrade head` is first executed. A
later deployment runs only migrations newer than its recorded revision. This
allows schema changes to preserve existing production data.

Before the first production deployment, the development migrations may
optionally be squashed into one baseline migration once the schema is stable.
After a database has been deployed, migration history must advance forward and
should not be rewritten.

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
