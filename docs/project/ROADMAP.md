# Roadmap

Future product enhancements. Remaining operational work is in [TODO.md](TODO.md).

## Catalog lifecycle

- Review retention using production crawl data. Evaluate whether repository
  retirement should depend on elapsed time, successful-crawl counts, or both.

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
