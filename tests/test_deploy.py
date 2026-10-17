"""Offline deploy state-machine tests; real Docker rehearsals run in container CI."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def deploy_tools(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    directory = Path(__file__).resolve().parents[1] / "deployment"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("deploy_tools", directory / "deploy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def make_release(module: ModuleType, root: Path, *, version: str = "v0.1.0") -> dict[str, Any]:
    root.mkdir()
    files = {}
    for name in module.CRITICAL_FILES:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("release file\n", encoding="utf-8")
        files[name] = module.checksum(path)
    metadata = {
        "deployment_format": 1,
        "tag": version,
        "commit": "a" * 40,
        "repository": "Owner/Repo",
        "digest": "sha256:" + "b" * 64,
        "image": "ghcr.io/owner/repo@sha256:" + "b" * 64,
        "platform": "linux/amd64",
        "deployment_files": files,
    }
    (root / "release.json").write_text(json.dumps(metadata))
    return metadata


@pytest.fixture
def setup(deploy_tools: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "release"
    metadata = make_release(deploy_tools, root)
    env_file = tmp_path / "compose.env"
    private_text(env_file, "GH_RANDOM_IMAGE=old-image\nPOSTGRES_IMAGE=postgres:17\n")
    arguments = argparse.Namespace(
        command="apply",
        release_dir=root,
        env_file=env_file,
        state_dir=tmp_path / "state",
        backup_dir=tmp_path / "backups",
        url="https://example.com",
        project_name="test-project",
        timeout_seconds=10,
        smoke_timeout_seconds=2,
        bootstrap=False,
        allow_loopback_http=False,
        confirm_schema_compatible=False,
    )
    events: list[str] = []
    behavior = {"failure": "", "revision": "base", "head": "base"}

    class FakeDeployment(deploy_tools.Deployment):
        def preflight(self, supplied):
            events.append("preflight")
            if behavior["failure"] == "preflight":
                raise RuntimeError("bad config")
            self.compose = ["docker", "compose"]
            return {
                "services": {
                    "database": {
                        "image": "postgres:17",
                        "environment": {
                            "POSTGRES_DB": "test",
                            "POSTGRES_USER": "test",
                            "POSTGRES_PASSWORD": "test",
                        },
                    }
                }
            }

        def validate_proxy(self, config):
            pass

        def container(self, service):
            return "database" if service == "database" else None

        def image_info(self, image):
            return {"Id": "database-image"}

        def run(self, command, **kwargs):
            if command[:2] == ["docker", "inspect"]:
                return json.dumps(
                    [
                        {
                            "State": {"Running": True},
                            "Image": "database-image",
                            "Config": {
                                "Image": "postgres:17",
                                "Env": [
                                    "POSTGRES_DB=test",
                                    "POSTGRES_USER=test",
                                    "POSTGRES_PASSWORD=test",
                                ],
                            },
                        }
                    ]
                )
            if "up" in command:
                events.append("restart_" + command[-1])
            return ""

    class FakeCoordinator:
        def __init__(self, deployment):
            self.name = "test-coordinator"

        def __enter__(self):
            events.append("acquire")
            if behavior["failure"] == "acquire":
                raise RuntimeError("busy")
            return self

        def request(self, command):
            events.append(command)
            if behavior["failure"] == command:
                raise RuntimeError("injected failure")
            return {"ok": True, "revision": behavior["revision"], "head": behavior["head"]}

        def __exit__(self, *args):
            events.append("release")
            if behavior["failure"] == "release":
                raise RuntimeError("cleanup failed")

    def fake_backup(*args):
        events.append("backup")
        if behavior["failure"] == "backup":
            raise RuntimeError("backup failed")
        return tmp_path / "completed-backup"

    def fake_smoke(*args):
        events.append("smoke")
        if behavior["failure"] == "smoke":
            raise RuntimeError("unhealthy page")

    monkeypatch.setattr(deploy_tools, "Coordinator", FakeCoordinator)
    monkeypatch.setattr(deploy_tools, "Database", lambda *args, **kwargs: object())
    monkeypatch.setattr(deploy_tools, "backup", fake_backup)
    monkeypatch.setattr(deploy_tools, "smoke", fake_smoke)
    return arguments, metadata, events, behavior, FakeDeployment


def test_success_orders_backup_migration_smoke_and_promotion(setup) -> None:
    args, metadata, events, behavior, implementation = setup
    result = implementation(args).execute()
    assert result["status"] == "success"
    assert events.index("acquire") < events.index("backup") < events.index("migrate")
    assert events.index("migrate") < events.index("restart_web") < events.index("smoke")
    assert events[-1] == "release"
    current = json.loads((args.state_dir / "current.json").read_text())
    assert current["image"] == metadata["image"]
    assert current["previous"] is None
    assert f"GH_RANDOM_IMAGE={metadata['image']}" in args.env_file.read_text()
    assert "POSTGRES_IMAGE=postgres:17" in args.env_file.read_text()


@pytest.mark.parametrize("failure", ["preflight", "acquire", "backup", "migrate", "smoke"])
def test_failures_do_not_promote_or_rewrite_config(setup, failure: str) -> None:
    args, metadata, events, behavior, implementation = setup
    behavior["failure"] = failure
    with pytest.raises(RuntimeError):
        implementation(args).execute()
    assert not (args.state_dir / "current.json").exists()
    assert "GH_RANDOM_IMAGE=old-image" in args.env_file.read_text()
    journal = next(args.state_dir.glob("*.json"))
    assert json.loads(journal.read_text())["status"] == "failed"
    if failure in ("backup", "migrate", "smoke"):
        assert events[-1] == "release"
    if failure != "smoke":
        assert "restart_web" not in events
    assert "downgrade" not in events


def test_rollback_never_runs_upgrade_and_requires_same_schema(
    setup, deploy_tools: ModuleType
) -> None:
    args, metadata, events, behavior, implementation = setup
    implementation(args).execute()
    implementation(args).execute()  # Establish an explicit previous successful release.
    events.clear()
    args.command = "rollback"
    args.confirm_schema_compatible = True
    result = implementation(args).execute()
    assert result["status"] == "success"
    assert "migrate" not in events
    assert "backup" in events and "restart_web" in events
    behavior["revision"] = "newer-than-old-image"
    events.clear()
    with pytest.raises(ValueError, match="Schema-changing rollback"):
        implementation(args).execute()
    assert "backup" not in events and "restart_web" not in events


def test_rollback_requires_acknowledgement(setup) -> None:
    args, metadata, events, behavior, implementation = setup
    args.command = "rollback"
    with pytest.raises(ValueError, match="acknowledgement"):
        implementation(args).execute()
    assert events == []


def test_cleanup_failure_is_recorded_after_verified_runtime_promotion(setup) -> None:
    args, metadata, events, behavior, implementation = setup
    behavior["failure"] = "release"
    with pytest.raises(RuntimeError, match="cleanup"):
        implementation(args).execute()
    assert json.loads((args.state_dir / "current.json").read_text())["image"] == metadata["image"]
    records = [p for p in args.state_dir.glob("*.json") if p.name != "current.json"]
    assert json.loads(records[0].read_text())["phase"] == "release_catalog"


def test_kernel_lock_excludes_a_second_deployer_without_stale_file_lockout(
    deploy_tools: ModuleType, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    with deploy_tools.deployment_lock(state):
        with pytest.raises(RuntimeError, match="Another deployment"):
            with deploy_tools.deployment_lock(state):
                pytest.fail("second owner admitted")
    assert (state / "deploy.lock").exists()
    with deploy_tools.deployment_lock(state):
        pass


def test_deployment_signal_exception_is_not_retryable_io(deploy_tools: ModuleType) -> None:
    assert not issubclass(deploy_tools.DeploymentInterrupted, OSError)


def test_edited_or_incomplete_release_files_are_rejected(
    deploy_tools: ModuleType, tmp_path: Path
) -> None:
    root = tmp_path / "release"
    make_release(deploy_tools, root)
    deploy_tools.load_release(root)
    (root / "Caddyfile").write_text("edited")
    with pytest.raises(ValueError, match="Release file changed"):
        deploy_tools.load_release(root)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://localhost",
        "http://192.0.2.1",
        "file:///etc/passwd",
        "https://name:password@example.com",
        "https://example.com/extra",
        "https://example.com?token=x",
    ],
)
def test_unsafe_smoke_urls_are_rejected(deploy_tools: ModuleType, url: str) -> None:
    with pytest.raises(ValueError):
        deploy_tools.validate_url(url, allow_loopback_http=True)


def test_tls_is_default_and_plain_http_needs_explicit_loopback_flag(
    deploy_tools: ModuleType,
) -> None:
    deploy_tools.validate_url("https://example.com", allow_loopback_http=False)
    with pytest.raises(ValueError, match="HTTPS"):
        deploy_tools.validate_url("http://127.0.0.1:1234", allow_loopback_http=False)
    deploy_tools.validate_url("http://127.0.0.1:1234", allow_loopback_http=True)


@pytest.fixture
def preflight(deploy_tools: ModuleType, tmp_path: Path):
    root = tmp_path / "release"
    metadata = make_release(deploy_tools, root)
    contents = "GH_RANDOM_IMAGE=placeholder\n"
    for key in (
        "GH_RANDOM_POSTGRES_ENV_FILE",
        "GH_RANDOM_APP_ENV_FILE",
        "GH_RANDOM_CRAWLER_ENV_FILE",
        "GH_RANDOM_CADDY_ENV_FILE",
        "GITHUB_APP_PRIVATE_KEY_HOST_PATH",
    ):
        path = tmp_path / key
        private_text(path, "fixture\n")
        contents += f"{key}={path.as_posix()}\n"
    env_file = tmp_path / "compose.env"
    private_text(env_file, contents)
    args = argparse.Namespace(
        release_dir=root,
        env_file=env_file,
        timeout_seconds=10,
        project_name="test",
        url="https://example.com",
    )
    config = {
        "services": {
            "database": {
                "environment": {
                    "POSTGRES_DB": "catalog",
                    "POSTGRES_USER": "owner",
                    "POSTGRES_PASSWORD": "a@b",
                }
            },
            "caddy": {"environment": {"GH_RANDOM_DOMAIN": "example.com"}},
            **{
                role: {
                    "image": metadata["image"],
                    "environment": {
                        "DATABASE_URL": "postgresql+psycopg://owner:a%40b@database:5432/catalog",
                        "APP_ALLOWED_HOSTS": '["example.com"]',
                    },
                }
                for role in ("web", "migrate", "crawler")
            },
        }
    }
    image = {
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "Labels": {
                "org.opencontainers.image.source": "https://github.com/Owner/Repo",
                "org.opencontainers.image.revision": metadata["commit"],
                "org.opencontainers.image.version": metadata["tag"],
            }
        },
    }

    class InspectDeployment(deploy_tools.Deployment):
        def run(self, command, **kwargs):
            if command[1] == "info":
                return "x86_64"
            assert command[-3:] == ["config", "--format", "json"]
            return json.dumps(config)

        def image_info(self, reference):
            assert reference == metadata["image"]
            return image

    return InspectDeployment(args), metadata, config, image


def test_preflight_accepts_explicit_private_config_and_encoded_credentials(preflight) -> None:
    deployment, metadata, config, image = preflight
    assert deployment.preflight(metadata) == config


def test_scheduler_preflight_does_not_require_a_public_smoke_url(preflight) -> None:
    deployment, metadata, config, image = preflight
    del deployment.args.url
    assert deployment.preflight(metadata, check_site=False) == config


@pytest.mark.parametrize("role", ["web", "migrate", "crawler"])
def test_preflight_rejects_mismatching_database_credentials(preflight, role: str) -> None:
    deployment, metadata, config, image = preflight
    config["services"][role]["environment"]["DATABASE_URL"] += "_wrong_database"
    with pytest.raises(ValueError, match="credentials"):
        deployment.preflight(metadata)


@pytest.mark.parametrize("field", ["source", "revision", "version"])
def test_preflight_rejects_mismatching_image_labels(preflight, field: str) -> None:
    deployment, metadata, config, image = preflight
    image["Config"]["Labels"][f"org.opencontainers.image.{field}"] = "wrong"
    with pytest.raises(ValueError, match="labels"):
        deployment.preflight(metadata)


def test_preflight_rejects_cross_architecture_image(preflight) -> None:
    deployment, metadata, config, image = preflight
    image["Architecture"] = "arm64"
    with pytest.raises(ValueError, match="architecture"):
        deployment.preflight(metadata)


def test_preflight_rejects_unrelated_smoke_hostname(preflight) -> None:
    deployment, metadata, config, image = preflight
    deployment.args.url = "https://unrelated.example"
    with pytest.raises(ValueError, match="hostname"):
        deployment.preflight(metadata)


@pytest.mark.parametrize(
    "hosts",
    [
        "[]",
        '["*"]',
        '["*.example.com"]',
        '"example.com"',
        "[1]",
        '["example.com", "https://bad.example"]',
        '["example.com", "bad..example"]',
    ],
)
def test_preflight_rejects_missing_or_wildcard_host_allowlist(preflight, hosts: str) -> None:
    deployment, metadata, config, image = preflight
    config["services"]["web"]["environment"]["APP_ALLOWED_HOSTS"] = hosts
    with pytest.raises(ValueError, match="APP_ALLOWED_HOSTS"):
        deployment.preflight(metadata)


def test_preflight_rejects_mutable_role_image(preflight) -> None:
    deployment, metadata, config, image = preflight
    config["services"]["crawler"]["image"] = "app:latest"
    with pytest.raises(ValueError, match="release digest"):
        deployment.preflight(metadata)


def test_preflight_rejects_missing_private_configuration(preflight) -> None:
    deployment, metadata, config, image = preflight
    deployment.args.env_file.write_text("GH_RANDOM_IMAGE=placeholder\n")
    with pytest.raises(ValueError, match="configuration path"):
        deployment.preflight(metadata)


def test_migration_diagnostics_never_contaminate_stdio_protocol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import deployment

    def upgrade(config, target):
        assert target == "head"
        assert callable(config.attributes["catalog_ownership_check"])

    def check(config):
        config.print_stdout("No new upgrade operations detected.")

    monkeypatch.setattr(deployment.command, "upgrade", upgrade)
    monkeypatch.setattr(deployment.command, "check", check)
    deployment.migrate(None, lambda: None)  # type: ignore[arg-type]
    deployment.respond({"ok": True})
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}
    assert "No new upgrade" in captured.err


def test_smoke_checks_homepage_and_database_discovery(
    deploy_tools: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b"<!DOCTYPE html><html><body>GH Random</body></html>"

    class Opener:
        def open(self, url, **kwargs):
            requested.append(url)
            return Response()

    monkeypatch.setattr(deploy_tools, "build_opener", lambda *args: Opener())
    deploy_tools.smoke("https://example.com/", 1)
    assert requested == ["https://example.com/", "https://example.com/?discover=true"]


def test_smoke_never_follows_redirects(deploy_tools: ModuleType) -> None:
    with pytest.raises(ValueError, match="must not redirect"):
        deploy_tools.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere/")
