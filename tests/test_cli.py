from __future__ import annotations

import logging

import pytest

from app.cli import build_parser, main
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


def test_cli_requires_an_explicit_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
