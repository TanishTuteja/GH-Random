# Deployment checklist — Oracle Cloud Always Free

Reviewed against the repository on 2026-10-20. `[x]` means implemented and
locally verified; `[ ]` means work remains. Local checks do not establish that
something works in production. OCI resources have not been verified. The public
repository's active main-protection ruleset was verified on 2026-10-17; other
GitHub settings remain pending unless explicitly marked complete.

Current execution scope: complete every non-OCI item. OCI provisioning, private
VM configuration and acceptance that requires the live OCI stack remain manual
and unchecked. GitHub administration/releases and repository implementation are
still in scope; they must not be treated as OCI prerequisites to avoid doing them.
On 2026-10-20 the operator explicitly chose to configure and test Gmail alerts
alongside OCI setup. That private account/credential and inbox-acceptance work
remains manual and unchecked; monitoring stays disabled until acceptance.
On 2026-10-27 the operator confirmed all OCI work, SSH and hostname setup will
be done personally. Finish repository/release work, then hand off the remaining
VM steps; do not attempt remote access or live deployment.

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
- [x] Pin reviewed Python, PostgreSQL, and Caddy images to immutable digests and
  document how updates will be reviewed.
- [x] Enforce selected Python/tool versions and synchronized production/CI/env
  image pins in offline tests. Prevent automatic Python minor/major and database
  major proposals from silently changing the supported runtime/data policy.

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
- [x] Add optional production pgAdmin bound to `127.0.0.1`, separate opt-in
  overlay/private password file, pinned image and bounded non-root runtime.
  Test login and database connectivity on a disposable stack; document managed
  Bastion SSH forwarding and service-only cleanup in deployment/PGADMIN.md.
- [ ] Verify the real Bastion tunnel and external port isolation if production
  pgAdmin is used. The optional tool need not block launch.

### 3. HTTPS and reverse proxy

- [x] Add and validate `Caddyfile` for HTTPS, reverse proxying, compression, and
  basic response headers.
- [ ] Verify live DNS, certificate issuance/renewal, HTTP-to-HTTPS redirects, and
  the real hostname on the VM; these were not covered by local validation.
- [x] Add exact-host allowlisting and explicit private-network proxy trust;
  test spoofed forwarded headers and canonical redirect schemes through Caddy.
  Verify the real HTTPS scheme/client address again on the VM before launch.

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
- [x] Implement crawler failure/timeout systemd email hooks and an hourly
  missing-success monitor with durable retries, cooldowns and recovery notices.
- [ ] Configure SMTP and verify real failure/recovery/staleness emails on the VM;
  Docker outages may require operator recovery of the recorded managed child.
- [x] Smoke-test real App authentication in a locally built hardened ARM64 role:
  root-owned mode-0600 key, read-only mount/root, outbound authenticated HTTPS,
  token revocation and owned-resource cleanup. No database/crawl was involved.
- [ ] Recheck the real VM's key bind ownership/access and outbound network path;
  local Docker volume staging does not establish OCI host permissions/firewalls.

### 6. Backups, alerts, and operations

- [x] Add host-side PostgreSQL backup/restore tooling using container client
  tools: consistent custom-format dumps, private/atomic files, checksums,
  explicit new-database-only restores, and no live database drops/overwrites.
  Ship the tool with matching release deployment files.
- [x] Rehearse backup/restore against a disposable production stack in native
  ARM64 CI: verify schema/catalog counts, restored app/API discovery, source
  preservation, and corrupt-archive/existing-target refusal. This is not a
  production/off-site recovery rehearsal.
- [x] Add daily encrypted backups at 05:15 UTC, seven-day/minimum-two-verified-copy
  local retention, a 24-hour RPO and an initial one-hour restore target after host
  readiness. Document unmeasured production limits in deployment/BACKUPS.md.
- [x] Add age sealing/offline recovery and OCI CLI upload plus full SHA-256
  read-back verification; test real encryption/restoration and a local object-store
  contract without an OCI account. Support encrypted pre-deploy backups too.
- [ ] Install/configure the backup timer, offline identities and scoped OCI
  instance-principal permissions on the VM; verify real capacity/runtime/quotas.
- [ ] Upload encrypted backups to OCI Object Storage with scoped permissions and
  lifecycle rules. Keep recovery material accessible if the VM is lost.
- [ ] Restore an encrypted off-site production backup into a separate database
  and verify schema, catalog counts, and discovery, including VM-loss recovery,
  before relying on the complete backup procedure.
- [x] Choose email alerts through configurable SMTP; credentials/recipients stay
  in private VM configuration, not the repository. No live delivery tested yet.
- [x] Implement TLS-only email for crawler/systemd and opt-in deployment failures,
  with safe identifiers/diagnostic commands, not raw logs or secrets. Test actual
  TLS on loopback, durable retries, cooldown, recovery and non-recursive handlers.
- [x] Wire backup failure email and success watermarks; refuse watermark promotion
  on failed upload/read-back/retention/lease cleanup. A later failed run cannot
  erase the last verified success. Explicit local-only rehearsals never count.
- [ ] Configure the real SMTP provider/sender/recipients, confirm its free quota,
  and test inbox delivery and failure/recovery without relying solely on SMTP acceptance.
- [x] Implement GitHub-hosted external HTTPS/API and host/crawl/backup telemetry
  monitoring with protected environment-only SMTP secrets, durable pending email,
  reminders and recovery. Test real TLS loopback delivery and forged/stale data.
  The independent monitoring environment is configured and read back.
  The permanent issue #26 and its Actions variable are configured; monitoring
  is explicitly disabled pending live acceptance.
- [ ] Configure the real hostname, dedicated telemetry App/anchor and SMTP
  environment secrets, enable the external schedule and verify actual outage,
  staleness and recovery emails after the OCI stack exists. GitHub itself is
  still a shared monitoring failure domain; see deployment/MONITORING.md.
- [x] Bound all production Docker logs and container resources; add a reviewed,
  optional host-wide journald limits example and operator commands.
- [x] Add bounded host-local free-space/inode and verified TLS-expiry probes to
  the hourly email monitor, including safe failure/recovery notices and tests.
  Crawl/backup success-age monitoring is already implemented above.
- [ ] Install/verify the journald policy and configure/test real disk/TLS probes
  on the VM; verify certificate renewal and actual alert inbox delivery.
- [x] Document recovery from stuck crawls, failed migrations, full disks, revoked
  GitHub credentials, and complete VM loss/replacement.
- [x] Define host updates/reboots, secret rotation, and image cleanup in
  deployment/OPERATIONS.md; preserve data, backup keys and rollback images.
- [ ] Measure initial resource limits, disk growth and recovery targets on the
  actual VM; rehearse the documented incidents and reboot recovery there.

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
- [x] Publish signed `v0.1.0` and verify all four assets, source-matched archive,
  signed archive/image attestations, actual provenance/SBOM, image labels and
  ARM64 digest smoke tests. Actual publication and local acceptance evidence is
  recorded below; this is no longer inferred from offline tests.
- [x] Choose public GHCR images so the VM can pull without registry credentials.
- [x] Verify the first GHCR package is publicly accessible with a complete
  anonymous exact-digest ARM64 pull using a credential-free Docker configuration.
  Never copy the temporary Actions token to the VM. The release workflow and
  all four deployment assets are ready; no production deployment has started.
- [x] Implement the production workflow, protected production approval and
  outbound-only VM pull mechanism with a separate repository-scoped App. Bind
  approval to immutable run/attempt receipts, verify signed source and
  archive/image attestations, and report actual VM version/status/URL only.
- [x] Serialize deployments without remote cancellation during DDL; durably fence
  replay after crashes, retry terminal status delivery and preserve prior releases.
  Offline trust/failure checks and the native ARM64 approved-pull rehearsal pass;
  require the native rehearsal again on each final PR head.
- [x] Use least-privilege workflow permissions and pinned third-party action
  revisions; keep untrusted PR content away from privileged events and secrets.
- [ ] Install the reviewed puller, configure its dedicated App and protected
  environment variable, and verify the first real approved deployment after OCI
  exists. See deployment/AUTOMATION.md; no live VM path is claimed by CI.

### 8. Repository administration and contributor basics

- [x] Keep the project README and license, including local setup and production
  configuration documentation.
- [x] Adopt feature branches and pull requests for all changes, with no permanent
  `dev` branch. Record the workflow in `AGENTS.md` and the README; branch
  protection now enforces PRs and passing CI on GitHub.
- [x] Create SSH-signed commits and configure the local ignored allowed-signers
  trust file. Verify commit signatures locally and through GitHub; releases
  additionally require signed annotated tags.
- [x] Protect `main` with the active `Protect main` ruleset: require PRs, resolved
  conversations, and `Python checks`, `PostgreSQL tests`, and `Production
  containers` from GitHub Actions.
  Require branches to be up to date before merging; block force pushes and
  deletion. Require zero reviewer approvals for the solo-maintainer workflow,
  with an empty bypass list so administrators follow the same rules.
  Verified via GitHub's ruleset API and protected-branch flag on 2026-10-17.
- [x] Protect `v*` tags against update/deletion with no bypass actors. Verify
  the active tag ruleset and document signed tags/verification and new-version
  fixes instead of moving released tags.
- [x] Add weekly Dependabot proposals for uv, Actions, Docker and Compose,
  grouping minor/patch updates and limiting open version PRs to two per ecosystem.
  Enable/verify dependency alerts and automatic security-fix PRs; never auto-merge.
- [x] Add contributor/conduct/security guidance, bug/feature/question forms,
  PR template and useful maintenance/deployment/dependency/security/test labels.
  Enable and verify private vulnerability reporting before publishing its link.
- [x] Enable and read back free public-repository secret scanning and push
  protection. Inspect alert identifiers/counts without exposing secret values.
  No open secret/dependency alerts were reported at acceptance; this is not a
  claim of exhaustive detection or vulnerability-free dependencies.
- [x] Create GitHub's `production` environment with protected-branch restriction
  and owner review. Allow self-review for the solo maintainer; this is not a
  second-person audit. Actual deployment automation is tracked separately above.

## Infrastructure and account setup

Unchecked items require setup or confirmation. Repository files alone cannot
establish that OCI resources exist; completed GitHub settings were read back
through authenticated APIs, as recorded above.

- [ ] Create/verify OCI tenancy and MFA, choose the home region, and confirm
  current Always Free quotas, ARM availability, storage, and network costs.
  Use eligible resources and quotas/budget notifications for the zero-cost
  requirement; budget alerts alone do not cap spending.
- [ ] Complete setup of the provisioned AMD64 E2.1.Micro (Ubuntu 24.04 Minimal,
  1 GB RAM, 100 GB boot volume); install Docker Engine, Compose, and host tools.
  A1 provisioning failed for lack of capacity. The VM boots and Bastion SSH
  access works with the replacement key. Document boot/data volume preservation, automatic
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
- [x] Configure and verify GitHub main/tag protection, security reporting,
  dependency maintenance and protected production environment/review settings.
- [ ] Configure private VM deployment credentials/scopes and validate the live
  deployment path after automation and OCI setup; package publication/access
  acceptance is tracked independently in section 7.

## First-release acceptance sequence

- [x] Finish CI/protection and merge the release candidate to `main` through PR
  #25, with all three required checks green on the exact PR and merge commits.
- [x] Rehearse deployment, encrypted/local restore and same-schema rollback with
  disposable data, including discovery and failure guards, in native ARM64 CI.
  Production/off-site acceptance remains separately unchecked above.
- [x] Create signed `v0.1.0` and its GitHub Release; confirm the workflow publishes
  the expected ARM64 image and immutable digest.
- [ ] Publish a new signed AMD64 release after the low-memory PR and required
  CI checks pass. v0.1.0 is ARM64-only and cannot be used on the current VM.
  Rehearse actual container memory use before release; finish live micro-VM
  acceptance in [LOW_MEMORY.md](deployment/LOW_MEMORY.md) before public launch.
- [ ] Deploy the new AMD64 digest and matching files, back up as appropriate, migrate
  once, and start PostgreSQL/web/Caddy. Record the deployment in GitHub's
  `production` environment as part of the deployment workflow.
- [ ] Complete the first full crawl and confirm an active catalog before launch.
  Verify filters, Any, single-result fallback, immediate repetition avoidance,
  and no-result behavior through the public page/API.
- [ ] Verify DNS/HTTPS, intended public routes/ports, six-hour scheduling and
  sort rotation, failed/interrupted-run recovery, restored backups, alert delivery,
  and recovery after a host reboot.
- [x] Record operator commands, verified release version/digest and recovery
  instructions in the README and matching operator guides; retain product
  enhancements in ROADMAP.md.
- [ ] Record the actual public site URL and deployed version/container state
  after OCI setup. A published release is not evidence of a deployed website.

## First-release evidence — 2026-10-20

- [Signed v0.1.0 release](https://github.com/TanishTuteja/GH-Random/releases/tag/v0.1.0),
  source `a312576a631ab575894e2a780ada1904844f0569`; the SSH tag verifies locally
  and GitHub reports its signature valid. Release tags cannot be moved/deleted.
- [Required PR checks](https://github.com/TanishTuteja/GH-Random/actions/runs/39311472537),
  [merged-source checks](https://github.com/TanishTuteja/GH-Random/actions/runs/39312206594)
  and [actual publication](https://github.com/TanishTuteja/GH-Random/actions/runs/39312984113)
  all succeeded. Linux CI passed all 452 tests; the local Windows suite passed
  450 with two Linux-only skips. Ruff and Pyright passed.
- Exact published index:
  `ghcr.io/tanishtuteja/gh-random@sha256:6917abb8e53573cb7b6040060621e6044b4959165403c9402e25ff00bb465959`.
  The full anonymous pull succeeded without a registry credential/helper.
- Downloaded all four assets and verified API digests, SHA256SUMS, matching inner/
  outer metadata and 44 packaged deployment files against signed-source Git blobs.
  Verified both archive and image signatures with the exact repository, workflow,
  source/signer commit, tag ref and GitHub-hosted-runner policy.
- Independently fetched the real OCI graph anonymously: ARM64 platform and exact
  labels, SLSA v1 with six materials, linked SPDX 2.3 inventory with 672 packages.
  Native CI exercised the published digest's deployment/restore/scheduler paths.
- Actual GitHub App authentication also passed against this published image;
  token revocation and private test-volume removal succeeded, without a crawl or
  database operation. Only the pre-existing local database container remains up.

Live OCI installation, actual DNS/TLS, private SMTP/telemetry configuration,
inbox delivery, production crawls and off-site recovery remain separately
unchecked. No public website or production deployment is claimed here.

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
