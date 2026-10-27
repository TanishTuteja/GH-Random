"""Content-addressed OCI fixtures: no registry, credentials, Docker or database."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = "Owner/Example"
COMMIT = "a" * 40
TAG = "v0.1.0"


def load(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / ".github/scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verifier() -> ModuleType:
    return load("verify_image_supply_chain")


def graph(
    module: ModuleType, changes: dict[str, Callable[[dict[str, Any]], None]] | None = None
) -> tuple[str, dict[str, bytes]]:
    changes = changes or {}
    blobs: dict[str, bytes] = {}

    def node(stage: str, value: dict[str, Any], media: str) -> dict[str, Any]:
        if stage in changes:
            changes[stage](value)
        raw = json.dumps(value, separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        blobs[digest] = raw
        return {"mediaType": media, "digest": digest, "size": len(raw)}

    config = node(
        "config",
        {
            "os": "linux",
            "architecture": "amd64",
            "config": {
                "Labels": {
                    "org.opencontainers.image.source": "https://github.com/" + REPO,
                    "org.opencontainers.image.revision": COMMIT,
                    "org.opencontainers.image.version": TAG,
                    "org.opencontainers.image.licenses": "MIT",
                }
            },
        },
        module.CONFIG,
    )
    image = node(
        "image",
        {"schemaVersion": 2, "mediaType": module.MANIFEST, "config": config, "layers": []},
        module.MANIFEST,
    )
    source = {
        "uri": "https://github.com/" + REPO + ".git#" + COMMIT,
        "digest": {"sha1": COMMIT},
        "entryPoint": "Dockerfile",
    }
    predicate = {
        "buildType": "https://mobyproject.org/buildkit@v1",
        "invocation": {"configSource": source, "parameters": {"locals": []}},
        "materials": [
            copy.deepcopy(source),
            {"uri": "pkg:docker/python@3.12", "digest": {"sha256": "b" * 64}},
        ],
        "metadata": {"completeness": {"parameters": True, "materials": True}},
        "buildConfig": {"llbDefinition": [{}]},
    }
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "dataLicense": "CC0-1.0",
        "documentNamespace": "https://example.test/spdx/unique",
        "creationInfo": {"creators": ["Tool: syft"], "created": "2026-10-18T00:00:00Z"},
        "packages": [{"SPDXID": "SPDXRef-Package", "name": "python", "versionInfo": "3.12"}],
    }
    layers = []
    for name, kind, value in (("provenance", module.SLSA, predicate), ("sbom", module.SPDX, sbom)):
        if name in changes:
            changes[name](value)
        statement = {
            "_type": "https://in-toto.io/Statement/v0.1",
            "subject": [{"name": "_", "digest": {"sha256": image["digest"][7:]}}],
            "predicateType": kind,
            "predicate": value,
        }
        layer = node(name + "_statement", statement, module.INTOTO)
        layer["annotations"] = {"in-toto.io/predicate-type": kind}
        layers.append(layer)
    attest = node(
        "attest",
        {
            "schemaVersion": 2,
            "mediaType": module.MANIFEST,
            "artifactType": "application/vnd.docker.attestation.manifest.v1+json",
            "subject": copy.deepcopy(image),
            "layers": layers,
        },
        module.MANIFEST,
    )
    attest["platform"] = {"os": "unknown", "architecture": "unknown"}
    attest["annotations"] = {
        "vnd.docker.reference.type": "attestation-manifest",
        "vnd.docker.reference.digest": image["digest"],
    }
    image["platform"] = {"os": "linux", "architecture": "amd64"}
    index = node(
        "index",
        {"schemaVersion": 2, "mediaType": module.INDEX, "manifests": [image, attest]},
        module.INDEX,
    )
    return "ghcr.io/owner/example@" + index["digest"], blobs


def verify(module: ModuleType, changes=None) -> dict[str, Any]:
    image, blobs = graph(module, changes)
    return module.verify(image, REPO, COMMIT, TAG, lambda kind, digest: blobs[digest])


def test_exact_oci_graph_source_subject_and_inventory_pass(verifier) -> None:
    result = verify(verifier)
    assert result["platform"] == "linux/amd64"
    assert result["provenance"]["source_commit"] == COMMIT
    assert result["provenance"]["materials"] == 2
    assert result["sbom"]["packages"] == 1
    assert result["sbom"]["version"] == "SPDX-2.3"


def test_consistent_arm64_graph_cannot_pass_the_amd64_release_policy(verifier) -> None:
    with pytest.raises(ValueError, match="linux/amd64"):
        verify(
            verifier,
            {
                "config": lambda d: d.update(architecture="arm64"),
                "index": lambda d: d["manifests"][0]["platform"].update(architecture="arm64"),
            },
        )


@pytest.mark.parametrize(
    ("stage", "change", "message"),
    [
        ("config", lambda d: d.update(architecture="arm64"), "platform mismatch"),
        (
            "config",
            lambda d: d["config"]["Labels"].update({"org.opencontainers.image.revision": "b" * 40}),
            "label mismatch",
        ),
        (
            "index",
            lambda d: d["manifests"][0]["platform"].update(architecture="arm64"),
            "linux/amd64",
        ),
        ("index", lambda d: d["manifests"].pop(), "one image and one attestation"),
        (
            "index",
            lambda d: d["manifests"][1]["annotations"].update(
                {"vnd.docker.reference.digest": "sha256:" + "f" * 64}
            ),
            "descriptor target mismatch",
        ),
        (
            "attest",
            lambda d: d["subject"].update(digest="sha256:" + "f" * 64),
            "artifact subject mismatch",
        ),
        ("attest", lambda d: d["layers"].pop(), "exactly provenance and SBOM"),
        (
            "attest",
            lambda d: d["layers"][0]["annotations"].update({"in-toto.io/predicate-type": "wrong"}),
            "annotation mismatch",
        ),
        (
            "provenance_statement",
            lambda d: d["subject"][0]["digest"].update(sha256="f" * 64),
            "subject digest mismatch",
        ),
        ("sbom_statement", lambda d: d.update(subject=None), "Missing in-toto subjects"),
        (
            "provenance",
            lambda d: d["invocation"]["configSource"].update(
                uri="https://github.com/Owner/Evil.git"
            ),
            "actual Git build source",
        ),
        (
            "provenance",
            lambda d: d["invocation"]["configSource"]["digest"].update(sha1="c" * 40),
            "build source commit mismatch",
        ),
        (
            "provenance",
            lambda d: d["invocation"]["configSource"].pop("uri"),
            "actual Git build source",
        ),
        (
            "provenance",
            lambda d: d["invocation"]["parameters"].update(locals=[{"name": "context"}]),
            "Untracked local inputs",
        ),
        (
            "provenance",
            lambda d: d["metadata"]["completeness"].update(materials=False),
            "Incomplete build materials",
        ),
        (
            "provenance",
            lambda d: d["metadata"]["completeness"].update(parameters=False),
            "maximum provenance",
        ),
        ("provenance", lambda d: d["materials"].pop(0), "Git source missing"),
        (
            "provenance",
            lambda d: d["materials"][0]["digest"].update(sha1="c" * 40),
            "material commit mismatch",
        ),
        (
            "provenance",
            lambda d: d["materials"][1].update(digest={}),
            "non-immutable build material",
        ),
        ("provenance", lambda d: d.update(buildType="attacker"), "Not BuildKit"),
        ("sbom", lambda d: d.update(packages=[]), "no package inventory"),
        ("sbom", lambda d: d.update(spdxVersion="not-spdx"), "Unsupported SPDX"),
        ("sbom", lambda d: d.update(documentNamespace=""), "namespace"),
        ("sbom", lambda d: d.update(packages=[{}]), "Malformed SPDX package"),
    ],
)
def test_rejects_missing_or_mismatched_semantics(verifier, stage, change, message) -> None:
    with pytest.raises(ValueError, match=message):
        verify(verifier, {stage: change})


def test_vcs_hints_cannot_replace_actual_git_materials(verifier) -> None:
    def mutate(predicate):
        predicate["invocation"]["configSource"] = {"entryPoint": "Dockerfile"}
        predicate["metadata"]["https://mobyproject.org/buildkit@v1#metadata"] = {
            "vcs": {"source": "https://github.com/" + REPO, "revision": COMMIT},
        }

    with pytest.raises(ValueError, match="actual Git build source"):
        verify(verifier, {"provenance": mutate})


def modern_statement(statement):
    predicate = statement["predicate"]
    source = predicate["invocation"]["configSource"]
    source["path"] = source.pop("entryPoint")
    statement["predicateType"] = "https://slsa.dev/provenance/v1"
    statement["subject"] = []
    statement["predicate"] = {
        "buildDefinition": {
            "buildType": "https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md",
            "externalParameters": {"configSource": source, "request": {"locals": []}},
            "internalParameters": {"buildConfig": predicate["buildConfig"]},
            "resolvedDependencies": predicate["materials"],
        },
        "runDetails": {
            "metadata": {
                "buildkit_completeness": {
                    "request": True,
                    "resolvedDependencies": True,
                }
            }
        },
    }


def test_current_buildkit_oci_artifact_and_slsa_v1(verifier):
    def annotations(attest):
        attest["layers"][0]["annotations"]["in-toto.io/predicate-type"] = verifier.SLSA_V1

    result = verify(
        verifier,
        {
            "provenance_statement": modern_statement,
            "sbom_statement": lambda d: d.update(subject=[]),
            "attest": annotations,
        },
    )
    assert result["provenance"]["predicate_type"] == verifier.SLSA_V1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["buildDefinition"]["externalParameters"]["configSource"]["digest"].update(
            sha1="b" * 40
        ),
        lambda p: p["buildDefinition"]["resolvedDependencies"].pop(0),
        lambda p: p["runDetails"]["metadata"]["buildkit_completeness"].update(
            resolvedDependencies=False
        ),
        lambda p: p["buildDefinition"]["externalParameters"]["request"].update(
            locals=[{"name": "context"}]
        ),
    ],
)
def test_slsa_v1_cannot_bypass_source_material_completeness_checks(verifier, mutate):
    def statement(value):
        modern_statement(value)
        mutate(value["predicate"])

    def annotations(attest):
        attest["layers"][0]["annotations"]["in-toto.io/predicate-type"] = verifier.SLSA_V1

    with pytest.raises(ValueError):
        verify(verifier, {"provenance_statement": statement, "attest": annotations})


def test_empty_statement_subject_without_oci_artifact_binding_is_rejected(verifier):
    def legacy(attest):
        attest.pop("subject")
        attest.pop("artifactType")

    with pytest.raises(ValueError, match="Missing in-toto subjects"):
        verify(
            verifier,
            {
                "sbom_statement": lambda d: d.update(subject=[]),
                "attest": legacy,
            },
        )


def test_legacy_attestation_binding_uses_full_statement_subjects(verifier):
    def legacy(attest):
        attest.pop("subject")
        attest.pop("artifactType")

    assert verify(verifier, {"attest": legacy})["sbom"]["packages"] == 1


def test_each_descriptor_content_is_checked_not_just_annotations(verifier) -> None:
    image, blobs = graph(verifier)
    first = next(iter(blobs))
    blobs[first] += b" "
    with pytest.raises(ValueError, match="size mismatch"):
        verifier.verify(image, REPO, COMMIT, TAG, lambda kind, digest: blobs[digest])
    blobs[first] = blobs[first][:-2] + b"x"
    with pytest.raises(ValueError, match="hash mismatch"):
        verifier.verify(image, REPO, COMMIT, TAG, lambda kind, digest: blobs[digest])


@pytest.mark.parametrize(
    "image", ["latest", "ghcr.io/owner/example:v0.1.0", "ghcr.io/evil/repo@sha256:" + "a" * 64]
)
def test_bad_reference_never_contacts_registry(verifier, image) -> None:
    with pytest.raises(ValueError):
        verifier.verify(image, REPO, COMMIT, TAG, lambda *_: pytest.fail("Registry contacted"))


def test_registry_cli_uses_ordinary_auth_without_reading_tokens(verifier, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(verifier, "command_bytes", lambda args: calls.append(args) or b"{}")
    fetch = verifier.registry_fetch("/opt/oras", "ghcr.io/owner/example")
    assert fetch("blob", "sha256:" + "a" * 64) == b"{}"
    assert calls == [
        ["/opt/oras", "blob", "fetch", "--output", "-", "ghcr.io/owner/example@sha256:" + "a" * 64]
    ]
    with pytest.raises(ValueError):
        fetch("delete", "sha256:" + "a" * 64)


def test_json_and_size_fail_closed(verifier) -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        verifier.document(b'{"subject":[],"subject":[1]}')
    with pytest.raises(ValueError, match="size"):
        verifier.descriptor({"digest": "sha256:" + "a" * 64, "size": True})
    with pytest.raises(ValueError):
        verifier.document(b"[]")


def test_cli_writes_only_verified_summary_and_never_overwrites(verifier, monkeypatch, tmp_path):
    image, blobs = graph(verifier)
    monkeypatch.setattr(verifier, "registry_fetch", lambda *args: lambda _, digest: blobs[digest])
    output = tmp_path / "verified.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify",
            "--image",
            image,
            "--repository",
            REPO,
            "--commit",
            COMMIT,
            "--tag",
            TAG,
            "--output",
            str(output),
        ],
    )
    assert verifier.main() == 0
    assert json.loads(output.read_text())["image"] == image
    assert verifier.main() == 1
    assert json.loads(output.read_text())["image"] == image


def test_registry_failure_never_prints_credential_helper_payload(verifier, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("private-token-value")

    monkeypatch.setattr(verifier, "verify", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify", "--image", "unused", "--repository", REPO, "--commit", COMMIT, "--tag", TAG],
    )
    assert verifier.main() == 1
    assert "private-token-value" not in capsys.readouterr().out


def test_cli_transport_enforces_return_code_and_output_bound(verifier, monkeypatch):
    assert verifier.command_bytes([sys.executable, "-c", "print('ok',end='')"]) == b"ok"
    with pytest.raises(ValueError, match="Registry CLI operation failed"):
        verifier.command_bytes([sys.executable, "-c", "raise SystemExit(2)"])
    monkeypatch.setattr(verifier, "LIMIT", 4)
    with pytest.raises((RuntimeError, ValueError)):
        verifier.command_bytes([sys.executable, "-c", "print('too much')"])


def test_pinned_installer_extracts_only_allowlisted_files(tmp_path, monkeypatch) -> None:
    module = load("install_oras")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, value in (("oras", b"tool"), ("LICENSE", b"license"), ("../evil", b"evil")):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    raw = stream.getvalue()
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setitem(module.PINS, "linux_arm64", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: io.BytesIO(raw))
    destination = tmp_path / "tools"
    assert module.install(destination).read_bytes() == b"tool"
    assert {p.name for p in destination.iterdir()} == {"oras", "LICENSE"}
    assert not (tmp_path / "evil").exists()
    assert module.install(destination) == destination / "oras"
    (destination / "oras").write_bytes(b"changed")
    with pytest.raises(ValueError, match="overwrite"):
        module.install(destination)
    monkeypatch.setitem(module.PINS, "linux_arm64", "f" * 64)
    with pytest.raises(ValueError, match="checksum"):
        module.install(tmp_path / "never-created")
