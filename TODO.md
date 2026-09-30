# TODO

## Before release

- Define a retention and cleanup policy for failed, abandoned, and superseded
  catalog generations, so staging snapshots do not accumulate indefinitely.

## Deployment — Oracle Cloud Always Free

### Infrastructure and network

- Create the OCI tenancy, choose its home region carefully, and provision an
  Always Free Arm VM with persistent block storage. Record the recovery procedure
  because OCI may reclaim an idle Always Free instance.
- Configure the VM's VCN, security list/network security group, and host firewall
  to expose only HTTP/HTTPS (ports 80/443). Do not expose PostgreSQL (5432),
  pgAdmin (5050), or public SSH (22).
- Add Caddy as the public reverse proxy and obtain a hostname. OCI supplies an IP
  address and DNS hosting, not a free registered domain; begin with a free
  subdomain or acquire a domain before the final public launch. Use HTTPS via
  Let's Encrypt.
- Configure OCI Bastion for time-limited administrator access. Use separate,
  passphrase-protected SSH keys; disable password and root login on the VM; limit
  Bastion client CIDRs where practical; and enable MFA on the OCI account.
- Configure encrypted, tested PostgreSQL backups to OCI Object Storage and define
  restoration steps. Never include the database volume in routine application
  container replacement.

### Production containers and secrets

- Add a production Dockerfile that builds an ARM64 image (or a multi-architecture
  image) for both the FastAPI server and crawler commands.
- Add a production Compose definition: Caddy, FastAPI, PostgreSQL, a one-off
  Alembic migration command, and a profile/one-shot crawler command. Keep the
  database on Docker's private network and omit pgAdmin from the default stack.
- Store production configuration in root-owned files outside the repository. Keep
  the PostgreSQL password, database URL, and GitHub App private key out of images,
  Git history, and CI logs. Mount the private key read-only only for the crawler.
- Make pgAdmin an on-demand administrative container bound to `127.0.0.1` on the
  VM. Access it only through an OCI Bastion SSH tunnel, then stop it after use.

### Release and deployment pipeline

- Add PR and `main` CI workflows for formatting, Ruff, Pyright, and the
  PostgreSQL-backed test suite. Protect `main` with required CI and pull requests
  before relying on production automation.
- Adopt signed semantic-version tags and GitHub Releases, beginning with
  `v0.1.0`. A release must build and publish an immutable ARM64 image to GHCR;
  deploy the image digest, not a mutable `latest` tag.
- Add a GitHub `production` environment and deployment workflow. It should record
  deployment status and URL, require the intended approval rules, and deploy via
  a short-lived OCI Bastion connection or another pull-based mechanism—not an
  internet-exposed SSH service.
- Have the deploy script back up PostgreSQL, pull the exact image digest, run
  `alembic upgrade head` exactly once, update the web container, smoke-test `/`,
  and report success or failure. Define rollback as redeploying the prior image;
  do not automatically downgrade database migrations.
- Pin the crawler to the same deployed image digest as the web application. The
  scheduled crawler must not implicitly run unreleased code from `main`.

### Crawling, monitoring, and operations

- Install a persistent systemd timer that starts one normal full crawl every six
  hours (four runs per day, so each successful sort ordering completes once per
  day). It should run `python -m app.cli crawl`, not `--resume`, and must prevent
  overlapping operating-system invocations.
- Preserve the existing behavior that a failed run leaves the active catalog
  untouched and does not block the following six-hourly normal crawl. An operator
  may explicitly run `--resume` between scheduled runs when appropriate.
- Configure alerts for crawler and deployment failures. Alert from a systemd
  failure handler and/or OCI Monitoring after a non-zero crawl exit, include the
  relevant journal log and `crawl_runs` identifier, and send to an email or webhook
  destination. Avoid alert loops and test one intentional failure before launch.
- Send service logs to journald with rotation, document `journalctl` and the local
  `python -m app.cli status` operator commands, and monitor database disk use,
  backup failures, and the three-day rolling-catalog retention behavior.
