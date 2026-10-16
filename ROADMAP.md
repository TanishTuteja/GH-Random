# Roadmap

This file tracks product enhancements and future improvements that are not part
of the current implementation.

## Catalog lifecycle

- Review the current rolling-catalog policy after real production crawl data is
  available. A new staging generation copies active memberships, refreshes each
  repository rediscovered by the crawl, and retires memberships whose `last_seen`
  time exceeds the configured three-day retention window when the crawl publishes.
  Retired and failed snapshots older than seven days are then removed, along with
  their orphaned repository metadata.
  Revisit whether that duration should instead be based on successful-crawl counts,
  elapsed time, or both.

## Crawl coverage

- Evaluate expanding the GitHub repository-search sort rotation. The current
  cycle uses `updated` and `stars` in both ascending and descending order.
  GitHub also supports `forks` and `help-wanted-issues`, each with ascending or
  descending order. Decide which of those four additional orderings improve
  discovery enough to justify their additional crawl time. GitHub's default
  best-match ranking is also available only by omitting `sort`; it has no
  independently configurable `order` and should be evaluated separately.

## Discovery interface

- Support selecting multiple values within each repository filter. Combine
  selected values with OR within a filter category and AND across categories,
  while retaining `Any` as the no-restriction choice for that category.

## Open-source repository operations

CI, branch/tag protection, Dependabot, contributor files, and workflow security
are tracked with their completion status in [TODO.md](TODO.md). Keep that
deployment checklist as the single source of status for repository operations.
