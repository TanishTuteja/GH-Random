# Development guide

Complete [local setup](../../README.md#first-time-local-setup) first. Run commands
from the repository root with the virtual environment active. Contribution and
review rules are in [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Dependencies and releases

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
Review Dependabot's grouped proposals and security fixes; dependency updates
are not automatically merged.

Keep Dockerfile, Compose defaults, env examples and CI service pins synchronized
as applicable. Interpolated defaults may need manual updates; a bot PR does not
prove every copy was updated. Major PostgreSQL upgrades need a recovery plan.

Follow the [signed-release flow](../releases/RELEASES.md#creating-a-release) after
protected `main` CI passes. Active `v*` tag rules block updates/deletion,
including by the owner; publish a new version,
not a moved tag. The release workflow verifies source/signature/CI, tests the
published digest and attaches matching files. Never deploy incomplete assets.

GitHub's `production` environment restricts jobs to protected branches and
requires the owner's review. Self-review is allowed for this solo-maintainer
project; this gate is not a second-person audit. VM credentials and live
acceptance remain separate. Follow the [operations](../operations/OPERATIONS.md)
and [recovery](../operations/BACKUPS.md) guides; never prune volumes or downgrade
schema as incidental maintenance. Contributions use the [MIT license](../../LICENSE);
there is no separate CLA.

## Local development details

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
For production access, use [PGADMIN.md](../operations/PGADMIN.md), not this local setup.

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
production child cleanup, follow [OPERATIONS.md](../operations/OPERATIONS.md).

### Schema migrations

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
[LOW_MEMORY.md](../deployment/LOW_MEMORY.md).

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
[BACKUPS.md](../operations/BACKUPS.md).
