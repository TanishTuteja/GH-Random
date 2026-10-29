# Clean-host deployment

Use this reference for a new **Ubuntu 24.04 Minimal AMD64 E2.1.Micro** host
(1 GB RAM, 100 GB boot volume). The existing production installation is live;
its verified release/digest and results are in the
[operations record](OPERATIONS.md#deployed-release). [TODO.md](../TODO.md) tracks
only unfinished work.

For replacement of an existing host, start with
[complete VM recovery](OPERATIONS.md#complete-vm-loss-or-replacement): restore
and test the catalog before cutover. Do not enable two production pullers against
the same approval queue during a rehearsal. Empty-database bootstrap is not a
restore procedure.

## Before the first deployment

1. **Prepare the host and private access.** Confirm Always Free eligibility and
   tenancy-wide quotas before provisioning. Patch Ubuntu/CA certificates and
   install official Docker Engine/Compose, Python 3.12+, OpenSSL, GitHub CLI
   2.97.0+ with the [required attestation flags](AUTOMATION.md), the pinned age
   tool and official OCI CLI. No host uv, application virtualenv, PostgreSQL
   server/client or pgAdmin is required. Keep working Bastion access and prove a
   replacement administrative connection before removing an old key.

   Run these inspection commands unchanged; no replacements are needed:

   ```sh
   uname -m
   free -h
   df -h /
   swapon --show
   python3 --version
   openssl version
   sudo docker version
   sudo docker compose version
   gh --version
   gh attestation verify --help
   ```

   Expect `x86_64`. Follow [LOW_MEMORY.md](LOW_MEMORY.md): one web worker,
   tuned PostgreSQL and bounded resources. Build images elsewhere; preserve
   existing data volumes when applying settings.

2. **Configure DNS and network isolation.** Point a controlled hostname's A record
   at the VM's public IPv4 address; keep any DNS-update token private. Permit
   public TCP 80/443 and UDP 443 only if using HTTP/3. Permit SSH through Bastion
   with a restricted client CIDR, not public SSH. Keep PostgreSQL, pgAdmin and
   Docker API private. Check external reachability as well as OCI/host rules;
   Docker port publishing needs explicit review. Publish AAAA only after IPv6
   networking and firewall rules are tested.

3. **Verify the release before execution.** Obtain all four assets for a trusted
   signed release in a fresh staging directory. Follow the
   [release verification procedure](../README.md#verifying-a-published-release):
   verify SHA256SUMS, signed source, archive/image attestations and matching
   metadata. Expect `linux/amd64`; v0.1.0 is ARM64-only and predates the CA fix.
   Transfer files through Bastion with checked hashes, retain version-specific
   immutable release directories, and use the exact digest with its matching
   bundle. Never deploy files from moving `main` with a released image.

4. **Configure separate GitHub Apps.** Retain the crawler's search App; use a
   separate repository-scoped deployment App with the permissions and bot
   identity in [AUTOMATION.md](AUTOMATION.md#manual-vm-setup-after-provisioning).
   Configure the protected production environment's App/bot variables there.
   The deployment App also publishes telemetry using a token narrowed to
   Deployments write. Neither App requires a visitor login.

5. **Install private configuration.** Keep `/etc/gh-random` root-owned mode 0700
   and its configuration/key files mode 0600, outside the release. Copy matching
   templates and replace their placeholders privately. Configure:

   - `compose.env`, `postgres.env`, `app.env`, `crawler.env`, `caddy.env` and the
     crawler PEM: matching DB credentials, URL-encoded SQLAlchemy passwords,
     hostname/allowlist, exact image digest, private paths and micro limits.
   - `pull.json` and deployment PEM; `alerts.json` and SMTP password;
     `backup.json` and public age recipients; `telemetry.json`.

   Follow [automation](AUTOMATION.md), [backups](BACKUPS.md) and
   [monitoring](MONITORING.md) for each configuration's fields. Keep recoverable
   private configuration independently of the VM. For the immutable v0.1.1
   bundle, set private `WEB_MEMORY=192m` as required by [LOW_MEMORY.md](LOW_MEMORY.md).
   Ensure `pull.json.backup_dir`
   and `backup.json.archive_dir` match, normally
   `/var/backups/gh-random/encrypted`. Do not publish credentials or raw logs.

6. **Prepare encrypted off-site backups.** Follow [BACKUPS.md](BACKUPS.md#install-and-configure-manual-vm-work).
   Generate the age recovery identity on the recovery machine; retain independent
   offline copies and install only public recipients on the VM. Configure a
   private bucket and scoped instance-principal create/read access within free
   quotas. Keep remote automatic deletion off. Even initial empty-database
   deployment requires an encrypted uploaded/read-back-verified pre-migration
   backup; the catalog restore test follows the first successful crawl.

7. **Verify real email delivery.** Configure private TLS-only SMTP and install
   the matching notifier/failure-handler units using the
   [email instructions](../README.md#email-failure-alerts-and-missing-success-monitoring).
   Confirm the test message arrives in the inbox. Keep backup freshness checks
   disabled until the managed backup succeeds and leave `tls_host` unset until
   HTTPS works. Credentials and recipient addresses stay private.

8. **Install the outbound puller.** Follow [AUTOMATION.md](AUTOMATION.md), including
   bootstrap compatibility fixes for v0.1.1, private directories and matching
   systemd units. Set `allow_initial_bootstrap=true` only for the first deployment
   with a missing database service; backups and alerts remain required. Public
   GHCR needs no registry password. The host needs no public SSH, deployment
   webhook, self-hosted Actions runner or personal GitHub CLI login.

## First installation, before announcing the site

9. **Approve and verify deployment.** Dispatch **Production deployment** on `main`
   for the verified release tag and approve the protected production environment;
   see [deployment commands](../README.md#deploying-a-verified-release). Observe
   the actual VM result and public HTTPS, not just the queue job. Set
   `allow_initial_bootstrap=false` after success. For failures use
   [OPERATIONS.md](OPERATIONS.md); preserve locks, migration history and volumes.

10. **Publish the first supervised catalog.** Install matching crawler units and
    start `gh-random-crawler.service`; web startup does not crawl. Watch resources,
    progress and cleanup, and verify discovery from the active catalog. Enable
    the crawler timer after success: 00/06/12/18 UTC, or
    05:30/11:30/17:30/23:30 IST. Managed runs update freshness markers.

11. **Enable verified backups and monitoring.** Complete encrypted upload/full
    read-back and restore an off-site archive into a separate recovery database.
    Check schema, counts and discovery. Use a disposable recovery host rather
    than a parallel restore stack on the micro. Enable the 05:15 UTC/10:45 IST
    backup timer and local freshness/disk/TLS monitoring after success.
    Follow [MONITORING.md](MONITORING.md) for the stable telemetry anchor,
    protected monitoring secrets, existing issue #26 and independent availability
    monitor. Keep `MONITORING_ENABLED=false` until real configuration and
    failure/recovery inbox tests pass, then enable the schedule.

12. **Record host acceptance.** Check page/API/filter behavior, intended public
    ports, crawl rotation, backup/restore, alerts and reboot recovery. Measure
    resources and latency using [LOW_MEMORY.md](LOW_MEMORY.md#capacity-checks).
    Resolve OOMs, sustained swapping or five-hour crawl timeouts before relying
    on the host. Record results in [OPERATIONS.md](OPERATIONS.md#acceptance-record)
    and keep unverified tasks/observation windows in [TODO.md](../TODO.md).

Preserve current/previous release files, trusted backups and recovery keys.
Optional pgAdmin and a custom paid domain are not required for deployment.
