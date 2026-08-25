from __future__ import annotations

from pathlib import Path

import pytest

from devagent import __version__
from devagent.cli import main
from devagent.config import ProviderConfig
from devagent.models import (
    ChangeMetrics,
    DeveloperReviewEvidence,
    Outcome,
    RepositoryModel,
    RiskLevel,
    RunResult,
    SourceControlResult,
    TaskSpec,
    TaskType,
)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_setup_and_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("DEVAGENT_CONFIG", str(path))
    assert main(["setup", "--provider", "compatible", "--base-url", "http://127.0.0.1:11434/v1"]) == 0
    assert path.is_file()
    assert main(["doctor"]) == 0
    assert "DEVAGENT DOCTOR" in capsys.readouterr().out


def test_status_without_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert "No DevAgent runs" in capsys.readouterr().out


def _verified_result(repo: Path, run_dir: Path) -> RunResult:
    working = repo.parent / "isolated-working"
    working.mkdir(exist_ok=True)
    return RunResult(
        outcome=Outcome.VERIFIED,
        task=TaskSpec(
            task_type=TaskType.FEATURE,
            goal="Add multiplication support",
            requires_code_change=True,
            requires_tests=True,
            acceptance_criteria=[],
            risk=RiskLevel.LOW,
        ),
        repository=RepositoryModel(
            root=str(repo),
            kind="single-component",
            components=[],
            facts=[],
            git_branch="master",
            git_head="baseline",
        ),
        run_id="20260825T020000Z-test",
        run_dir=str(run_dir),
        root_cause="Multiplication support is missing",
        implementation=["Add multiply(a, b) and tests"],
        changes=ChangeMetrics(files_changed=1, lines_added=2, lines_deleted=0, paths=["calculator.py"]),
        verification=[],
        review=None,
        not_run=[],
        recommendations=[],
        state_history=[],
        working_root=str(working),
    )


def _patch_engineering_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> tuple[Path, RunResult]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = repo / ".devagent" / "runs" / "20260825T020000Z-test"
    run_dir.mkdir(parents=True)
    result = _verified_result(repo, run_dir)

    monkeypatch.setattr(
        "devagent.cli.load_config",
        lambda: ProviderConfig("compatible", "local-model", "http://127.0.0.1:11434/v1", None),
    )
    monkeypatch.setattr("devagent.cli.create_provider", lambda _config: object())
    monkeypatch.setattr(
        "devagent.cli.analyze_developer_review",
        lambda _root, _paths: DeveloperReviewEvidence(),
    )

    class FakeDevAgent:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self, _repo: Path, _requirement: str) -> RunResult:
            events.append("run")
            return result

    monkeypatch.setattr("devagent.orchestrator.DevAgent", FakeDevAgent)

    def fake_render_report(_result: RunResult) -> str:
        events.append("report")
        return "FULL ENGINEERING REPORT"

    monkeypatch.setattr("devagent.report.render_report", fake_render_report)
    return repo, result


def test_verified_run_prints_report_before_automatic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    repo, result = _patch_engineering_run(tmp_path, monkeypatch, events)

    def fake_publish(
        _result: RunResult,
        *,
        branch: str | None = None,
        remote: str = "origin",
    ) -> SourceControlResult:
        events.append("publish")
        return SourceControlResult(
            requested=True,
            remote=remote,
            branch=branch,
            commit="abc123",
            committed=True,
            pushed=True,
        )

    monkeypatch.setattr("devagent.cli.publish_verified_branch", fake_publish)

    assert main(["--repo", str(repo), "Add multiplication support"]) == 0

    output = capsys.readouterr().out
    assert events == ["run", "report", "publish"]
    assert output.index("FULL ENGINEERING REPORT") < output.index("Starting deterministic branch publication")
    assert output.index("Starting deterministic branch publication") < output.index("SOURCE CONTROL PUBLICATION RECEIPT")
    assert "Status: PUSHED" in output
    assert "Branch: devagent/20260825T020000Z-test" in output
    assert "Commit: abc123" in output
    assert "Pull request: NOT CREATED" in output
    assert "Merge: NOT PERFORMED" in output
    assert result.source_control.pushed is True


def test_no_publish_keeps_verified_run_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    repo, result = _patch_engineering_run(tmp_path, monkeypatch, events)

    def unexpected_publish(*_args: object, **_kwargs: object) -> SourceControlResult:
        raise AssertionError("publisher must not run with --no-publish")

    monkeypatch.setattr("devagent.cli.publish_verified_branch", unexpected_publish)

    assert main(["--repo", str(repo), "--no-publish", "Add multiplication support"]) == 0

    output = capsys.readouterr().out
    assert events == ["run", "report"]
    assert "FULL ENGINEERING REPORT" in output
    assert "SOURCE CONTROL PUBLICATION RECEIPT" not in output
    assert result.source_control.requested is False
