# Decisions

Current architecture and operating policies, reviewed on 2026-10-29. Git history
and pull requests retain the implementation chronology. Use [TODO.md](TODO.md)
for remaining work, [ROADMAP.md](ROADMAP.md) for product choices, and the
[operations record](deployment/OPERATIONS.md#live-acceptance--2026-10-28) for dated
production evidence.

## One repository and three runtime roles

FastAPI, the crawler and migrations share one codebase, Python environment and
PostgreSQL schema. PostgreSQL 17 is used in development, CI and production so
foreign keys, migrations and SQL behavior are exercised consistently.

The web application and crawler start independently. Web serves the last
published catalog continuously; starting or restarting it never starts a crawl.
One immutable application image supplies web, one-shot crawler and migration
roles. Caddy is the public HTTPS proxy. See [README.md](README.md).

## Local discovery and a small public interface

Discovery reads PostgreSQL only; visitors never trigger GitHub requests or need
a GitHub login. Only `/` and `/api/random-repository` are public application
routes. OpenAPI, documentation, crawler status and filter-management endpoints
stay disabled. Operators inspect progress with `python -m app.cli status`.

`Any` omits that filter's SQL predicate without changing crawler coverage.
Pushed-time filters use GitHub `pushed_at`, with exact timestamp comparisons
locally; GitHub's date-granular search can discover approximate bucket edges.
The next selection excludes the displayed repository when alternatives exist,
but returns it again if it is the sole match.

JavaScript updates the result in place and keeps the displayed URL clean, with
ordinary GET submission as a fallback. Store operational timestamps in UTC and
display activity in the visitor's local time zone, with exact local timestamps
available on hover. Random responses use `no-store`.

## Canonical repositories and atomic catalog publication

GitHub's numeric repository ID is the canonical identity. Owner/name can change
or be reused, so historical full names are not unique identifiers. Metadata is
upserted across generations and may refresh during a staging crawl.

A new generation copies active memberships and refreshes each rediscovered
membership's `last_seen` time. Only a fully successful crawl atomically replaces
the active discovery pool. Failure leaves the prior active membership intact.

At publication, remove staging memberships unseen for three days. Separately,
delete retired/failed generations created more than seven days earlier, cascading
to their run and membership records; then delete repository metadata with no
remaining membership. Active/staging generations are excluded from this cleanup.
Historical references can therefore retain metadata beyond the active catalog's
three-day window. Both retention durations are configurable.

Keep both generation-first and repository-first membership indexes: the latter
avoids expensive foreign-key scans during orphan cleanup. Refresh planner
statistics after initial population/restore when measured plans warrant it;
the [operations guide](deployment/OPERATIONS.md) gives the maintenance command.

## Bounded crawl coverage and scheduling

Each crawl covers 10 languages × 5 star ranges × 4 pushed-time ranges: 200 concrete
combinations, each with up to ten 100-result search pages. Search request starts,
including bounded rate-limit and transport retries, are paced at least five
seconds apart by default. Authentication token requests are separate.

Use one ordering per full crawl, rotating after success through `updated desc`,
`stars desc`, `updated asc`, `stars asc`. Failures do not advance the cycle. The
normal production timer runs at 00/06/12/18 UTC (05:30/11:30/17:30/23:30 IST).
Four successful runs complete a rotation; failed runs can extend that period.

The persistent timer makes one catch-up activation after downtime. Each later
slot remains independent of a prior failure, without an immediate restart loop.
The supervisor's five-hour limit and independent systemd fallback bound hung
runs. See the [operations guide](deployment/OPERATIONS.md) for stop/retry policy.

## Explicit resume and fenced ownership

Each ingested page commits its next work item/page and a fingerprint of the
original search plan. `crawl --resume` is an explicit operator choice retaining
those original boundaries and sort order. Scheduled runs always start normally.

Crawls and online Alembic share a nonblocking PostgreSQL session advisory lock.
All writes/checkpoints/DDL stay on its owning physical connection; connection
and commit guards prevent a disconnected worker from reconnecting as an unfenced
writer. The owner is physically closed on exit rather than returned to a pool.
Readers remain available; transaction-pooling proxies are unsupported.

A new exclusive owner reconciles abandoned running/staging pairs, preserving
checkpoints and active membership. Inconsistent state requires inspection. Run
age never authorizes eviction of a live owner. This is cooperative coordination;
manual SQL and legacy processes can bypass it and must be managed explicitly.

The host supervisor also holds deployment exclusion and tracks its exact Docker
child by invocation/name/labels/ID. Timeout, signals and `ExecStopPost` clean only
that owned child; killing a Docker client alone does not establish child cleanup.
A daemon failure leaves an inspectable recovery obligation, not assumed success.

## GitHub Flow and reproducible dependencies

Use short-lived branches and SSH-signed commits through protected `main`; there
is no permanent `dev` branch. Require `Python checks`, `PostgreSQL tests` and
`Production containers`, with up-to-date branches and no protection bypass.
Solo-maintainer rules do not require a second reviewer, while agent merges still
need user authorization. UI changes require manual user verification before
completion or commit. Contributor instructions live in [AGENTS.md](AGENTS.md)
and [CONTRIBUTING.md](CONTRIBUTING.md).

Commit one `uv.lock`; use locked installs in development, CI and production,
with reviewed/hash-constrained build tools and immutable image/action pins.
Python minor/major and PostgreSQL major changes need deliberate migration review.
Dependabot proposes reviewed updates rather than automatically deploying them.
CI uses disposable credentials/data and fake search responses, never production
secrets or live GitHub crawls. Container CI runs natively on the target architecture.

## Signed releases are separate from deployments

Stable releases use SSH-signed annotated semantic-version tags and the exact
source commit's passing main CI. Tags and completed assets are immutable. A fix
requires a new version; prereleases require a separate policy decision.

Publish to public GHCR for anonymous VM pulls, using Actions `GITHUB_TOKEN`.
Deploy by immutable digest with source-matched Compose/scripts. Verify the
published digest, signed archive/image attestations, source/workflow/tag identity,
platform/labels and actual provenance/SBOM before usable assets are attached.
Checksums and labels alone do not authenticate the publisher.

Current releases target AMD64 for the provisioned E2 micro. Preserve the older
ARM64-only v0.1.0 artifacts, which also predate the CA trust fix; do not deploy
that release on this host. Merging `main` never deploys it automatically.
See [deployment automation](deployment/AUTOMATION.md) for the trust protocol.

## Approved outbound deployment and safe rollback

A protected, manually approved `main` workflow queues a short-lived request. The
VM polls over outbound HTTPS; it exposes no deployment SSH/webhook and runs no
privileged self-hosted Actions runner. A Deployment payload alone is insufficient:
verify its immutable run/attempt approval receipt, reviewed workflow, source and
repository-scoped operations App's numeric bot identity. Optional App metadata
cannot replace these checks. Keep the crawler App separate and narrowly scoped.

The trusted host bootstrap verifies the selected release and dispatches its own
matching worker. Serialize backup, migration, restart, smoke test and promotion
under host/database ownership. Verify configuration agreement and a fresh checked
backup before DDL; explicit first bootstrap never overwrites an existing database.

Durably record claims and execution outcomes so crashes/cancellation cannot replay
claimed migrations. Retry remote status delivery separately from truthful local
execution state. Preserve current/previous release files, images and attempt
journals. A successful pointer need not describe runtime after a failed attempt.

Rollback is explicit and requires a verified prior release, unchanged schema
revision and operator compatibility acknowledgement. Never automatically downgrade,
restore over live data or remove volumes. See [operations](deployment/OPERATIONS.md).

## Host policy and bootstrap compatibility

Host units/bootstrap upgrades are deliberate maintenance, separate from app
release deployment. Preserve published v0.1.1 artifacts when applying the reviewed
host-side fixes: writable private alert state within the puller sandbox, distinct
Actions-artifact/release-asset media negotiation, and a fresh private GH/XDG runtime
for attestation verification. Retain download/redirect bounds, credential stripping,
TLS and every source/signer constraint. See [AUTOMATION.md](deployment/AUTOMATION.md).

Secrets live in private root-owned files outside Git/release archives. Only the
crawler receives its read-only App-key mount. Its required root/Docker access is
administrative access; capabilities and filesystem restrictions are defense in
depth. Install/update policies only from verified matching sources.

## Small-host capacity and private networking

The zero-cost target is the provisioned Ubuntu AMD64 E2.1.Micro with about 952 MiB
RAM and a 100 GB boot volume. Verify free eligibility/quotas before adding resources;
never silently upgrade the account or add paid capacity.

Use one web worker and measured limits: PostgreSQL 256 MiB, web 192 MiB, crawler
192 MiB and Caddy 64 MiB; one-shot migrations have 128 MiB. A real web OOM at the
old 128 MiB ceiling justified raising only web. The 704 MiB normal ceiling total
is not reserved RAM or a capacity guarantee. Native CI uses a shared 768 MiB
no-swap rehearsal; live CPU, catalog growth and host overhead still need observation.
Do not build images or add parallel recovery stacks on this VM. See
[LOW_MEMORY.md](deployment/LOW_MEMORY.md).

Only Caddy publishes ports. Web/database stay on trusted private Docker networks;
Uvicorn trusts reachable proxy peers and Caddy sanitizes public forwarded headers.
An untrusted peer, public web port or additional proxy requires reviewing that
trust boundary. Exact hostname allowlists, read-only application filesystems,
resource limits and bounded Docker/journal logs remain explicit policy.

Use restricted, temporary Bastion access with passphrase-protected keys and
root/password SSH disabled. PostgreSQL and Docker APIs stay private. Optional
[pgAdmin](deployment/PGADMIN.md) uses a separate loopback-only overlay and SSH
forwarding through the VM; normal deployment never starts it.

## Encrypted recovery copies with conservative retention

Daily backups run at 05:15 UTC / 10:45 IST using matching PostgreSQL dump tools.
Encrypt with age public recipients on the VM; private recovery identities stay
offline, independently of the VM. Archives exclude App/SMTP/database credentials,
which need their own recovery records. Restore only trusted archives into a new
explicitly named database, validate checksums/schema/discovery, then plan cutover.

Promote a backup success watermark only after encryption, unique OCI upload,
complete downloaded SHA-256 verification, retention and lease cleanup. Use a scoped
instance principal without general overwrite/delete rights. Local-only rehearsals
do not count as off-site success.

Local retention is seven days with at least two verified copies, even when old.
Prune only after a new verified off-site backup; preserve unknown/corrupt/pending
artifacts for inspection. Remote deletion stays off: an age-only lifecycle rule
must not expire every usable copy during a prolonged outage. Review remote usage
monthly and protect recovery copies before any separately supervised pruning.
Recovery objectives are targets until full replacement-host rehearsal measures
them. Details and trust limits are in [BACKUPS.md](deployment/BACKUPS.md).

## Durable email alerts and independent monitoring

Use private TLS-only SMTP configuration with separately revocable Gmail App
Passwords for host and GitHub monitoring. Keep credentials/addresses out of Git,
commands and public logs. Persist pending notification state before delivery;
accepted SMTP can still precede a failed state update and duplicate a retry.
Recovery follows an accepted failure notice. Actual inbox receipt needs validation.

Success watermarks advance only after managed work and cleanup succeed; later
failures preserve prior successes. The hourly host monitor checks crawl/backup
freshness, disk/inodes and verified TLS expiry, with durable retries and cooldowns.
It cannot report loss of its own VM or network.

GitHub-hosted monitoring validates public HTTPS/body and authenticated compact
host/crawl/backup timestamps through a non-production deployment anchor. Its
SMTP secrets use a separate protected-branch monitoring environment; safe incident
codes/pending state live in one dedicated issue, without raw logs or catalog data.

GitHub's fifteen-minute cron is best effort; measured delivery gaps justified a
free five-minute UptimeRobot HTTP/S availability supplement. It does not replace
GitHub's strict response/freshness checks or provide a detection SLA. Both retain
provider/SMTP dependencies. See [MONITORING.md](deployment/MONITORING.md).

## Remaining choices and evidence

Retaining membership by successful-crawl counts versus elapsed time, expanding
sort coverage and multi-select filters remain product questions in
[ROADMAP.md](ROADMAP.md). Keep actionable acceptance, time-dependent observations
and explicitly parked work in [TODO.md](TODO.md). Completed release and production
checks belong in the dated [operations record](deployment/OPERATIONS.md), not in
an ever-growing task checklist. CI, injected faults and real outages are distinct
evidence; none substitutes for a full replacement-VM recovery rehearsal.
