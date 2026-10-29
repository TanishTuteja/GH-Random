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

If broad discovery becomes slow after initial catalog population or a restore,
inspect a bounded read-only query plan and the planner statistics first. The
small generation table can remain unanalyzed while the membership table grows.
Run this unchanged for the documented production container; no replacements
are needed. It refreshes statistics without changing catalog rows or schema:

```sh
sudo docker exec gh-random-database-1 sh -eu -c 'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1 -c "SET statement_timeout = 15000; ANALYZE catalog_generations;"'
```

Measure the same request again and keep memory limits in place. This maintenance
does not remove the cost of randomly selecting across a growing catalog; inspect
plans and latency again after substantial growth rather than raising every limit.

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

The operator performed the initial OCI/SSH/private configuration work and supplied
the first-deployment results. Later, the operator explicitly authorized remote
VM acceptance through temporary Bastion SSH. The additional checks below combine
that direct inspection with public HTTPS/GitHub evidence and operator-confirmed
inbox delivery. The separate Windows restore did not overwrite production.
No credentials, email addresses or private key material/paths belong in this record.

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
  confirmed at 06:00 UTC/11:30 IST. Subsequent scheduled rotation is recorded below;
  interrupted-run recovery remains a separate live check.
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

### Further live acceptance — 2026-10-28/29

- **Scheduled crawling:** at 19:39 UTC on October 28, three completed crawls had
  used updated-descending, stars-descending and updated-ascending. The latter two
  ran 06:00:24–08:06:41 and 12:00:23–14:09:28 UTC. A fourth, stars-ascending,
  started at 18:00:19 UTC and was still progressing at 20:06 UTC. The inspection
  counted 174,915 canonical repositories, 145,707 active memberships and a 150 MB
  database; staging/retired memberships explain why these counts differ. These
  are dated observations, not current or lifetime totals.
- **Next-morning inspection:** at 08:05 UTC on October 29, five crawls had
  completed with zero failures, and a sixth was running stars-descending. The
  fourth completed stars-ascending at 20:08:37 UTC on October 28; the fifth ran
  updated-descending from 00:00:33 to 02:22:14 UTC on October 29. This proves all
  four sort orders and a cycle wrap. Read-only SQL counted 203,609 canonical
  repositories, 903,801 memberships across six generations, 202,688 active
  memberships and 227,186,355 database bytes. Counts include retained/staging
  data and are snapshots, not lifetime totals.
- **Live memory correction:** kernel evidence showed Uvicorn OOM-killed at
  19:22:35 UTC under its 128 MiB limit. Only web was recreated with a private
  192 MiB setting, retaining the exact v0.1.1 digest and database/crawler. The
  subsequent 20 representative API requests, at most two concurrent, produced
  no web OOM events or restarts and a roughly 106 MiB measured peak. Client
  transport-inclusive p95 was about 13.25 seconds; this sample establishes
  neither sustained capacity nor a latency guarantee. PR #41 mirrors the
  192 MiB default; normal container ceilings total 704 MiB. The shared 768 MiB
  native rehearsal remains synthetic and separate from this real incident.
  The October 29 inspection still found zero web restarts/OOMs under 192 MiB,
  a healthy database and no database/Caddy restarts. The host had 353 MiB
  available memory and approximately 92 GB free disk space.
- **Host hardening:** effective sshd configuration rejects root, password and
  keyboard-interactive login; a fresh Ubuntu key connection passed after reload.
  Private production files were root-owned mode 0600. The installed journald
  policy limits persistent/runtime usage to 256/64 MiB, reserves 1 GiB free and
  retains at most seven days. The operator confirmed OCI MFA. The console showed
  the running micro as Always Free and the tenancy as Free Tier/trial.
- **Public behavior:** all language, star and pushed filter options matched
  their API semantics. Ruby with 100,001+ stars produced the expected empty 404;
  C# in the same band had one repository and exclusion correctly returned it
  again. Other exclusions switched results when alternatives existed. Browser
  discovery updated the result while leaving the root URL clean. These automated
  checks do not stand in for the operator's manual UI verification.
  Fresh browser checks on October 29 rendered the Ruby/100,001+ empty-result
  message correctly and returned the sole C#/100,001+ repository on two
  submissions, retaining the clean URL in both cases.
- **Planner maintenance:** on October 29, a bounded query plan underestimated
  202,688 matching rows as 1,181 and used a nested-loop repository lookup.
  Refreshing only `catalog_generations` statistics changed the plan to a hash
  join. The direct full discovery request measured 10.807 seconds before and
  1.973 seconds after; a subsequent public HTTPS request from the VM took 2.318
  seconds. This is a short measurement under a live crawl, not a latency SLA.
  No catalog rows, schema, application image or memory settings changed.
- **Network observations:** public TCP 80/443 were reachable; 22, 5432, 5050,
  2375, 2376 and 8000 timed out from the external client. Only Caddy published
  container ports. This does not identify which firewall blocked a port or prove
  OCI ingress rules, external UDP/IPv6, or the app's forwarded client address.
  TLS peer/hostname validation and the canonical HTTP redirect passed; the
  observed certificate expires January 25, 2027. Renewal has not yet occurred.
- **Email acceptance:** installed notification helpers ran with isolated state
  on the VM. A real loopback TLS SMTP 550 rejection retained pending delivery;
  retry through the configured provider succeeded. Stale crawl, stale backup,
  low-space threshold and near-expiry TLS probes each produced failure/recovery
  mail; the operator confirmed all eight arrived. Disk space was not consumed
  artificially, the host clock/certificate were unchanged, and production alert
  state/success-marker hashes were preserved. Host/crawl/backup external freshness
  codes were also checked independently with in-memory API fixtures, not forged
  production telemetry or a real host outage.
- **Independent availability:** UptimeRobot monitor `804462089` is active/Up
  on the Free plan, checking the discovery API every five minutes. The operator
  confirmed its test email arrived; no payment method or paid credits were added.
  It supplements GitHub's stricter response/freshness checks. Actual consecutive
  GitHub scheduled runs on October 28 were separated by 6 h 45 min 51 s; the
  configured fifteen-minute cron must not be treated as an execution guarantee.
- **Backup usage:** at 19:51 UTC on October 28, the private remote prefix held
  four objects totaling about 14.9 MB, including the setup-test object. Three
  verified local backups existed. Remote deletion remains disabled; review usage
  monthly and preserve at least two known-good off-site copies before considering
  any independently supervised pruning policy.
- **Scheduled backup and timers:** the October 29 backup ran 05:15:08–05:16:16 UTC
  and succeeded after full off-site GET/checksum verification at 05:16:12 UTC.
  All five managed timers were active/enabled at the morning inspection, with
  the last service result successful. This is normal scheduling evidence;
  it does not prove startup or missed-slot catch-up after a reboot.
- **Packaged crash recovery on the VM:** an isolated temporary database used
  the exact released application image and a one-item fake search plan with no
  GitHub traffic. An actual `os._exit(17)` left a running checkpoint while its
  prior active membership remained intact. The next normal crawl marked that
  run/generation failed, retained the original membership, published successfully
  and used stars-descending without advancing past the failed sort. Concurrent
  ownership was refused. The test finished at 08:23:40 UTC on October 29; its
  owned database and containers were removed. The real production crawl and
  success markers were untouched. This verifies the packaged application path
  on the host; managed systemd-child interruption remains a separate exercise.
- **Updated remote inventory:** at 08:26 UTC on October 29, the backup prefix
  held five objects totaling 41,892,828 bytes. No test containers remained.
- **Real public-site outage and recovery:** Caddy alone was stopped from
  08:29:43 to 08:37:15 UTC on October 29, with an eight-minute automatic recovery
  backstop installed first and removed after restoration. PostgreSQL and the
  real crawl continued running. GitHub's
  [failure check](https://github.com/TanishTuteja/GH-Random/actions/runs/40842618556)
  recorded a `site` incident and its
  [recovery check](https://github.com/TanishTuteja/GH-Random/actions/runs/40843624776)
  succeeded; issue #26 returned to no active/pending incident. The operator
  confirmed both GitHub and UptimeRobot failure/recovery email pairs arrived.
  This proves real public-site outage detection without claiming complete
  VM/network-loss recovery. The operator also confirmed the live page's ordinary
  discovery, empty-result and singleton behavior manually.
- **Controlled reboot:** the sixth crawl published successfully at 08:43:40 UTC
  on October 29. Read-only preflight counted 203,759 canonical/active repositories,
  903,951 total memberships, active generation 6, six completed crawls and zero
  running crawls at schema `20261027_01`. A new managed encrypted backup finished
  at 08:47:44 after off-site verification at 08:47:39. Writer/deployment timers
  were paused and no writer was active before requesting reboot at 08:49:30 UTC.
  At 08:53:51 a changed boot ID, the same named database volume, exact catalog
  counts/schema/generation, unchanged v0.1.1 web digest and 192 MiB limit were
  verified. Homepage/discovery HTTPS, enabled/active startup of all five timers,
  SSH hardening, journald policy and managed monitor/telemetry/puller success
  passed. The next crawl remained scheduled for 12:00 UTC. No six-hour slot was
  missed, so this establishes reboot startup, not observed missed-slot catch-up.

Still required: the explicitly deferred second independent offline recovery-key
copy; genuinely missed-slot catch-up and managed-child interruption acceptance;
multi-day full-crawl resource peaks and three-/seven-day retention growth; actual
certificate renewal; remaining OCI/network checks; and replacement-host
configuration/DNS/credential recovery with measured RPO/RTO. Keep the specific
pending items in [TODO.md](../TODO.md) open.
