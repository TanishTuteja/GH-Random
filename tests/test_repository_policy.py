"""Offline checks for contributor metadata and privileged workflow boundaries."""

from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def document(path: Path) -> dict:
    # BaseLoader uses primitive strings/lists/maps, not YAML 1.1's `on` -> True.
    # It cannot construct Python objects or resolve executable custom tags.
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def test_dependabot_covers_locked_python_actions_and_both_image_manifests():
    data = document(ROOT / ".github/dependabot.yaml")
    assert data["version"] == "2"
    updates = data["updates"]
    assert {item["package-ecosystem"] for item in updates} == {
        "uv",
        "github-actions",
        "docker",
        "docker-compose",
    }
    for update in updates:
        assert update["directory"] == "/"
        assert update["schedule"] == {
            "interval": "weekly",
            "day": "monday",
            "time": "04:00",
            "timezone": "Etc/UTC",
        }
        assert update["open-pull-requests-limit"] == "2"
        assert "dependencies" in update["labels"]
        for group in update["groups"].values():
            assert set(group["update-types"]) == {"minor", "patch"}


def test_issue_forms_have_unique_ids_required_content_and_known_labels():
    forms = ROOT / ".github/ISSUE_TEMPLATE"
    for name in ("bug_report.yaml", "feature_request.yaml", "question.yaml"):
        form = document(forms / name)
        assert form["name"] and form["description"]
        assert set(form["labels"]) <= {"bug", "enhancement", "question"}
        inputs = [item for item in form["body"] if item["type"] != "markdown"]
        identifiers = [item["id"] for item in inputs]
        assert len(set(identifiers)) == len(identifiers)
        assert all(re.fullmatch(r"[a-zA-Z0-9_-]+", item) for item in identifiers)
        assert all(item["attributes"]["label"] for item in inputs)
        assert any(item.get("validations", {}).get("required") == "true" for item in inputs)
    config = document(forms / "config.yaml")
    assert config["blank_issues_enabled"] == "false"
    report = "https://github.com/TanishTuteja/GH-Random/security/advisories/new"
    assert config["contact_links"][0]["url"] == report
    assert report in (ROOT / "SECURITY.md").read_text()


def test_workflows_default_to_read_only_and_pin_external_actions():
    for path in (ROOT / ".github/workflows").glob("*.yaml"):
        data = document(path)
        assert data["permissions"] == {"contents": "read"}, path
        assert "pull_request_target" not in data["on"], path
        for job in data["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action is not None and not action.startswith("./"):
                    assert re.fullmatch(r"[\w.-]+/[\w./-]+@[a-f0-9]{40}", action), action


def assert_runtime_pins(root: Path) -> None:
    python_version = (root / ".python-version").read_text().strip()
    project = tomllib.loads((root / "pyproject.toml").read_text())
    dockerfile = (root / "Dockerfile").read_text()
    image = re.search(r"(?m)^FROM (\S+)$", dockerfile)
    assert image is not None
    assert re.fullmatch(rf"python:{re.escape(python_version)}-slim@sha256:[a-f0-9]{{64}}", image[1])
    assert project["tool"]["pyright"]["pythonVersion"] == python_version
    assert project["tool"]["ruff"]["target-version"] == "py" + python_version.replace(".", "")
    uv = project["tool"]["uv"]["required-version"].removeprefix("==")
    assert re.search(
        rf"COPY --from=ghcr.io/astral-sh/uv:{re.escape(uv)}@sha256:[a-f0-9]{{64}} ", dockerfile
    )
    production = document(root / "compose.production.yaml")
    development = document(root / "compose.yaml")
    examples = dict(
        line.split("=", 1)
        for line in (root / "deployment/compose.env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    ci = document(root / ".github/workflows/ci.yaml")
    for role, variable, repository in (
        ("database", "POSTGRES_IMAGE", "postgres"),
        ("caddy", "CADDY_IMAGE", "caddy"),
    ):
        pinned = examples[variable]
        assert re.fullmatch(rf"{repository}:\d+(?:\.\d+)?-alpine@sha256:[a-f0-9]{{64}}", pinned)
        assert production["services"][role]["image"] == "${" + variable + ":-" + pinned + "}"
    assert ci["jobs"]["tests"]["services"]["postgres"]["image"] == examples["POSTGRES_IMAGE"]
    postgres_tag = examples["POSTGRES_IMAGE"].split("@", 1)[0]
    assert development["services"]["database"]["image"] == postgres_tag
    admin = document(root / "compose.pgadmin.yaml")["services"]["pgadmin"]["image"]
    assert re.fullmatch(r"dpage/pgadmin4:\d+\.\d+@sha256:[a-f0-9]{64}", admin)
    assert development["services"]["pgadmin"]["image"] == admin.split("@", 1)[0]
    for job in ci["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/setup-python@"):
                assert step["with"]["python-version-file"] == ".python-version"


def test_runtime_and_shared_service_pins_agree():
    assert_runtime_pins(ROOT)
    updates = document(ROOT / ".github/dependabot.yaml")["updates"]
    policies = {update["package-ecosystem"]: update for update in updates}
    assert policies["docker"]["ignore"] == [
        {
            "dependency-name": "python",
            "update-types": ["version-update:semver-major", "version-update:semver-minor"],
        }
    ]
    assert policies["docker-compose"]["ignore"] == [
        {
            "dependency-name": "postgres",
            "update-types": ["version-update:semver-major"],
        }
    ]


@pytest.mark.parametrize("changed", ["Dockerfile", "ci", "caddy", "uv"])
def test_inconsistent_runtime_or_duplicate_pins_fail_closed(tmp_path: Path, changed: str):
    for name in (
        ".python-version",
        "pyproject.toml",
        "Dockerfile",
        "compose.production.yaml",
        "compose.yaml",
        "compose.pgadmin.yaml",
        "deployment/compose.env.example",
        ".github/workflows/ci.yaml",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    if changed == "Dockerfile":
        version = (ROOT / ".python-version").read_text().strip()
        path, old, new = "Dockerfile", f"FROM python:{version}-slim", "FROM python:999.0-slim"
    elif changed == "ci":
        image = document(ROOT / ".github/workflows/ci.yaml")["jobs"]["tests"]["services"][
            "postgres"
        ]["image"]
        path, old, new = (
            ".github/workflows/ci.yaml",
            "image: " + image,
            "image: postgres:999-alpine",
        )
    elif changed == "caddy":
        image = document(ROOT / "compose.production.yaml")["services"]["caddy"]["image"]
        pinned = image.removeprefix("${CADDY_IMAGE:-").removesuffix("}")
        path, old, new = (
            "deployment/compose.env.example",
            "CADDY_IMAGE=" + pinned,
            "CADDY_IMAGE=caddy:999-alpine",
        )
    else:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        uv = project["tool"]["uv"]["required-version"].removeprefix("==")
        path, old, new = "Dockerfile", "astral-sh/uv:" + uv, "astral-sh/uv:999.0.0"
    target = tmp_path / path
    content = target.read_text()
    assert old in content
    target.write_text(content.replace(old, new))
    with pytest.raises(AssertionError):
        assert_runtime_pins(tmp_path)
