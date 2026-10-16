# Decisions

This file records decisions that materially shape the MVP, including choices made
autonomously while completing the project.

## 2026-09-29 — Single application repository

The FastAPI server and offline crawler share this repository, Python environment,
and database. A separate client repository or service would add deployment and
coordination overhead without helping the MVP.

## 2026-09-29 — Activity means GitHub `pushed_at`

The user clarified that the four activity buckets are based on GitHub's
`pushed_at`, not `updated_at`. The crawler includes a `pushed:` qualifier
in every GitHub search query and stores both GitHub timestamps. The web filter
uses `github_pushed_at` from the local database.

GitHub's search qualifier is date-granular, while local filtering uses the
returned timestamp. Bucket boundaries can therefore be approximate during
discovery but exact for results served by the application.

## 2026-09-29 — 200 queries per full crawl

Every full crawl performs the Cartesian product of 10 languages, 5 star buckets,
and 4 pushed-time buckets: 200 combinations. This avoids relying on the first
1,000 results of a broad language/star query to contain older repositories.

## 2026-09-30 — `Any` filters are local catalog queries

The web page and public random-repository endpoint offer `Any` for language,
stars, and last-pushed time. It means no SQL restriction for that category; it
does not generate an unbounded GitHub Search request or change the 200 concrete
crawler combinations. The form uses JavaScript to request a result in place and
remove its query string from the displayed URL, while retaining a normal GET
submission as the no-JavaScript fallback.

## 2026-09-29 — Five-second search pacing

`GITHUB_SEARCH_DELAY_SECONDS` remains the sole pacing setting and defaults to
5 seconds. The search client spaces search request starts by at least that
interval. Authentication-token requests are not crawl-search requests and occur
only when the cached token expires.

## 2026-09-29 — Four-cycle GitHub ordering

“Four sort orders” is interpreted as the product brief's four initial modes:
`updated desc`, `stars desc`, `updated asc`, and `stars asc`. One full
crawl uses one mode; the next successful crawl advances to the next mode. The
cycle is kept in configuration rather than selected by the HTTP client.

Open question: if “four sort orders” instead meant GitHub's four sort fields
(`stars`, `forks`, `help-wanted-issues`, `updated`), revise the cycle
before relying on long-running crawl coverage.

## 2026-09-29 — Active/staging generations

Canonical repository metadata is upserted across crawls. Membership is
generation-scoped: users query only the one ACTIVE generation while the crawler
builds a STAGING one. A staging generation is seeded from active membership,
repositories not seen in the preceding 24 hours are removed at successful
completion, and the status swap occurs in one transaction.

Metadata can be refreshed while a staging crawl is running because metadata is
canonical. Membership—the discovery pool exposed to users—never changes until
the atomic generation swap.

A failed request marks that crawl and its staging generation FAILED, leaving the
previous ACTIVE generation untouched. The next sort mode is selected only after
a successfully completed crawl, so a failed run does not consume a rotation
slot. The client makes no general request retries: it preserves failures for
inspection and requires a fresh, complete crawl.

The exception is an explicit GitHub rate-limit response. The client performs a
bounded retry using GitHub's Retry-After header when present, otherwise the
configured 60-second fallback. This pause supplements, rather than replaces,
the five-second minimum between ordinary request starts.

A network or timeout failure receives the same bounded, separately configured
retry treatment. Repository-search GET requests are safe to retry, and every
retry passes through the five-second request pacer before it starts.

## 2026-09-29 — GitHub ID is the only canonical repository identity

The numeric GitHub repository ID is immutable, while an owner/name full name can
be reused after a transfer, rename, or replacement repository. The catalog
schema intentionally permits duplicate historical full names. A generation can
briefly retain an old membership for the
24-hour stale-retention window even when a distinct repository has since taken
the same full name.

## 2026-09-29 — Local-only discovery requests

Both discovery routes select from the local database, constrained by the
requested language, stars, and exact local pushed-at bounds. They join only the
ACTIVE generation; they do not import the GitHub client or make any network
call during discovery.

Before the first successful full crawl, the endpoint returns a clear
not-found response instead of silently falling back to GitHub. This makes the
local-first guarantee visible and keeps user requests predictable.

## 2026-09-29 — Minimal public web surface

The public application exposes only the server-rendered index page and the
random-repository endpoint. Filter options are rendered into the page, while
the page itself submits filters back to the index route. Health, crawler-status,
and filter-options endpoints are not public, and FastAPI's documentation,
ReDoc, and OpenAPI routes are disabled.

The random-repository endpoint remains public by design so it can be used by
the index page or external callers. It exposes local catalog metadata only and
sends a no-store cache header because each call is intended to produce a fresh
random selection.

Crawler progress stays local-only through `python -m app.cli status`. This
keeps the public web surface small while still providing operators with the
latest run's state, request count, and discovered-repository count.

## 2026-09-29 — PostgreSQL from the first development environment

SQLite support was removed before a catalog generation was published. Local
development uses PostgreSQL 17 in Docker Compose with the psycopg driver, and
the container exposes its port only on localhost. The same PostgreSQL-oriented
Alembic migrations and SQLAlchemy behavior now apply in development and
deployment.

Tests use an isolated PostgreSQL test database initialized by Docker Compose.
This exposed and fixed an ordering issue that SQLite's disabled foreign-key
enforcement had hidden: repository rows are flushed before generation
memberships reference them.

## 2026-09-29 — Explicit, checkpointed crawl resume

After each successfully ingested GitHub Search page, the crawler atomically
stores the next work-item index, next page, page count, and an exact fingerprint
of the original search plan. `python -m app.cli crawl --resume` continues the
latest failed crawl only when its plan still matches; it retains the original
date boundaries and sort order, and does not publish the staging generation
until it is complete.

Resume is operator-triggered rather than automatic. This preserves a visible
failed state for inspection, while preventing a network interruption from
discarding hours of successful requests. Crawls that failed before this feature
was deployed lack a durable cursor and cannot be resumed exactly.

## 2026-09-30 — The active catalog rolls forward with a three-day retention window

Starting a crawl copies the active generation's repository memberships into the
new staging generation. Repositories rediscovered by that crawl have their
membership `last_seen` time refreshed. At successful publication, the crawler
removes only memberships not seen during the preceding three days, then atomically
activates the new generation. This avoids dropping repositories merely because a
single GitHub Search crawl did not return them. The default is configured through
`CATALOG_MEMBERSHIP_RETENTION_DAYS`, currently set to three, and is tracked for
review in `ROADMAP.md`.

Production scheduling will start one normal crawl every six hours. Four successful
runs complete the four-ordering cycle once per day. A failed run does not block the
following normal crawl: only a `running` crawl prevents another start, and an
explicit `--resume` remains an operator choice.

## 2026-09-30 — One pre-production migration baseline

The four development-only Alembic revisions were replaced by one
`20260930_01` baseline migration before the first deployment. The existing local
database was stamped to that revision, preserving its tables and catalog data;
new databases create the final schema from the baseline. Future migrations will
advance from this revision and will not rewrite history.

## 2026-09-30 — Retain non-live catalog history for seven days

The active catalog remains a rolling three-day membership view: a successful
publication drops memberships not rediscovered during that window. To preserve a
short operational history and a seven-day window for resuming failed crawls, a
successful publication deletes only `retired` and `failed` generations created
more than seven days ago. `active` and `staging` generations are never deletion
candidates.

Database cascades remove the expired generation's crawl run and membership rows.
The crawler then deletes canonical repository rows with no membership in any
remaining generation. This intentionally makes physical repository-metadata
retention longer than the three-day active-catalog window: metadata remains while
one of the retained historical snapshots references it. Both windows are runtime
configuration values, defaulting to three and seven days respectively.

## 2026-10-16 — One production image, isolated runtime roles

Production will use one immutable application image for FastAPI, Alembic, and
the crawler. Compose runs the web server continuously, but migrations and
crawls are explicit one-shot services. This keeps the deployed application code
identical in every role while preventing a container start or restart from
silently launching a crawler.

Only Caddy publishes host ports. PostgreSQL, the web server, and migrations use
an internal network; the crawler alone additionally receives outbound network
access for GitHub. Production configuration is split into root-owned files
outside the checkout. The GitHub App private key is mounted read-only only in
the crawler service, whose filesystem is read-only and capabilities are dropped.

## 2026-10-16 — CI with disposable PostgreSQL and no production secrets

GitHub Actions runs separate Python quality and PostgreSQL test jobs on Ubuntu
with Python 3.12, matching the production image's Python version. Each job creates
the `.venv` expected by Pyright. The database job uses its own PostgreSQL 17
service and disposable credentials, then checks baseline migration and model
drift after the test fixtures have removed their tables. No production settings,
GitHub App credentials, or live GitHub requests are required.

Use ordinary pull-request/push events, read-only repository permissions, pinned
action commits, and checkout without persisted credentials. Cancel superseded
CI runs. Keep the job names `Python checks` and `PostgreSQL tests` stable for
future branch-protection rules; protection itself is a separate setup step.

## 2026-10-16 — Feature branches and pull requests for every change

The repository is now public. Use short-lived feature branches from `main` for
all future changes, including documentation and maintenance; do not add a
permanent `dev` branch or push directly to `main`. Keep commits SSH-signed with
descriptive bodies, require successful CI, and leave PRs ready for the user's
review and authorization to merge. Preserve the existing requirement for manual
UI verification before committing/pushing UI changes or completing their TODOs.

These instructions live in `AGENTS.md` and the README so future sessions and
contributors follow the same workflow. At the time of this decision, GitHub
reported `main` unprotected and no repository rulesets; configuring protection
is the next step. Production deployment remains tied to a released version and
image digest, separately from merging to `main`.

## 2026-10-17 — Enforce main protection without mandatory second-person approval

The user merged PR #1 after both CI jobs passed and deleted its remote branch.
The merge is valid; the local checkout was fast-forwarded to the merged main
revision, and the merged local feature branch was removed.

Created the active `Protect main` branch ruleset (ID `25266382`) targeting the
default branch, currently `main`. It requires a PR, resolved review conversations,
and successful `Python checks` and `PostgreSQL tests` from GitHub Actions, with
the branch up to date before merging. Deletion and force pushes are blocked.
The bypass list is empty, including for administrators. Required reviewer
approvals are zero so a solo maintainer can merge their own passing PR; agent
instructions still require the user's authorization before an agent merges.

Verified the saved ruleset using GitHub's API and confirmed that `main` reports
`protected: true`. This changes repository settings, not deployment behavior.

## 2026-10-17 — One dependency lock for development, CI, and production

Use uv 0.13.1 and commit its universal `uv.lock` rather than maintaining separate
platform-specific requirement files. Keep the existing dependency ranges and
optional `dev` extra in `pyproject.toml`; the lock records resolved runtime/dev
versions and artifact hashes, while `.python-version` selects Python 3.12.
Initial locking resolves the current compatible versions, which may be newer
than the existing local virtual environment; validate the resulting environment
and images rather than assuming the previous test results still apply.

Local setup and CI use `uv sync --locked --extra dev`. Production installs the
runtime subset without editable installation. `--locked` rejects drift between
the declarations and lock instead of silently choosing new versions. Pin uv in
project configuration, let the SHA-pinned setup action read that version, and
copy the official Docker uv binary from an immutable multi-architecture digest.

Build dependencies are resolved separately, so constrain Hatchling and its full
Python 3.12 build dependency graph to reviewed versions and published wheel
hashes as well. Keep these constraints in project configuration and the lock.
Dependency changes, lock refreshes, and tool updates require their own tested
PRs. This establishes Python dependency reproducibility; pinning the Python,
PostgreSQL, and Caddy images remains a separate tracked task.

