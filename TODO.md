# Deployment checklist — Oracle Cloud Always Free

Reviewed against the repository on 2026-10-17. `[x]` means implemented and
locally verified; `[ ]` means work remains. Local checks do not establish that
something works in production. OCI resources have not been verified. The public
repository's active main-protection ruleset was verified on 2026-10-17; other
GitHub settings remain pending unless explicitly marked complete.

## Repository implementation

### 1. Production image

- [x] Add `Dockerfile` for web, crawler, and migrations. Build and run AMD64 and
  ARM64 images locally. The image defaults to non-root; Compose overrides the
  crawler to root to read its root-owned key, with capabilities dropped and a
  read-only filesystem.
- [x] Add `.dockerignore`; package application code and migrations without
  copying local secrets, keys, or development files.
- [x] Make dependencies repeatable with committed `uv.lock`, shared locked
  installation in local setup/CI/Docker, and hash-verified build-tool constraints.
  Pin uv and use Python 3.12 across the supported environments.
- [ ] Pin reviewed Python, PostgreSQL, and Caddy images to immutable digests and
  document how updates will be reviewed.

### 2. Production Compose and configuration

- [x] Add `compose.production.yaml`: PostgreSQL, FastAPI, Caddy, and explicit
  one-shot migration/crawler profiles. Only Caddy publishes host ports; the
  database uses a persistent named volume and internal network.
- [x] Keep migration/crawl startup explicit and share the application image
  setting across roles. Restarting web does not start a crawl.
- [x] Add role-specific `deployment/*.env.example` templates. Mount the private
  key read-only only into the crawler. Actual VM secrets still need setup.
- [x] Smoke-test an empty disposable production database, baseline migration,
  and packaged web response (HTTP 200); remove that test stack.
- [ ] Add optional production pgAdmin bound to `127.0.0.1`, reached through a
  Bastion tunnel and stopped after use. Development pgAdmin already exists;
  production pgAdmin need not block launch.

### 3. HTTPS and reverse proxy

- [x] Add and validate `Caddyfile` for HTTPS, reverse proxying, compression, and
  basic response headers.
- [ ] Verify live DNS, certificate issuance/renewal, HTTP-to-HTTPS redirects, and
  the real hostname on the VM; these were not covered by local validation.
- [ ] Verify trusted proxy/forwarded-header handling and host validation behind
  Caddy so scheme, client IP, and host handling match production.

### 4. Deployment and migrations

- [x] Document scripted first deployment and explicit crawler commands in `README.md`.
- [x] Squash pre-production migrations into one baseline. Preserve migration
  history from the first production deployment onward.
- [x] Add a repeatable deploy script: validate configuration, acquire a lock,
  back up PostgreSQL, pull the exact image digest, run `alembic upgrade head`
  once, update web, smoke-test `/`, and report the result.
- [x] Hold database ownership across backup/migration/restart/smoke/promotion;
  fail promptly on an existing crawl/migration, exclude new cooperating owners,
  and release the named coordinator on normal failure/interruption. Do not
  disable scheduling. EOF/idle expiry handles a lost controller. Timer-specific
  child termination and hung-run alerts remain in section 5.
- [x] Deploy Compose, Caddy, and scripts from the same release
  revision as the image. Record version, commit, digest, and prior digest;
  never combine a release image with deployment files taken from moving `main`.
- [x] Include systemd files and the crawler bootstrap in matching release assets;
  dispatch the verified release's worker/digest and document deliberate
  stop/update/verify/reload/start for host unit or bootstrap policy changes.
- [x] Define and rehearse same-schema rollback to the previous verified image.
  Require explicit compatibility acknowledgement. Never automatically
  downgrade migrations; incompatible schema changes need a recovery plan.
  Preserve database volumes during normal redeployment.
- [x] Test real scripted bootstrap/redeploy, migration failure, smoke failure,
  controller EOF/idle cleanup, exclusion, and rollback using disposable data.
  Keep live production acceptance separate; no production deployment performed.

### 5. Scheduled crawler and crash recovery

- [x] Implement 200 language/star/pushed-time combinations, five-second pacing,
  and rotation across four sort orders after successful crawls.
- [x] Preserve the active catalog on a normally handled crawl failure and allow
  the next normal crawl. Support explicit `--resume` with durable checkpoints.
- [x] Implement three-day membership retention and cleanup of retired/failed
  generations older than seven days after successful publication, including
  orphaned repository metadata.
- [x] Add a systemd service and persistent six-hour UTC timer for a normal crawl,
  without `--resume`, using the verified deployed digest. Document IST times,
  one catch-up after missed slots, runtime limits, and failure/next-slot behavior.
- [ ] Install/enable the units on the production VM, verify real schedule and
  catch-up after reboot, and measure crawl duration against the five-hour limit.
- [x] Add database-scoped session ownership for all manual/scheduled CrawlService
  calls and online Alembic operations. Contenders fail promptly; every statement
  and commit is fenced to the original owning connection, including after a
  disconnect. Browsing/status reads are not excluded. Whole-deploy orchestration
  is tracked above, separately from schema/crawler exclusion.
- [x] Recover abandoned `running` runs after PostgreSQL releases a dead owner's
  connection, including unclean process exit and failure-recording outages.
  The next exclusive normal/resume crawler marks only consistent staging state
  failed, preserves checkpoints/active catalog, and never uses age to evict a
  live owner. Stop legacy pre-lock processes before the first upgrade.
- [x] Track/stop the exact labelled Docker child; add a five-hour supervisor
  limit, independent systemd fallback and ExecStopPost cleanup. Rehearse Docker
  timeout/orphan recovery plus native systemd timer retry/stop/SIGKILL/watchdog
  in CI without real GitHub traffic or permanent service installation.
- [ ] Connect failure/timeout and missing-success states to real alerts; Docker
  daemon outages may require operator recovery of the recorded managed child.
- [ ] Smoke-test real GitHub App authentication from the hardened ARM64 crawler
  container, including root-owned key access and outbound network connectivity.

### 6. Backups, alerts, and operations

- [x] Add host-side PostgreSQL backup/restore tooling using container client
  tools: consistent custom-format dumps, private/atomic files, checksums,
  explicit new-database-only restores, and no live database drops/overwrites.
  Ship the tool with matching release deployment files.
- [x] Rehearse backup/restore against a disposable production stack in native
  ARM64 CI: verify schema/catalog counts, restored app/API discovery, source
  preservation, and corrupt-archive/existing-target refusal. This is not a
  production/off-site recovery rehearsal.
- [ ] Add backup scheduling and retention. Define frequency, acceptable data
  loss, and restore time; do not prune the only usable recovery copy. Local
  snapshots are currently unencrypted and no timer is installed.
- [ ] Upload encrypted backups to OCI Object Storage with scoped permissions and
  lifecycle rules. Keep recovery material accessible if the VM is lost.
- [ ] Restore an encrypted off-site production backup into a separate database
  and verify schema, catalog counts, and discovery, including VM-loss recovery,
  before relying on the complete backup procedure.
- [ ] Choose email/webhook alerts for crawler, backup, and deployment failures.
  Include run/deployment identifiers and useful logs without secrets; test
  delivery and prevent alert loops.
- [ ] Add monitoring outside the VM for site downtime and missing successful
  crawls/backups. An on-host failure handler cannot alert while its VM is down.
- [ ] Bound Docker and journald logs; document container logs, `journalctl`, and
  `python -m app.cli status`. Monitor disk use, backup age, last successful
  crawl, and TLS expiry/renewal failures.
- [ ] Document recovery from stuck crawls, failed migrations, full disks, revoked
  GitHub credentials, and complete VM loss/replacement.
- [ ] Define host updates/reboots, secret rotation, image cleanup, and resource
  limits suitable for the VM. Preserve persistent data and rollback images.

### 7. GitHub Actions and releases

- [x] Add PR/`main` CI for formatting, Ruff, Pyright, and PostgreSQL-backed tests
  using disposable credentials and no production secrets/GitHub App keys.
  Include fresh-schema migration and Alembic drift checks.
- [x] Add native ARM64 container/configuration checks to CI: image build,
  production Compose/HTTPS Caddy validation, fresh-database migration/drift,
  hardened crawler status, and proxied web/API smoke tests. Use isolated test
  resources and automatic cleanup; require the `Production containers` check.
- [x] Add a release workflow for signed semantic-version tags and GitHub
  Releases, starting with `v0.1.0`. Verify the source and passing checks, build
  ARM64 (optionally AMD64 too), and publish to GHCR using `GITHUB_TOKEN`.
- [x] Implement digest/metadata/checksum and matching deployment-archive assets;
  include source/version image labels and document signed release creation and
  rollback constraints. Request BuildKit provenance and an SBOM. Smoke-test the
  exact published digest before attaching assets; refuse completed asset rewrites.
- [ ] Perform the first actual signed release/GHCR publish and verify all assets,
  provenance/SBOM, image labels, and ARM64 digest smoke tests. Implementation and
  offline/CI tests do not establish that publication permissions work.
- [x] Choose public GHCR images so the VM can pull without registry credentials.
- [ ] After first publication, change the initially private GHCR package to public
  and verify anonymous digest pulls. Never copy the temporary Actions token to
  the VM. Do not announce/deploy a release until its workflow and assets are ready.
- [ ] Add a production deployment workflow and GitHub `production` environment.
  Record version/digest, status, and URL; set intended branch/approval controls
  after confirming availability for the repository/account plan.
- [ ] Implement automation access through short-lived OCI Bastion sessions or a
  VM-side pull mechanism, with scoped, rotatable credentials and no public SSH.
- [ ] Serialize deployments and avoid cancellation during migrations; report
  failures accurately and retain the previous release.
- [ ] Use least-privilege workflow permissions and pinned third-party action
  revisions; keep untrusted PR content away from privileged events and secrets.

### 8. Repository administration and contributor basics

- [x] Keep the project README and license, including local setup and production
  configuration documentation.
- [x] Adopt feature branches and pull requests for all changes, with no permanent
  `dev` branch. Record the workflow in `AGENTS.md` and the README; branch
  protection now enforces PRs and passing CI on GitHub.
- [x] Create SSH-signed commits. Local signature verification still needs
  `gpg.ssh.allowedSignersFile`; GitHub verified the CI commit's signature.
- [x] Protect `main` with the active `Protect main` ruleset: require PRs, resolved
  conversations, and `Python checks`, `PostgreSQL tests`, and `Production
  containers` from GitHub Actions.
  Require branches to be up to date before merging; block force pushes and
  deletion. Require zero reviewer approvals for the solo-maintainer workflow,
  with an empty bypass list so administrators follow the same rules.
  Verified via GitHub's ruleset API and protected-branch flag on 2026-10-17.
- [ ] Protect release tags from accidental changes/deletion and document signed
  tag creation and verification.
- [ ] Add Dependabot for Python, GitHub Actions, and Docker base images; enable
  dependency alerts/security updates and choose a manageable update cadence.
- [ ] Add `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue forms,
  a PR template, and useful labels. Confirm the private vulnerability reporting
  channel/contact before publishing the security policy.

## Infrastructure and account setup

All items require setup or confirmation. Repository files alone cannot establish
that OCI resources or GitHub account settings exist.

- [ ] Create/verify OCI tenancy and MFA, choose the home region, and confirm
  current Always Free quotas, ARM availability, storage, and network costs.
  Use eligible resources and quotas/budget notifications for the zero-cost
  requirement; budget alerts alone do not cap spending.
- [ ] Provision the ARM VM with persistent storage; install Docker Engine,
  Compose, and host tools. Document boot/data volume preservation, automatic
  startup, and recovery after idle-instance reclamation.
- [ ] Reserve/record the public IP, choose a hostname, and configure DNS. Use a
  free subdomain or separately acquired domain; OCI is not a free registrar.
  Publish IPv6 DNS only after testing IPv6 networking/firewall rules too.
- [ ] Configure OCI VCN/security groups and host firewall: public TCP 80/443 and,
  if using Caddy HTTP/3, UDP 443. Keep PostgreSQL, pgAdmin, Docker API, and public
  SSH closed. Test externally; Docker-published ports can bypass UFW rules.
- [ ] Configure Bastion access and required private SSH rules/agent/session
  prerequisites. Use passphrase-protected admin keys, disable password/root SSH
  login, and restrict client CIDRs. Verify access before closing bootstrap access.
- [ ] Create root-owned production configuration/key files outside the repo,
  replace placeholders, and check permissions and matching DB credentials.
  URL-encode reserved password characters in the SQLAlchemy database URL.
- [ ] Configure Object Storage, restricted backup identity, encryption/recovery
  access, lifecycle rules, and alert destination within free quotas.
- [ ] Configure GitHub branch/tag rules, Actions/package permissions, production
  environment, and deployment credentials once workflows exist.

## First-release acceptance sequence

- [ ] Finish CI/protection and merge the release candidate to `main`.
- [ ] Rehearse deployment, restore, and rollback with disposable data.
- [ ] Create signed `v0.1.0` and its GitHub Release; confirm the workflow publishes
  the expected ARM64 image and immutable digest.
- [ ] Deploy that digest and matching files, back up as appropriate, migrate
  once, and start PostgreSQL/web/Caddy. Record the deployment in GitHub's
  `production` environment as part of the deployment workflow.
- [ ] Complete the first full crawl and confirm an active catalog before launch.
  Verify filters, Any, single-result fallback, immediate repetition avoidance,
  and no-result behavior through the public page/API.
- [ ] Verify DNS/HTTPS, intended public routes/ports, six-hour scheduling and
  sort rotation, failed/interrupted-run recovery, restored backups, alert delivery,
  and recovery after a host reboot.
- [ ] Record operator commands, URL, current version/digest, recovery instructions,
  and remaining optional improvements in the README.

## Implementation order

CI -> main protection -> repeatable release image/GHCR publishing -> backup and
deploy/restore scripts -> crawler locking/recovery and systemd scheduling ->
alerts and deployment workflow -> first-release rehearsal and acceptance.
OCI provisioning, hostname selection, and access setup can proceed alongside
repository work. Optional production pgAdmin and contributor extras need not
block an otherwise verified first launch.

## Reference documentation

- [OCI Always Free resources and current limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Docker port publishing and host firewalls](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Publishing images to GHCR from Actions](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
- [GitHub deployment environments and protection rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
