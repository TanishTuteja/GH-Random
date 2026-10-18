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

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md), the
[code of conduct](CODE_OF_CONDUCT.md), and [private security reporting](SECURITY.md).

Requires Python 3.12 (selected by `.python-version`), uv 0.13.1, Docker Desktop,
and a configured GitHub App installation and private key. Install the pinned uv
version using the [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/),
or install it with Python from a terminal with no virtual environment active:

~~~powershell
python -m pip install --user "uv==0.13.1"
~~~

Ensure the installed `uv` command is on your PATH (`uv --version`), then run the
following from the project directory:

~~~powershell
uv sync --locked --extra dev
.\.venv\Scripts\Activate.ps1
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

### Dependency locking and updates

`pyproject.toml` declares supported dependency ranges; the committed `uv.lock`
pins the resolved runtime and optional `dev` dependencies, including transitive
packages and artifact hashes across platforms. `uv sync --locked --extra dev`
creates or updates `.venv` to match those pins and installs this project in
editable mode. CI uses the same command. A stale or missing lock fails instead
of being regenerated during an install.

Docker installs the runtime subset with `uv sync --locked --no-default-groups
--no-editable --no-cache`, without the `dev` extra. The uv version is pinned in
`pyproject.toml`; CI reads it there and Docker copies the corresponding official
uv image by digest. Hatchling and its build dependencies have separate pinned,
hash-verified constraints in `pyproject.toml`, also recorded in the lockfile.

After editing dependency declarations, regenerate the lock deliberately and
commit both files in the same PR:

~~~powershell
uv lock
uv sync --locked --extra dev
~~~

To update one package within the declared ranges, use
`uv lock --upgrade-package httpx`, then sync and run the verification checks.
Use `uv lock --upgrade` only for an intentional broader dependency update.
Build-tool updates also require reviewing their constraint versions and wheel
hashes; uv updates must keep `required-version` and the Docker uv digest aligned.
Run `uv lock --check` to check freshness without rewriting the lock.

Dependency locking does not pin operating-system packages or the Python base
image; production base/service image digest pinning is tracked separately in
`TODO.md`.

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
one is running. A PostgreSQL advisory lock now enforces this across cooperating
processes/containers and also excludes online Alembic migrations; a contender
fails promptly rather than waiting indefinitely or issuing GitHub searches.
Browsing and local `status` reads remain available.

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

### Crawl ownership, interruptions, and upgrades

**Before upgrading from a version without catalog locking, stop every old
crawler and migration process.** An older process does not participate in this
protocol, so the new code cannot safely infer its ownership. Do not run old and
new crawler versions together, even if they connect to the same database.

Each crawl retains one physical PostgreSQL connection and a session-level lock
for its whole run. Its short transactions still commit checkpoints normally;
the lock survives commits/rollbacks. Statements and commits verify the original
connection still owns the lock. Losing the connection/lock aborts the old run;
reconnecting cannot silently resume writing, publishing, or marking another
owner's state failed. The connection is physically closed on exit rather than
returning an advisory lock to the connection pool. Use a direct PostgreSQL
connection, not a transaction-pooling proxy, for these operator processes.

After PostgreSQL releases a dead owner's session (for example after process
death, reboot, or connection loss), the next normal or `--resume` crawl can
acquire exclusive ownership. It marks leftover `running` runs and their staging
generations failed, preserving the active catalog and committed checkpoints.
A normal run starts fresh; `--resume` attempts the newest failed checkpoint as
before. Failed/interrupted runs do not advance the successful-sort rotation.
If generation/run state is inconsistent, recovery fails for manual inspection
instead of guessing or demoting an active catalog. No schema migration is
required for this ownership change.

Recovery uses ownership, **not run age**. A live/hung connection can keep its
lock until it exits or PostgreSQL detects its disconnection; elapsed time alone
does not authorize takeover. `status` shows persisted state, not proof that a
process is alive. Do not clear a running row in pgAdmin to force concurrency.
Inspect the actual process/container and database activity first. A disconnected
old crawler may finish an in-flight GitHub request, but its guarded database
connection cannot checkpoint/publish the response. The production supervisor
and timer below add Docker-child cleanup, runtime limits, and deployment
exclusion. Installation on the VM and failure-alert delivery remain separate.

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

Online Alembic commands acquire the same catalog lock as the crawler, including
schema checks/introspection through Alembic's online environment. If a crawl or
another migration owns it, the command fails before catalog DDL; retry after the
owner finishes rather than clearing its database row. The migration's statements
and commits use the guarded owning connection. Offline `--sql` generation does
not connect or lock anything: manually executing that SQL, editing tables in
pgAdmin, or using old operator code bypasses this cooperative protection.

## Verification

~~~powershell
python deployment/install_age.py --destination build/age-tools
pytest
ruff format --check app tests alembic .github/scripts deployment
ruff check app tests alembic .github/scripts deployment
pyright
~~~

The test suite covers query-plan coverage, five-second request pacing,
GitHub-App authentication behavior, repository mapping, generation publishing,
failure retention, random filtering, the API, and the rendered start page.

### Continuous integration

[CI](https://github.com/TanishTuteja/GH-Random/actions/workflows/ci.yaml) runs on
pull requests and pushes to `main`, and can also be started from GitHub's Actions
tab. It runs three independent jobs on Ubuntu with Python 3.12:

- `Python checks`: Ruff formatting/linting (including Alembic, CI, and deployment scripts)
  and Pyright.
- `PostgreSQL tests`: the complete test suite against a fresh PostgreSQL 17
  service, followed by `alembic upgrade head` and `alembic check` to verify the
  baseline creates the schema and matches the models.
- `Production containers`: build the production image on native ARM64, validate
  production Compose and the HTTPS Caddy configuration, migrate a fresh database,
  check schema drift and the hardened crawler's local status command, then test
  the packaged web app through Caddy. Verify empty/seeded catalog discovery,
  proxy headers, the non-root runtime, exclusion of dev/build packages, and
  disabled documentation/health routes. Rehearse a real dump/restore into a new
  database, compare schema/catalog counts, verify discovery against the restored
  copy, and reject corrupt archives/existing targets. No live crawl or GitHub key
  is needed. Also verify packaged crawler exclusion and abandoned-run recovery
  using an interrupted fake one-combination crawl with no GitHub API traffic.
    Then exercise the actual deploy CLI against synthetic, locally built digest
    releases: bootstrap/redeploy, migration exclusion, controller EOF/idle expiry,
    failed DDL, failed public smoke tests, and same-schema rollback with preserved
    catalog data. Nothing is published to GHCR during these rehearsals.
    Test the scheduled supervisor with failing, hanging, and successful fake
    crawls, real Docker cleanup and killed-host recovery. Linux CI also exercises
    temporary native systemd units with the production sandbox: scaled timer
    failure/next-tick success, service stop, killed supervisor, and runtime limit.

CI installs the pinned uv tool and creates its virtual environment from
`uv.lock` using `uv sync --locked --extra dev`, with disposable database credentials.
It needs no repository secrets, GitHub App key, production database, or real
GitHub API requests. Both database URLs point to the disposable test database;
test fixtures recreate its tables. Never point the tests at a database you want
to keep. The workflow has read-only repository permissions and pins actions to
commit hashes. Superseded runs for the same PR or branch are cancelled.

The active `Protect main` ruleset requires `Python checks`, `PostgreSQL tests`,
and `Production containers` from GitHub Actions and an up-to-date branch before merging.
It requires pull requests and resolved conversations, blocks force pushes and
branch deletion, and has no bypass actors. Zero mandatory reviewer approvals
allows the solo maintainer to merge their own PR after CI passes.

Run the container check locally with Docker running:

~~~powershell
python .github/scripts/check_containers.py
~~~

The host script needs only Python's standard library and Docker Compose; it
builds for the Docker daemon's default architecture. Each run uses a unique
`gh-random-ci-*` project, the tracked production configuration examples, and a
plain-text placeholder instead of a real GitHub key. It does not read the local
`.env`. The small `.github/compose.ci.yaml` overlay removes host port bindings
and uses HTTP internally, avoiding public DNS or certificate requests. The
production HTTPS configuration is validated separately; live certificate
issuance/renewal still needs deployment verification.

On failure, the script prints service status/logs. It cleans up its own test
containers, volumes, networks, and image on exit, preserving development and
production data. CI runs the same check on an ARM64 runner to match the VM.

### Database backup and restore rehearsal

`deployment/database_backup.py` runs on the **host** using Python 3.12's standard
library and Docker Compose. It uses `pg_dump`/`pg_restore` from the running
database container, not an extra database service or host PostgreSQL install.
The Compose env file and project are explicit; passwords remain inside the
container rather than appearing in host commands or backup metadata.

On the Linux VM, from the matching release directory, create a private backup
directory and take a snapshot:

~~~sh
sudo install -d -m 0700 /var/backups/gh-random
sudo python3 deployment/database_backup.py --env-file /etc/gh-random/compose.env backup --output-dir /var/backups/gh-random
~~~

The command prints the completed backup directory. It contains a compressed
custom-format `database.dump` and `manifest.json` with its size, SHA-256, source
database, PostgreSQL version, and completion time. On Linux, directories must
belong to the current user with no group/other access; new directories/files
use `0700`/`0600`. Dump/manifest writes are synced before an atomic rename marks
the backup complete. A failed/interrupted backup remains `.partial-*` and is
never accepted as complete. Inspect and remove such partials deliberately;
there is no automatic backup pruning yet.

The dump is a consistent database snapshot even during ordinary writes. Counts
are checked on restore, not recorded with a separate live query that might race
the dump. This does not replace migration/crawler coordination or a deployment
lock. See PostgreSQL's [pg_dump documentation](https://www.postgresql.org/docs/17/app-pgdump.html).

Rehearse restoring a **trusted** completed backup into a new database, replacing
the example timestamp/suffix with the directory printed above:

~~~sh
sudo python3 deployment/database_backup.py --env-file /etc/gh-random/compose.env restore --backup /var/backups/gh-random/gh-random-20261017T000000Z-012345abcdef --target-database gh_random_restore_rehearsal --trust-backup
~~~

Restore checks private paths, completeness, size/checksum, archive readability,
and PostgreSQL major-version agreement **before** creating the target. The name
must start with `gh_random_restore_`; the live database and every existing target
are refused. Restoration uses an empty `template0` database and one transaction,
without `--clean`, drops, or automatic migration downgrades. The command reports
the Alembic revision, table counts, active memberships, and running crawl rows.
A database without the application schema/revision is not certified as a usable
catalog restore. CI additionally checks the restored copy through the app/API.

The restored database remains available for inspection; this tool never deletes
it, overwrites production, or switches the app to it. On failure it may remain
empty, partially inspected, or restored but unverified. Diagnose before retrying
with another name; never resolve an existing-target error by dropping a live DB.
`--timeout-seconds` defaults to 1,800 per PostgreSQL command. A Docker client
timeout does not guarantee its in-container process stopped: inspect activity
before any manual cleanup. Interrupted crawl rows may also exist in a snapshot;
their safe recovery and promotion of a restored DB are separate deployment work.

Only restore dumps from a source you trust: PostgreSQL dumps can execute code,
and a matching checksum is not proof of authorship. The archive excludes cluster
roles/passwords, host configuration, GitHub keys, and Caddy certificates; preserve
those separately through secure recovery procedures. Restore applies the current
database user as owner and omits grants. Cross-major upgrades and untrusted imports
are intentionally outside this rehearsal tool. See [pg_restore's security warning](https://www.postgresql.org/docs/17/app-pgrestore.html).

For a manual development backup in PowerShell, with the development database up:

~~~powershell
python deployment/database_backup.py --compose-file compose.yaml --env-file .env backup --output-dir backups
~~~

Use the same `--compose-file compose.yaml --env-file .env` options for a local
restore rehearsal. Windows users must secure the backup folder using Windows
ACLs; this tool enforces POSIX ownership/modes only on Linux. Never commit or
upload plaintext backups. `backups/`, `*.dump`, and real `*.env` files are ignored
by Git and the Docker build context; keep production backups outside the repo.

Those manual commands produce local **unencrypted** snapshots. For production,
use the [encrypted backup and recovery guide](deployment/BACKUPS.md): daily
05:15 UTC scheduling, seven-day/minimum-two-copy local retention, public-key
encryption, verified OCI upload/read-back, safe offline recovery and email hooks.
Its live OCI/key/VM setup and production VM-loss rehearsal remain operator work.
No timer is installed/enabled by the development commands above.

### Change workflow

Use a short-lived feature branch from the latest `main` for each change,
including documentation and maintenance. Push signed commits with descriptive
bodies and open a pull request targeting `main`. All three CI jobs must pass before
merge; leave changes ready for review and merge with the maintainer's approval.
Delete the feature branch after merging. Do not push directly to `main` or
maintain a permanent `dev` branch.

UI changes also need the maintainer's manual verification before committing,
pushing, or marking their TODO complete. Merging code does not deploy it;
production releases use signed version tags and immutable image digests.
See `AGENTS.md` for repository instructions followed by coding agents.

## Deployment notes

See [TODO.md](TODO.md) for completed work, remaining deployment setup, and the
first-release acceptance checklist.

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

Deploy from the extracted, verified release archive using the script described
below. Starting web never starts a crawl. After deployment, run the initial
crawler explicitly from that same release directory:

~~~sh
docker compose --env-file /etc/gh-random/compose.env -f compose.production.yaml --profile crawler run --rm crawler
~~~

Run the initial crawler to completion before announcing the site, then replace
the manual crawler command with the six-hour systemd timer below. Actual VM
installation, alerts, and off-site backup setup remain separate deployment work.

### Creating a release (not deploying it)

Merging a PR does not publish an image or change production. When a release
candidate is ready, first merge it into `main` and wait for all three **main-push**
CI jobs to pass. PR checks alone do not qualify the merge commit for release.
Keep `project.version` in `pyproject.toml` aligned with the intended stable tag;
version changes also need `uv lock` and the ordinary PR process.

From an up-to-date, clean local `main`, create an annotated, signed tag:

~~~powershell
git switch main
git pull --ff-only
git status --short
git tag -s v0.1.0 -m "GH Random v0.1.0"
git push origin v0.1.0
~~~

`git tag -s` uses the configured SSH signing key, just like our signed commits.
The signing public key must be registered on GitHub and the tag must show
**Verified** there. Local `git verify-tag v0.1.0` additionally requires an
appropriate SSH allowed-signers file; a missing local trust file does not mean
GitHub rejected the signature. Never create the tag through the release UI,
which would bypass this signing step. Protect `v*` tags against updates/deletion
before the first release; that repository setting is still a deployment TODO.

In GitHub → Releases → Draft a new release, select the **existing** `v0.1.0`
tag, write release notes, leave prerelease unchecked, and publish. The `Release`
workflow then:

1. Requires a stable `vMAJOR.MINOR.PATCH` tag matching the package version, an
   annotated signature verified by GitHub, and a commit belonging to `main`.
2. Requires successful `Python checks`, `PostgreSQL tests`, and `Production
   containers` in the latest main-push CI run for that exact commit. A pending,
   cancelled, failed, or skipped required check stops publishing.
3. Builds `linux/arm64` on a native ARM runner and publishes
   `ghcr.io/tanishtuteja/gh-random:v0.1.0` using the temporary `GITHUB_TOKEN`.
   It adds source/revision/version/license labels and BuildKit provenance/SBOM.
   Initial releases are ARM64-only, not AMD64 images for Windows PCs.
4. Pulls the **published digest**, then runs the isolated production Compose,
   migration, crawler-status, and web/API checks against that exact image.
5. Attaches `image-digest.txt`, `release.json`, `SHA256SUMS`, and
   `gh-random-v0.1.0-deployment.tar.gz` only after successful smoke tests.
   The archive contains matching Compose/Caddy files, safe configuration
   templates, deploy/backup scripts, README/license, and release metadata.
   Metadata includes SHA-256 hashes for every packaged deployment file.
   Its Compose template already
   pins the released image digest; no production secrets are included.

A release is ready to deploy only when this workflow is green **and** all four
assets exist. GitHub's published-release event precedes the image build, so a
visible release alone is not a readiness signal. A failed smoke test may leave
an image in GHCR without deployment assets; do not deploy it. No production
deployment is triggered by this workflow. Never move a successful version tag,
republish its image, or replace its assets. A rerun refuses existing deployment
assets rather than silently overwriting a completed release. If the final
asset upload partially fails, inspect it and deliberately remove only those
incomplete generated assets before retrying; do not do this for a usable release.

We choose **public GHCR images** so the VM needs no registry credential. GitHub
initially creates packages as private even for public repositories. After the
first publish, open the package's settings, change its visibility to public,
and verify an anonymous digest pull with a clean Docker configuration before
deploying. Do not copy the Actions token onto the VM. Publishing uses GitHub's
[documented GHCR workflow authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry);
package visibility/anonymous access still need that first-release verification.

On the Linux VM, download these four release assets into a new version-specific
directory. Verify before extracting (checksums detect corruption, not a malicious
replacement of both assets and checksums):

~~~sh
sha256sum --check SHA256SUMS
tar -xzf gh-random-v0.1.0-deployment.tar.gz
cat image-digest.txt
cat release.json
~~~

Use the archive's files rather than a checkout of moving `main`. Review/copy
`deployment/compose.env.example` to `/etc/gh-random/compose.env`, retaining the
exact digest and existing secret paths; never overwrite live role credentials
with example passwords. Use the deployment script below. The Python dependencies
are locked, but the Python,
PostgreSQL, and Caddy base-image digest pins are still tracked separately.

### Deploying a verified release

The host needs Python 3.12+, Docker Engine, and Docker Compose. No host virtualenv,
PostgreSQL client, or Docker socket mounted into application containers is needed.
Use a trusted release's script, not one downloaded separately from moving `main`.
Keep each extracted release at a stable, version-specific absolute path, for
example `/opt/gh-random/releases/v0.1.0`. Retain previous directories/images for
rollback; do not edit packaged files. The script checks their manifest hashes,
image digest, source/revision/version labels, Linux architecture, configuration
paths, database credentials, and Caddy configuration before migrations.
Hashes detect accidental edits/mixed releases, not malicious publishers: verify
the trusted GitHub release and outer `SHA256SUMS` before executing any script.

Create `/etc/gh-random` with mode `0700`, and its configuration files and PEM with
mode `0600`, owned by the deployment user (root in these examples). All five
configuration/key paths in `compose.env` must be explicit absolute paths outside
the release directory; no variable interpolation in these path values. Use one
flat `GH_RANDOM_IMAGE=...` assignment. Secret-bearing files stay separate from
immutable release files. Keep using the same project name (`gh-random` by default),
state directory, and backup directory across every release.

First deployment, after configuring DNS/firewalls, private env files, and the
SMTP configuration described under email alerts below:

~~~sh
sudo python3 /opt/gh-random/releases/v0.1.0/deployment/deploy.py apply \
  --env-file /etc/gh-random/compose.env \
  --state-dir /var/lib/gh-random/deployments \
  --backup-dir /var/backups/gh-random \
  --backup-config /etc/gh-random/backup.json \
  --alerts-config /etc/gh-random/alerts.json \
  --url https://YOUR_HOSTNAME \
  --bootstrap
~~~

The default release directory is the directory containing the script. For later
releases, invoke the new release's script with the same paths and **omit
`--bootstrap`**. It performs this sequence:

1. Take a host kernel lock; validate the release/configuration and cache the exact
   image digest. A busy deployment fails promptly rather than waiting indefinitely.
2. Start PostgreSQL only for an explicitly bootstrapped first deployment. Existing
   DB/proxy image or database-credential changes require separate maintenance;
   normal deployment never recreates the database or deletes volumes.
3. Hold the database's catalog ownership lock across backup, migration, web/proxy
   restart, public smoke tests, and recording the verified release. An existing
   crawler/migration owner causes a clear failure **before backup or DDL**. New
   cooperating crawlers/migrations are excluded while deployment holds ownership.
4. Create a consistent, checked local backup **before** running Alembic. Even
   bootstrap takes an empty-database backup before creating the schema.
5. Run `alembic upgrade head` once on the owning connection and check schema drift.
   Recreate web/Caddy using the cached release/configuration; expect brief downtime.
6. Check HTTPS `/` and `/?discover=true`, including a database-backed page render.
   TLS verification stays enabled and redirects are refused. Empty catalogs are
   valid before the first crawl; no new public health endpoint is added.
7. Atomically update only `GH_RANDOM_IMAGE` in the external Compose env and write
   `current.json` with the verified version, commit, digest, release directory,
   and previous verified release. Release catalog ownership on exit.

The state directory contains private per-attempt JSON journals and command logs,
plus `current.json` and a persistent `deploy.lock` file. The latter is a kernel
lock, not a stale-file sentinel: **do not delete it** to force another deployment.
Logs/backups can contain sensitive data. Protect them. Deployment does not install
timers; `--backup-config` seals and verifies off-site copies before DDL, while the
daily backup worker owns retention. Omitting that policy retains the explicit
plaintext development path. Email is opt-in through
`--alerts-config /etc/gh-random/alerts.json`; configure and test it as described
below before production deployment. Without that flag, deploy does not send mail.

`--timeout-seconds` defaults to 1,800 per operational command/controller idle
period and bounds migration statements. `--smoke-timeout-seconds` defaults to 60.
An encrypted backup policy extends the controller's idle budget for its bounded
transfer phases, without extending the SQL statement timeout.
Increase deliberately for slow backups/migrations. SIGINT/SIGTERM and normal
failures clean up the uniquely named coordinator container, releasing its DB
lease. If the host is killed abruptly, controller EOF/idle expiry releases it;
PostgreSQL must detect a lost connection before another owner proceeds. Inspect
the recorded container and database activity after a daemon/network outage.
Never kill an unknown crawler or remove its lock just because it is old.

A failed attempt exits nonzero and records its last phase. There is **no automatic
image rollback, schema downgrade, database restore, or volume deletion**. A failure
before web restart leaves web untouched; a later smoke failure can leave the new
image running while `current.json` still identifies the last verified release.
Inspect the journal, actual containers, schema, and backup before recovery. After
fixing a transient/configuration issue, rerun `apply` against the intended release;
Alembic respects already-applied revisions. Failed migrations may need a reviewed
forward fix. Do not assume every future migration is transactionally reversible.

The Compose env and state pointer are individually atomic, not one cross-file
transaction. If interrupted during `record_success`, reconcile both with runtime.
A cleanup error at `release_catalog` can report failure after a verified release
has already been recorded; it does not mean the previous image is still running.
Scheduled crawls fail promptly on contention and try normally at the next slot;
this script does not
disable a timer, so there is no disabled schedule to forget to re-enable.

### Explicit image rollback

Keep the current script/release directory, state directory, and the previous
release's files and image. Review compatibility, then run:

~~~sh
sudo python3 /opt/gh-random/releases/v0.1.1/deployment/deploy.py rollback \
  --env-file /etc/gh-random/compose.env \
  --state-dir /var/lib/gh-random/deployments \
  --backup-dir /var/backups/gh-random \
  --backup-config /etc/gh-random/backup.json \
  --alerts-config /etc/gh-random/alerts.json \
  --url https://YOUR_HOSTNAME \
  --confirm-schema-compatible
~~~

Rollback selects `current.json`'s previous **verified** release and its matching
Compose/Caddy files. It takes another backup, requires the live Alembic revision
to equal the old image's head, and never runs upgrade/downgrade. Equal revisions
are necessary but not sufficient for application/data compatibility, hence the
explicit acknowledgement. Schema-changing rollback is refused; use a reviewed
forward fix or separate restore/recovery plan. After a failed deployment, the last
good release is still `current.json` itself: re-applying that release may be the
appropriate recovery, rather than going back one additional verified release.

Local/ARM64 CI rehearses these paths with synthetic releases and disposable data.
The rehearsal uses Docker's containerd image store for local digest-addressable
images and a loopback-only HTTP Caddy port; production defaults remain HTTPS.
CI configures that store using Docker's
[documented Actions setup](https://docs.docker.com/build/ci/github-actions/multi-platform/).
`--allow-loopback-http` is solely for explicit literal-loopback rehearsals, never
a way to disable TLS for a public hostname. Live OCI/DNS/TLS, first GHCR publish,
off-site recovery, timer installation, and GitHub deployment recording still
require their separate acceptance checks.

### Six-hour scheduled crawler (Linux/systemd)

Release archives include `deployment/crawler.py` and the matching
`deployment/systemd/gh-random-crawler.{service,timer}`. These are host-side tools,
not another permanent container. Install them **after a successful deployment**
and the first catalog crawl. The installed bootstrap reads the private deploy
`current.json`, checks the release supervisor's hash/identity, and executes that
release's supervisor with its matching Compose/deploy helpers. It does not pin
an old image or read deployment files from moving `main`. Future successful
deployments/rollbacks therefore select the appropriate crawler automatically.
Before the enable/start commands below, install and test the email handler and
monitor from the next section; the crawler unit depends on that failure handler.

Default schedule: 00:00, 06:00, 12:00, 18:00 **UTC**, which is **05:30, 11:30,
17:30, 23:30 IST**. It is independent of the VM's local timezone. `Persistent=true`
produces one catch-up activation after missed slots while powered off, not one
queued crawl for every missed slot. A timer does not start another instance of
an already active service. The exact semantics are documented by
[systemd.timer](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml).
Four daily successful runs cycle through the four existing sort orders; failures
do not advance rotation, so all four are not guaranteed on a day with failures.

From the trusted, extracted, successfully deployed release directory:

~~~sh
sudo install -Dm0644 deployment/crawler.py /usr/local/lib/gh-random/crawler.py
sudo install -Dm0644 deployment/systemd/gh-random-crawler.service /etc/systemd/system/gh-random-crawler.service
sudo install -Dm0644 deployment/systemd/gh-random-crawler.timer /etc/systemd/system/gh-random-crawler.timer
sudo systemd-analyze verify /etc/systemd/system/gh-random-crawler.service /etc/systemd/system/gh-random-crawler.timer
sudo systemctl daemon-reload
sudo systemctl enable --now gh-random-crawler.timer
~~~

These commands intentionally **enable real crawling**; implementation/tests do
not install anything on your computer or VM. Only enable the timer, not the service
at boot. Python at `/usr/bin/python3` must be 3.12+, Docker must already be running,
and the private `/var/lib/gh-random/deployments` and `/etc/gh-random/compose.env`
must be the same ones used for deployment. The service defaults to project
`gh-random`. For custom paths/project names, change both ExecStart/ExecStopPost
and ReadWritePaths consistently in a reviewed systemd override. Retain all
referenced release directories and images.

To update the installed bootstrap/unit policy in a later release, first stop
**both** the timer and service, finish the intended deployment, copy the matching
files again, verify/reload them, then enable/start the timer again. Deployment
does not silently overwrite host service configuration or enable new schedules.
Changing only application code does not require reinstalling unchanged units;
the bootstrap selects the verified release's worker each time. Review bootstrap
protocol/unit changes as part of release operations.

~~~sh
sudo systemctl list-timers gh-random-crawler.timer
sudo systemctl status gh-random-crawler.service
sudo journalctl -u gh-random-crawler.service --since today --follow
sudo systemctl start gh-random-crawler.service
sudo systemctl stop gh-random-crawler.timer gh-random-crawler.service
~~~

Stopping only the timer does **not** stop a crawl already running. Stopping the
service sends the supervisor SIGTERM; it stops its exact labelled Docker child
(30-second grace, then Docker kills it), removes it, and releases host ownership.
ExecStopPost is an additional cleanup path if the host supervisor is SIGKILLed.
It checks systemd invocation identity and Docker labels/ID, not just a name,
and will not kill unrelated/manual crawlers or a successor activation. Docker
owns container processes, so killing only the Docker CLI is not sufficient;
see [Docker's stop behavior](https://docs.docker.com/reference/cli/docker/container/stop/).

The supervised crawl has a five-hour execution limit; systemd adds an independent
five-hour-five-minute service limit and a 90-second shutdown limit. The service
uses `Type=exec` because RuntimeMaxSec does not bound `Type=oneshot` in the same
way; see [systemd.service](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml).
The runtime limit is a safety policy, not a promise that every large/rate-limited
crawl fits. Review it using measured production durations before changing it.

Every activation invokes a **normal** `python -m app.cli crawl`, never `--resume`,
with the cached verified digest and no automatic container/DB startup or image
pull. A failed, interrupted, or timed-out attempt leaves the current catalog
available, exits nonzero, cleans its managed child, and does not disable the
timer. The next activation starts fresh; the existing database ownership rules
recover abandoned running/staging records after the old connection is gone.
There is no immediate retry loop. Existing manual crawls/migrations still contend
for the database lock and are never evicted by elapsed age.

The supervisor holds the same host lock as deployment throughout its managed
crawl. Stop it deliberately before an urgent deployment, or let it finish;
contention fails promptly. A failed deployment leaving runtime ahead of
`current.json` causes scheduled crawling to refuse the mismatched stack until
deployment is recovered, rather than running stale crawler code against it.

`/var/lib/gh-random/deployments/crawler.json` records the latest managed
invocation, digest, container name/ID, status, exit code when observed, timestamps,
and cleanup outcome. Keep it private. If Docker is unreachable, cleanup cannot be
guaranteed: the unit fails and leaves the exact ownership record for inspection.
A later supervisor, after obtaining the host lock, cleans only that recorded
orphan before starting another child. Do not delete this state to force a run.
Use the standalone installed bootstrap's `stop --state-dir ...` only as an
explicit operator recovery action; it works without loading application code.

The root service is sandboxed and uses an empty runtime Docker configuration,
not an administrator's registry credentials. Docker access remains root-equivalent;
these settings are defense in depth, not a security boundary around untrusted code.
Crawler Docker logs rotate at 10 MB × 3 and stream into the journal. Global
journald/disk retention still needs VM configuration. Error/timeout states are
observable. Install/configure the email handler and hourly monitor below before
enabling production crawling. Live VM timer installation, reboot behavior, real
GitHub authentication, and inbox delivery remain acceptance tasks.

### Email failure alerts and missing-success monitoring

Use `deployment/notifications.py` (host Python 3.12+, standard library only) for
SMTP-over-TLS alerts. Nothing contacts an email provider until explicitly invoked
with your private configuration. CI captures mail on loopback only; it cannot
verify your provider's permissions, quotas, spam filtering or real inbox delivery.

Copy deployment/alerts.json.example to **/etc/gh-random/alerts.json**, outside the
repository/release directory, owned by root with mode 0600. Replace the SMTP
host, port, username, sender and recipient addresses. Put the SMTP credential in
**/etc/gh-random/smtp-password**, also root-owned mode 0600; never put it in a
command line, Git, Docker env, unit file or chat. Use a provider-issued SMTP/app
credential, not a general account password where the provider supports one.
Confirm the provider's free allowance and permitted sender before enabling it.

`smtp_security` accepts only `ssl` (usually port 465) or `starttls` (usually 587).
Certificate and hostname verification are mandatory; STARTTLS never falls back
to plaintext. `smtp_ca_file` is an optional absolute private CA file for an
explicitly trusted private relay, not a verification bypass. For a trusted relay
without SMTP AUTH, omit/empty **both** username and password-file fields. Use
1–5 recipient addresses. See [Python SMTP](https://docs.python.org/3.12/library/smtplib.html)
and [TLS contexts](https://docs.python.org/3.12/library/ssl.html#ssl.create_default_context).

Initial policies in alerts.json are configurable: repeat unresolved alerts no
more than every **six hours**, warn after **12 hours** without a successful crawl,
and (when enabled) after **36 hours** without a successful daily backup. These
are initial operational choices, not measured SLAs. The hourly UTC :15 check runs
at local :45 in IST. A missing first success receives the configured grace period
starting at the first monitor invocation; an existing success is aged immediately.
The first real crawl should complete before launch. `monitor_backups` defaults
false until scheduled backup automation is installed; do not imply backup
protection merely because its alert policy exists.

From a trusted, extracted release, after preparing the private configuration:

~~~sh
sudo install -Dm0644 deployment/notifications.py /usr/local/lib/gh-random/notifications.py
sudo install -Dm0644 deployment/systemd/gh-random-alert@.service /etc/systemd/system/gh-random-alert@.service
sudo install -Dm0644 deployment/systemd/gh-random-monitor.service /etc/systemd/system/gh-random-monitor.service
sudo install -Dm0644 deployment/systemd/gh-random-monitor.timer /etc/systemd/system/gh-random-monitor.timer
sudo python3 /usr/local/lib/gh-random/notifications.py test --config /etc/gh-random/alerts.json
~~~

That **test command sends a real email**. Verify its arrival (including spam) and
inspect provider rejection/bounce information before proceeding. Verify/reload
the matching crawler/alert/monitor units, then enable the monitor timer:

~~~sh
sudo systemd-analyze verify /etc/systemd/system/gh-random-alert@.service /etc/systemd/system/gh-random-monitor.service /etc/systemd/system/gh-random-monitor.timer /etc/systemd/system/gh-random-crawler.service /etc/systemd/system/gh-random-crawler.timer
sudo systemctl daemon-reload
sudo systemctl start gh-random-monitor.service
sudo systemctl enable --now gh-random-monitor.timer
sudo systemctl list-timers gh-random-monitor.timer
sudo journalctl -u 'gh-random-alert@*' -u gh-random-monitor.service --since today
~~~

Install the handler **before** enabling the crawler timer: the crawler now has
an OnFailure template hook, including supervisor timeout/SIGKILL and preflight
failures. Use systemd 251+ (for example Ubuntu 24.04) so the handler receives the
original failure's invocation/result snapshot instead of racing the next run;
see [systemd's monitor variables](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).
The standalone `unit-failure --unit gh-random-crawler.service` command instead
reads the current unit state and is only for deliberate operator diagnostics.
For nonstandard unit names set crawler_unit/backup_unit/monitor_unit in the same
private JSON and adjust the unit paths/commands consistently.

Deploy and rollback should pass `--alerts-config /etc/gh-random/alerts.json` in
addition to their existing arguments. Configuration is checked before deployment
mutations. Notifications run **after** deployment ownership is released, in a
separate child bounded to 60 seconds; delivery failure cannot turn a successful
deployment into a failed one or hold the database lease. SIGKILL, power loss or
failure before a notifier starts can still prevent a deploy email. Do not infer
that lack of an email proves a deployment succeeded; inspect its journal and
current.json, and monitor the deployment workflow outcome separately.

Alerts contain whitelisted run/deploy IDs, phase/status, release tag and unit
result/invocation, plus diagnostic commands—not raw logs, SQL, database URLs,
SMTP credentials or the GitHub key. Exceptions from the mail provider are
redacted in public command output. Messages are considered sent only after SMTP
acceptance, not guaranteed inbox delivery. Delivery is **at least once**, not
exactly once: a crash after acceptance or partial recipient rejection can cause
duplicates on retry. A successful later operation clears its incident and sends
one recovery email only if a failure was previously accepted.

Private /var/lib/gh-random/alerts contains the durable alert ledger, monitor
heartbeat and kernel lock. Do not delete the lock to force delivery. Failed SMTP
attempts remain pending for the next hourly check, with no restart/alert loop.
A separate crawler-success.json watermark survives later failed runs; only a
successful child **and** cleanup update it. Older successful state cannot clear
a newer preflight failure. The watermark tracks **managed** crawls: direct manual
CLI/Docker crawls do not update it. For a monitored manual activation, use
`systemctl start gh-random-crawler.service` after deployment.
The mailer has no OnFailure hook; the monitor's own
failure can alert once through the same bounded handler, without recursion.
Unit sandboxing limits filesystem writes to alert state and masks the standard
Docker socket and app-secret paths. Review those masks if using custom paths.
Root identity and remaining host access still make this defense in depth, not an
untrusted-code sandbox. Install/upgrade the host script and units deliberately
from matching release files; deployments do not silently change host policy.

An **independent external monitor is still required** for public HTTPS downtime,
missed monitoring/backup heartbeats, and complete VM/network/mail-provider failure.
The on-host timer cannot report its own powered-off VM or deliver through a dead
mail provider. Configure that external service and prove its email delivery before
public launch; no external monitor or live SMTP provider is provisioned here.

The same hourly monitor can warn about free disk bytes, free-space percentage
and (on Linux) free inode percentage. In private alerts.json, `disk_paths` accepts
up to three existing **local** filesystem paths; the example checks `/`. Include
`/var/lib/docker` and `/var/backups/gh-random` after provisioning if they use other
mounts. Initial thresholds are 2 GiB **or** 10% free space, or 5% free inodes.
Missing/inaccessible paths are failures, not healthy. Emails identify paths by
their zero-based configuration index, without exposing arbitrary host paths;
keep the order stable while handling incidents. Removing/disabling a check is
not evidence that a previously reported incident recovered.

Set `tls_host` to the exact lowercase public hostname (no scheme, port or path)
after DNS/HTTPS works. Null disables this probe during bootstrap. It verifies
the live port-443 certificate using SNI, hostname checks and system trust, warns
at 14 days remaining by default, and treats certificate/connectivity/probe
failures as incidents. The child probe bounds DNS plus handshake to 20 seconds;
it does not trust an expired/self-signed certificate or inspect Caddy's private
key files. Disk/TLS incidents use the same pending-delivery ledger, cooldown and
recovery emails. These are host-local warnings, not an external uptime check,
and low-disk alerts can themselves fail if no space remains to write state.
Install the updated notifier from a matching release deliberately, start the
monitor manually, and test real alerts/recovery before relying on it.

### Production runtime hardening and maintenance

Use the [production operations runbook](deployment/OPERATIONS.md) for incident
triage, stuck crawls, failed migrations, disk pressure, credential rotation,
host maintenance, safe image cleanup and VM replacement. Its live acceptance
steps still need the real deployment; no production host is configured by CI.

Optional production database browsing uses a separate, loopback-only admin
overlay. See [private production pgAdmin](deployment/PGADMIN.md) for credentials,
Bastion/SSH forwarding and service-specific start/stop commands. Normal deploys
never start pgAdmin, and neither pgAdmin nor PostgreSQL should be public.

Set `APP_ALLOWED_HOSTS` in the private **app.env** to a JSON list of exact,
lowercase public hostnames, for example `APP_ALLOWED_HOSTS=["example.com"]`.
Do not include a scheme, path, port, or wildcard. Match `GH_RANDOM_DOMAIN` in
caddy.env; deployment preflight refuses a missing/nonmatching allowlist. Local
defaults allow localhost, loopback IPs and TestClient's testserver, not arbitrary
public names. Unknown Host headers receive HTTP 400, without a www redirect.

Caddy alone publishes host ports. Web joins only the internal `proxy` and
`backend` networks, not the internet-facing `edge` network. Production Uvicorn
explicitly accepts forwarded scheme/client-address headers from its private
peers (`--proxy-headers --forwarded-allow-ips=*`). This trusts **all reachable
Docker peers**, not cryptographically only Caddy: do not publish port 8000,
attach untrusted containers to these networks, or reuse this command for a
publicly exposed Uvicorn server. Docker/root operators and app/database peers
are within this trust boundary. Caddy is the first public proxy and
[ignores untrusted incoming forwarded values](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#defaults).
Adding a CDN/load balancer requires a separate review of its exact trusted ranges;
never blindly trust internet-supplied X-Forwarded-* headers. Container smoke tests
check public spoof rejection and trusted-peer HTTP/HTTPS redirect schemes. Verify
real HTTPS redirects and client addresses on the VM too.

Web, migrations and crawler have read-only root filesystems, a bounded 64 MB
temporary directory, dropped capabilities and no-new-privileges. Caddy also has
a read-only root filesystem with writable data/config volumes, and retains only
NET_BIND_SERVICE for its listening ports. PostgreSQL retains its standard
initialization permissions and a persistent data volume; do not blindly apply
the application's capability policy to its entrypoint.

Each service rotates Docker JSON logs at 10 MB × 3 files **per container**.
CPU/memory ceilings are configurable in compose.env via `DATABASE_CPUS/MEMORY`,
`WEB_CPUS/MEMORY`, `CRAWLER_CPUS/MEMORY`, `MIGRATE_CPUS/MEMORY` and
`CADDY_CPUS/MEMORY` (each slash denotes the two suffixes, e.g. WEB_CPUS and
WEB_MEMORY). PID ceilings are 256 for PostgreSQL and 128 for other roles.
These are initial ceilings, not reserved capacity or measured sizing guarantees.
Memory exhaustion can kill a process; inspect Docker State.OOMKilled and review
limits against real crawl/backup traffic and the VM's RAM before launch.

The release bundle includes an **optional host-wide** journal policy at
deployment/systemd/journald-limits.conf.example. Review existing host policies
with `systemd-analyze cat-config systemd/journald.conf`, then deliberately install:

~~~sh
sudo install -Dm0644 deployment/systemd/journald-limits.conf.example /etc/systemd/journald.conf.d/gh-random-limits.conf
sudo systemctl restart systemd-journald
sudo journalctl --disk-usage
sudo journalctl -u gh-random-crawler.service --since today
~~~

This limits disk journals to 256 MB, memory journals to 64 MB, requests 1 GB
free disk and seven-day maximum retention. It affects **other host services**
too. Size/free-space limits can retain less history; active files and unrelated
logs/backups are not a total disk quota. See
[journald's limits](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html).
Nothing installs/restarts the host journal automatically. View container output
with the matching release's Compose command followed by `logs --tail 100 web`
(or database/caddy), and use `run --rm crawler python -m app.cli status` for
catalog status. Deployment journals/backups require their separate retention
policy; Docker/journald rotation does not prune them.

Python, uv, Dockerfile frontend, PostgreSQL and Caddy use immutable multi-platform
index digests. Pins prevent silent changes; they do **not** automatically receive
security patches or certify vulnerability-free images. Review upstream releases
at least monthly and promptly for relevant security fixes. Inspect candidate
indexes with `docker buildx imagetools inspect IMAGE:TAG`, confirm linux/arm64
and linux/amd64 support, and update pins together in Dockerfile, production
Compose defaults, compose.env.example and CI's PostgreSQL service as applicable.
Use a focused PR and require all three CI jobs, including fresh migrations,
proxy/resource checks, restore, redeploy and rollback rehearsals.

Python/uv updates ship in a new signed application release. PostgreSQL/Caddy
updates also require deliberate host maintenance: the deploy script refuses an
incidental database/proxy image change. Take a tested backup, stop the crawler
timer/service, plan downtime, review compatibility, then update/recreate only the
intended services with matching reviewed configuration. Major PostgreSQL upgrades
need their own pg_upgrade or dump/restore plan; never point a new major at the old
data volume. Preserve persistent volumes, verified releases and rollback images;
never use blanket Docker volume/system pruning. Restart/verify scheduling after
maintenance. No image update or host policy was applied to a live VM here.
