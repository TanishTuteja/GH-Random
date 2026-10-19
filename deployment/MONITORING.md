# Independent monitoring

The hourly VM monitor cannot email while its VM is stopped. `monitor.yaml` runs
outside that VM on GitHub-hosted runners and detects both public-site failure
and missing **successful** crawls/backups. It requires no paid monitoring service
and adds no public application endpoint. The existing `/` and
`/api/random-repository` remain the only routes.

This implementation is fixture-tested, not an already-configured live service.
Leave `MONITORING_ENABLED` unset until the real hostname, telemetry identity,
SMTP credentials and inbox delivery have been verified. Enabling it with missing
configuration fails the workflow; it never silently reports a healthy site.

## What is checked

The GitHub workflow requests the canonical HTTPS homepage, then the random API
with all three filters set to `any`. Both must return HTTP 200 without redirects;
the first must be the GH Random HTML page and the second must contain an actual
repository. A functioning homepage with an unavailable database or empty active
catalog is not considered healthy. The public origin cannot contain credentials,
an explicit port, path or query, or resolve to local/private addresses. TLS peer
and hostname verification remain enabled. No cookies, GitHub token, or SMTP
credentials are sent to the site. Requests have ten-second socket timeouts and
one-MiB response limits; the complete job is limited to five minutes.

A separate, non-production GitHub deployment (`gh-random-monitoring`) is a stable
telemetry anchor. It is **not** a record that an application release was deployed.
The VM posts a short status description containing only:

```json
{"v":1,"h":1792324800,"c":1792324800,"b":1792324800}
```

`h` is the last completed local monitor pass, `c` a fully cleaned-up successful
crawl, and `b` a successful encrypted backup including off-site upload, complete
read-back verification, retention and lease cleanup. Values are UTC Unix seconds;
zero means no proven success. Local-only backups never count. No database URL,
run identifier, host path, raw logs, response body, repository catalog data, email
address, or key is published. The watermark files are the existing private
`crawler-success.json` and `backup-success.json`; the local monitor writes
`monitor.json` only after completing its checks and notification processing.

The external checker requires:

| Signal | Maximum age |
| --- | --- |
| Completed VM monitor | 3 hours |
| Successful crawl | 12 hours |
| Verified off-site backup | 36 hours |

The hourly publisher runs at `*:20 UTC` (five minutes after the host monitor),
preserves previously published success timestamps if a later run fails or local
files disappear, and never treats a new publication time as a successful crawl.
Future timestamps beyond five minutes, malformed descriptions, a changed author,
or the wrong deployment environment fail closed. The checker examines the newest
status and never hides a malformed latest status by falling back to older data.
Only the explicitly configured numeric bot identity may author the anchor/status.

The independent checker runs at minutes `07,22,37,52` each UTC hour, away from the
top-of-hour scheduling peak. This is not a guaranteed 15-minute SLA: GitHub can
delay/drop scheduled runs, disable inactive public-repository schedules after 60
days, or suffer its own outage. Review the Actions schedule/run history regularly,
enable GitHub Actions failure notifications by email, and re-enable the schedule
after inactivity. A second provider/watchdog would be needed to detect GitHub
itself failing; this free initial setup does not claim that coverage.

## Private VM setup (after OCI exists)

Use the dedicated repository-scoped **deployment** GitHub App, not the crawler's
public-search App. Its installation needs Deployments read/write for telemetry;
the App also has the deployment client's documented read permissions for release
verification, but the telemetry publisher requests an installation token narrowed
to **Deployments write only**. Install that App only on this repository. Keep its PEM
root-owned, mode `0600`, outside the checkout. Do not grant the crawler App extra
permissions or mount the deployment key in application/crawler containers.

Copy `telemetry.json.example` to `/etc/gh-random/telemetry.json`, mode `0600`, and
replace App ID, client ID, installation ID and key path. These are distinct: the
client ID identifies the JWT issuer, the numeric App ID identifies the App, the
installation ID identifies its repository installation, and the telemetry creator
ID identifies its bot user (not the App ID). Install release-matching
`external_monitor.py`, `github_api.py`, `database_backup.py`, and `notifications.py`
beside each other in `/usr/local/lib/gh-random/`. The shared App client signs JWTs
using host OpenSSL, exchanges a short-lived installation token, refuses API
redirects, and never logs credentials.

Create the anchor once with the private VM configuration:

```sh
sudo python3 /usr/local/lib/gh-random/external_monitor.py create-anchor \
  --config /etc/gh-random/telemetry.json
```

Record the printed `deployment_id` and `telemetry_creator_id` in that private
configuration and in the repository variables below. They are identifiers, not
secrets. Reuse the same anchor through upgrades and VM replacement. Creating a new
anchor is an explicit operator action requiring both configurations to change;
never auto-create anchors during a normal heartbeat. Old application deployments
are not selected heuristically. The anchor deliberately uses `auto_inactive=false`
when recording statuses, so it cannot retire actual application deployments.

After verifying local monitor/crawl/backup successes, install the matching units:

```sh
sudo install -m 0644 deployment/systemd/gh-random-telemetry.service /etc/systemd/system/
sudo install -m 0644 deployment/systemd/gh-random-telemetry.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start gh-random-telemetry.service
sudo systemctl enable --now gh-random-telemetry.timer
sudo systemctl status gh-random-telemetry.service
sudo systemctl list-timers gh-random-telemetry.timer
```

The service has a two-minute limit, no Docker socket, read-only host filesystem,
no capabilities and no application/crawler/SMTP secrets. Its separate deployment
key is required solely to post telemetry. It writes no operational files and has
no recursive email hook; a broken publisher is detected by missing telemetry on
the independent runner. Inspect `journalctl -u gh-random-telemetry.service` privately.

## GitHub workflow configuration

Create one permanent operations issue using the script's `create-issue` command
with a trusted GitHub CLI/Actions `GH_TOKEN` and a private JSON configuration
containing `{"repository":"TanishTuteja/GH-Random"}`. It prints the issue number.
The issue title must remain `[Operations] External monitor state`. Do not hand-edit
the machine-readable body or delete the issue; the runner validates it instead of
silently discarding incident history. No secrets are stored in it. Keep it open;
comments give a fixed-code failure/recovery trail and can also be subscribed to
using GitHub's email notifications. Repository maintainers are trusted to manage
this ledger. The issue is not a private incident channel.

Set these **repository variables** under Settings → Secrets and variables → Actions:

This repository already has the permanent state [issue #26](https://github.com/TanishTuteja/GH-Random/issues/26)
and `MONITORING_ISSUE_NUMBER=26`. Reuse it rather than creating another ledger.
`MONITORING_ENABLED=false` is explicit; real monitoring is not yet enabled.

| Variable | Value |
| --- | --- |
| `MONITORING_ORIGIN` | `https://your-real-hostname` |
| `MONITORING_DEPLOYMENT_ID` | Stable telemetry anchor's numeric ID |
| `MONITORING_CREATOR_ID` | Expected telemetry App bot's numeric user ID |
| `MONITORING_ISSUE_NUMBER` | Permanent state issue number |
| `MONITORING_ENABLED` | `true`, only after setup/testing |

Create a dedicated GitHub **`monitoring` environment** restricted to protected
branches. It must have no required reviewer or wait timer: the outside monitor
must run unattended during an outage. Keep production's approval policy unchanged.
The workflow uses `environment.deployment: false` to access the scoped secrets
without manufacturing a production/environment deployment for every health check.
Verify the branch restriction by reading it back after creation.

Set these **`monitoring` environment secrets**, never repository-wide secrets,
repository variables or workflow inputs:

* `MONITORING_SMTP_CONFIG`: JSON containing only `smtp_host`, `smtp_port`,
  `smtp_security` (`ssl` or mandatory `starttls`), optional `smtp_username`,
  `from_address`, `to_addresses` (one to five) and optional
  `smtp_timeout_seconds` (at most 30). Example shape:

  ```json
  {"smtp_host":"smtp.example.com","smtp_port":465,"smtp_security":"ssl","smtp_username":"private-user","from_address":"alerts@example.com","to_addresses":["operator@example.com"]}
  ```

* `MONITORING_SMTP_PASSWORD`: SMTP password when a username is set. It is placed
  in a private temporary runner file and removed after the check. Never include
  password-file paths or passwords inside the JSON secret.

Use the same reviewed free SMTP provider as host alerts if suitable; confirm its
quota and the runner's ability to connect. SMTP acceptance does not establish
inbox delivery. This independently located copy of SMTP credentials lets GitHub
send the selected **email** alerts while the VM is unavailable. Keep the provider's
credentials scoped and rotate both copies together. Configuration secrets are
read by trusted default-branch code only: this workflow has no PR event, explicitly
requires the `main` ref, checks out the immutable event `github.sha` with credentials
not persisted, uses SHA-pinned actions, and grants
only Contents read, Deployments read and Issues write. It never needs production
environment approval or a VM key to monitor an outage. Environment branch
protection, not the in-file guard alone, prevents an edited workflow on an
unprotected same-repository branch from reading SMTP secrets.

## Alert behavior and acceptance

The issue stores durable active codes, pending delivery and accepted delivery
time, not raw diagnostics. Incidents use only `site`, `telemetry`, `host`, `crawl`
and `backup`. A new/changed incident sends email immediately; unchanged failures
send reminders at most every six hours. Recovery sends one email only if a
failure email was accepted. A failed SMTP send leaves a pending entry for the
next run; a failed issue update after SMTP acceptance can produce a duplicate
(at-least-once delivery), never a false accepted state. Runs are serialized and
not canceled by later schedules. Issue/API/mail failures fail the Actions job
with only a safe error class, not the server's potentially sensitive response.

Manually dispatch **External monitoring** and verify a healthy result and fresh
telemetry. Then, in a controlled maintenance window:

1. Stop the website briefly; verify a failed check, issue event and actual inbox
   delivery from GitHub, not from the stopped VM. Start it and verify recovery.
2. Stop the telemetry timer long enough to exceed three hours, or use a separate
   disposable anchor with old timestamps. Verify missing-host telemetry email;
   resume and verify recovery. Do not forge production success watermarks.
3. Use a disposable anchor with crawl older than twelve hours and backup older
   than thirty-six hours to verify each stale-success code independently, then
   publish real fresh fixture success and verify recovery.
4. Temporarily reject SMTP delivery using test credentials/configuration. Verify
   the job fails, the issue remains pending, and a later run retries. Restore the
   reviewed provider configuration and verify inbox delivery.
5. Verify Actions failure emails and issue subscriptions separately so broken
   SMTP/configuration can still be noticed. Review schedule history regularly.

Do not mark live external monitoring/SMTP acceptance complete from repository CI.
Tests here use mocked GitHub contracts plus real loopback TLS HTTPS/SMTP only.

References: [GitHub deployment status API](https://docs.github.com/en/rest/deployments/statuses),
[scheduled workflow limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[GitHub notification settings](https://docs.github.com/en/account-and-profile/managing-subscriptions-and-notifications-on-github/setting-up-notifications/configuring-notifications).
