# Deployment and host configuration

Use a [verified signed release](../releases/RELEASES.md), its exact image digest and matching
deployment files. Host installation commands run from that extracted release's
root directory, not a checkout of moving main. Keep immutable release files
separate from private root-owned configuration and preserve database volumes.

## Operator guides

| Guide | Purpose |
| --- | --- |
| [SETUP.md](SETUP.md) | Clean-host installation order and private configuration |
| [AUTOMATION.md](AUTOMATION.md) | Approved outbound deployment and trust checks |
| [OPERATIONS.md](../operations/OPERATIONS.md) | Inspection, incident recovery, maintenance and deployment evidence |
| [BACKUPS.md](../operations/BACKUPS.md) | Encryption, retention and off-site recovery |
| [MONITORING.md](../operations/MONITORING.md) | Local/external monitoring and telemetry |
| [LOW_MEMORY.md](LOW_MEMORY.md) | Micro-VM limits and capacity checks |
| [PGADMIN.md](../operations/PGADMIN.md) | Optional private production database browsing |

The following commands are a reference for deliberate installation/maintenance;
they are not a checklist of outstanding work on the current host. See
[TODO.md](../project/TODO.md) for remaining tasks.

## Deploying a verified release

The normal production route is an approved outbound pull:
[AUTOMATION.md](AUTOMATION.md). Dispatch `deploy.yaml` on `main` with
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

## Explicit image rollback

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

## Six-hour scheduled crawler (Linux/systemd)

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
See [OPERATIONS.md](../operations/OPERATIONS.md) for stop/recovery commands.

Updating installed host units/bootstrap is deliberate: stop, install matching
reviewed files, verify, reload and restore scheduling. Application deploys do not
silently overwrite host policy. The published v0.1.1 puller compatibility fixes
are documented in [AUTOMATION.md](AUTOMATION.md#compatibility-override-for-v011).

## Explicit live AMD64 GitHub App authentication smoke

From a trusted repository checkout on the operator workstation, this opt-in
check contacts GitHub using real App credentials, without a crawl or
database operation. **Replace `VERIFIED_IMAGE` with a trusted pulled AMD64 digest
and `PRIVATE_APP_ENV` with the private App configuration path**:

~~~powershell
python .github/scripts/check_github_auth.py --image VERIFIED_IMAGE --env-file PRIVATE_APP_ENV
~~~

The harness stages only App credentials through a uniquely labelled private
key volume, verifies HTTPS authentication, revokes its short-lived token and
removes owned resources on normal completion. Abrupt host loss still requires
inspection of the exact owned resources; it is separate from VM acceptance.

## Email failure alerts and missing-success monitoring

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

[MONITORING.md](../operations/MONITORING.md) covers external GitHub response/freshness
checks, hourly telemetry and independent free UptimeRobot availability. Those
monitors are configured for the current site; new installations need their own
verified identity, configuration and inbox acceptance before schedules are enabled.

## Production runtime hardening and maintenance

Only Caddy publishes host ports. Web/database use internal networks; trusted
proxy headers depend on keeping those Docker networks private. Configure exact
`APP_ALLOWED_HOSTS` and matching `GH_RANDOM_DOMAIN`; do not publish Uvicorn,
PostgreSQL, Docker API or pgAdmin ports. Adding another proxy needs its own trust
review.

Container resources and logs are bounded. The micro defaults are one web worker,
192 MiB web, 256 MiB database, 192 MiB crawler, 128 MiB migration and 64 MiB Caddy.
Use [LOW_MEMORY.md](LOW_MEMORY.md) for tuning and measured limits.
[OPERATIONS.md](../operations/OPERATIONS.md) covers incidents, journald, host updates,
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
