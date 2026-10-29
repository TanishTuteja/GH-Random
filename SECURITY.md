# Security policy

## Report privately

Use [Report a vulnerability](https://github.com/TanishTuteja/GH-Random/security/advisories/new).
Private vulnerability reporting was enabled and verified on 2026-10-18; it
shares reports with maintainers instead of publishing an issue. If the form is
unavailable, ask for a private contact in a general question **without details**,
or contact GitHub Support for an account/platform issue. Never disclose exploits,
passwords, keys, tokens, database URLs or personal data in public issues/PRs/logs.

Include the affected release/commit, impact, prerequisites and a minimal
reproduction using synthetic data. Redact secrets. Test only systems you own or
have permission to test; do not disrupt a public service or access others' data.
There is no paid bug bounty or authorization beyond your own test environment.

This is a volunteer project. We aim to acknowledge reports within seven days,
without a guaranteed response/fix deadline. Please allow investigation and
coordinated disclosure instead of publishing a working exploit during assessment.

## Supported versions and updates

Only the latest stable release receives security fixes; older versions should
upgrade. A release is not a security certification. Operators must also patch
hosts/Docker/reviewed service images, protect secrets and recovery keys, and
maintain working alerts/tested recovery using the operations runbooks.

Maintainers should assess private reports and Dependabot alerts promptly, test
fixes through protected PRs, publish a new signed version and issue an advisory
where appropriate. Never move a published tag or dismiss an alert without
assessing applicability. Revoke/rotate exposed credentials immediately; deleting
a secret from Git history does not make it safe again.
