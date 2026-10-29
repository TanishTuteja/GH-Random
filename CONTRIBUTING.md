# Contributing to GH Random

Bug reports, focused fixes, documentation and feature proposals are welcome.
Search existing issues first and include a minimal reproduction with synthetic
data. Discuss major product, schema or operational changes before building them.

Follow the [code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities through
[SECURITY.md](SECURITY.md), keeping credentials, private configuration and raw
production logs out of public issues and pull requests.

## Development and review

Start with [local setup](README.md#first-time-local-setup), then use the
[development guide](docs/development/DEVELOPMENT.md) for dependencies, migrations,
local pgAdmin and [guarded verification](docs/development/DEVELOPMENT.md#verification).
Tests drop/recreate tables: use a separate disposable database, never data you
need to keep. Unit/CI tests need no real GitHub credentials.

Use GitHub Flow: branch from current `main`, make focused commits with descriptive
bodies and open a pull request. There is no permanent `dev` branch. Signing is
encouraged; maintainer commits and release tags are signed. Do not bypass branch
protection, force-push protected refs or rewrite published migrations.

An up-to-date PR must pass `Python checks`, `PostgreSQL tests` and `Production
containers`. UI changes need manual maintainer verification before commit/push
or completion. Keep unrelated changes separate and follow [AGENTS.md](AGENTS.md).

## Releases and project context

Publishing and deploying are separate actions; follow the
[signed-release guide](docs/releases/RELEASES.md) and
[deployment guide](docs/deployment/DEPLOYMENT.md). Contributions use the
[MIT license](LICENSE); there is no separate CLA.

See the [documentation index](docs/README.md) for project plans, current decisions
and operator runbooks.
