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
   `--confirm-schema-compatible` and the [rollback command](../deployment/DEPLOYMENT.md#explicit-image-rollback). The script
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

Keep the bounded Docker logs and [resource limits](../deployment/LOW_MEMORY.md) in place. If additional journal
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

## Public IP and DNS recovery

The October 29 console read-back confirmed the production public IPv4 is
**ephemeral**, not reserved. It does not transfer to a replacement instance;
see [Oracle's public-IP lifecycle](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/managingpublicIPs.htm).

1. Recover access to the existing DuckDNS account from an independent device.
   Keep its account recovery material and token private and available outside
   the VM; no automated IP updater has been established by these checks.
2. Read the replacement VM's public IPv4 in OCI after its restored database,
   application and restricted network path are ready. Record the old address
   privately for rollback if the old host is still available.
3. In the signed-in DuckDNS dashboard, set `ghrandom`'s IPv4 explicitly to the
   replacement VM address and update it. Do not use the device's detected IP.
   For a scripted update, use HTTPS and an explicit `ip` with the account token
   supplied privately, as documented in the [DuckDNS API](https://www.duckdns.org/spec.jsp).
4. Preserve the Google ownership TXT record. DuckDNS has one shared TXT value
   for the hostname and its subdomains; changing that value is a separate update.
   Publish no AAAA record until IPv6 routing, ingress and service reachability
   have been verified end to end.
5. Verify public A resolution, Caddy certificate issuance/validation, homepage,
   discovery and both external monitors. DNS caching can delay cutover. If
   reverting, first establish that the old host is healthy, then restore its
   explicit address and repeat those checks. No live DNS cutover was rehearsed.

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
5. [Switch/verify DNS and TLS](#public-ip-and-dns-recovery), install matching
   managed units, run a fresh managed crawl and encrypted off-site backup, verify
   alerts/external monitors and reboot recovery, then record actual data loss
   (RPO) and recovery duration (RTO).

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

Verified October 28–29, 2026; the reboot snapshot is from **October 29 at
08:53:51 UTC**, with later checks individually dated below. These are dated
results, not a live status page.
Remaining tasks and observation windows are tracked only in [TODO.md](../project/TODO.md).

### Deployed release

- Site: https://ghrandom.duckdns.org
- Release: [v0.1.1](https://github.com/TanishTuteja/GH-Random/releases/tag/v0.1.1), AMD64.
- Signed source: `94b9c94ff0232b3f3d8f0e8d5d7c25b902af548b`.
- Image: `ghcr.io/tanishtuteja/gh-random@sha256:c5a6732f67d445be36eaaa00ded7706415982b13a5f3ad10b68eea7ca991e697`.
- Schema: `20261027_01`; initial bootstrap authorization is disabled.
- Separately installed puller fixes from PRs #37–39 and the private 192 MiB web
  setting preserve the immutable release archive/image. PR #41 mirrors the
  memory default for future releases.

### Verified checks

| Check | Result |
| --- | --- |
| Public discovery | Valid HTTPS, canonical HTTP 308 redirect, working homepage/API, all language/star/pushed filters, exclusion with alternatives, singleton fallback, and empty results. Browser submissions retain the clean URL; the operator manually confirmed ordinary, empty and singleton cases. |
| Scheduled catalog | Six completed crawls, all four sort orders and cycle wrap. Final snapshot: 203,759 canonical/active repositories, 903,951 total memberships, active generation 6, zero running crawls. Counts include retained history and are not lifetime totals. |
| Off-site backups | Encrypted upload and full SHA-256 read-back passed. A fresh pre-reboot backup completed October 29 at 08:47:44 UTC after verification at 08:47:39. Daily schedule is 05:15 UTC; remote automatic deletion remains off. |
| Independent restore | An actual off-site archive restored on the separate Windows host into isolated PostgreSQL 17: schema `20261027_01`, 62,459 repositories/active memberships, one generation, zero running crawls. The exact released image passed page/API discovery, filtering and repeat avoidance. Owned restore resources were removed. |
| Application crash recovery | The exact released image ran a one-item fake plan in an isolated database on the VM. Actual `os._exit(17)` preserved the active membership; the next normal crawl recovered the abandoned run, retained membership and the expected sort, and published successfully. Concurrent ownership was refused; owned test resources were removed. |
| Failure-email behavior | A real loopback TLS SMTP 550 rejection retained pending delivery and provider retry succeeded. The operator received eight isolated stale-crawl/stale-backup/disk/TLS failure/recovery emails. Production success markers, host clock, certificate and disk contents were unchanged. |
| Real site outage alerts | Caddy alone stopped October 29 from 08:29:43 to 08:37:15 UTC, with an automatic recovery backstop installed first. Database and crawler kept running. GitHub detected/cleared the site incident; the operator received both GitHub and UptimeRobot failure/recovery pairs. The backstop was removed after restoration. |
| Host hardening | Root/password/keyboard-interactive SSH disabled and a fresh Ubuntu key connection verified; private production files root-owned mode 0600. Installed journald limits: 256 MiB persistent, 64 MiB runtime, 1 GiB free reserve, seven days. OCI MFA was operator-confirmed; the console showed the micro's Always Free badge and Free Tier/trial tenancy. |
| Network observations | External TCP 80/443 reachable; 22, 5432, 5050, 2375, 2376 and 8000 timed out. Only Caddy publishes container ports. |
| Controlled reboot | After the fresh verified backup and idle writers, reboot was requested at 08:49:30 UTC. A changed boot ID, unchanged database volume/catalog/schema/image and 192 MiB web limit were verified. Homepage/API, all five enabled/active timers, SSH/journal policy and managed monitor/telemetry/puller success passed. Next crawl remained at 12:00 UTC. |
| OCI rule read-back | October 29: the instance has one VNIC with no NSGs. Its subnet has one security list, allowing public TCP 80/443 and UDP 443, TCP 22 only from the Bastion private endpoint /32, and the default ICMP rules. No database/admin or IPv6 ingress grants were listed; subnet details showed no IPv6 prefix. Bastion has one client /32 and a one-hour maximum/session TTL. The public IP is ephemeral. |
| Cost awareness | The existing micro has the Always Free badge and the tenancy remains Free Tier/trial. Unfiltered October 1–29 Cost Analysis showed SGD 0.00, subject to billing-data delay; the monthly usage report also listed the free Compute and Block Volume SKUs. With operator approval, a root-compartment SGD 1 monthly budget and 1% actual-spend email rule were created at 21:47:40 UTC. This is a notification at SGD 0.01, not a spending cap or paid upgrade; delivery has not been induced with real spending. |
| Forwarded client address | At 18:00:20 UTC, ordinary public HTTPS and forged-header requests both returned 200. Their uniquely matched FastAPI access logs showed the same actual external client address; the forged multi-address `X-Forwarded-For` chain was ignored. Proto, host, real-IP and `Forwarded` headers were also supplied, but backend scheme is not exposed by the existing routes/logs and was not directly asserted. |
| Managed crawler interruption | Completed at 18:15:41 UTC using the installed bootstrap, byte-identical released supervisor and exact application image. A cloned systemd unit and unsigned fixture Compose configuration used isolated PostgreSQL, deterministic application inputs and no production credentials/public ports. Orderly stop and main-process SIGKILL each removed only the owned daemon child, preserved active fixture membership, and allowed a successful normal successor with the same pending sort and zero resumes. Production state/core containers were unchanged; all owned fixture resources were removed. |
| Persistent timer catch-up | At 18:01:59 UTC, a disposable actual-host persistent timer coalesced three missed calendar slots into one activation, about 0.23 seconds after restart. The host clock and production schedules were unchanged; the disposable unit, stamp and data were removed. This does not establish production recovery after a genuinely missed six-hour slot. |

At 21:44:31 UTC, eight crawls had completed with 216,777 canonical/active
repositories and 1,335,662 memberships; none was running. Five local encrypted
archives and six current backup-bucket objects each totalled about 67.4 MiB.
The bucket count excludes old versions and other buckets. Disk use was 4.71%;
about 496 MiB RAM was available during the incident follow-up. These snapshots
do not establish multi-day growth or tenancy-wide billed usage.

The console inventory showed Hyderabad as the only subscribed/home region, one
micro instance, one 100 GB boot volume at VPU 10, no additional block volumes,
and one private Standard backup bucket; root instance/boot/bucket lists were
empty. The bucket's versioning was disabled with zero unfinished multipart
uploads. A complete version listing at 21:54:15 UTC matched all six current
objects and 70,633,476 bytes, with no additional versions or delete markers.
The AD micro service limit was two, while usage/availability fields were
unavailable. Recheck retained volume backups and actual free host capacity
before provisioning a replacement; these observations do not reserve capacity.

The restored archive's ciphertext SHA-256 was
`6014267aedfe205350027853fa811262ce3618200eddae084d0f07319b4a332b`;
age authentication and the inner dump checksum also passed.

### Capacity and limits of this evidence

The earlier 128 MiB web ceiling caused an OOM kill on October 28. Raising only
web to 192 MiB kept the exact image and production data; subsequent checks,
including the next morning, found no further web OOMs/restarts and measured an
approximately 106 MiB peak. Normal container ceilings total 704 MiB. Host
inspection found 353 MiB available RAM and about 92 GB free disk space.

Refreshing `catalog_generations` planner statistics reduced the measured local
full discovery request from 10.807 to 1.973 seconds under a live crawl. These
samples and the separate 768 MiB synthetic CI rehearsal do not establish
sustained capacity or a latency guarantee.

The real outage covered the public proxy, not complete VM/network loss. Managed
interruption used isolated fixture configuration, not the production catalog or
GitHub API. No six-hour slot was missed during reboot. The rule read-back does
not establish actual HTTP/3/IPv6 reachability.
Initial TLS issuance and expiry-alert tests do not establish renewal.
GitHub cron remains best effort; UptimeRobot's Free-plan five-minute HTTP/S
monitor supplements it with basic availability checks.

UptimeRobot reported a connection timeout confirmed by three German probes at
October 29, 21:33:50 UTC; initial detection was 21:32:46 and recovery was 21:38:58.
The recorded incident lasted 5m 8s, more than three hours after rehearsal cleanup.
Core containers ran continuously with no restarts or OOM kills; bounded kernel,
proxy, web and database logs showed no corresponding failure. The real crawler
had completed at 20:13:15. Other clients received homepage/API 200 responses
during the window, and a [manually triggered independent monitor](https://github.com/TanishTuteja/GH-Random/actions/runs/40959434609)
passed at 21:38:23, including durable crawl/backup telemetry. This supports a
temporary path-specific reachability problem, but does not prove its cause or
establish a false alarm. Keep monitoring; capture network evidence if it recurs.

Evidence: [production deployment](https://github.com/TanishTuteja/GH-Random/actions/runs/40596008134),
[real outage check](https://github.com/TanishTuteja/GH-Random/actions/runs/40842618556),
[recovery check](https://github.com/TanishTuteja/GH-Random/actions/runs/40843624776),
[accepted operational record](https://github.com/TanishTuteja/GH-Random/pull/42).
Full incident journals and private recovery receipts stay outside the repository.
