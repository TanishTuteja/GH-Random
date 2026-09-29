# TODO

## Before release

- Refine the end-user empty-catalog and discovery-page copy. Do not expose
  operational instructions such as “Run a full crawl,” and remove the
  implementation-focused explanation about a paced crawler and live APIs.
  Keep any recovery or crawl guidance in the operator documentation instead.
- When the schema is stable and before the first production deployment, decide
  whether to squash the development-only Alembic history into one baseline
  migration. Rebuild or explicitly stamp the development database as part of
  that controlled transition; do not rewrite migration history after deployment.
