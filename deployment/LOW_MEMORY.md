# OCI micro deployment

The current target is the provisioned `VM.Standard.E2.1.Micro`: AMD64, about
952 MiB RAM and a 100 GB boot volume. This replaces the proposed Ampere host
because A1 capacity was unavailable in Hyderabad. Published `v0.1.0` is ARM64
only; **do not deploy that image on this host**. Wait for a newly signed AMD64
release and its matching deployment bundle. Never rebuild or replace v0.1.0.

## Configuration

The production Compose defaults and `compose.env.example` now use:

| Service | Memory ceiling | Notes |
| --- | --- | --- |
| PostgreSQL | 256 MiB | 64 MB shared buffers, 32 connections |
| Web | 128 MiB | One Uvicorn worker |
| Crawler | 192 MiB | One scheduled crawl; five-second GitHub pacing |
| Migration | 128 MiB | One-shot process under the deployment lock |
| Caddy | 64 MiB | Public proxy; no pgAdmin on this small host |

These are hard limits, not reserved RAM. The normal web/database/proxy/crawler
ceilings total 640 MiB; deployment and crawler coordinators also consume host
memory outside these containers. Migrations and managed crawls do not overlap.
The backup lock serializes managed database backups against crawls/deployments;
public requests continue. Do not build images on this VM or run development
tools, parallel restores, pgAdmin, or extra web workers here.

`WEB_WORKERS` and the existing `*_MEMORY`/`*_CPUS` settings live in the private
Compose env file. PostgreSQL tuning is configurable there through
`DATABASE_MAX_CONNECTIONS`, `DATABASE_SHARED_BUFFERS`, `DATABASE_WORK_MEM`,
`DATABASE_MAINTENANCE_WORK_MEM`, `DATABASE_AUTOVACUUM_WORK_MEM`, and
`DATABASE_MAX_PARALLEL_WORKERS`. JIT is disabled. Changing these server settings
requires deliberately recreating the database container **with its existing
volume**, during a maintenance window after a verified backup. Never remove its
volume to apply tuning. More connections and workers can consume more memory;
`work_mem` applies per operation, not once per database. See
[PostgreSQL resource settings](https://www.postgresql.org/docs/17/runtime-config-resource.html).

## Rehearsal and limits

Required AMD64 container CI runs `.github/scripts/check_low_memory.py` in a
disposable Compose project under a shared 768 MiB cgroup with swap disabled.
It exercises a synthetic catalog, retained generation memberships, crawler
publication, discovery traffic and a database dump, and reports measured peak
memory. The exact published image must pass the same rehearsal before release
assets are attached. This checks actual memory enforcement rather than adding
up individual limits. Each rehearsal creates and removes only its own resources.
The fixture has 50,000 active repositories, 28 retained snapshots and one expired
snapshot (1,455,000 memberships initially). The expired snapshot also owns
5,000 otherwise unreferenced repositories, exercising bulk orphan deletion.
All 200 filter combinations run;
each synthetic search returns one page, refreshing 20,000 repositories. This
does not cover the maximum ten-page result set or a larger future catalog.

A cgroup on a faster CI machine does not reproduce the micro VM's shared CPU,
disk, kernel, Docker daemon, Oracle agent or real repository sizes. Synthetic
search responses do not establish live GitHub throughput, and a short test
does not establish multi-day capacity. A failed test blocks release; a passing
test is followed by the live acceptance below, not a claim of unlimited capacity.

## Before public launch

1. Patch Ubuntu and install Docker/Compose; verify the release reports
   `linux/amd64`. Pull the immutable image; build elsewhere.
2. Optionally add a 1 GiB swap file as a host emergency margin after checking
   existing swap and disk. Do this once, not blindly on every deploy. Swap does
   not increase the container memory ceilings and is not a substitute for RAM.
3. Start the verified bundle using the documented deployment procedure, then
   run the first supervised full crawl while watching host and container memory.
4. Verify discovery during the crawl, six-hour scheduling, the three-day rolling
   catalog and seven-day generation retention, a complete encrypted backup and
   off-site read-back, alerts, and a tested restore on a separate disposable host
   or during a deliberate maintenance window. Do not run an extra restore stack
   beside the production stack on this VM.
5. Check after reboot and after several days of retained generations. Record
   peak usage, swap activity, request latency, crawl duration and OOM counters.
   The micro's CPU allowance can make a memory-safe job too slow for its window.

Read-only observations over Bastion:

```sh
free -h
swapon --show
vmstat 1 10
sudo docker stats --no-stream
sudo journalctl -k --since today --grep='oom\|Out of memory\|Killed process'
```

Inspect each affected container's `State.OOMKilled` and `RestartCount`; a healthy
web page after an automatic restart is not proof that the crawl succeeded.
If memory kills, persistent swapping, unacceptable latency or five-hour crawl
timeouts occur, hold launch and revise capacity. Retry A1 availability or split
the database onto a second eligible VM after reviewing its private networking,
backups and actual free-tier allowances. Do not silently raise all ceilings.
