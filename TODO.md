# Deployment checklist — Oracle Cloud Always Free

Reviewed against the repository and deployment evidence on 2026-10-29. `[x]`
means verified at the scope stated in that item; `[ ]` means work remains.
Local/CI checks alone do not establish production behavior. The operator performed
the initial OCI setup, then expressly authorized remote VM acceptance work.
Public HTTPS, GitHub monitoring, private host checks and an off-site restore have
been checked at the scope in the [live acceptance record](deployment/OPERATIONS.md#live-acceptance--2026-10-28). The public
repository's active main-protection ruleset was verified on 2026-10-17; other
GitHub settings remain pending unless explicitly marked complete.

The first production deployment, full crawl, encrypted catalog backup, separate
Windows restore and email acceptance are complete. GitHub monitoring and a free
independent availability monitor are enabled. Keep unverified operational checks
below open; do not infer them from the first successful run.

## Repository implementation

### 1. Production image

- [x] Retarget future releases and native container CI to the provisioned AMD64
  micro (PR #32). Preserve the ARM64-only v0.1.0 assets. Test a shared 768 MiB
  no-swap budget with 50,000 active repos and 1.45 million retained memberships:
  initial peak 401.8 MiB, no OOM kills/restarts, all 200 synthetic queries passed.
  The subsequent live memory correction and remaining capacity observations are
  tracked below; synthetic CI load is not a live capacity guarantee.
- [x] Resolve the pre-release CA trust incident (PR #33): certifi 2026.10.23,
  uv 0.13.5, Python 3.12.16, patched Caddy CA store, unused pip/ensurepip removal,
  real trust-store checks and no-cache release builds. Recheck repository alerts
  before publication; the old release notes now warn against deploying it.

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
  key read-only only into the crawler. The operator confirmed private VM files
  and their ownership/permissions during setup.
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
- [x] Verify live DNS, valid HTTPS and the real hostname, plus the canonical
  HTTP-to-HTTPS 308 redirect at https://ghrandom.duckdns.org.
- [ ] Observe certificate renewal and verify the real forwarded client address
  on the VM; initial certificate issuance does not establish renewal.
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
  Keep these disposable failure rehearsals separate from live acceptance.

### 5. Scheduled crawler and crash recovery

- [x] Implement 200 language/star/pushed-time combinations, five-second pacing,
  and rotation across four sort orders after successful crawls.
- [x] Preserve the active catalog on a normally handled crawl failure and allow
  the next normal crawl. Support explicit `--resume` with durable checkpoints.
- [x] Implement three-day membership retention and cleanup of retired/failed
  generations older than seven days after successful publication, including
  orphaned repository metadata.
- [x] Merge and verify the repository-first membership lookup index and
  additive migration (PR #35), preserving the published baseline. Populated
  upgrade/downgrade/re-upgrade preserves exact data; the strengthened published-
  image memory fixture removes 5,000 expired-only repositories without OOMs.
- [x] Add a systemd service and persistent six-hour UTC timer for a normal crawl,
  without `--resume`, using the verified deployed digest. Document IST times,
  one catch-up after missed slots, runtime limits, and failure/next-slot behavior.
- [x] Install/enable the production crawler units and verify the six-hour UTC
  schedule. The first managed crawl completed in 2 h 8 min with cleanup confirmed.
- [x] Verify scheduled subsequent runs and all four sort orders, followed by a
  wrap to updated-descending. At 08:05 UTC on 2026-10-29, five crawls had
  completed, none had failed, and the sixth was running stars-descending.
- [x] Verify automatic service/timer startup after a controlled reboot following
  six successful crawls and a fresh verified off-site backup. Preserve the exact
  active catalog, schema, database volume and application image.
- [ ] Observe production catch-up after a genuinely missed six-hour slot;
  measure full-crawl resource peaks and longer-term runtime headroom.
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
- [x] Configure VM SMTP and confirm real puller failure/recovery emails arrived.
- [x] Verify crawl/backup staleness failure/recovery inbox delivery from an
  isolated VM acceptance state, without altering production success markers.
- [x] Rehearse abrupt process exit and the next normal crawl on this VM with
  the exact released image and an isolated temporary database: preserve the
  active membership, recover the abandoned run and retain sort rotation.
  Remove only the owned test database/containers; the production crawl continued.
- [ ] Exercise managed-child cleanup during a real service interruption or host
  restart. Docker outages may require operator recovery of the recorded child.
- [x] Smoke-test real App authentication in a locally built hardened ARM64 role:
  root-owned mode-0600 key, read-only mount/root, outbound authenticated HTTPS,
  token revocation and owned-resource cleanup. No database/crawl was involved.
- [x] Verify the VM crawler's key access and outbound authenticated network path
  through a successful full production crawl, not just local volume staging.

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
- [x] Configure the backup timer, public recipients and scoped OCI instance
  principal. The first catalog backup completed in 46 seconds; enable the daily
  05:15 UTC schedule and 36-hour missing-success monitoring after its success.
- [x] Upload the encrypted catalog backup to private OCI Object Storage and
  verify a complete downloaded copy against its SHA-256 checksum. The subsequent
  scheduled backup succeeded on 2026-10-29 at 05:16:16 UTC after full read-back.
- [x] Restore that off-site production archive on a separate Windows host:
  schema `20261027_01`, 62,459 repositories/active memberships, zero running
  crawls, and working released-image page/API discovery, filtering and exclusion.
- [ ] Preserve two independent offline recovery-key copies. The operator
  confirmed only one copy currently exists and deferred the second copy.
  Different partitions on one physical disk are not independent copies.
- [x] Inspect current backup usage and define safe remote-retention review:
  41,892,828 bytes across five objects on 2026-10-29. Remote deletion stays off;
  review usage monthly and preserve at least two known-good off-site copies.
- [ ] Confirm actual free storage quotas and measure multi-day growth before
  enabling any supervised remote expiration. Local seven-day/minimum-two
  retention does not delete remote objects.
- [ ] Rehearse replacement-host configuration, credentials/DNS recovery and
  measure full VM-loss RPO/RTO. A local database restore is not a rebuilt VM.
- [x] Choose email alerts through configurable SMTP; credentials/recipients stay
  in private VM configuration, not the repository. Live delivery is now confirmed.
- [x] Implement TLS-only email for crawler/systemd and opt-in deployment failures,
  with safe identifiers/diagnostic commands, not raw logs or secrets. Test actual
  TLS on loopback, durable retries, cooldown, recovery and non-recursive handlers.
- [x] Wire backup failure email and success watermarks; refuse watermark promotion
  on failed upload/read-back/retention/lease cleanup. A later failed run cannot
  erase the last verified success. Explicit local-only rehearsals never count.
- [x] Configure dedicated Gmail sender/App Passwords privately for the VM and
  protected GitHub monitoring environment; confirm both failure/recovery emails
  arrived in the operator's inbox, not only that SMTP accepted them.
- [ ] Review ongoing provider sending/storage quotas against actual usage.
- [x] Implement GitHub-hosted external HTTPS/API and host/crawl/backup telemetry
  monitoring with protected environment-only SMTP secrets, durable pending email,
  reminders and recovery. Test real TLS loopback delivery and forged/stale data.
  The independent monitoring environment is configured and read back.
  The permanent issue #26 and its Actions variable are configured.
- [x] Configure the real hostname, dedicated App/telemetry anchor and protected
  SMTP secrets. Verify fresh host/crawl/backup watermarks, real-site healthy runs
  and a controlled probe failure/recovery with both emails received; restore
  the real origin and enable the external schedule.
- [x] Exercise host/crawl/backup staleness independently using disposable
  in-memory telemetry; verify pending notification retry after a real loopback
  SMTP rejection and subsequent provider delivery. Production watermarks and
  the GitHub incident ledger were unchanged by this isolated acceptance test.
- [x] Add independent free UptimeRobot HTTP/S availability monitoring of the
  discovery API at a configured five-minute interval; verify active/Up status,
  Free plan and operator-confirmed test email. Retain GitHub's stricter response
  validation and freshness checks; its cron delivery is best effort.
- [x] Verify real public-site outage/recovery through both external monitors:
  stop only Caddy with an automatic restoration backstop, confirm GitHub's site
  incident and closure, and receive both providers' failure/recovery email pairs.
  The production database/crawler stayed running; full VM loss remains separate.
- [x] Bound all production Docker logs and container resources; add a reviewed,
  optional host-wide journald limits example and operator commands.
- [x] Add bounded host-local free-space/inode and verified TLS-expiry probes to
  the hourly email monitor, including safe failure/recovery notices and tests.
  Crawl/backup success-age monitoring is already implemented above.
- [x] Enable the hourly VM monitor and verify a completed disk/TLS/freshness
  check plus hourly telemetry publication after successful crawl and backup.
- [x] Install/verify the journald policy: 256 MiB persistent, 64 MiB runtime,
  1 GiB free-space reserve and seven-day retention. Verify isolated low-disk and
  TLS-expiry failure/recovery emails; the operator received all eight acceptance
  emails covering these and crawl/backup staleness. No disk was filled or host
  clock/certificate changed. Actual certificate renewal remains open above.
- [x] Document recovery from stuck crawls, failed migrations, full disks, revoked
  GitHub credentials, and complete VM loss/replacement.
- [x] Define host updates/reboots, secret rotation, and image cleanup in
  deployment/OPERATIONS.md; preserve data, backup keys and rollback images.
- [x] Inspect live resource limits and correct the observed web OOM: raise only
  web from 128 to 192 MiB, recreate it with the same verified v0.1.1 digest, and
  mirror the default in PR #41. Twenty subsequent API requests produced no OOM
  events/restarts, with a roughly 106 MiB measured web peak. This limited sample
  does not establish sustained capacity or response-time guarantees. The next
  morning's inspection still showed zero web restarts/OOMs at the 192 MiB limit.
- [ ] Measure multi-day disk/memory growth and full recovery targets on the
  actual VM; rehearse the remaining incidents and reboot recovery there.

### 7. GitHub Actions and releases

- [x] Add PR/`main` CI for formatting, Ruff, Pyright, and PostgreSQL-backed tests
  using disposable credentials and no production secrets/GitHub App keys.
  Include fresh-schema migration and Alembic drift checks.
- [x] Add native container/configuration checks to CI (initially ARM64, now
  AMD64 for the provisioned host): image build,
  production Compose/HTTPS Caddy validation, fresh-database migration/drift,
  hardened crawler status, and proxied web/API smoke tests. Use isolated test
  resources and automatic cleanup; require the `Production containers` check.
- [x] Add a release workflow for signed semantic-version tags and GitHub
  Releases, starting with `v0.1.0`. Verify the source and passing checks, build
  ARM64 for v0.1.0 and AMD64 for v0.1.1, and publish to GHCR using `GITHUB_TOKEN`.
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
  all four deployment assets are ready. The later AMD64 production deployment
  is recorded separately below.
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
- [x] Install the reviewed puller, configure its dedicated App and protected
  environment variables, and verify the first real approved deployment. The
  VM reported success and independent public HTTPS/discovery checks passed.
  During v0.1.1 setup, apply the documented alert-state systemd override and
  verify an idle poll plus its recovery notification before dispatching. Upgrade
  the bootstrap HTTP client for Actions artifact media negotiation and verify
  actual approval ZIP/release archive downloads. Upgrade the attestation helper
  to use isolated writable CLI configuration/cache directories and verify both
  signatures inside the service sandbox. These operator-applied compatibility
  fixes preserve the immutable v0.1.1 release archive/image.

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

- [x] Verify the Hyderabad OCI tenancy and Always Free badge for the running
  E2.1.Micro. The console shows Free Tier/trial, and the operator confirmed MFA.
- [ ] Confirm tenancy-wide free quotas, storage/network usage and cost alerts.
  Budget notifications alone do not cap spending; do not upgrade to paid usage.
- [x] Set up the provisioned AMD64 E2.1.Micro (Ubuntu 24.04 Minimal,
  1 GB RAM, 100 GB boot volume); install and verify Docker, Compose and host tools.
  A1 provisioning failed for lack of capacity. The VM boots and Bastion SSH
  access works with the replacement key.
- [x] Verify deployed-stack startup and boot/data-volume preservation after the
  2026-10-29 controlled reboot; public page/API and all five timers passed.
- [ ] Rehearse recovery after complete VM loss or idle-instance reclamation.
- [x] Record the current public IP privately and configure the free hostname
  https://ghrandom.duckdns.org. Public DNS/HTTPS/discovery checks passed.
- [ ] Confirm public-IP reservation/change recovery and DNS update maintenance.
  Publish IPv6 DNS only after testing IPv6 networking/firewall rules too.
- [ ] Configure OCI VCN/security groups and host firewall: public TCP 80/443 and,
  if using Caddy HTTP/3, UDP 443. Keep PostgreSQL, pgAdmin, Docker API, and public
  SSH closed. Test externally; Docker-published ports can bypass UFW rules.
- [ ] Configure Bastion access and required private SSH rules/agent/session
  prerequisites; read back OCI ingress/client CIDR rules. One-hour private
  Bastion sessions and a passphrase-protected replacement key already work.
- [x] Disable root/password/keyboard-interactive SSH, validate effective sshd
  settings and a fresh Ubuntu connection before closing the working session.
- [x] Test public TCP isolation: 80/443 reachable; 22, 5432, 5050, 2375, 2376
  and 8000 filtered/unreachable. Verify only Caddy publishes container ports.
  OCI rule inspection, external UDP/IPv6 and forwarded-client-address checks
  remain separate; a timeout does not identify the blocking firewall layer.
- [x] Create root-owned production configuration/key files outside the repo,
  replace placeholders, and check permissions and matching DB credentials.
  URL-encode reserved password characters in the SQLAlchemy database URL.
- [x] Configure private Object Storage, restricted instance-principal backup
  access, public-key encryption and email destinations; verify upload/read-back
  and independent off-site recovery. Quotas/retention/key redundancy remain above.
- [x] Configure and verify GitHub main/tag protection, security reporting,
  dependency maintenance and protected production environment/review settings.
- [x] Configure private VM deployment credentials/scopes and validate the live
  deployment path after automation and OCI setup; package publication/access
  acceptance is tracked independently in section 7.

## First-release acceptance sequence

- [x] Finish CI/protection and merge the release candidate to `main` through PR
  #25, with all three required checks green on the exact PR and merge commits.
- [x] Rehearse deployment, encrypted/local restore and same-schema rollback with
  disposable data, including discovery and failure guards, in native ARM64 CI.
  Production/off-site evidence and remaining operational checks are separate above.
- [x] Create signed `v0.1.0` and its GitHub Release; confirm the workflow publishes
  the expected ARM64 image and immutable digest.
- [x] Publish and independently verify signed AMD64 v0.1.1 after green PR and
  exact-source main CI. All four assets, 46 source-matched packaged files,
  archive/image signatures, actual provenance/SPDX, anonymous pull and real
  App-auth/revocation passed. Published-image peak: 401.9 MiB under 768 MiB with
  no swap/OOMs/restarts. See the evidence below and use the ordered
  [operator handoff](deployment/FIRST_DEPLOYMENT.md); live acceptance stays manual.
- [x] Deploy the new AMD64 digest and matching files, back up as appropriate, migrate
  once, and start PostgreSQL/web/Caddy. Record the deployment in GitHub's
  `production` environment as part of the deployment workflow.
- [x] Complete the first full crawl and confirm an active 62,459-repository
  catalog, public Any/Python filtering and immediate repetition avoidance.
- [x] Verify real public API singleton fallback and empty results: C# with
  100,001+ stars repeats its sole repository when excluded; Ruby in that band
  returns the expected 404. All language/star/pushed filter values passed.
- [x] Verify the browser empty-result page with Ruby/100,001+ stars and the
  expected message; browser discovery with Any also preserves the clean root URL.
- [x] Verify browser singleton fallback: two submissions with C#/100,001+ stars
  rendered the same sole repository successfully and retained the clean URL.
- [x] Obtain operator manual verification of ordinary discovery, empty results
  and singleton fallback on the public site. Further UI changes still require
  their own manual verification before completion or commit.
- [x] Verify public DNS/HTTPS/page/API, TCP port isolation, scheduled sort
  rotation, isolated packaged crash recovery, off-site database/app restore,
  real outage alerts and controlled host reboot recovery. Remaining OCI rule
  read-back, managed-child interruption and full VM-loss checks stay separate.
- [x] Record operator commands, verified release version/digest and recovery
  instructions in the README and matching operator guides; retain product
  enhancements in ROADMAP.md.
- [x] Record the actual public site URL, deployed version/digest and operator
  evidence in deployment/OPERATIONS.md. Release publication alone is insufficient.

## Live deployment evidence — 2026-10-28

The operator completed initial VM setup; later remote acceptance was explicitly
authorized. See the
[acceptance record](deployment/OPERATIONS.md#live-acceptance--2026-10-28) for the
successful deployment/restore, scheduled crawling, host hardening, memory
correction, isolated email acceptance and independent availability monitor.
Unchecked operational items above remain open; this does not certify a complete
replacement-VM or reboot rehearsal.

## AMD64 release evidence — 2026-10-27

- [Signed v0.1.1](https://github.com/TanishTuteja/GH-Random/releases/tag/v0.1.1),
  source `94b9c94ff0232b3f3d8f0e8d5d7c25b902af548b`; annotated SSH tag object
  `cfbaaf74125554549252c027ff7668d2cba36f07` verifies locally and on GitHub.
- PRs #32–35 merged only after all required checks passed. The
  [exact-source main CI](https://github.com/TanishTuteja/GH-Random/actions/runs/40524537930)
  and [actual publication](https://github.com/TanishTuteja/GH-Random/actions/runs/40525960770)
  passed. Linux CI ran 460 tests; local Windows ran 458 with two Linux-only skips.
  Ruff and Pyright passed. The release rebuilt without Docker cache.
- Exact published index:
  `ghcr.io/tanishtuteja/gh-random@sha256:c5a6732f67d445be36eaaa00ded7706415982b13a5f3ad10b68eea7ca991e697`.
  A complete anonymous pull using an empty Docker credential configuration
  succeeded; actual platform/version/source labels match AMD64/v0.1.1/source.
- Independently downloaded all four assets; verified their API digests,
  SHA256SUMS, matching inner/outer release metadata and all 46 packaged files
  against the signed commit's Git blobs. Verified archive and image signatures
  with exact repository/workflow/source/signer/ref and GitHub-hosted-runner policy.
- Independently fetched and checked the content-addressed OCI graph: SLSA v1,
  six immutable materials, source commit match and SPDX 2.3 with 648 packages.
  Structural inventory verification is not a vulnerability-free certification.
- The exact published image passed packaged app/schema/trust-store checks and
  real App token mint/use/revocation. Its private temporary key volume was removed;
  no database/crawl work was performed by this authentication probe.
- Exact-digest memory rehearsal: 50,000 active repositories, 1,455,000 initial
  memberships, 200 synthetic searches, 20,000 refreshed repos and 5,000 orphan
  deletions. Peak 401.9 MiB under a shared 768 MiB/no-swap limit; no OOMs/restarts.
  All 86 concurrent discovery requests passed (p95 0.373 s; max 0.523 s).
- Actual populated baseline→head→baseline→head migration preserves exact data
  and finishes without drift. This adds an index; it does not rewrite the baseline.
- GitHub reports zero open Dependabot alerts and zero open secret-scanning
  alerts at acceptance. Monitoring remains disabled until operator setup.
  v0.1.0's notes warn against deploying its old CA bundles/ARM64-only image;
  original tags/images/assets are preserved. Routine Actions PRs remain separate
  maintenance, not identified first-deployment blockers.

At this release-publication milestone, operator-owned OCI/private configuration
and live acceptance still remained. The later deployment evidence above records
what has since been verified; repository publication itself configured no VM.

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

At this historical v0.1.0 milestone, live OCI installation, DNS/TLS, SMTP,
production crawls and off-site recovery remained unchecked. Later v0.1.1 live
acceptance is recorded above; this ARM64 release is not the deployed version.

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
