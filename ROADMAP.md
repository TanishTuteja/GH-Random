# Roadmap

This file tracks product enhancements and future improvements that are not part
of the current implementation.

## Catalog lifecycle

- Revisit the current rolling-catalog policy. A new staging generation copies
  active memberships, refreshes each repository rediscovered by the crawl, and
  retires memberships whose `last_seen` time is more than 24 hours old when the
  crawl publishes. Decide whether 24 hours is appropriate or whether retention
  should instead be configured in successful-crawl counts, elapsed time, or both.

## Crawl coverage

- Evaluate expanding the GitHub repository-search sort rotation. The current
  cycle uses `updated` and `stars` in both ascending and descending order.
  GitHub also supports `forks` and `help-wanted-issues`, each with ascending or
  descending order. Decide which of those four additional orderings improve
  discovery enough to justify their additional crawl time. GitHub's default
  best-match ranking is also available only by omitting `sort`; it has no
  independently configurable `order` and should be evaluated separately.

## Open-source repository operations

- Add GitHub Actions continuous integration for pull requests and `main`:
  formatting verification, Ruff linting, Pyright, and the PostgreSQL-backed test
  suite. Make the resulting CI check a required merge status check.
- Configure a GitHub ruleset or branch-protection rule for `main` (this
  repository's default branch): require pull requests, required CI, and resolved
  review conversations; prevent direct pushes, force pushes, and deletion. Decide
  whether to require an approving review and whether administrators may bypass
  the rules before enabling it.
- Add Dependabot version updates for Python dependencies and GitHub Actions, and
  enable GitHub's dependency alerts and security updates. Review update cadence
  and pull-request limits to keep maintenance work manageable.
- Add contributor-facing repository files: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue forms/templates, a pull-request template, and maintainers'
  labels. The repository already has a license; document its contribution and
  vulnerability-reporting expectations alongside it.
- Establish safe workflow defaults: least-privilege `GITHUB_TOKEN` permissions,
  pinned third-party action revisions, and no untrusted pull-request content in
  privileged workflow events.
