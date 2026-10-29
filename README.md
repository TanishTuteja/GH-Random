# GH Random

Discover a random GitHub repository by language, stars, and recent activity.
[Try GH Random](https://ghrandom.duckdns.org/).

The web app reads a local PostgreSQL catalog. A separate crawler refreshes it
every six hours in production, while browsing continues from the last published
generation.

## Architecture and catalog behavior

~~~text
Browser → FastAPI → PostgreSQL ← crawler → GitHub Search API
~~~

PostgreSQL, web and crawler start independently; starting web never starts a
crawl. Only the crawler needs GitHub credentials or contacts the GitHub API.

A full crawl covers 10 languages × 5 star ranges × 4 pushed-time ranges: 200
queries, each with up to ten pages. Requests start at least five seconds apart.
Successful crawls rotate through updated-descending, stars-descending,
updated-ascending and stars-ascending; failures do not advance the rotation.

Each staging generation copies active memberships, refreshes rediscovered
repositories, and publishes only after every query succeeds. Memberships unseen
for three days are omitted from the new active catalog. After publication,
retired/failed generations older than seven days, their crawl runs and
memberships are deleted; repository metadata is deleted only when no generation
references it. Active and staging generations are protected from cleanup.

Filters support `Any` and the configured languages/star/pushed ranges in
[app/core/filters.py](app/core/filters.py). Discovery prefers a different repository
from the one currently displayed and repeats it when it is the only match.

## First-time local setup

Requires the Python 3.12 version in [.python-version](.python-version),
uv 0.13.5, and Docker Desktop or Docker Engine with Compose. A GitHub App
installation/private key is required only for crawling. Contributions follow
[CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), the
[code of conduct](CODE_OF_CONDUCT.md) and [security policy](SECURITY.md).

Install uv through its [official instructions](https://docs.astral.sh/uv/getting-started/installation/),
or use a patched Python/pip installation with no virtual environment active:

~~~powershell
python -m pip install --user "uv==0.13.5"
~~~

From the project root, run unchanged:

~~~powershell
uv sync --locked --extra dev
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
~~~

Before starting services, edit the ignored `.env`:

- Set a local PostgreSQL password and matching `DATABASE_URL`/`TEST_DATABASE_URL`.
  The test URL must identify a separate disposable database, normally
  `gh_random_test`. URL-encode reserved password characters.
- For crawling, set `GITHUB_APP_CLIENT_ID` and an absolute
  `GITHUB_APP_PRIVATE_KEY_PATH` outside the repository. Set
  `GITHUB_APP_INSTALLATION_ID` when the App has multiple installations.
- Set optional pgAdmin credentials only if you intend to start its profile.

The remaining settings and defaults are documented in [.env.example](.env.example).
Never commit credentials, private keys, database dumps or production env files.
Commands below assume the project root and an active virtual environment.

### Dependency locking and updates

`uv sync --locked --extra dev` installs the committed `uv.lock`; it does not
silently regenerate a stale lock. Docker installs the same locked runtime subset.
The uv version, build-tool constraints and image digests are also pinned.

After changing dependency declarations, run:

~~~powershell
uv lock
uv sync --locked --extra dev
~~~

For a focused update, use `uv lock --upgrade-package httpx`, then sync and verify.
Use `uv lock --upgrade` only for an intentional broad update. Commit declarations
and lock changes together; review pinned tool/image versions too.

## Local services

Start PostgreSQL and apply migrations:

~~~powershell
docker compose up --detach database
alembic upgrade head
python -m app.cli status
~~~

PostgreSQL binds only to `127.0.0.1:5432`. The named Docker volume preserves data
across restarts and `docker compose down`. Adding `--volumes` deletes that data.
Fresh volumes also initialize `gh_random_test`. For an older volume or custom
test name, create a separate disposable database explicitly; keep the existing
volume intact.

Start web in another activated terminal:

~~~powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
~~~

Open http://127.0.0.1:8000. Before the first successful crawl, no active catalog
exists and discovery cannot return a repository.

Run a full crawl when you want to populate or refresh the catalog:

~~~powershell
python -m app.cli crawl
~~~

A full crawl can take several hours. Check its persisted state with
`python -m app.cli status`. To explicitly continue the newest failed checkpoint:

~~~powershell
python -m app.cli crawl --resume
~~~

Resume keeps the original plan, date boundaries and sort order and refuses a
changed plan. Production scheduling always starts a normal crawl.

### Optional local pgAdmin

Set `PGADMIN_DEFAULT_EMAIL` and `PGADMIN_DEFAULT_PASSWORD` privately in `.env`,
then run:

~~~powershell
docker compose --profile admin up --detach pgadmin
~~~

Open http://127.0.0.1:5050 and register PostgreSQL with host `database`, port
`5432`, and the database/user/password from `.env`. The host app connects through
`127.0.0.1`; pgAdmin connects through the private Compose network.

Stop only pgAdmin with `docker compose --profile admin stop pgadmin`.
For production access, use [PGADMIN.md](deployment/PGADMIN.md), not this local setup.

### Crawl ownership and interruptions

Crawls and online Alembic commands share a PostgreSQL session-level ownership
lock. Contenders fail promptly; ordinary browsing/status reads remain available.
Use direct PostgreSQL connections for these operations, not transaction pooling.

A lost owning connection cannot reconnect and continue writing. Once PostgreSQL
releases the dead owner's session, the next exclusive normal/resume crawl marks
consistent abandoned running/staging records failed while preserving the active
catalog and checkpoints. It refuses inconsistent state for manual inspection.
Elapsed time alone never authorizes takeover of a live owner.

Stop legacy pre-lock crawlers/migrations before upgrading them. Do not delete a
lock/state record or edit a running row to force concurrency. For managed
production child cleanup, follow [OPERATIONS.md](deployment/OPERATIONS.md).

## Public interface

| Route | Purpose |
| --- | --- |
| `GET /` | Repository discovery page |
| `GET /api/random-repository` | Random repository JSON from the active catalog |

The API accepts `language`, `star_bucket` and `pushed_bucket` filter IDs,
including `any`. Optional `exclude_github_id` avoids the current result when
alternatives exist. This local example runs unchanged:

~~~powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/random-repository?language=python&star_bucket=0-100&pushed_bucket=pushed-within-week"
~~~

OpenAPI/docs, health, crawler-status and filter-configuration routes are not public.

## Schema migrations

Apply new revisions with `alembic upgrade head`. Migration history starts at
baseline `20260930_01` and advances additively from the first production
deployment; never rewrite published migrations or recreate a live database to
apply settings.

Online migration statements and commits are fenced to the owning connection.
Offline `--sql` output does not connect or acquire that lock; manually executing
it bypasses the cooperative protocol. Inspect failed migrations and prefer a
reviewed forward fix rather than blindly stamping or downgrading.

## Verification

Tests drop and recreate their target tables. Set `TEST_DATABASE_URL` to a
separate disposable PostgreSQL database ending in `_test`, such as
`gh_random_test`, never the development/production database. The preflight below
checks that name and rejects the application's database name before tests run.
From the project root with the local `.env` prepared, run unchanged:

~~~powershell
python deployment/install_age.py --destination build/age-tools
@'
import os
import subprocess
import sys
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
load_dotenv('.env')
test_url = os.environ['TEST_DATABASE_URL']
target = make_url(test_url)
application = make_url(os.environ['DATABASE_URL'])
if (
    target.get_backend_name() != 'postgresql'
    or not target.database
    or not target.database.endswith('_test')
):
    raise SystemExit('Use a disposable PostgreSQL _test database')
if target.database == application.database:
    raise SystemExit('Refusing the application database name')
environment = {**os.environ, 'DATABASE_URL': test_url}
raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest'], env=environment))
'@ | python -
if ($LASTEXITCODE -ne 0) { throw 'Test database preflight or tests failed.' }
ruff format --check app tests alembic .github/scripts deployment
ruff check app tests alembic .github/scripts deployment
pyright
~~~

This documented invocation points both database variables at the test database
for the child test process. The name guard does not establish that a database is
disposable: verify the configured server/database yourself. Never point either
test/CI URL at data you want to keep.

[CI](https://github.com/TanishTuteja/GH-Random/actions/workflows/ci.yaml) requires:

- **Python checks:** formatting, lint and types.
- **PostgreSQL tests:** the full suite, fresh migrations and schema-drift checks.
- **Production containers:** native AMD64 builds, proxy/configuration checks,
  packaged discovery, isolated deploy/restore/rollback and systemd recovery
  rehearsals, plus the bounded-memory fixture.

CI uses disposable credentials and fake GitHub contracts, not production secrets
or live search traffic. Run the container check locally with Docker available:

~~~powershell
python .github/scripts/check_containers.py
~~~

It uses a unique disposable project and cleans only its owned resources.
Synthetic tests and resource limits do not establish live capacity; see
[LOW_MEMORY.md](deployment/LOW_MEMORY.md).

### Database backup and restore rehearsal

For an unencrypted **development-only** snapshot, with the local database running:

~~~powershell
python deployment/database_backup.py --compose-file compose.yaml --env-file .env backup --output-dir backups
~~~

Use the same Compose/env options for `restore`, supplying the printed trusted
backup directory and a new `gh_random_restore_*` target. The tool verifies
checksums/major-version compatibility and refuses existing targets; it never
switches the app or drops production. Secure Windows backup folders with ACLs.
For production encryption, retention and off-site recovery, follow
[BACKUPS.md](deployment/BACKUPS.md).

### Change workflow

Every change uses a short-lived branch from up-to-date `main`, signed commits
with descriptive bodies, and a PR with all three required checks green.
Merge with maintainer authorization and delete the feature branch afterward.
UI changes require manual verification before commit/push or TODO completion.

## Deployment notes

The production site runs on an AMD64 OCI E2.1.Micro. Use the dated
[acceptance record](deployment/OPERATIONS.md#live-acceptance--2026-10-28) for verified
evidence, [TODO.md](TODO.md) for remaining work, [ROADMAP.md](ROADMAP.md) for future
features and [decisions.md](decisions.md) for current design decisions.

| Guide | Purpose |
| --- | --- |
| [FIRST_DEPLOYMENT.md](deployment/FIRST_DEPLOYMENT.md) | Clean-host installation order and private configuration |
| [AUTOMATION.md](deployment/AUTOMATION.md) | Approved outbound deployment and signature/identity checks |
| [OPERATIONS.md](deployment/OPERATIONS.md) | Inspection, incident recovery and maintenance |
| [BACKUPS.md](deployment/BACKUPS.md) | Encryption, retention and safe off-site restore |
| [MONITORING.md](deployment/MONITORING.md) | Local/external monitoring and telemetry |
| [LOW_MEMORY.md](deployment/LOW_MEMORY.md) | Micro-VM limits and capacity checks |
| [PGADMIN.md](deployment/PGADMIN.md) | Optional private production database browsing |

The production Dockerfile builds one image for web, migrations and crawler.
Caddy alone publishes ports. Private root-owned configuration lives outside the
verified release directory; only the crawler receives its GitHub private key.
Published releases, image digests and deployment files remain immutable.

### Current verified release

Use [v0.1.1](https://github.com/TanishTuteja/GH-Random/releases/tag/v0.1.1) for the
AMD64 host, source `94b9c94ff0232b3f3d8f0e8d5d7c25b902af548b`:

~~~text
ghcr.io/tanishtuteja/gh-random@sha256:c5a6732f67d445be36eaaa00ded7706415982b13a5f3ad10b68eea7ca991e697
~~~

The older v0.1.0 is ARM64-only and predates the CA trust fix. Keep its historical
assets intact, but use v0.1.1 on this VM. The current private host configuration
uses `WEB_MEMORY=192m`; new templates use the same ceiling.

### Creating a release (not deploying it)

Merge the version-bump PR, with `project.version`/`uv.lock` aligned, then wait for
all three **main-push** checks on that exact merge commit. A green PR alone does
not qualify a release source.

The commands below illustrate the next unused version. **Replace every
`v0.1.2` if another version is intended**, and first verify that the chosen tag
does not already exist. Start with a clean, up-to-date checkout:

~~~powershell
git switch main
git pull --ff-only
git status --short
git tag -s v0.1.2 -m "GH Random v0.1.2"
git verify-tag v0.1.2
git push origin v0.1.2
~~~

Use the configured SSH signing key, registered on GitHub. Local verification
also needs its allowed-signers trust file. Publish a GitHub Release using the
**existing signed tag**, with release notes and prerelease disabled.

The release workflow verifies stable version/source/signature/main ancestry and
exact-source main CI, builds AMD64 from the pinned Git source, pushes to GHCR,
then tests the published digest. It checks actual provenance/SBOM content and
signs image/archive attestations; see [SUPPLY_CHAIN.md](.github/SUPPLY_CHAIN.md).

Wait for a green release workflow and all four assets:

- `image-digest.txt`
- `release.json`
- `SHA256SUMS`
- `gh-random-VERSION-deployment.tar.gz`, where `VERSION` is the signed tag

A visible Release or GHCR tag alone is insufficient. Never move a published tag,
republish its image or overwrite completed assets; fixes use a new version.
Public GHCR permits credential-free VM pulls. Verify package visibility and an
anonymous full-digest pull when publishing or changing package ownership.
Publishing does not deploy.

### Verifying a published release

Use an official maintained GitHub CLI **2.97.0+**. Download the four assets into
a fresh version-specific directory. Independently verify the signed tag's full
commit SHA; do not trust only the archive's own metadata.

The following Linux/operator-shell example is specifically for immutable
v0.1.1. For another release, **replace the version, verified commit, image digest
and archive filename throughout**. Authenticate the CLI privately; the VM's
automated puller uses a short-lived App token.

~~~sh
set -eu
sha256sum --check SHA256SUMS
GH_RANDOM_VERIFIED_COMMIT=94b9c94ff0232b3f3d8f0e8d5d7c25b902af548b
GH_RANDOM_RELEASE_IMAGE=ghcr.io/tanishtuteja/gh-random@sha256:c5a6732f67d445be36eaaa00ded7706415982b13a5f3ad10b68eea7ca991e697
for target in gh-random-v0.1.1-deployment.tar.gz "oci://$GH_RANDOM_RELEASE_IMAGE"; do
  gh attestation verify "$target" --repo TanishTuteja/GH-Random \
    --signer-workflow TanishTuteja/GH-Random/.github/workflows/release.yaml \
    --source-ref refs/tags/v0.1.1 --source-digest "$GH_RANDOM_VERIFIED_COMMIT" \
    --signer-digest "$GH_RANDOM_VERIFIED_COMMIT" --deny-self-hosted-runners
done
tar -xzf gh-random-v0.1.1-deployment.tar.gz
~~~

Stop if any verification fails. Checksums detect corruption, not authorship.
Use only the matching extracted files; preserve their hashes and signed metadata.

### Deploying a verified release

The normal production route is an approved outbound pull:
[AUTOMATION.md](deployment/AUTOMATION.md). Dispatch `deploy.yaml` on `main` with
the published tag and approve the production environment. The VM verifies source,
archive/image signatures and the approval receipt before applying it and
reporting actual GitHub Deployment status. GitHub needs no inbound SSH.

For a deliberately manual deployment, use that verified release's script.
The example below assumes the documented private configuration and public site.
**Replace `/opt/gh-random/releases/v0.1.1` with the actual verified release
directory, including any deployment-ID suffix, and replace the URL for another
hostname.** Add `--bootstrap` only when explicitly creating
the first empty database service; omit it for normal deployments:

~~~sh
sudo python3 /opt/gh-random/releases/v0.1.1/deployment/deploy.py apply \
  --env-file /etc/gh-random/compose.env \
  --state-dir /var/lib/gh-random/deployments \
  --backup-dir /var/backups/gh-random/encrypted \
  --backup-config /etc/gh-random/backup.json \
  --alerts-config /etc/gh-random/alerts.json \
  --url https://ghrandom.duckdns.org
~~~

Keep the Compose project, state and backup directories stable. Env/key paths
must be explicit absolute paths outside the release, mode `0600` in root-owned
`0700` directories; preserve actual credentials when copying templates.

Apply acquires host/database ownership, verifies configuration/image/source,
takes an encrypted off-site-verified backup before DDL, runs migrations once,
recreates web/Caddy and smoke-tests HTTPS before recording `current.json`.
Existing crawls/migrations cause prompt contention failure. Database/proxy
image or credential changes require deliberate separate maintenance.

Failure does not automatically roll back images, downgrade schema, restore data
or delete volumes. Runtime may differ from the last successful pointer after a
late failure: inspect the attempt journal, actual containers and schema before
retrying. Never remove kernel lock files to force deployment. Interrupted
controllers release their named coordinator via cleanup or bounded EOF/idle
expiry; Docker outages still require inspection.

### Explicit image rollback

Use the current verified script and its recorded previous release only after
reviewing application/data compatibility. **Replace the example release-directory
path with the actual current verified directory**, retaining private configuration:

~~~sh
sudo python3 /opt/gh-random/releases/v0.1.1/deployment/deploy.py rollback \
  --env-file /etc/gh-random/compose.env \
  --state-dir /var/lib/gh-random/deployments \
  --backup-dir /var/backups/gh-random/encrypted \
  --backup-config /etc/gh-random/backup.json \
  --alerts-config /etc/gh-random/alerts.json \
  --url https://ghrandom.duckdns.org \
  --confirm-schema-compatible
~~~

Rollback backs up first, requires the live schema to equal the previous image's
head, and never migrates/downgrades. Equal revisions do not prove compatibility.
After a failed deployment, reapplying `current.json`'s last successful release
may be the recovery required; rollback selects its *previous* verified release.

### Six-hour scheduled crawler (Linux/systemd)

The installed bootstrap selects the successful `current.json` release/digest.
Each activation runs a normal crawl, never `--resume`. Slots are **00/06/12/18 UTC**
(**05:30/11:30/17:30/23:30 IST**). Persistent scheduling catches up once after
missed slots and does not overlap an active service.

For a clean host, prepare the private configuration, successful deployment and
email handler below first. From the **matching verified extracted release**,
run these commands unchanged:

~~~sh
sudo install -Dm0644 deployment/crawler.py /usr/local/lib/gh-random/crawler.py
sudo install -Dm0644 deployment/systemd/gh-random-crawler.service /etc/systemd/system/gh-random-crawler.service
sudo install -Dm0644 deployment/systemd/gh-random-crawler.timer /etc/systemd/system/gh-random-crawler.timer
sudo systemd-analyze verify /etc/systemd/system/gh-random-crawler.service /etc/systemd/system/gh-random-crawler.timer
sudo systemctl daemon-reload
sudo systemctl start gh-random-crawler.service
~~~

After the first successful managed crawl, enable its timer:

~~~sh
sudo systemctl enable --now gh-random-crawler.timer
~~~

Managed success, including child cleanup, updates freshness watermarks. Direct
CLI crawls do not. The supervisor has a five-hour limit and ownership-checked
child cleanup, with an independent systemd fallback. A later slot starts normally
after failure. Stop **both** timer and service for intentional maintenance;
killing only a Docker client does not prove its daemon-owned child stopped.
See [OPERATIONS.md](deployment/OPERATIONS.md) for stop/recovery commands.

Updating installed host units/bootstrap is deliberate: stop, install matching
reviewed files, verify, reload and restore scheduling. Application deploys do not
silently overwrite host policy. The published v0.1.1 puller compatibility fixes
are documented in [AUTOMATION.md](deployment/AUTOMATION.md#compatibility-override-for-v011).

### Explicit live AMD64 GitHub App authentication smoke

This opt-in check contacts GitHub using real App credentials, without a crawl or
database operation. **Replace `VERIFIED_IMAGE` with a trusted pulled AMD64 digest
and `PRIVATE_APP_ENV` with the private App configuration path**:

~~~powershell
python .github/scripts/check_github_auth.py --image VERIFIED_IMAGE --env-file PRIVATE_APP_ENV
~~~

The harness stages only App credentials through a uniquely labelled private
key volume, verifies HTTPS authentication, revokes its short-lived token and
removes owned resources on normal completion. Abrupt host loss still requires
inspection of the exact owned resources; it is separate from VM acceptance.

### Email failure alerts and missing-success monitoring

Configure `/etc/gh-random/alerts.json` from the example and a separate root-owned
mode-0600 `smtp-password`. Replace host/port/username/sender/recipients privately.
Use mandatory TLS (`ssl` or `starttls`); verification cannot be disabled.

The initial Gmail setup uses a dedicated account, 2-Step Verification, separately
revocable App Passwords for VM/GitHub monitoring, `smtp.gmail.com:465` and the full
sender address as username/from address. These credentials are not send-only
scopes; keep them private and review provider quotas.
See [Google's App Password guidance](https://support.google.com/mail/answer/185833).

On a clean host, from the matching verified release with private files prepared,
run unchanged. **The last command sends a real test email**:

~~~sh
sudo install -Dm0644 deployment/notifications.py /usr/local/lib/gh-random/notifications.py
sudo install -Dm0644 deployment/systemd/gh-random-alert@.service /etc/systemd/system/gh-random-alert@.service
sudo install -Dm0644 deployment/systemd/gh-random-monitor.service /etc/systemd/system/gh-random-monitor.service
sudo install -Dm0644 deployment/systemd/gh-random-monitor.timer /etc/systemd/system/gh-random-monitor.timer
sudo python3 /usr/local/lib/gh-random/notifications.py test --config /etc/gh-random/alerts.json
~~~

Confirm actual inbox arrival, then verify/reload installed units and enable
monitoring. On an established host, coordinate unit changes with active jobs:

~~~sh
sudo systemd-analyze verify /etc/systemd/system/gh-random-alert@.service /etc/systemd/system/gh-random-monitor.service /etc/systemd/system/gh-random-monitor.timer
sudo systemctl daemon-reload
sudo systemctl start gh-random-monitor.service
sudo systemctl enable --now gh-random-monitor.timer
~~~

Use systemd 251+ for original failure invocation snapshots. Default alerts warn
after 12 hours without a successful crawl, 36 hours without a successful backup
and 14 days before TLS expiry. Disk thresholds are 2 GiB or 10% free, or 5% free
inodes. Backups/TLS remain opt-in during clean-host bootstrap until their first
real success. The hourly monitor runs at `:15 UTC`.

The private ledger retries pending delivery, applies six-hour reminders and sends
recovery after an accepted failure notification. SMTP acceptance is not inbox
delivery; duplicates are possible. Do not delete alert locks/state to force a
send. Deploy emails run after ownership release and cannot replace inspecting
the deployment journal.

[MONITORING.md](deployment/MONITORING.md) covers external GitHub response/freshness
checks, hourly telemetry and independent free UptimeRobot availability. Those
monitors are configured for the current site; new installations need their own
verified identity, configuration and inbox acceptance before schedules are enabled.

### Production runtime hardening and maintenance

Only Caddy publishes host ports. Web/database use internal networks; trusted
proxy headers depend on keeping those Docker networks private. Configure exact
`APP_ALLOWED_HOSTS` and matching `GH_RANDOM_DOMAIN`; do not publish Uvicorn,
PostgreSQL, Docker API or pgAdmin ports. Adding another proxy needs its own trust
review.

Container resources and logs are bounded. The micro defaults are one web worker,
192 MiB web, 256 MiB database, 192 MiB crawler, 128 MiB migration and 64 MiB Caddy.
Use [LOW_MEMORY.md](deployment/LOW_MEMORY.md) for tuning and measured limits.
[OPERATIONS.md](deployment/OPERATIONS.md) covers incidents, journald, host updates,
credential rotation and exact-image cleanup. Preserve data volumes, current/
previous releases and verified backups; major PostgreSQL upgrades need an
explicit migration plan.

For a clean host, first inspect existing journald policy with
`systemd-analyze cat-config systemd/journald.conf`. After reviewing this
host-wide change, run unchanged from the verified release to install its
256 MiB persistent/64 MiB runtime, 1 GiB free-space and seven-day limits:

~~~sh
sudo install -Dm0644 deployment/systemd/journald-limits.conf.example /etc/systemd/journald.conf.d/gh-random-limits.conf
sudo systemctl restart systemd-journald
sudo journalctl --disk-usage
~~~

Python/tool/service digest pins require reviewed updates; they do not receive
security patches automatically. Review relevant advisories and upstream releases,
then use green PRs and a new signed application release. Database/proxy image
changes also require planned host maintenance and a tested backup.
