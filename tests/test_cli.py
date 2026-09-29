from __future__ import annotations

import logging

import pytest

from app.cli import CatalogStatus, build_parser, main
from app.crawler.planning import CRAWL_SORT_CYCLE
from app.crawler.service import CrawlSummary


def test_crawl_command_dispatches_and_reports_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "app.cli.run_crawl",
        lambda: CrawlSummary(
            run_id=1,
            generation_id=2,
            ordering=CRAWL_SORT_CYCLE[0],
            requests_made=200,
            repositories_seen=123,
        ),
    )

    assert main(["crawl"]) == 0

    assert (
        capsys.readouterr().out
        == "Crawl complete: generation=2, requests=200, repositories=123, sort=updated desc\n"
    )


def test_crawl_command_returns_failure_when_a_crawl_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing_crawl() -> CrawlSummary:
        raise RuntimeError("GitHub is unavailable")

    monkeypatch.setattr("app.cli.run_crawl", failing_crawl)

    with caplog.at_level(logging.ERROR):
        assert main(["crawl"]) == 1

    assert "previous active catalog remains available" in caplog.text


def test_crawl_resume_flag_dispatches_to_the_resumable_crawl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received_resume_flags: list[bool] = []

    def resuming_crawl(*, resume: bool) -> CrawlSummary:
        received_resume_flags.append(resume)
        return CrawlSummary(
            run_id=1,
            generation_id=2,
            ordering=CRAWL_SORT_CYCLE[0],
            requests_made=200,
            repositories_seen=123,
        )

    monkeypatch.setattr("app.cli.run_crawl", resuming_crawl)

    assert main(["crawl", "--resume"]) == 0

    assert received_resume_flags == [True]
    assert "Crawl complete:" in capsys.readouterr().out


def test_status_command_reports_local_catalog_and_crawl_progress(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "app.cli.get_catalog_status",
        lambda: CatalogStatus(
            active_generation_id=4,
            active_generation_activated_at=None,
            latest_run_id=5,
            latest_run_status="running",
            latest_run_started_at=None,
            latest_run_completed_at=None,
            latest_run_requests_made=17,
            latest_run_repositories_seen=1_234,
            latest_run_resume_count=2,
        ),
    )

    assert main(["status"]) == 0

    assert (
        capsys.readouterr().out
        == "Catalog status: active_generation=4, active_at=none, latest_run=5, "
        "run_status=running, started_at=none, completed_at=none, resumes=2, requests=17, "
        "repositories=1234\n"
    )


def test_cli_requires_an_explicit_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
