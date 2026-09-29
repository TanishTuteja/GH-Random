# Roadmap

This file tracks product enhancements and future improvements that are not part
of the current implementation.

## Catalog lifecycle

- Replace the current replacement-snapshot publication model with a rolling
  catalog. Retain repositories that a successful crawl does not rediscover for
  an explicit retention window, then retire them from the active catalog. Define
  that window before implementation (for example, a number of successful crawls,
  elapsed time, or both), and retain `last_seen` information needed to apply it.
