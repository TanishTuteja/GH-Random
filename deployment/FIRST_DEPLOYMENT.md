# First OCI deployment

This is the operator handoff for the already provisioned **Ubuntu 24.04 Minimal,
AMD64 E2.1.Micro, 1 GB RAM, 100 GB boot volume**. Repository CI and a published
release do not configure OCI, send real alerts, or establish a live website.
Complete the steps below yourself over the working Bastion connection.

Verified release: **v0.1.1**. Its publication and independent local verification
completed; verify the signed tag and assets again before installing:

```text
Signed source commit: 94b9c94ff0232b3f3d8f0e8d5d7c25b902af548b
Application image: ghcr.io/tanishtuteja/gh-random@sha256:c5a6732f67d445be36eaaa00ded7706415982b13a5f3ad10b68eea7ca991e697
```

Do not use the older ARM64-only v0.1.0 image on this VM. Do not build an image
on the micro VM or deploy from moving `main`. Use the exact image and matching
deployment archive attached to the verified release.

## Before the first deployment

1. **Finish host and access setup.** Keep the working Bastion SSH path and a
   second verified administrative session before removing an obsolete public
   key. Patch Ubuntu and its CA certificates; install Docker Engine/Compose
   from the official Docker repository, Python 3.12+, OpenSSL, and a maintained
   official GitHub CLI **2.97.0 or newer**. The application needs no host uv,
   virtualenv, PostgreSQL server/client, or pgAdmin. Confirm the non-secret facts:

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

   Expect `x86_64`; verify the required attestation flags listed in
   [AUTOMATION.md](AUTOMATION.md). Follow [LOW_MEMORY.md](LOW_MEMORY.md): one web
   worker, tuned PostgreSQL, and the shipped memory ceilings. Optional emergency
   swap does not increase those ceilings or replace the live capacity test.

2. **Choose the public hostname and finish networking.** Use a free subdomain
   you control or an existing domain; OCI does not supply a free registered
   domain. One free option is [DuckDNS](https://www.duckdns.org/faqs.jsp): choose
   an available subdomain in your own account and set its IPv4 address to the VM.
   Keep its DNS update token private. Set the A record to the VM's current public
   IP and keep it updated.
   Allow public TCP 80/443 in OCI and the host firewall; add UDP 443 only for
   HTTP/3. Preserve Bastion-private SSH access. Keep public SSH, PostgreSQL,
   pgAdmin and Docker API ports closed. Do not add an AAAA record without working
   IPv6 networking and firewall rules. Check what is actually reachable from an
   independent connection; Docker port publishing needs explicit review.

3. **Obtain and verify the release before executing it.** Download all four
   v0.1.1 assets into a fresh version-specific staging directory in your trusted
   operator environment. Check
   `SHA256SUMS` and the archive's GitHub attestation against the independently
   verified signed tag's commit, version ref and release workflow. Follow the
   [release verification procedure](../README.md#verifying-a-published-release).
   The verified metadata must say `linux/amd64`.
   Transfer the verified archive/files through Bastion, preserving their verified
   hashes. Keep immutable release files under `/opt/gh-random/releases/v0.1.1`;
   do not edit the packaged files. The initial verification can use your already
   authenticated operator workstation; the installed VM puller later supplies
   short-lived App authentication for its own CLI checks. Public GHCR needs no
   registry password.

4. **Configure the crawler and deployment GitHub Apps.** Keep the existing public-search
   crawler App and its private key. Create a separate deployment App installed
   only on `TanishTuteja/GH-Random`, as described in
   [AUTOMATION.md](AUTOMATION.md#manual-vm-setup-after-provisioning):
   **Contents read, Actions read, Attestations read, Deployments read/write**;
   Metadata read is implicit. This same deployment App also publishes telemetry
   using a token narrowed to Deployments write.
   Do not broaden the crawler App's permissions. Neither App needs a visitor's
   GitHub login or OAuth authorization flow.

   Record the deployment App's client ID, numeric App ID, installation ID and
   bot user ID in the designated private configuration fields. Set production
   environment variables `PRODUCTION_DEPLOYMENT_APP_ID` and
   `PRODUCTION_DEPLOYMENT_CREATOR_ID`; the latter is the **bot user ID**.
   [AUTOMATION.md](AUTOMATION.md) explains how to obtain and verify each value.

5. **Create private production configuration.** Use root-owned mode `0700`
   `/etc/gh-random` and mode `0600` configuration/key files outside the release.
   Copy the matching release's templates, replace placeholders and keep:

   | Configuration | Main values to supply |
   | --- | --- |
   | `compose.env` | Exact released image digest; absolute private file/key paths; shipped micro limits |
   | `postgres.env`, `app.env`, `crawler.env` | Matching database name/user/password; SQLAlchemy URLs use hostname `database`; encode reserved password characters |
   | `app.env`, `caddy.env` | Host allowlist, matching Caddy hostname and ACME email |
   | Crawler PEM and `crawler.env` | Existing crawler App credentials and installation |
   | `pull.json` and separate deployment PEM | Dedicated deployment App, bot ID, origin, stable state/release paths |
   | `alerts.json`, `smtp-password` | Private sender/recipients and VM SMTP credential |
   | `backup.json`, `backup-recipients.txt` | Private bucket/prefix, installed tool paths, public age recipient |
   | `telemetry.json` | Deployment App identity and later the stable telemetry anchor IDs |

   Keep passwords, PEM contents, SMTP credentials, database URLs and raw logs
   private. Do not paste them into chat, Git, issue bodies, commands in shell
   history or public workflow inputs. Record recoverable private configuration
   independently of the VM; losing the VM must not lose every recovery copy.
   Ensure `pull.json`'s `backup_dir` matches `backup.json`'s `archive_dir`
   exactly; use `/var/backups/gh-random/encrypted` consistently with the shipped
   encrypted backup policy. A mismatch fails before deployment.

6. **Configure encrypted off-site backups before deploying.** Follow
   [BACKUPS.md](BACKUPS.md#install-and-configure-manual-vm-work). Generate the age
   recovery identity on your recovery machine and preserve two independent
   offline copies; put only its public recipient on the VM. Install the pinned
   age tool and official OCI CLI. Create a private Standard Object Storage
   bucket/prefix within confirmed free quotas and configure a scoped dynamic
   group/instance-principal policy for upload and read-back, without general
   overwrite/delete permissions. Do not configure a lifecycle rule that can
   expire every recovery copy during a long outage. Even the first empty-database
   deployment takes an encrypted uploaded/read-back-verified pre-migration backup.
   No live database is available yet, so the full catalog backup/restore test
   follows deployment and the first crawl.

7. **Configure and test email before enabling services.** Personal Gmail can
   provide low-volume SMTP alerts when the account supports App Passwords.
   Follow the [email guide](../README.md#email-failure-alerts-and-missing-success-monitoring):
   use 2-Step Verification, `smtp.gmail.com`, port 465/TLS, and the full Gmail
   address as username/sender. Use separately revocable App Passwords for the VM
   and external GitHub monitor; never the normal account password. Install the
   matching notifier and failure-handler units. The notifier's `test` command
   sends a real email: confirm inbox arrival, not just a zero exit status.
   Leave `monitor_backups` false until the real backup worker succeeds and
   `tls_host` unset until HTTPS works.

8. **Install the approved outbound puller.** Follow
   [AUTOMATION.md](AUTOMATION.md), including the reviewed bootstrap's relative
   paths under `/opt/gh-random-automation`. Create its private state/release/backup
   directories and install the matching units. Set `allow_initial_bootstrap`
   true **only for this first empty-database deployment**; encrypted backup and
   alert configuration remain required. Verify/enable `gh-random-pull.timer`.
   This does not require public SSH, an Actions runner on the VM, a registry
   token, or a personal GitHub CLI login on the VM.

## First installation, before announcing the site

9. **Approve v0.1.1 through the deployment workflow.** On GitHub, run
   **Production deployment** on `main`, input tag `v0.1.1`, and approve the
   protected production environment. Alternatively, from your authenticated
   operator workstation:

   ```sh
   gh workflow run deploy.yaml --repo TanishTuteja/GH-Random --ref main -f tag=v0.1.1
   ```

   Observe Actions, the GitHub Deployment status and the VM's puller journal.
   A successful queue job alone is not a deployed website. Only proceed once
   the VM reports success and actual HTTPS checks pass. Then set
   `allow_initial_bootstrap` back to false. For failures, use
   [OPERATIONS.md](OPERATIONS.md); do not bypass locks or replay migrations.

10. **Run the first supervised full crawl.** Install the release-matching
    crawler bootstrap/service and tested email handler. Start
    `gh-random-crawler.service` explicitly; merely starting web never crawls.
    Watch memory, CPU pressure, duration and the journal through Bastion. Confirm
    an active catalog and working discovery during the crawl. Keep the six-hour
    crawler timer disabled until this first successful run is verified, then
    enable it. The slots are 00/06/12/18 UTC, or 05:30/11:30/17:30/23:30 IST.
    Use the managed service so freshness watermarks are updated.

11. **Complete backup and monitoring acceptance.** Run the encrypted backup
    service and verify actual OCI upload plus full read-back. Restore a trusted
    off-site copy into a separate database on a disposable recovery host or in a
    deliberate maintenance window; do not add a parallel restore stack to this
    1 GB VM. Check schema, counts and discovery. Then enable the daily backup
    timer (05:15 UTC / 10:45 IST), `monitor_backups`, disk/TLS checks and the local
    monitor timer. Test real failure/recovery emails without destructive changes
    to the production data.

    Follow [MONITORING.md](MONITORING.md): reuse the deployment App, create one
    stable telemetry anchor, install telemetry publishing and configure the
    protected `monitoring` environment's SMTP secrets. Reuse existing issue #26
    and its variable; do not create a second ledger. Keep
    `MONITORING_ENABLED=false` until the hostname, fresh telemetry and independent
    inbox tests work, then enable and verify outage/staleness/recovery handling.

12. **Accept the real micro VM before public launch.** Follow
    [LOW_MEMORY.md](LOW_MEMORY.md#before-public-launch) and the
    [operations acceptance record](OPERATIONS.md#acceptance-record). Verify HTTPS,
    filters/Any, repeat avoidance/single-result fallback and no-result behavior;
    confirm only intended ports/routes are public. Rehearse reboot recovery,
    timer retry/catch-up and successful sort rotation. Measure actual crawl
    duration, memory/swap/OOMs, response latency and disk growth over retained
    generations. Do not launch with OOM kills, sustained swapping or crawls that
    miss their five-hour window. Record the actual URL, deployed commit/digest,
    last tested restore and monitoring results in the deployment checklist.

Read-only observations after services exist:

```sh
sudo systemctl list-timers 'gh-random-*'
sudo systemctl status gh-random-pull.timer gh-random-crawler.service gh-random-backup.service
sudo docker stats --no-stream
free -h
vmstat 1 10
sudo journalctl -k --since today --grep='oom\|Out of memory\|Killed process'
```

Keep the exact current/previous release files, recovery keys and good backups.
Never remove PostgreSQL volumes to apply settings. Production pgAdmin and a
custom paid domain are optional and are not required for first launch.
