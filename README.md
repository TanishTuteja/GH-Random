# GH Random

Discover a random GitHub repository by language, stars, and recent activity.
[Try GH Random](https://ghrandom.duckdns.org/).

## How it works

~~~text
Browser → FastAPI → PostgreSQL ← crawler → GitHub Search API
~~~

The web app reads a local catalog; only the separate crawler contacts GitHub.
PostgreSQL, web and crawler start independently. Production crawls run every
six hours, while browsing continues from the last successfully published catalog.

Each crawl covers 200 language/star/activity combinations with paced requests.
Successful runs rotate through four sort orders. The active catalog keeps
memberships rediscovered within three days; retired/failed generations expire
after seven days, followed by repository metadata with no remaining references.
See [decisions.md](docs/project/decisions.md) for the lifecycle and ownership policies.

Filters support `Any` and the configured values in
[app/core/filters.py](app/core/filters.py). Discovery avoids the currently shown
repository when alternatives exist and repeats it when it is the only match.

## First-time local setup

Requires the Python 3.12 version in [.python-version](.python-version),
uv 0.13.5, and Docker Desktop or Docker Engine with Compose.
A GitHub App installation/private key is needed only for crawling.

Install uv using its [official instructions](https://docs.astral.sh/uv/getting-started/installation/),
or run this with a patched Python/pip installation and no active virtual environment:

~~~powershell
python -m pip install --user "uv==0.13.5"
~~~

From a fresh checkout's project root, run unchanged. If `.env` already exists,
skip `Copy-Item` to preserve its private configuration:

~~~powershell
uv sync --locked --extra dev
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
~~~

Edit the ignored `.env` before starting services:

- Set a local PostgreSQL password and matching `DATABASE_URL`. URL-encode
  reserved password characters in database URLs.
- For tests, set `TEST_DATABASE_URL` to a separate disposable database, normally
  `gh_random_test`. Tests drop/recreate tables; use the
  [guarded verification instructions](docs/development/DEVELOPMENT.md#verification).
- For crawling, set `GITHUB_APP_CLIENT_ID` and an absolute private-key path in
  `GITHUB_APP_PRIVATE_KEY_PATH` outside the repository. Set the optional
  `GITHUB_APP_INSTALLATION_ID` when the App has multiple installations.

Defaults and optional settings are in [.env.example](.env.example).
Keep credentials, private keys and database backups outside Git.
The commands below run from the project root with the virtual environment active.

## Run locally

Start PostgreSQL and apply migrations:

~~~powershell
docker compose up --detach database
alembic upgrade head
~~~

PostgreSQL binds to `127.0.0.1:5432`. Its named volume survives restarts and
`docker compose down`; adding `--volumes` deletes the data. Fresh volumes also
initialize `gh_random_test`. For older volumes or custom test names, create the
separate disposable test database explicitly without removing the existing volume.

Start web in another activated terminal:

~~~powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
~~~

Open http://127.0.0.1:8000. To populate or refresh the catalog, run:

~~~powershell
python -m app.cli crawl
~~~

A full crawl can take several hours. Until its first successful publication,
there is no active catalog. Starting web never starts a crawl.

Check persisted crawler/catalog state with `python -m app.cli status`. Use
`python -m app.cli crawl --resume` only when deliberately continuing the newest
failed checkpoint; production scheduling starts normal crawls.

See [DEVELOPMENT.md](docs/development/DEVELOPMENT.md) for optional local pgAdmin, dependency
updates, schema changes, ownership/recovery details and verification commands.

## Public API

| Route | Purpose |
| --- | --- |
| `GET /` | Repository discovery page |
| `GET /api/random-repository` | Random repository JSON from the active catalog |

The API accepts `language`, `star_bucket` and `pushed_bucket` IDs, including `any`.
Optional `exclude_github_id` avoids the current repository when alternatives
exist. This local example runs unchanged:

~~~powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/random-repository?language=python&star_bucket=0-100&pushed_bucket=pushed-within-week"
~~~

OpenAPI/docs, health, crawler-status and filter-configuration routes are not public.

## Verification and contributions

Follow the [development guide](docs/development/DEVELOPMENT.md#verification)
for the disposable database preflight, tests, formatting, type checks and
container rehearsals. PRs require
`Python checks`, `PostgreSQL tests` and `Production containers` to pass.

Use short-lived branches and focused PRs. Maintainer commits/releases are signed;
UI changes require manual verification before commit/push or TODO completion.
See [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and [SECURITY.md](SECURITY.md).
Contributions use the [MIT license](LICENSE).

## Releases and deployment

Production uses an AMD64 OCI micro VM and an immutable application image.
Merging, publishing a release and deploying are separate steps:

- [Releases](docs/releases/RELEASES.md): signed tags, publication and verification.
- [Deployment](docs/deployment/DEPLOYMENT.md): deployment/rollback commands, host
  configuration and the index of operator guides.
- [Operations record](docs/operations/OPERATIONS.md#deployed-release): deployed
  version/digest and verified live acceptance.

Use the [documentation index](docs/README.md) for all guides, including
[remaining work](docs/project/TODO.md), the [roadmap](docs/project/ROADMAP.md)
and [design decisions](docs/project/decisions.md).
