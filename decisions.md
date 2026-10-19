# Decisions

This file records decisions that materially shape the MVP, including choices made
autonomously while completing the project.

## 2026-10-18 — Verify real App authentication without a catalog crawl

Add an explicitly invoked local smoke harness, not a credential-bearing CI job.
Build/run the locked production image for ARM64 and pass only App identifiers
and its key, never the whole private env file or database credentials. Windows
bind permissions are not Unix ownership, so stage the key through stdin in a
uniquely labelled ephemeral Docker volume as root:root mode 0600 with no network.
Mount it read-only in a capability-dropped, read-only-root, resource-bounded ARM64
role; authenticate, make an authorized HTTPS probe, revoke the installation
token on normal completion and remove only invocation-owned resources.

The real local smoke succeeded, including revocation and key-volume cleanup,
without DB access or a crawl. Full local tests/checks also passed. This proves
the container/authentication component, not a real OCI bind ACL, firewall or full
crawl. Do not log credentials or captured container errors. Cleanup failure is
an explicit error; abrupt host loss may leave a temporary volume/token until
operator cleanup/expiry, and removing the volume is not forensic secure erase.

## 2026-10-18 — Ship explicit incident and maintenance runbooks

Include deployment/OPERATIONS.md in the immutable release bundle, alongside the
scripts it describes. Prefer ownership-checked managed crawler cleanup, forward
migration repair and new-database-only recovery to blanket process termination,
schema stamping, volume deletion or automatic data rollback. Image cleanup is an
explicit exact-image operation preserving current/previous/recovery artifacts;
host updates, unit upgrades and secret rotation remain deliberate maintenance.
The documented procedures do not establish live VM capacity, provider delivery,
DNS or recovery-time acceptance. Keep those manual checks separate and unchecked.

## 2026-10-18 — Production pgAdmin is explicit and loopback-only

Use a separate optional Compose overlay/profile, not the normal production
startup/deploy path. Pin the official multi-platform image, run UID/GID 5050 with
dropped capabilities and a private password-file bind. Join backend and a
dedicated admin-access bridge: Docker does not publish a port for internal-only
networks. This separate bridge permits outbound traffic, not public ingress;
the explicit host binding stays loopback-only and app services do not join it.
Allow the image's writable initialization filesystem; do not pretend it has the
application's read-only root. Bind only 127.0.0.1, disable restart, and remove only
its container after use. Access requires SSH local forwarding through a managed
Bastion VM session, not a direct Bastion port-forward to the VM private IP. Keep
database credentials out of defaults and private GUI edits out of schema history.

## 2026-10-18 — Resolve the newly enabled dependency security alert

GitHub dependency alerts identified pytest's Unix temporary-directory issue
GHSA-6w46-j5rx-g56g/CVE-2025-71176, fixed in pytest 9.0.3. Raise the development
dependency floor and refresh the lockfile without upgrading runtime packages.
Remove unused pytest-asyncio: this suite has no asynchronous tests/fixtures and
the old plugin constraint would prevent adopting the patched pytest major.
Pytest is not installed in the production image; this is still important for
developer/CI environments and must not be dismissed as a false positive.

## 2026-10-18 — Host-local capacity and certificate early warnings

Extend the existing private hourly notifier, not the public application routes.
Monitor up to three local filesystems for low available bytes/percentage/inodes;
initial warnings are 2 GiB or 10% space or 5% inodes. Report configured indices
instead of arbitrary host paths. Keep the path order stable during incidents.
The TLS probe is opt-in until the real hostname works; verify SNI/hostname/system
trust and warn at 14 days. Bound DNS/handshake in a child with a 20-second parent
timeout. Unavailable probes are failures, not healthy. Reuse durable retries,
cooldown and recovery notices without emailing raw provider/certificate errors.
This does not solve whole-VM outage detection, prove Caddy renewal, or guarantee
email when disk/state storage is already full; independent monitoring remains.

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

## 2026-10-17 — Test production containers on native ARM64 before merge

Add a stable `Production containers` CI job on the standard `ubuntu-24.04-arm`
runner, matching the planned Oracle VM without QEMU emulation. Keep the existing
quality and PostgreSQL test jobs on AMD64. All three jobs are required by the
main-protection ruleset.

Use a standard-library Python runner so the same Compose integration check also
runs on Windows development machines. It builds the production Dockerfile and
uses the real production Compose file with a small test-only overlay: no host
ports and internal HTTP through Caddy. Validate the HTTPS Caddy configuration
separately without certificate issuance; live DNS/TLS remains a deployment task.

Use tracked example configuration, an invalid placeholder key, and a uniquely
named Compose project with fresh volumes. Exercise the migration and hardened
crawler status roles, then seed one disposable repository to check discovery
through Caddy and confirm the intentionally disabled public routes remain off.
The in-container seeding path requires a test-only environment marker. Never
run a real crawl or require production/GitHub secrets for this job. Print logs
on failure and remove only the generated test stack and image afterward.

## 2026-10-17 — Signed stable releases publish ARM64 images, not deployments

Use the published GitHub Release event, with a pre-existing SSH-signed annotated
`vMAJOR.MINOR.PATCH` tag. Require GitHub signature verification, version agreement
with `pyproject.toml`, membership of the exact commit in `main`, and all three
successful jobs in the latest main-push CI run for that commit. PR CI alone does
not certify the merge commit. Fail closed rather than wait indefinitely for CI;
publish only after main CI finishes. Prereleases need a future explicit policy.

Start with ARM64 only on the free standard native ARM runner, matching OCI.
Publish a version tag with source/revision/version/license labels and request
BuildKit provenance plus an SBOM. The source verifier is read-only; only the
publishing job receives package/release writes. Actions are commit-pinned and
checkout does not persist credentials. No personal publishing token or VM
secrets are required. Recheck the tag before publishing and before asset creation.

After pushing, pull the immutable digest and run the production integration
check against it, not a rebuild or mutable tag. Only then attach the digest,
release metadata, checksums, and an allowlisted deployment archive from the
same checkout. Pin that archive's Compose template to the digest. Do not glob
configuration directories or include live secrets. Refuse reruns once generated
assets exist, and do not overwrite completed releases. A failed run may leave
an image without usable deployment assets; release visibility alone does not
mean readiness. This workflow deliberately does not deploy production.

Choose public GHCR images for anonymous VM pulls. New packages initially default
to private, so visibility and anonymous access require explicit verification
after the first authorized publish. No release/tag/image has been published as
part of implementing the workflow. Tag protection, immutable base-image pins,
production deployment/rollback automation, and live first-release verification
remain separate checklist items; checksums are not cryptographic publisher
attestations of the release archive.

## 2026-10-17 — Safe local backup/restore before deployment automation

Add a standard-library Python host tool rather than requiring a second host
virtualenv or PostgreSQL client installation. Resolve exactly one running
database container from explicit Compose configuration/project, then use its
matching PostgreSQL tools over the local socket. Expand credentials only inside
that container; never log passwords, raw restore SQL, or archive contents.

Stream a compressed custom-format consistent pg_dump snapshot into a private
partial directory. Check the archive, sync its bytes and checksum manifest,
then rename atomically to mark completion. Enforce private ownership/modes on
Linux; Windows ACLs remain the operator's responsibility. Keep partials on failure
for explicit inspection, and never automatically prune backup data. Do not
claim an independently queried live row count describes the dump's MVCC snapshot.

Restores require acknowledgement of a trusted source and a new, explicitly named
`gh_random_restore_*` database, never the live DB or an existing target. Validate
checksum/format/version before creation, restore in one transaction without
clean/drop/ownership/grant replay, then report schema/catalog counts. Leave the
new database for inspection even on failure. This is deliberately not an
automated production switch, database deletion tool, or cross-major upgrade.

Include the tool in release archives and extend production-container CI to
dump the disposable catalog, restore it separately, verify the actual app/API
against the copy, and reject existing targets/corrupt backups without damaging
the source. Ignore real environment files and database dumps in Git/build context.

Scope this PR to local primitives and their rehearsal. Local snapshots are not
encrypted and cannot survive VM loss. Backup scheduling/retention, encryption
and Object Storage, alerting, RPO/RTO decisions, interrupted-crawl recovery, and
production restoration/promotion remain explicit open deployment items. No live
database was backed up/restored, no timer installed, and no OCI service configured
while implementing/testing this change.

## 2026-10-17 — Database ownership before deploy orchestration

Implement the coordination prerequisite before the deploy script: a `running`
row is not an atomic mutex or reliable liveness signal. Crawls and online Alembic
now acquire the same nonblocking, database-scoped PostgreSQL session advisory
lock. Its fixed namespace/resource are a protocol contract shared by versions,
not tunable environment settings that could accidentally disable exclusion.
No extra table, migration, public endpoint, or external locking service is needed.

Bind all crawler sessions to the exact physical connection owning the lock;
commit page checkpoints normally without releasing ownership. Connection-local
statement and commit guards check both physical identity and actual lock
ownership, so SQLAlchemy reconnects cannot turn a disconnected old process into
an unfenced writer. Online Alembic uses the same guarded connection for DDL.
Always physically close/invalidate the owning session on exit rather than
returning its session lock to a pool. Readers and snapshot backups remain free
to operate. Transaction-pooling database proxies are not supported here.

After acquiring ownership, a crawler can mark leftover running/staging pairs
failed without touching active membership or committed cursors. A normal run
starts fresh; explicit resume retains its existing newest-failed/checkpoint/plan
rules. Inconsistent run/generation state requires inspection rather than guessing.
Failure recording must not mask the original error, and a disconnected old
worker must not overwrite a successor's recovered/published state. No elapsed
timeout authorizes takeover of a live owner. PostgreSQL must detect the old
session ending before a new owner can recover it.

This protocol cannot identify a still-running legacy process that never acquired
the lock: stop all pre-lock crawler/migration processes before upgrading. Advisory
locking is cooperative, not an access-control boundary; direct SQL and offline
DDL scripts can bypass it. Use the online Alembic entrypoint for schema changes.

Test real PostgreSQL exclusion, lock lifetime over commit/rollback, physical
connection loss/reconnect, backend termination, stale-writer fencing, actual
online Alembic contention, unclean child-process exit, and normal/resume recovery
with fake GitHub responses. No live crawl or development DB mutation is needed.
Full deployment serialization, backup-before-migration workflow, timer control,
Docker child termination, timeouts/alerts, and systemd scheduling remain the next
operational increments; this PR does not claim those are implemented.

## 2026-10-17 — Host-side deployment with a fenced database coordinator

Implement the actual deploy script now, using Python's standard library on the
host. It consumes a trusted, extracted release with immutable GHCR digest,
source/revision/version labels, platform, and per-file hashes. Add the deploy
script to the allowlisted release archive. Never combine moving-main deployment
files with a release image. Manifest hashes detect accidental edits/mixing, not
publisher authenticity; trusting/verifying the release precedes executing code.

Use a kernel file lock for deploy attempts sharing a state directory and the
existing database mutex across the whole backup/migrate/restart/smoke/promotion
sequence. A private stdio-only agent in the migration container holds the owning
connection; Alembic receives that exact guarded connection, avoiding a second
backend deadlock or a gap between backup and DDL. No Docker socket, HTTP endpoint,
additional service, or production secret is introduced. All callers must retain
the cooperative lock protocol. Readers remain available; crawling fails promptly
on contention. Do not disable timers, so errors cannot leave them disabled.
Systemd scheduling/child termination is still a separate increment.

Preflight validates credentials agree across all roles/database, private external
configuration paths, image metadata/architecture, and Caddy. Cache release images
before DDL. First boot needs explicit --bootstrap; refuse stopped/mismatched
existing databases rather than recreating them. DB/proxy image or credential
changes require reviewed maintenance, not incidental app upgrades. Base image
digest pins remain TODOs. Recreate web/Caddy with brief downtime, then verify both
the public page and database-backed discovery over verified HTTPS, no redirects.

Take a checked local snapshot before every migration/rollback, including an empty
bootstrap DB. Keep private per-attempt logs/journals and current/previous verified
release pointers. Publish state only after smoke success. Preserve the actual
previous runtime image separately because a failed prior smoke can leave runtime
ahead of the verified pointer. Env and pointer updates are individually atomic,
not transactional together: document interrupted promotion and cleanup-after-
promotion ambiguity. Never pretend current.json always describes failed runtime.

No automatic rollback, schema downgrade, restore, or volume removal. Explicit
rollback requires the previous verified release, identical schema head, and
operator compatibility acknowledgement. Equal revisions do not establish data
or code compatibility; schema-changing rollback needs a reviewed recovery plan.
Rerunning apply is the recovery for many transient failures, not blind restoration.

Terminate the exact named coordinator container on normal exit/SIGINT/SIGTERM;
controller EOF or idle expiry and statement timeouts bound abandoned work after
host failure. Ownership checks fence subsequent operations after lock loss.
Daemon/network failure still requires inspecting the recorded container/session;
do not use elapsed age to evict another live owner. Failure alerts remain TODO.

Rehearse the actual CLI with local-only synthetic digest images: bootstrap,
redeploy, DB exclusion across migration commits, EOF/idle recovery, deliberately
failed transactional DDL, failed HTTP smoke, and explicit same-schema rollback.
Assert catalogs, schema, backups, and verified pointers remain consistent. Run in
native ARM64 container CI with the containerd image store; fixtures never publish
to GHCR, use real keys, call GitHub, or touch a developer database. Unit tests
cover preflight/state-machine guards and Alembic's stdout protocol isolation.
Production OCI/TLS, first release publication, off-site backups, scheduling,
alerts, and GitHub deployment recording remain intentionally unfinished.

## 2026-10-17 — Six-hour timer and managed crawler lifecycle

Use a host systemd Type=exec service plus a persistent UTC calendar timer at
00/06/12/18:00 (05:30/11:30/17:30/23:30 IST). Keep normal full crawls, never
implicit --resume. One catch-up after downtime avoids replaying a backlog.
The next slot remains independent of a prior failure; no Restart retry loop.
Rotation advances only on success, so four distinct orderings daily depend on
four successful crawls. Cap crawl execution at five hours, with an independent
5h5m systemd fallback and 90-second stop budget. These are initial safety-policy
choices requiring production duration measurements, not guarantees of completion.

Install a small stdlib bootstrap from a trusted release in a stable host location.
It dispatches through private current.json and verifies the selected release's
worker hash/identity; actual orchestration executes that release's worker and
matching tools/Compose, not the old installed code or moving main. Include the
bootstrap, worker (same file) and units in release hashes/assets. Unit/bootstrap
policy installation/upgrades remain explicit operator steps, not automatic
privileged configuration changes hidden inside app deployment.

Hold the shared kernel deployment lock for the whole managed run, then retain
existing DB session exclusion inside CrawlService for manual/other callers.
Validate the cached image and role configuration; refuse an incomplete/mismatched
running web/DB instead of starting/pulling services or running old code after a
failed deployment. Manual crawls remain protected by their database mutex.

Launch a detached, init-enabled, restart=no Docker child with a unique name and
invocation/project labels. Persist its intended identity before creation, record
the actual ID/exit status, stream bounded Docker logs to journald, and remove only
that owned child. Stop it with Docker TERM/grace/KILL on timeout/signals; stopping
the CLI alone does not stop a daemon-managed container. ExecStopPost independently
cleans it after a killed supervisor; check INVOCATION_ID so an old stop cannot
target a successor. After a dead supervisor releases its host lock, clean only
its recorded managed orphan before the next run. No age-based takeover of manual
DB owners. Preserve failure/cleanup status when the daemon is unavailable, never
certify that container disappearance has been proven merely because inspection
failed. Existing DB recovery handles abandoned rows on the next normal crawl.

Service hardening is defense in depth: root-owned key access and Docker control
are root-equivalent. Keep an empty per-unit runtime Docker config, no host secrets
in unit arguments, no permanent crawler container, no timer enablement in deploy
or test commands. Document timer vs service stop and deliberate upgrade steps.

Extend offline state/dispatch/identity tests and disposable Docker rehearsals
using derived synthetic images whose fake CLI never authenticates or calls GitHub.
Native ARM64 CI additionally exercises temporary real systemd units, preserving
the production sandbox but scaling schedule/runtime and fixture paths/user. Check
timer failure followed by a successful next tick, stop/SIGKILL/watchdog cleanup,
and subsequent normal recovery without resume. Remove only uniquely named test
units/stacks. Windows checks the matched worker; POSIX exec/systemd is Linux CI.
No actual VM installation, live crawl or schedule change is made here. Alert
delivery, missing-success monitoring, off-site backups and real reboot/live-key
acceptance remain pending rather than implied by observable failure states.

Native CI exposed a real signal-handling trap: raising builtin InterruptedError
from SIGTERM can be swallowed by selectors/subprocess as retryable EINTR. Use a
dedicated non-OSError exception in both crawler and deployment controllers;
assert prompt actual service stop, not merely eventual 90-second forced cleanup.
The independent ExecStopPost path still provides a fallback after SIGKILL.

## 2026-10-17 — Production hardening and email alert choice

The user selected email alerts. Future alert delivery will use configurable SMTP,
with secrets and recipient/sender addresses in private host configuration; do not
send live messages from CI or invent a provider. Transport selection is complete,
not delivery/failure/staleness implementation or production acceptance.

Pin reviewed official Python 3.12, PostgreSQL 17 and Caddy 2.10 image indexes,
plus the Dockerfile frontend and existing uv pin. Keep ARM64/AMD64 indexes rather
than a single workstation architecture. Digest pins need explicit security/update
reviews; document monthly/advisory-driven PRs, not automatic production updates.
Database/proxy changes remain deliberate maintenance and are still refused as an
incidental app deploy. Do not conflate a digest pin with a vulnerability audit.

Require exact host allowlists in production private app configuration; keep safe
loopback/test defaults locally, reject wildcard/URL-shaped values, and reject
unknown hosts before route/DB work. Use an internal proxy network, never publish
Uvicorn, and explicitly trust forwarded headers from all reachable private Docker
peers. This is a documented trusted-stack boundary, not Caddy-only authentication.
Caddy remains the first public proxy and sanitizes incoming forwarded headers;
adding untrusted peers, a public web port or a CDN requires revisiting this policy.
Rehearse private HTTPS scheme consumption and public spoof rejection without
adding debug/health endpoints; real certificates/client addresses still need VM
acceptance. No UI changes or public endpoints are added.

Bound CPU/memory/PIDs and 10 MB x 3 Docker logs on every production role. Initial
limits are configurable ceilings requiring measured VM sizing, not reservations
or a claim of stress-tested capacity. Read-only/capability-restricted app/Caddy
roles keep only their explicit writable mounts; preserve PostgreSQL entrypoint
initialization privileges. Supply optional journald limits as a reviewed global
host policy, never silently change it during deploy. Docker/journal rotation does
not solve backup/deployment-state retention or disk/staleness alerts; keep those
items pending.

## 2026-10-17 — TLS email alerts independent of crawler availability

Use a standalone Python stdlib SMTP notifier installed with host systemd policy,
not application dependencies or a persistent mail/crawler container. Require
verified TLS (implicit TLS or mandatory STARTTLS); private root-owned JSON holds
nonpublic configuration and references a separate private SMTP credential file.
Never pass the credential on a command line, emit provider replies in public
errors, email raw logs or attach app/database/GitHub secrets to messages. Keep
live provider/account setup and inbox acceptance explicit; CI sends only to a
loopback SMTP fixture using an ephemeral test certificate.

Use templated systemd OnFailure handlers rather than sending mail inside the
crawler's ownership/cleanup path. This covers preflight failure and supervisor
SIGKILL/watchdog failure without delaying the next six-hour slot. Prefer systemd
251+ MONITOR_* snapshots to identify the original invocation, not mutable current
unit state. Failure of the notifier cannot recursively alert/restart itself.
Deployment email is an explicit --alerts-config option, prevalidated before
mutation, executed after ownership release in a child with a 60-second bound;
mail failure never changes the recorded deployment outcome.

Maintain private atomic JSON state under its own kernel lock. Persist pending
delivery before SMTP, mark it accepted only after all recipients are accepted,
and let the hourly monitor retry; never promise exactly-once or inbox delivery.
Default to six-hour incident reminders, 12-hour crawl-success age and (future
daily backups) 36-hour backup-success age. These are documented configurable
initial policies. A later success sends recovery only after a delivered failure;
never let an older success erase a newer preflight failure. Preserve a separate
crawler success watermark only after child success and completed cleanup, even
when later runs fail. Missing first success gets an initial policy-length grace
period. Monitor at UTC :15 (IST :45), with one persistent catch-up.

Backup monitoring remains opt-in until its scheduled worker/success watermark is
implemented. A host-local monitor cannot observe its own missing power/network
or a dead mail provider. Independent external uptime/heartbeat monitoring and
real SMTP tests remain launch acceptance tasks, not silently marked complete.
Host script/unit upgrades stay explicit and ship in the matching release bundle.

## 2026-10-18 — Encrypted, verified daily recovery copies

Use official checksum-pinned age 1.3.2 and native X25519 public recipients on the
VM, with private recovery identities kept separately/offline by the operator.
Do not invent crypto or add database/SMTP/GitHub credentials to archives. Package
the existing checked pg_dump and manifest, encrypt it, and clean only the exact
private temporary work tree. Secure deletion on SSDs is not promised; unclean
exit can leave private staging files requiring deliberate recovery/cleanup.

Choose a daily 05:15 UTC (10:45 IST) persistent systemd timer in the gap between
crawl slots. Limit the whole unit to 40 minutes plus 90-second stop cleanup.
Initial RPO is 24 hours and restore target one hour after replacement host
readiness; these are policies requiring real-catalog/cloud rehearsal, not CI
performance claims. Reuse host deployment exclusion and the fenced DB lease.
Separate coordinator idle and SQL statement limits so a long encryption/transfer
does not silently release ownership or increase migration statement budgets.

Before promoting backup-success.json, require encryption, unique no-overwrite
OCI upload with checksum verification, a version-constrained full download and
SHA-256 match, retention completion and lease cleanup. Use scoped VM instance
principals, not host administrator API keys. The public CLI adapter is implemented;
actual OCI IAM, bucket behavior, quotas, immutability and transfer remain manual
acceptance. Explicit local-only rehearsal cannot promote an off-site watermark.

Keep at least two verified local encrypted copies, even beyond the seven-day
age policy. Prune only managed/checksummed artifacts with a valid off-site receipt
after a fresh verified backup. Preserve unknown/corrupt/pending files for operator
inspection, and never blanket-delete a backup root. Do not grant remote deletion
or install an age-only lifecycle expiry that could erase every off-site recovery
copy during a prolonged outage. OCI retention/quotas need an independently
supervised/protected recovery policy; no bucket lifecycle is silently changed.

Allow the deployment script's explicit --backup-config to seal/upload/read-back
verify pre-DDL snapshots using the same policy. The old plaintext path remains
only as an explicit compatibility/development option; production examples opt in
to encryption. Recovery is offline, bounded, path/link/overwrite-safe, and verifies
the inner dump before the existing new-database-only restore. age authenticates
ciphertext, not its sender; retain explicit trusted-source acknowledgement.

## 2026-10-18 — Public repository stewardship and deployment gates

Enable and verify private GitHub vulnerability reporting before publishing its
security-policy link; use GitHub's conduct-reporting tools rather than inventing
a private maintainer email. Add issue forms, contributor/conduct/security guides,
PR template and useful labels. Schedule weekly grouped minor/patch Dependabot
proposals for uv, Actions, Docker and Compose, limit version-update PRs, and keep
security updates prompt with no auto-merge. Add offline metadata/workflow pinning
tests using an explicit development-only YAML parser dependency.

The active Protect release tags ruleset blocks update/deletion of refs/tags/v*
with no bypass actors; creating a new signed tag remains possible. Future fixes
require a new version. Production environment jobs are restricted to protected
branches and require owner review. Self-review is allowed for the solo maintainer
but is not an independent audit. These GitHub settings were actually configured
and read back via authenticated CLI, not inferred from repository files. OCI
credentials and live deployment/acceptance remain separate manual setup.

## 2026-10-18 — Guard supported runtime lines and duplicated image pins

The first Dependabot run proposed Python 3.12 to 3.14 as a semver-minor update.
That does not preserve this project's selected interpreter line. Ignore Python
minor/major proposals and PostgreSQL majors until a deliberate migration review.
Keep ordinary patch/digest proposals and reviewed same-major proxy/admin updates.
Add invariant tests tying Docker Python to .python-version and lint/type targets,
uv to its project pin, and production defaults to env/CI/development image pins.
Negative fixtures prove inconsistent single-file updates fail. Green bot checks
do not authorize automatic adoption of a different runtime or database major.

## 2026-10-18 — Outbound deployment authorization and publisher identity

Use a protected, manually approved main-branch Actions job to queue a deployment,
then a VM-side puller over outbound HTTPS. Do not expose SSH/webhooks or install
a privileged self-hosted Actions runner. A GitHub Deployment payload alone is not
approval: bind it to the successful queue job's immutable run/attempt artifact
and the installed reviewed workflow. Report success only after the VM's existing
backup/migrate/restart/smoke path succeeds; preserve prior files/images and never
replay a claimed migration after a crash. Cancellation cannot abort active DDL.

Use a separate repository-scoped operations App; do not expand the crawler App.
Verify downloaded root scripts against signed source Git blobs and GitHub-signed
archive/image attestations with exact repository, workflow, source and tag policy.
Build releases from an immutable remote Git context. Independently inspect the
actual content-addressed ARM64 OCI graph, BuildKit provenance and SPDX inventory;
matching labels and unsigned metadata do not authenticate a publisher. These
checks do not claim hermetic builds, vulnerability freedom or a SLSA level.

## 2026-10-18 — Independent email monitoring without another paid service

Use GitHub-hosted scheduled checks, disabled until live acceptance, for public
HTTPS/API availability and compact success-only telemetry posted by the VM's
operations App. Publish only timestamps, not data, identifiers, paths or logs.
Preserve prior proven successes when a later operation fails. The external
checker validates author, anchor, newest status, schema and timestamp freshness.

Keep external SMTP secrets in a separate protected-branches-only monitoring
environment with no reviewer delay, not repository-wide secrets or production's
approval gate. Store durable safe incident codes/pending delivery in a dedicated
operations issue, with reminders and recovery; SMTP acceptance is not inbox
proof. GitHub scheduling/outages and the SMTP provider remain shared failure
domains. Real hostname/App/provider setup and outage tests await the live stack.

## 2026-10-20 — Gmail alert setup remains private and unproven

The operator has a personal Gmail account. Document its supported App Password
SMTP configuration as a free low-volume option, with separate revocable VM and
external-monitor credentials and an optional dedicated alerts account. Do not
collect credentials in chat, weaken account security, or equate SMTP acceptance
with inbox delivery. Provider setup and real delivery tests remain unchecked.
Create the durable operations ledger once as issue #26, set its repository
variable, and explicitly keep MONITORING_ENABLED=false until live acceptance.

