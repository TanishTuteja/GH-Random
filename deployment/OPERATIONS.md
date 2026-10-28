# Production operations

This Linux host runbook describes operator procedures; the
[dated acceptance record](#live-acceptance--2026-10-28) records verified live checks.
Use a private Bastion session. Commands assume the documented `/etc/gh-random`,
`/var/lib/gh-random/deployments` and `/opt/gh-random/releases` paths and Compose
project `gh-random`. Substitute the **verified current release directory** for
`/opt/gh-random/releases/v0.1.1`; do not use a checkout of moving `main`.
Never paste secrets, raw environment files, database URLs or unreviewed logs into
an issue or alert. Docker/root access is equivalent to administrative host access.

## Inspect first

Read `/var/lib/gh-random/deployments/current.json` privately to identify the
recorded release, commit, image digest and previous release. A failed deployment
may leave runtime containers different from that last successful record: inspect
the latest attempt journal and the actual container images too.

```sh
sudo systemctl list-timers 'gh-random-*'
sudo systemctl status gh-random-crawler.service gh-random-backup.service gh-random-monitor.service
sudo journalctl -u gh-random-crawler.service -u gh-random-backup.service --since today --no-pager
sudo docker compose --project-name gh-random --env-file /etc/gh-random/compose.env -f /opt/gh-random/releases/v0.1.1/compose.production.yaml ps
sudo docker compose --project-name gh-random --env-file /etc/gh-random/compose.env -f /opt/gh-random/releases/v0.1.1/compose.production.yaml logs --tail 100 web database caddy
sudo docker compose --project-name gh-random --env-file /etc/gh-random/compose.env -f /opt/gh-random/releases/v0.1.1/compose.production.yaml --profile crawler run --rm crawler python -m app.cli status
df -h / /var/lib/docker /var/backups/gh-random
df -i / /var/lib/docker /var/backups/gh-random
sudo docker system df
```

The status command reads PostgreSQL only; it does not start a crawl or contact
GitHub. `ps`, service state and an HTTP 200 alone do not establish freshness:
inspect `crawler-success.json` and `backup-success.json` and verify the public
discovery flow. Managed success markers survive later failures. Direct CLI
crawls do not update the managed marker; prefer the systemd service.

## Stuck or failed crawler

1. Inspect the crawler journal, catalog status, private `crawler.json` and
   Docker state. A live crawl owns a PostgreSQL session lock. Five-second pacing
   and GitHub retries can make slow progress normal; do not infer abandonment
   merely from elapsed time. The supervisor has a five-hour limit.
2. For an intentional stop, stop scheduling **and** the running service:

   ```sh
   sudo systemctl stop gh-random-crawler.timer gh-random-crawler.service
   ```

   ExecStopPost checks ownership labels and stops only its recorded Docker child.
   Killing a `docker` client or the Python controller alone does not prove that
   the daemon-owned crawler stopped. A Docker outage can prevent cleanup.
3. After Docker is healthy, retry the ownership-checked cleanup:

   ```sh
   sudo python3 /usr/local/lib/gh-random/crawler.py stop --state-dir /var/lib/gh-random/deployments
   ```

   If ownership validation refuses, inspect the exact record/container; do not
   remove unrelated containers, edit ownership labels, terminate arbitrary
   PostgreSQL sessions, or manually mark a live run failed.
4. Fix the underlying network, key, resource or service problem. Then start one
   managed **normal** crawl and restore the timer:

   ```sh
   sudo systemctl reset-failed gh-random-crawler.service
   sudo systemctl start gh-random-crawler.service
   sudo systemctl enable --now gh-random-crawler.timer
   ```

   The next exclusive crawler reconciles genuinely abandoned staging runs after
   PostgreSQL releases the dead owner's connection. The old active catalog stays
   available. A failed crawl does not advance the sort rotation. Manual `--resume`
   remains an explicit checkpoint-recovery choice, not the timer's default.
   Stopping a timer alone does not stop its active service; persistent timers
   make one catch-up activation after missed slots, not one per missed crawl.

## Failed deployment or migration

1. Retain the failed attempt journal, exact release files, previous digest and
   verified pre-deploy backup. Inspect the recorded phase, `schema_before`,
   `schema_after` (when present), and actual running images privately. Do not
   delete the database volume or report success merely because old web is up.
2. Ensure no deploy/crawl/backup still owns the host/database locks. Use the
   supervisor cleanup above if necessary. Do not bypass the locks or start a
   second migration process. EOF/idle limits release a lost coordinator; inspect
   its recorded identity before considering manual intervention.
3. A preflight or backup/upload failure occurs before DDL. Fix its cause and
   retry the same verified release. A migration failure needs schema inspection:
   PostgreSQL transactional DDL helps, but future nontransactional revisions may
   have partial effects. Never blindly `alembic stamp head`, downgrade, or rewrite
   production migration history.
4. After DDL succeeds but restart/smoke fails, the schema may already be new.
   Prefer a reviewed forward fix. Explicit image rollback is allowed only when
   both images work with the **current** schema, with
   `--confirm-schema-compatible` and the README's rollback command. The script
   refuses a schema-changing rollback. It never restores data automatically.
5. If data recovery is required, decrypt and restore a trusted backup into a
   **new database** as described in [BACKUPS.md](BACKUPS.md), inspect the restored
   schema/data, and test discovery with matching code. Stop writes, take a final
   recovery copy if possible, then deliberately switch the app **and crawler**
   database configuration during maintenance. Retain the old database for
   diagnosis; do not restore over it. Have a migration-specific recovery plan
   before releasing a future schema beyond the initial baseline.
6. Re-run public HTTPS/discovery smoke tests, catalog status and managed monitors.
   Record the effective digest, schema, outcome and any data-loss interval.

## Full disk, inodes or memory

Stop scheduled writers first, not PostgreSQL by force:

```sh
sudo systemctl stop gh-random-crawler.timer gh-random-crawler.service gh-random-backup.timer gh-random-backup.service
sudo journalctl --disk-usage
sudo docker system df
```

Inspect filesystem free **bytes and inodes**, Docker log usage, backup sizes,
failed `.partial-*`/`.work-*` directories and OOM events. Temporary backup work
may contain plaintext after SIGKILL or host loss. Only remove an individually
identified incomplete directory after proving no worker uses it; retain good
recovery copies. Do not delete PostgreSQL WAL/data files, truncate Docker-managed
log files, or use `docker system prune --volumes`/`docker volume prune`.

Use the bounded Docker/journald policies in README. If additional journal
vacuuming is necessary, first preserve incident evidence; vacuuming discards
history and is a deliberate host-wide action. Expand capacity or remove exact,
unneeded artifacts. Keep at least two verified backups and recovery keys outside
the VM. A failed upload is not grounds to delete the last local recovery copy.
Validate DB health, discovery, backup/read-back and crawl before re-enabling both
timers. Inspect `State.OOMKilled` for an affected container and measured peak
memory before adjusting limits; increasing every ceiling can overload the VM.

## Revoked or rotated credentials

- **GitHub App key:** stop the crawler timer/service, generate a replacement key
  in the App, install its PEM privately with root ownership and mode `0600`, and
  atomically replace the configured host path. Bind-mounted files are opened
  when a new crawler container starts: do not assume an existing mount changes.
  Test App/installation authentication without logging tokens, then run a managed
  crawl. Revoke the old key after the new one works; revoke first if compromised.
  Confirm the installation is still active and the App/installation IDs match.
- **Database password:** plan maintenance and stop all writers. Changing
  `POSTGRES_PASSWORD` in an env file does **not** alter an initialized database's
  role. Change the actual PostgreSQL role using a private administrative session,
  update postgres/app/crawler configuration consistently (URL-encode the password
  in SQLAlchemy URLs), then recreate affected services and test connections.
  Never pass the password in a public command line, issue or shell history.
- **SMTP:** install a new private password/config, run the notifier's `test`
  command, confirm arrival in the real inbox, and test failure/recovery. SMTP
  acceptance alone is not inbox delivery. Keep external monitoring independent.
- **Backup recovery keys:** follow BACKUPS.md. Keep old offline identities until
  every retained archive encrypted to them has expired or been re-encrypted and
  independently restored. The VM receives public recipients, not the identity.
- **Bastion/SSH and automation:** rotate access separately, prove the replacement
  path from a second session before removing working administrative access, and
  revoke old credentials. Do not open public SSH or Docker/PostgreSQL ports as a
  shortcut. Record key identifiers/dates, never private key material.

For suspected compromise, revoke exposed credentials promptly, preserve evidence,
use a clean replacement host, and verify code/backups before restoration. Do not
consider deleting a committed secret sufficient; rotate it and assess exposure.

## Updates and reboots

Review OS/Docker security updates at least monthly and promptly for relevant
advisories. Rehearse major upgrades separately. Schedule a maintenance window,
check external monitoring, verify a recent off-site backup and recovery access,
and stop crawler/backup timers **and services**. Apply host updates using the
distribution's supported tooling; review package prompts instead of an unattended
blind upgrade. Docker restart policies do not replace enabled host services.

After reboot, verify Docker is active, PostgreSQL is healthy, the expected image
digest is running, public HTTPS/discovery works, the active catalog is preserved,
and timers/monitoring are enabled. Check caught-up jobs and their success markers,
then test an email. Measure actual crawl duration/resource use and disk growth.
Do not automatically reboot in the middle of a crawl or migration. If a surprise
reboot interrupts one, inspect locks/schema/managed child before retrying.

Application and base-image updates go through green PRs and signed releases.
PostgreSQL major upgrades require an explicit `pg_upgrade` or dump/restore plan,
not attaching a new major image to the old volume. Replacing deployment
configuration is separate from silently updating installed host units/bootstrap:
stop, install matching reviewed files, `daemon-reload`, verify, then restart.

## Image and artifact cleanup

Keep the current digest, `current.json`'s previous digest, every active/failed
attempt's required recovery image, matching verified release directories and
trusted backups. Inspect before deleting:

```sh
sudo docker image ls --digests --no-trunc
sudo docker ps --all --no-trunc
sudo docker system df
```

Remove only an explicitly reviewed obsolete image using `docker image rm` and
its exact digest/ID, **without `--force`**. Do not run blanket image/system/volume
prunes or assume that an unused image is not the rollback image. Preserve
PostgreSQL/Caddy named volumes and backup/release metadata. Backup retention is
handled by the verified backup worker, not by a generic cleanup job; remote
lifecycle rules must not expire every usable recovery copy during an outage.

## Complete VM loss or replacement

1. From an independent device, obtain a verified signed release/digest, the
   matching deployment bundle, encrypted off-site archive, offline recovery
   identity, and private configuration/credential recovery material. Recovery
   must not depend on files existing only on the lost VM. Revoke compromised
   access if appropriate.
2. Provision a clean host matching the verified release architecture manually,
   patch it, install Docker/Compose and
   required Python/age/OCI tools, configure private Bastion access and firewall,
   and recover production configuration with private ownership/permissions.
   Do not copy arbitrary executables or raw private state from a compromised VM.
3. Verify/decrypt the archive **offline** and restore into a new PostgreSQL
   database/volume with matching major version and release schema. Test schema,
   catalog counts, discovery and backup integrity before switching public DNS.
   Keep the recovery identity off the replacement production VM when possible.
4. Deliberately start the database service, restore and verify it before running
   the normal `deploy.py apply` against its matching release and private config.
   An existing database must already be running with the expected image and
   credentials; `--bootstrap` only authorizes creating a missing database service,
   not restoring data. Normal apply takes a fresh backup and runs migrations:
   confirm the intended revision first. A new private deployment-state directory
   can record the recovered host's first successful apply. Do not copy old
   `current.json` paths blindly; image rollback requires a verified previous
   release record and files available on this host.
5. Switch/verify DNS and TLS, install matching managed units, run a fresh managed
   crawl and encrypted off-site backup, verify alerts/external monitors and reboot
   recovery, then record actual data loss (RPO) and recovery duration (RTO).

The 24-hour backup RPO and one-hour restore target **after a ready host** are
planning targets, not measured guarantees. Test this procedure with a real
off-site production archive before relying on it. OCI creation, public DNS,
provider delivery and live VM-loss acceptance remain manual prerequisites.

## Acceptance record

Keep a private operational record of hostname, VM/backup identifiers, installed
unit versions, current/previous image digests, schema revision, last verified
restore, credential rotation dates, SMTP/external monitor tests, actual capacity
and recovery measurements. Record identifiers/results, not passwords or keys.
Public launch requires real HTTPS, active discovery catalog, timely crawl/backup
success, independent downtime alerts and a verified recovery path. Repository CI
cannot establish any of those live-account conditions.

## Live acceptance — 2026-10-28

The operator performed OCI/SSH/private configuration work personally and supplied
the host results below. GitHub metadata and public HTTP/API checks were verified
independently. The recovery rehearsal used the operator's separate Windows host;
it did not connect to or overwrite the production database. No credentials,
email addresses, private key contents or private key paths belong in this record.

- **Release and deployment:** [v0.1.1](https://github.com/TanishTuteja/GH-Random/releases/tag/v0.1.1),
  source `94b9c94ff0232b3f3d8f0e8d5d7c25b902af548b`, AMD64 image
  `ghcr.io/tanishtuteja/gh-random@sha256:c5a6732f67d445be36eaaa00ded7706415982b13a5f3ad10b68eea7ca991e697`.
  [Production workflow](https://github.com/TanishTuteja/GH-Random/actions/runs/40596008134)
  and deployment `7372126619` succeeded. Bootstrap authorization was disabled
  afterward. Operator-applied puller sandbox, artifact-media and attestation
  runtime fixes from PRs #37–39 preserve the signed release archive/image.
- **Public service:** https://ghrandom.duckdns.org returns verified HTTPS 200,
  HTTP redirects to its canonical HTTPS origin with 308, and the public API
  returns actual catalog results. Any/Python filtering and exclusion of the
  currently displayed repository passed direct public API checks. Certificate
  renewal, forwarded-client-address and full port isolation remain separate.
- **First managed crawl:** 2026-10-27 22:45:51 to 2026-10-28 00:54:19 UTC
  (2026-10-28 04:15:51–06:24:19 IST), 200/200 combinations, 62,459 staged
  repositories and `succeeded cleanup=True`. The enabled timer's next slot was
  confirmed at 06:00 UTC/11:30 IST. Scheduled sort rotation and interrupted-run
  recovery have not yet been exercised on this host.
- **Catalog backup:** 2026-10-28 04:30:22–04:31:08 UTC, encrypted upload and
  complete off-site SHA-256 read-back succeeded. The daily 05:15 UTC/10:45 IST
  timer and 36-hour backup freshness checks were enabled afterward. Local
  retention is seven days/minimum two verified copies; remote deletion is off.
- **Independent restore:** the exact off-site archive's SHA-256 was
  `6014267aedfe205350027853fa811262ce3618200eddae084d0f07319b4a332b`.
  Windows download, age authentication and inner dump checksum verification
  passed. An isolated PostgreSQL 17 container restored schema `20261027_01`,
  62,459 repositories, one generation, 62,459 memberships/active memberships
  and zero running crawls. The exact released image passed homepage, discovery,
  immediate-repeat avoidance and Python filtering checks against the restored
  database. The temporary container/anonymous database volume were removed;
  encrypted backup and offline identity were preserved. This proves database/app
  recovery, not a rebuilt OCI VM or a measured full recovery-time target.
- **Local monitoring:** the operator confirmed successful completed disk/TLS/
  freshness checks and telemetry publication. The hourly monitor and publisher
  timers are enabled. Puller failure/recovery email receipt was also confirmed.
- **Independent monitoring:** telemetry anchor `7372452460` is authored by the
  expected deployment App bot `349016861`; host/crawl/backup watermarks match
  proven success. Protected `monitoring` secrets and branch restrictions were
  read back without exposing values. The
  [healthy baseline](https://github.com/TanishTuteja/GH-Random/actions/runs/40634684303),
  [controlled failure](https://github.com/TanishTuteja/GH-Random/actions/runs/40634818327)
  and [recovery](https://github.com/TanishTuteja/GH-Random/actions/runs/40634860298)
  exercised the real GitHub/SMTP/issue-ledger path. Only the monitor origin was
  temporarily set to a reserved invalid hostname; the production site stayed
  online. The operator confirmed both emails arrived before enabling the schedule.
  `MONITORING_ORIGIN` was restored to the real site, issue #26 has no active or
  pending incident, and the
  [enabled real-site check](https://github.com/TanishTuteja/GH-Random/actions/runs/40635039621)
  passed. This does not establish actual VM-loss or stale-watermark handling.

Still required: a second independent offline recovery-key copy (the operator
confirmed only one exists), actual scheduled runs/rotation, full-crawl resource
peaks and retention growth, reboot/renewal/port checks, separate staleness and
SMTP-retry inbox tests, and replacement-host configuration/DNS/credential
recovery. Keep the specific pending items in [TODO.md](../TODO.md) open.
