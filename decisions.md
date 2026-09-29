# Decisions

This file records decisions that materially shape the MVP, including choices made
autonomously while completing the project.

## 2026-09-29 — Single application repository

The FastAPI server and offline crawler share this repository, Python environment,
and database. A separate client repository or service would add deployment and
coordination overhead without helping the MVP.

## 2026-09-29 — Activity means GitHub `pushed_at`

The user clarified that the four activity buckets are based on GitHub's
`pushed_at`, not `updated_at`. The crawler includes a `pushed:` qualifier
in every GitHub search query and stores both GitHub timestamps. The web filter
uses `github_pushed_at` from the local database.

GitHub's search qualifier is date-granular, while local filtering uses the
returned timestamp. Bucket boundaries can therefore be approximate during
discovery but exact for results served by the application.

## 2026-09-29 — 200 queries per full crawl

Every full crawl performs the Cartesian product of 10 languages, 5 star buckets,
and 4 pushed-time buckets: 200 combinations. This avoids relying on the first
1,000 results of a broad language/star query to contain older repositories.

## 2026-09-29 — Five-second search pacing

`GITHUB_SEARCH_DELAY_SECONDS` remains the sole pacing setting and defaults to
5 seconds. The search client spaces search request starts by at least that
interval. Authentication-token requests are not crawl-search requests and occur
only when the cached token expires.

## 2026-09-29 — Four-cycle GitHub ordering

“Four sort orders” is interpreted as the product brief's four initial modes:
`updated desc`, `stars desc`, `updated asc`, and `stars asc`. One full
crawl uses one mode; the next successful crawl advances to the next mode. The
cycle is kept in configuration rather than selected by the HTTP client.

Open question: if “four sort orders” instead meant GitHub's four sort fields
(`stars`, `forks`, `help-wanted-issues`, `updated`), revise the cycle
before relying on long-running crawl coverage.

## 2026-09-29 — Active/staging generations

Canonical repository metadata is upserted across crawls. Membership is
generation-scoped: users query only the one ACTIVE generation while the crawler
builds a STAGING one. A staging generation is seeded from active membership,
repositories not seen in the preceding 24 hours are removed at successful
completion, and the status swap occurs in one transaction.

Metadata can be refreshed while a staging crawl is running because metadata is
canonical. Membership—the discovery pool exposed to users—never changes until
the atomic generation swap.

A failed request marks that crawl and its staging generation FAILED, leaving the
previous ACTIVE generation untouched. The next sort mode is selected only after
a successfully completed crawl, so a failed run does not consume a rotation
slot. The client makes no general request retries: it preserves failures for
inspection and requires a fresh, complete crawl.

The exception is an explicit GitHub rate-limit response. The client performs a
bounded retry using GitHub's Retry-After header when present, otherwise the
configured 60-second fallback. This pause supplements, rather than replaces,
the five-second minimum between ordinary request starts.

## 2026-09-29 — GitHub ID is the only canonical repository identity

The numeric GitHub repository ID is immutable, while an owner/name full name can
be reused after a transfer, rename, or replacement repository. The initial
schema incorrectly made full name unique; migration 20260929_03 removes that
constraint. A generation can briefly retain an old membership for the
24-hour stale-retention window even when a distinct repository has since taken
the same full name.

## 2026-09-29 — Local-only discovery requests

The web interface calls a FastAPI endpoint that selects a row using the
database's random-order function, constrained by the requested language, stars,
and exact local pushed-at bounds. It joins only the ACTIVE generation; it
does not import the GitHub client or make any network call during discovery.

Before the first successful full crawl, the endpoint returns a clear
not-found response instead of silently falling back to GitHub. This makes the
local-first guarantee visible and keeps user requests predictable.
