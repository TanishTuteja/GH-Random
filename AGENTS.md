# Working on GH Random

## Branches and pull requests

- Use GitHub Flow for every repository change, including documentation and
  maintenance. Start a short-lived, descriptive feature branch from up-to-date
  `origin/main`; use a separate branch for each independent task. There is no
  permanent `dev` branch.
- Do not commit or push changes directly to `main`. Keep commits focused, sign
  them with the configured SSH signing key, and include a descriptive commit body.
- Push the feature branch and open a pull request targeting `main`. Run checks
  appropriate to the change and verify that all required CI jobs, `Python
  checks`, `PostgreSQL tests`, and `Production containers`, pass on the PR
  before considering it ready.
- Leave the PR ready for user review. Merge only when the user authorizes it;
  delete the feature branch after an authorized successful merge. Do not bypass
  failing checks or repository protection rules.
- For UI changes, obtain the user's manual verification before marking the
  associated TODO complete or committing/pushing the change.

## Documentation and releases

- Track implementation/deployment status in `TODO.md`, product enhancements in
  `ROADMAP.md`, and material design decisions or unresolved choices in
  `decisions.md`.
- Merging to `main` does not mean deploying. Production releases use signed
  version tags and the corresponding immutable application image digest.
