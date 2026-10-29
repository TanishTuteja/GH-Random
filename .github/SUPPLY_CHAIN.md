# Image content and build-metadata verification

The release workflow verifies the exact published **index digest**, not a mutable
version tag. Its BuildKit check is independent of GitHub's signed artifact
attestation verification: content consistency is not publisher authentication.
Both checks must pass before publishing usable deployment assets.

Install the reviewed ORAS 1.3.4 binary into an isolated tool directory, then run:

```sh
python .github/scripts/install_oras.py --destination "$RUNNER_TEMP/oras-tools"
python .github/scripts/verify_image_supply_chain.py \
  --oras "$RUNNER_TEMP/oras-tools/oras" \
  --image 'ghcr.io/tanishtuteja/gh-random@sha256:REPLACE_WITH_RELEASE_INDEX_DIGEST' \
  --repository TanishTuteja/GH-Random \
  --commit REPLACE_WITH_VERIFIED_40_CHARACTER_COMMIT \
  --tag REPLACE_WITH_VERIFIED_RELEASE_TAG \
  --output "$RUNNER_TEMP/image-supply-chain.json"
```

This check uses Python's standard library and ORAS, not a running application,
database or Docker daemon. The installer supports Linux AMD64/ARM64 and Windows
AMD64, verifies pinned official archive SHA-256 values, extracts only `oras` and
`LICENSE`, and refuses to overwrite different tools. On Windows use `oras.exe`.
Registry access uses ORAS's normal Docker configuration/credential-helper
support. Do not extract credentials or pass tokens on the command line. Existing
`docker/login-action` authentication works in release CI; a public image needs
no operator registry credential after anonymous pulls have been verified.

## Enforced graph and source policy

The checker fetches raw manifests/config/statement blobs by immutable digest and
checks every inspected descriptor's size and SHA-256. It verifies:

- An OCI index with exactly one `linux/amd64` runnable image and one linked
  BuildKit attestation manifest. Multi-platform releases need an explicit policy
  and verifier change; they cannot silently pass this single-platform check.
- Image config architecture/OS and exact repository/source/revision/version/MIT
  labels. These are consistency checks, not proof of authorship.
- Attestation descriptor linkage to the image manifest and exactly one SPDX
  predicate plus one supported SLSA predicate, with matching type annotations.
- Full in-toto statement subjects for legacy attestations, or the validated
  enclosing OCI-artifact manifest's image subject for current BuildKit storage.
  Current BuildKit intentionally emits empty statement subject arrays in the
  latter mode. Any nonempty subjects must still match the image manifest digest.
- BuildKit SLSA v0.2 or v1 maximum provenance, including complete immutable
  materials, the actual Git config source and material commit, `Dockerfile`,
  a recorded build definition, and no local/untracked input context.
- Actual SPDX 2.2/2.3 document metadata and a nonempty, named, uniquely identified
  package inventory. This is structural checking, not exhaustive SPDX schema
  certification, vulnerability scanning, license approval or an inventory audit.

Build the release from the **remote Git context pinned to the verified commit**,
not `context: .`. BuildKit documents that local context's `vcs:source` and
`vcs:revision` fields are client-supplied hints; this checker does not accept
them instead of actual config-source/material digests. Normal package downloads
inside Docker `RUN` commands are not hermetic just because Git/base-image inputs
are immutable. The checker does not claim reproducibility or a SLSA level.

CLI output is bounded and temporary; remote payloads and registry errors are not
echoed. The optional JSON result contains only identifiers, digests and inventory
counts, is created only after all checks pass, and never overwrites an existing
file. A failure returns nonzero and must stop asset publication/deployment.
Future BuildKit/schema changes fail closed until reviewed and tested.

## Verification evidence and limits

New releases target the provisioned x86-64 OCI E2.1.Micro VM. Their metadata,
native production-container CI, image build, graph verification and pull worker
all require `linux/amd64`. This is an explicit target change, not a multi-platform
release policy. The already published `v0.1.0` remains ARM64-only: its signed tag,
image digest and deployment assets must not be changed or used on this VM.
Use its matching source verifier to inspect that historical release; the current
verifier deliberately rejects ARM64 images. Use a verified signed AMD64 release
and matching bundle for deployment; the current production snapshot is in the
[operations record](../deployment/OPERATIONS.md#deployed-release).

Tests construct content-addressed fixture graphs, including tampered descriptor
contents, wrong image/platform/source/subjects, missing inventory/materials,
untracked contexts and forged VCS hints. Both legacy and current artifact modes
are covered without credentials, registry traffic or a database.

Fixture tests do not replace verification of each published application's real
digest, signed GitHub attestations, release assets and anonymous package access.

Primary schema/tool references:

- [BuildKit attestation storage](https://github.com/moby/buildkit/blob/master/docs/attestations/attestation-storage.md)
- [BuildKit SLSA field definitions](https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md)
- [Docker provenance attestations](https://docs.docker.com/build/metadata/attestations/slsa-provenance/)
- [ORAS authentication](https://oras.land/docs/how_to_guides/authentication/)
- [ORAS manifest fetch](https://oras.land/docs/commands/oras_manifest_fetch/)
- [ORAS blob fetch](https://oras.land/docs/commands/oras_blob_fetch/)
- [Official ORAS 1.3.4 release](https://github.com/oras-project/oras/releases/tag/v1.3.4)
