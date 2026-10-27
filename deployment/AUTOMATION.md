# Approved production deployments, without inbound SSH

Publishing a release does **not** deploy it. The manually dispatched
`Production deployment` workflow runs on `main`, waits for the `production`
environment's reviewer/branch rules, and queues one signed stable release.
An outbound-only systemd puller on the VM applies it. There is no public SSH
listener, deployment webhook, self-hosted Actions runner, or VM credential in
GitHub Actions. OCI provisioning and installing this configuration are manual.

The environment uses `deployment: false`: approvals still apply, but finishing
the queue job must not falsely mark the website deployed. A separate GitHub
Deployment stays `queued`, then the VM reports `in_progress`, then `success`
**only after** the existing deploy script's backup, migration and HTTPS smoke
tests pass. An observer job waits up to two hours for the dedicated VM App's
status; timeouts mean *unconfirmed*, never successful deployment.

## Trust boundaries

1. The workflow rejects non-main dispatches and verifies a published stable
   `vMAJOR.MINOR.PATCH` release: annotated GitHub-verified signature, matching
   source version, main ancestry and latest successful main-push CI including
   all three required jobs. The exact tag/commit's latest release workflow must
   also finish successfully with both release jobs and all four uploaded assets;
   merely publishing a Release or uploading the archive first is not readiness.
2. A Deployment payload is not authorization. The approved queue job uploads an
   immutable Actions artifact with the exact deployment ID and request. The VM
   independently checks its workflow/run/attempt, repository, event, main
   ancestry, completed successful queue job, artifact digest and matching
   receipt. Another workflow cannot forge a receipt inside that run. The
   installed, operator-reviewed workflow file must match the run's Git blob.
3. Requests expire after 30 minutes before claim. The VM refuses canceled or
   completed workflow runs and rechecks approval immediately before starting.
   A newer authenticated claim supersedes older queued requests; an unapproved
   request's rejection journal cannot supersede legitimate work. No silent old
   version is applied later. A rejected request requires a fresh dispatch.
4. The VM rechecks release/source CI and compares **every** archive file with
   the signed commit's Git blob, not just the archive's own manifest. The only
   transformation allowed is the documented immutable image line in the
   example Compose environment. Tar links, duplicate/traversing paths, unknown
   source files and oversized archives are rejected without extraction.
   The whole decompressed gzip stream is capped before parsing tar extension
   headers, not merely after the parser yields ordinary files.
5. GitHub CLI cryptographically verifies both archive and exact OCI digest
   attestations, restricted to this repository's `release.yaml`, the exact
   source commit and version ref, and GitHub-hosted runners. Unsigned BuildKit
   metadata or matching image labels alone are not proof of publisher identity.
   The release workflow also validates the image's attached source provenance
   and SBOM before publishing deployment assets.
6. Only then are matching root deployment scripts executed. The existing host
   mutex and PostgreSQL lease still serialize deployments, crawls, backups and
   migrations. Prior release files/images are retained; `current.json` keeps
   the previous successful release for explicit, schema-compatible rollback.

The approved workflow file is deliberately pinned to the installed automation
copy. Updating that workflow requires a reviewed puller upgrade on the VM.
The puller does not auto-replace its own trust root from a downloaded release.

## Manual VM setup (after provisioning)

Use the existing host setup, encrypted backup and email guides first. The host
needs Python 3.12+, Docker/Compose, OpenSSL, and a maintained official GitHub CLI
**2.97.0 or newer** (older versions have a signer-workflow verification vulnerability)
whose `gh attestation verify --help` includes `--source-digest`,
`--signer-digest`, `--source-ref` and `--deny-self-hosted-runners`. Install CLI
from its official signed package repository; record/update its installed
version during normal host maintenance. No CLI browser login is needed on the
VM: the puller passes a short-lived App token through the child's environment,
never through arguments or URLs. Public GHCR images need no registry secret.

Register a **separate** repository-scoped deployment GitHub App, installed only
on this repository. No webhooks or user authorization are needed. Give it
repository permissions **Contents: read**, **Actions: read**,
**Attestations: read**, **Deployments: read/write** (Metadata read is implicit).
Do not expand the crawler discovery App's permissions. Put its client ID,
numeric App ID and installation ID in a root-owned private `pull.json`; store
its PEM separately at the absolute path in that JSON. The client ID signs JWT
`iss`; numeric App ID validates optional App metadata. Installation tokens
are restricted to this repository/permission set and refreshed before expiry.

Set production environment variables `PRODUCTION_DEPLOYMENT_APP_ID` to the
numeric App ID and `PRODUCTION_DEPLOYMENT_CREATOR_ID` to its **bot user ID**.
Put that same bot user ID in `pull.json` as `creator_id` (and reuse it as
monitoring's `telemetry_creator_id`). It is not the App ID or installation ID.
You can inspect the App's bot user with
`gh api 'users/YOUR-APP-SLUG[bot]' --jq '{login,id,type}'`; verify the expected
bot login/type, then copy its numeric `id`. GitHub returns this unforgeable
server-controlled `creator.id` on deployment statuses. Real App-authored
responses can have `performed_via_github_app: null`, so that optional field
is checked when present but never used as the sole identity proof.
Missing configuration fails before queue creation. Only the expected bot's
statuses can satisfy the workflow observer; an unrelated user/App's
`success` is rejected. These account/configuration steps are intentionally not
performed by CI and require no credential pasted into chat or committed file.

Copy `pull.json.example` to `/etc/gh-random/pull.json`, mode 0600 in a mode 0700
root-owned directory. Replace every example value, use the production HTTPS
origin, existing encrypted/off-site backup policy and SMTP alerts configuration.
`allow_initial_bootstrap` defaults false: explicitly enable it for the first
empty database deployment only, then disable it after the first success.
The automatic production path always requires encrypted backup and alert
configuration; it has no plaintext/loopback development bypass.

Install the reviewed bootstrap from a verified signed source checkout or an
attested deployment archive under `/opt/gh-random-automation` (root-owned,
mode 0700). Preserve these relative paths:

```text
.github/workflows/deploy.yaml
deployment/pull.py
deployment/github_api.py
deployment/attestations.py
deployment/deploy.py
deployment/database_backup.py
deployment/notifications.py
```

Verify archive attestation **before** extracting or executing it, e.g.:

```bash
gh attestation verify gh-random-v0.1.0-deployment.tar.gz \
  --repo TanishTuteja/GH-Random \
  --signer-workflow TanishTuteja/GH-Random/.github/workflows/release.yaml \
  --source-ref refs/tags/v0.1.0 --source-digest VERIFIED_COMMIT \
  --signer-digest VERIFIED_COMMIT --deny-self-hosted-runners
```

Do not substitute an unchecked commit supplied by the archive itself: obtain it
from the verified signed tag/source. The normal release installation guide's
checksum and path checks still apply. Never run a script from an unverified
download merely to ask it whether it is trusted.

Create `/opt/gh-random/releases`, `/var/lib/gh-random/deployments` and
`/var/backups/gh-random` with mode 0700 before enabling the unit. Install the
two `gh-random-pull.*` units, `systemctl daemon-reload`, then
`systemctl enable --now gh-random-pull.timer`. The timer polls once a minute;
systemd does not overlap activations. Its private Docker/CLI runtime avoids
using a user's stored credentials. Root Docker access is host-root authority;
the unit's filesystem restrictions are defense in depth, not a Docker sandbox.
Atomic Compose configuration replacement needs write access to its private
parent directory, not just the existing file inode.

## Deploy, observe, recover

Dispatch `Production deployment` on **main** with the published version, review
the production approval and inspect both Actions and the GitHub Deployments
record. On the host:

```bash
systemctl status gh-random-pull.timer gh-random-pull.service
journalctl -u gh-random-pull.service --since today
```

The puller holds a separate local queue lock; the existing deploy worker owns
the shared catalog-operation lock. Individual HTTP requests run in killable
stdlib children with an overall 45-second deadline, including DNS. CLI
attestation checks have a two-minute deadline. Killing either network child
does not kill database work. Active deployment is not canceled when Actions
is canceled, times out, or another dispatch arrives. Do not stop/restart the
host service during DDL merely because the observer disappeared.

Each claimed request has a private durable journal. Terminal status delivery
is retried from local records independently of remote queue pagination if
GitHub was temporarily unreachable. Only a returned status from the dedicated
App acknowledges a journal; matching unrelated statuses do not. A malformed
status response never replaces a real successful execution with a preflight
failure. A restart
after an in-progress journal marks it `error` and **never replays migrations**.
Inspect the deployment journal, actual containers, current pointer and schema,
then make a new approved request or follow the manual recovery runbook.
There is no automatic schema downgrade, data restore, or rollback after failure.

Successful root deployment preserves the previous pointer. Use the existing
explicit rollback procedure only after confirming schema compatibility; a new
pull request/release is normally preferable to changing a published release.
Neither the puller nor cancellation deletes release directories, image digests,
backup archives, database volumes, or previous signed release assets.

## Verification and outstanding live acceptance

Offline tests forge payloads, canceled/branch runs, altered workflow pins,
replayed/mutated receipts, changed signed source, unsafe tar paths, forged
status origin and interrupted journals. The disposable native container
rehearsal exercises the same pull/deploy path with GitHub/attestation metadata
fixtures and real Docker, PostgreSQL, age encryption/read-back, migration,
web/proxy replacement and discovery. Fixtures are **not** proof of a live
GitHub environment approval or genuine signing key. The first published
release separately verifies real attestations and anonymous exact-digest pull.

Actual VM installation, App/PEM setup, provider SMTP delivery, off-site OCI
storage, first approved deployment, public HTTPS and failure/cancellation
acceptance remain manual infrastructure steps. Until those pass, this is
tested deployment automation—not a claim that production exists.

Primary references: [GitHub environment deployment controls](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments),
[Deployments REST API](https://docs.github.com/en/rest/deployments/deployments),
[Actions artifact REST API](https://docs.github.com/en/rest/actions/artifacts),
[App installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation),
and [GitHub CLI attestation policy](https://cli.github.com/manual/gh_attestation_verify).
