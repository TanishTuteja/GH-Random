"""Offline checks for contributor metadata and privileged workflow boundaries."""

from __future__ import annotations

import re
from pathlib import Path

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
