# Releases

Publishing creates immutable signed release assets; deployment is a separate
approved action. The [operations record](../operations/OPERATIONS.md#deployed-release) identifies
the deployed release, source commit and exact image digest.

New releases target the AMD64 OCI host. Historical v0.1.0 is ARM64-only and
predates the CA trust fix; preserve its assets but do not deploy it on this host.

## Creating a release

Merge the version-bump PR, with `project.version`/`uv.lock` aligned, then wait for
all three **main-push** checks on that exact merge commit. A green PR alone does
not qualify a release source.

The commands below illustrate the next unused version. **Replace every
`v0.1.2` if another version is intended**, and first verify that the chosen tag
does not already exist. Start with a clean, up-to-date checkout:

~~~powershell
git switch main
git pull --ff-only
git status --short
git tag -s v0.1.2 -m "GH Random v0.1.2"
git verify-tag v0.1.2
git push origin v0.1.2
~~~

Use the configured SSH signing key, registered on GitHub. Local verification
also needs its allowed-signers trust file. Publish a GitHub Release using the
**existing signed tag**, with release notes and prerelease disabled.

The release workflow verifies stable version/source/signature/main ancestry and
exact-source main CI, builds AMD64 from the pinned Git source, pushes to GHCR,
then tests the published digest. It checks actual provenance/SBOM content and
signs image/archive attestations; see [SUPPLY_CHAIN.md](SUPPLY_CHAIN.md).

Wait for a green release workflow and all four assets:

- `image-digest.txt`
- `release.json`
- `SHA256SUMS`
- `gh-random-VERSION-deployment.tar.gz`, where `VERSION` is the signed tag

A visible Release or GHCR tag alone is insufficient. Never move a published tag,
republish its image or overwrite completed assets; fixes use a new version.
Public GHCR permits credential-free VM pulls. Verify package visibility and an
anonymous full-digest pull when publishing or changing package ownership.
Publishing does not deploy.

## Verifying a published release

Use an official maintained GitHub CLI **2.97.0+**. Download the four assets into
a fresh version-specific directory. Independently verify the signed tag's full
commit SHA; do not trust only the archive's own metadata.

The following Linux/operator-shell example is specifically for immutable
v0.1.1. For another release, **replace the version, verified commit, image digest
and archive filename throughout**. Authenticate the CLI privately; the VM's
automated puller uses a short-lived App token.

~~~sh
set -eu
sha256sum --check SHA256SUMS
GH_RANDOM_VERIFIED_COMMIT=94b9c94ff0232b3f3d8f0e8d5d7c25b902af548b
GH_RANDOM_RELEASE_IMAGE=ghcr.io/tanishtuteja/gh-random@sha256:c5a6732f67d445be36eaaa00ded7706415982b13a5f3ad10b68eea7ca991e697
for target in gh-random-v0.1.1-deployment.tar.gz "oci://$GH_RANDOM_RELEASE_IMAGE"; do
  gh attestation verify "$target" --repo TanishTuteja/GH-Random \
    --signer-workflow TanishTuteja/GH-Random/.github/workflows/release.yaml \
    --source-ref refs/tags/v0.1.1 --source-digest "$GH_RANDOM_VERIFIED_COMMIT" \
    --signer-digest "$GH_RANDOM_VERIFIED_COMMIT" --deny-self-hosted-runners
done
tar -xzf gh-random-v0.1.1-deployment.tar.gz
~~~

Stop if any verification fails. Checksums detect corruption, not authorship.
Use only the matching extracted files; preserve their hashes and signed metadata.
