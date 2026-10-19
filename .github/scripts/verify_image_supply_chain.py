"""Check the exact ARM64 OCI image and its BuildKit SBOM/provenance graph.

This checks content and source consistency, NOT publisher authenticity. The release
workflow separately verifies GitHub's signed artifact attestation. ORAS uses the
operator's ordinary Docker registry configuration/credential helpers; this program
never opens that configuration, extracts a token, or prints remote payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
INTOTO = "application/vnd.in-toto+json"
SPDX = "https://spdx.dev/Document"
SLSA = "https://slsa.dev/provenance/v0.2"
SLSA_V1 = "https://slsa.dev/provenance/v1"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)
LIMIT = 80 * 1024 * 1024
Fetch = Callable[[str, str], bytes]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON property")
        result[key] = value
    return result


def document(data: bytes) -> dict[str, Any]:
    require(0 < len(data) <= LIMIT, "Invalid metadata size")
    value = json.loads(data, object_pairs_hook=no_duplicates)
    require(isinstance(value, dict), "Expected a JSON object")
    return value


def command_bytes(arguments: list[str]) -> bytes:
    # Private temporary output bounds memory, is removed on every exit, and is
    # monitored while the child runs. Do not echo stderr: auth helpers may log.
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(arguments, stdout=output, stderr=subprocess.DEVNULL) as child:
            deadline = time.monotonic() + 120
            while child.poll() is None:
                if output.tell() > LIMIT or time.monotonic() >= deadline:
                    child.kill()
                    child.wait(timeout=10)
                    raise RuntimeError("Registry CLI exceeded its output/time limit")
                time.sleep(0.05)
            require(child.returncode == 0, "Registry CLI operation failed")
        require(output.tell() <= LIMIT, "Registry CLI output too large")
        output.seek(0)
        return output.read(LIMIT + 1)


def registry_fetch(oras: str, name: str) -> Fetch:
    def fetch(kind: str, digest: str) -> bytes:
        require(kind in {"manifest", "blob"}, "Unsupported registry operation")
        require(DIGEST.fullmatch(digest) is not None, "Invalid registry digest")
        return command_bytes([oras, kind, "fetch", "--output", "-", f"{name}@{digest}"])

    return fetch


def descriptor(value: Any, media_type: str | None = None) -> dict[str, Any]:
    require(isinstance(value, dict), "Missing OCI descriptor")
    require(DIGEST.fullmatch(value.get("digest", "")) is not None, "Invalid OCI digest")
    size = value.get("size")
    require(type(size) is int and 0 < size <= LIMIT, "Invalid OCI descriptor size")
    if media_type is not None:
        require(value.get("mediaType") == media_type, "Unexpected OCI media type")
    return value


def checked(fetch: Fetch, kind: str, desc: dict[str, Any]) -> dict[str, Any]:
    data = fetch(kind, desc["digest"])
    require(len(data) == desc["size"], "OCI content size mismatch")
    require("sha256:" + hashlib.sha256(data).hexdigest() == desc["digest"], "OCI hash mismatch")
    return document(data)


def source_uri(uri: Any, repository: str) -> bool:
    if not isinstance(uri, str):
        return False
    parsed = urlsplit(uri.removeprefix("git+"))
    return (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and parsed.path.removesuffix(".git").lower() == "/" + repository.lower()
        and not parsed.query
    )


def provenance(
    predicate: dict[str, Any], repository: str, commit: str, kind: str
) -> dict[str, Any]:
    if kind == SLSA_V1:
        definition = predicate.get("buildDefinition", {})
        require(
            definition.get("buildType")
            == "https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md",
            "Not BuildKit",
        )
        external = definition.get("externalParameters", {})
        invocation = {
            "configSource": external.get("configSource", {}),
            "parameters": external.get("request", {}),
        }
        source = invocation["configSource"]
        require(source.get("path") == "Dockerfile", "Unexpected build Dockerfile")
        metadata = predicate.get("runDetails", {}).get("metadata", {})
        complete = metadata.get("buildkit_completeness", {})
        completeness = {
            "materials": complete.get("resolvedDependencies"),
            "parameters": complete.get("request"),
        }
        build_config = definition.get("internalParameters", {}).get("buildConfig", {})
        materials = definition.get("resolvedDependencies")
    else:
        require(predicate.get("buildType") == "https://mobyproject.org/buildkit@v1", "Not BuildKit")
        invocation = predicate.get("invocation", {})
        source = invocation.get("configSource", {})
        require(source.get("entryPoint") == "Dockerfile", "Unexpected build Dockerfile")
        completeness = predicate.get("metadata", {}).get("completeness", {})
        build_config = predicate.get("buildConfig", {})
        materials = predicate.get("materials")
    require(source_uri(source.get("uri"), repository), "Missing actual Git build source")
    require(source.get("digest", {}).get("sha1") == commit, "Git build source commit mismatch")
    require(invocation.get("parameters", {}).get("locals", []) == [], "Untracked local inputs")
    require(completeness.get("materials") is True, "Incomplete build materials")
    require(completeness.get("parameters") is True, "Not maximum provenance")
    steps = build_config.get("llbDefinition")
    require(isinstance(steps, list) and bool(steps), "Missing build")
    require(isinstance(materials, list) and bool(materials), "Missing build materials")
    require(
        all(
            isinstance(m, dict)
            and isinstance(m.get("uri"), str)
            and bool(m["uri"])
            and isinstance(m.get("digest"), dict)
            and bool(m["digest"])
            and all(
                isinstance(value, str)
                and (
                    (algorithm == "sha1" and SHA.fullmatch(value))
                    or (algorithm == "sha256" and DIGEST.fullmatch("sha256:" + value))
                )
                for algorithm, value in m["digest"].items()
            )
            for m in materials
        ),
        "Malformed or non-immutable build material",
    )
    git_materials = [m for m in materials if source_uri(m.get("uri"), repository)]
    require(bool(git_materials), "Git source missing from materials")
    require(
        all(m.get("digest", {}).get("sha1") == commit for m in git_materials),
        "Source material commit mismatch",
    )
    docker_materials = [m for m in materials if m.get("uri", "").startswith("pkg:docker/")]
    require(bool(docker_materials), "Missing immutable container build materials")
    require(
        all(
            DIGEST.fullmatch("sha256:" + m.get("digest", {}).get("sha256", ""))
            for m in docker_materials
        ),
        "Unpinned container material",
    )
    return {"predicate_type": kind, "source_commit": commit, "materials": len(materials)}


def spdx(predicate: dict[str, Any]) -> dict[str, Any]:
    require(predicate.get("spdxVersion") in {"SPDX-2.2", "SPDX-2.3"}, "Unsupported SPDX version")
    require(predicate.get("SPDXID") == "SPDXRef-DOCUMENT", "Missing SPDX document identifier")
    require(predicate.get("dataLicense") == "CC0-1.0", "Unexpected SPDX data license")
    namespace = predicate.get("documentNamespace", "")
    require(
        isinstance(namespace, str) and namespace.startswith("https://"), "Missing SPDX namespace"
    )
    creation = predicate.get("creationInfo", {})
    creators = creation.get("creators")
    require(
        isinstance(creators, list)
        and bool(creators)
        and all(
            isinstance(c, str) and c.startswith(("Tool: ", "Person: ", "Organization: "))
            for c in creators
        ),
        "Missing SPDX creation metadata",
    )
    timestamp = datetime.fromisoformat(creation.get("created", "").replace("Z", "+00:00"))
    require(timestamp.tzinfo is not None, "Missing SPDX creation timezone")
    packages = predicate.get("packages")
    require(isinstance(packages, list) and bool(packages), "SPDX has no package inventory")
    require(
        all(
            isinstance(p, dict)
            and isinstance(p.get("name"), str)
            and bool(p["name"])
            and isinstance(p.get("SPDXID"), str)
            and p["SPDXID"].startswith("SPDXRef-")
            for p in packages
        ),
        "Malformed SPDX package inventory",
    )
    require(len({p["SPDXID"] for p in packages}) == len(packages), "Duplicate SPDX package IDs")
    return {"version": predicate["spdxVersion"], "packages": len(packages)}


def verify(image: str, repository: str, commit: str, tag: str, fetch: Fetch) -> dict[str, Any]:
    require(REPOSITORY.fullmatch(repository) is not None, "Invalid repository")
    require(SHA.fullmatch(commit) is not None, "Invalid commit")
    require(TAG.fullmatch(tag) is not None, "Invalid stable version")
    name, separator, digest = image.partition("@")
    require(
        separator == "@"
        and name == "ghcr.io/" + repository.lower()
        and DIGEST.fullmatch(digest) is not None,
        "Use the exact repository's immutable GHCR image digest",
    )
    raw = fetch("manifest", digest)
    require("sha256:" + hashlib.sha256(raw).hexdigest() == digest, "OCI index digest mismatch")
    index = document(raw)
    require(
        index.get("schemaVersion") == 2 and index.get("mediaType") == INDEX, "Expected OCI index"
    )
    manifests = index.get("manifests")
    require(
        isinstance(manifests, list) and len(manifests) == 2,
        "Expected one image and one attestation",
    )
    descriptors = [descriptor(m, MANIFEST) for m in manifests]
    runnable = [m for m in descriptors if m.get("platform", {}).get("os") == "linux"]
    require(len(runnable) == 1, "Expected a single runnable platform")
    target = runnable[0]
    require(
        target.get("platform")
        in (
            {"os": "linux", "architecture": "arm64"},
            {"os": "linux", "architecture": "arm64", "variant": "v8"},
        ),
        "Expected linux/arm64 image",
    )
    image_manifest = checked(fetch, "manifest", target)
    require(
        image_manifest.get("schemaVersion") == 2 and image_manifest.get("mediaType") == MANIFEST,
        "Invalid image manifest",
    )
    config = checked(fetch, "blob", descriptor(image_manifest.get("config"), CONFIG))
    require(
        config.get("os") == "linux" and config.get("architecture") == "arm64",
        "Image config platform mismatch",
    )
    labels = config.get("config", {}).get("Labels", {})
    expected = {
        "org.opencontainers.image.source": "https://github.com/" + repository,
        "org.opencontainers.image.revision": commit,
        "org.opencontainers.image.version": tag,
        "org.opencontainers.image.licenses": "MIT",
    }
    require(all(labels.get(k) == v for k, v in expected.items()), "Image label mismatch")
    attest = next(m for m in descriptors if m is not target)
    annotations = attest.get("annotations", {})
    require(
        annotations.get("vnd.docker.reference.type") == "attestation-manifest",
        "Missing attestation descriptor",
    )
    require(
        annotations.get("vnd.docker.reference.digest") == target["digest"],
        "Attestation descriptor target mismatch",
    )
    require(
        attest.get("platform") == {"os": "unknown", "architecture": "unknown"},
        "Unexpected attestation platform",
    )
    attestation = checked(fetch, "manifest", attest)
    require(
        attestation.get("schemaVersion") == 2 and attestation.get("mediaType") == MANIFEST,
        "Invalid attestation manifest",
    )
    # Modern OCI-artifact storage intentionally emits empty statement subjects:
    # the hashed enclosing manifest's subject provides their image binding.
    artifact_bound = False
    if "artifactType" in attestation:
        require("subject" in attestation, "Missing OCI artifact subject")
    if "subject" in attestation:
        require(
            attestation.get("artifactType")
            == "application/vnd.docker.attestation.manifest.v1+json",
            "Unexpected attestation artifact type",
        )
        subject = descriptor(attestation["subject"], MANIFEST)
        require(
            all(subject[key] == target[key] for key in ("digest", "size", "mediaType")),
            "OCI artifact subject mismatch",
        )
        artifact_bound = True
    layers = attestation.get("layers")
    require(isinstance(layers, list) and len(layers) == 2, "Expected exactly provenance and SBOM")
    results: dict[str, Any] = {}
    for layer in layers:
        layer = descriptor(layer, INTOTO)
        statement = checked(fetch, "blob", layer)
        require(
            statement.get("_type")
            in {"https://in-toto.io/Statement/v0.1", "https://in-toto.io/Statement/v1"},
            "Invalid in-toto statement",
        )
        subjects = statement.get("subject")
        require(isinstance(subjects, list), "Missing in-toto subjects")
        require(
            bool(subjects) or artifact_bound, "Missing in-toto subjects and OCI artifact subject"
        )
        require(
            all(s.get("digest") == {"sha256": target["digest"].split(":")[1]} for s in subjects),
            "In-toto subject digest mismatch",
        )
        kind = statement.get("predicateType")
        require(
            kind == layer.get("annotations", {}).get("in-toto.io/predicate-type"),
            "Predicate annotation mismatch",
        )
        key = "sbom" if kind == SPDX else "provenance"
        require(
            kind in {SPDX, SLSA, SLSA_V1} and key not in results, "Unexpected/duplicate predicate"
        )
        predicate = statement.get("predicate")
        require(isinstance(predicate, dict), "Missing predicate")
        results[key] = (
            spdx(predicate) if kind == SPDX else provenance(predicate, repository, commit, kind)
        ) | {"statement_digest": layer["digest"]}
    require(set(results) == {"sbom", "provenance"}, "Missing provenance or SBOM")
    return {
        "format": 1,
        "image": image,
        "repository": repository,
        "commit": commit,
        "tag": tag,
        "platform": "linux/arm64",
        "image_manifest_digest": target["digest"],
        "attestation_manifest_digest": attest["digest"],
        "provenance": results["provenance"],
        "sbom": results["sbom"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--oras", default="oras")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(
            args.image,
            args.repository,
            args.commit,
            args.tag,
            registry_fetch(args.oras, args.image.partition("@")[0]),
        )
        if args.output is not None:
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(result, output, indent=2, sort_keys=True)
                output.write("\n")
        print("Exact ARM64 image, BuildKit Git provenance and SPDX inventory verified.")
        return 0
    except Exception as error:
        print(
            f"Image supply-chain verification failed ({type(error).__name__}); "
            "no remote payload printed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
