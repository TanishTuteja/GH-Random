# Contributing to GH Random

Bug reports, focused fixes, documentation and feature proposals are welcome.
Follow our [code of conduct](CODE_OF_CONDUCT.md). Vulnerabilities belong in the
private channel in [SECURITY.md](SECURITY.md), not a public issue or PR.

## Development and review

Search existing issues first. Include a release/commit and a minimal reproduction
with synthetic data, never raw `.env` files, credentials or unreviewed logs.
Discuss major product/schema/operations changes before building them.
`ROADMAP.md` tracks product ideas; `TODO.md` lists unfinished operations and
observation windows; `decisions.md` records current design policies.

Use GitHub Flow: fork if needed, branch from current `main`, make focused commits
with informative bodies, and open a PR. There is no permanent `dev` branch.
Signing is encouraged; maintainer commits and release tags are signed. Do not
push directly to `main`, rewrite production migration history or force-push
protected refs. UI changes require the maintainer's manual verification.

Follow [README setup](README.md#first-time-local-setup): Python 3.12, pinned uv,
`uv sync --locked --extra dev`, PostgreSQL and the documented age test tool.
Use a dedicated **disposable test database** in `TEST_DATABASE_URL`: test fixtures
drop and recreate its tables. Never use production or data you need to retain.
Unit/CI tests require no real GitHub credentials. Keep secrets outside the repo.
After activating the development environment, run:

```sh
python -m ruff format --check app tests alembic .github/scripts deployment
python -m ruff check app tests alembic .github/scripts deployment
python -m pyright
python -m pytest
```

Add regression tests, explain the problem/solution and operational impact, and
keep unrelated cleanup separate. Do not weaken tests to make CI pass. PRs require
an up-to-date branch and green `Python checks`, `PostgreSQL tests` and `Production
containers` checks. The last job exercises native AMD64 with disposable services,
not live GitHub or production secrets. A merge is not a deployment.

Fork PRs must not need secrets/elevated permissions. Never run untrusted PR code
through privileged `pull_request_target` events. Pin third-party Actions to full
reviewed SHAs. Review workflow, SQL, migration and backup changes carefully.

## Dependencies and releases

Use focused `uv lock --upgrade-package PACKAGE` updates, review the lockfile diff,
then sync and test. Dependabot proposes weekly grouped minor/patch updates;
review security fixes promptly, not just on that schedule. There is no auto-merge.
Keep Dockerfile, Compose defaults, env examples and CI service pins synchronized
as applicable. Interpolated defaults may need manual updates; a bot PR does not
prove every copy was updated. Major PostgreSQL upgrades need a recovery plan.

Follow README's signed-tag flow after protected `main` CI passes. Active `v*`
tag rules block updates/deletion, including by the owner; publish a new version,
not a moved tag. The release workflow verifies source/signature/CI, tests the
published digest and attaches matching files. Never deploy incomplete assets.

GitHub's `production` environment restricts jobs to protected branches and
requires the owner's review. Self-review is allowed for this solo-maintainer
project; this gate is not a second-person audit. VM credentials and live
acceptance remain separate. Follow the [operations](deployment/OPERATIONS.md)
and [recovery](deployment/BACKUPS.md) guides; never prune volumes or downgrade
schema as incidental maintenance. Contributions use the [MIT license](LICENSE);
there is no separate CLA.
