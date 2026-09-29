# Roadmap

This file tracks product enhancements and future improvements that are not part
of the current implementation.

## Catalog lifecycle

- Replace the current replacement-snapshot publication model with a rolling
  catalog. Retain repositories that a successful crawl does not rediscover for
  an explicit retention window, then retire them from the active catalog. Define
  that window before implementation (for example, a number of successful crawls,
  elapsed time, or both), and retain `last_seen` information needed to apply it.

## Crawl coverage

- Evaluate expanding the GitHub repository-search sort rotation. The current
  cycle uses `updated` and `stars` in both ascending and descending order.
  GitHub also supports `forks` and `help-wanted-issues`, each with ascending or
  descending order. Decide which of those four additional orderings improve
  discovery enough to justify their additional crawl time. GitHub's default
  best-match ranking is also available only by omitting `sort`; it has no
  independently configurable `order` and should be evaluated separately.
