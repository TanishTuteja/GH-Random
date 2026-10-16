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
Memberships unseen for three days are removed from the newly published catalog.
After a successful publish, retired and failed generations created more than
seven days earlier are deleted, cascading to their crawl runs and memberships;
repository rows with no remaining generation membership are then deleted too.
The active and staging generations are never cleanup candidates. The web
application never calls GitHub to satisfy a random-repository request.

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
CATALOG_MEMBERSHIP_RETENTION_DAYS=3
CATALOG_GENERATION_RETENTION_DAYS=7
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

The API accepts `language`, `star_bucket`, and `pushed_bucket` IDs. It also
accepts an optional `exclude_github_id` to prefer a different repository, while
falling back to that ID if it is the only match. For example:

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

Alembic records the schema revision applied to each database. This repository's
pre-production schema begins at the single `20260930_01` baseline migration, so
a fresh database creates the complete catalog schema with one
`alembic upgrade head` command. A later deployment runs only migrations newer
than its recorded revision, preserving existing production data.

The existing development database was explicitly stamped to that baseline when
the development-only history was squashed; no catalog data was rebuilt or
deleted. From the first deployed database onward, migration history must only
advance forward and must not be rewritten.

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

### Production container artifacts

The repository includes a production `Dockerfile`, `compose.production.yaml`,
and `Caddyfile`. They define one image used in three roles: the FastAPI web
server, a one-shot Alembic migration command, and a one-shot crawler command.
Production PostgreSQL, FastAPI, and the migration command share an internal
Docker network. Caddy is the only service with host ports, on HTTP/HTTPS. The
crawler additionally joins a separate outbound network so it can contact GitHub.
pgAdmin is deliberately absent from this stack.

The production Compose file expects root-owned configuration files outside the
repository. The tracked `deployment/*.example` files document their contents;
copy them to `/etc/gh-random/`, replace every placeholder, and restrict their
permissions before first use. Keep the GitHub App private-key PEM at
`/etc/gh-random/github-app-private-key.pem` with mode `0600`. It is bind-mounted
read-only only into the crawler, which has a read-only filesystem, no Linux
capabilities, and no-new-privileges enabled. The web and migration containers
never receive that key.

The `compose.env` file supplies Compose-level values, including the released
application image by immutable digest and absolute paths to the role-specific
environment files. The application and crawler use `database`, the Compose
service name, as the PostgreSQL hostname in `DATABASE_URL`, not `127.0.0.1`.

From a checked-out release directory on the VM, the manual first-deployment
sequence will be:

~~~sh
docker compose --env-file /etc/gh-random/compose.env -f compose.production.yaml pull
docker compose --env-file /etc/gh-random/compose.env -f compose.production.yaml --profile migrate run --rm migrate
docker compose --env-file /etc/gh-random/compose.env -f compose.production.yaml up --detach database web caddy
docker compose --env-file /etc/gh-random/compose.env -f compose.production.yaml --profile crawler run --rm crawler
~~~

Run the initial crawler to completion before announcing the site, then replace
the final manual crawler command with the planned six-hour systemd timer. A
future deployment script will automate the pull, backup, migration, restart,
and smoke-test steps; do not treat the commands above as a substitute for a
tested backup and restore procedure.
