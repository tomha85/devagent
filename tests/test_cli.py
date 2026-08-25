from __future__ import annotations

from pathlib import Path

import pytest

from devagent import __version__
from devagent.cli import _read_requirement_file, main
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
from devagent.source_control import PublicationPlan


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


@pytest.mark.parametrize(
    "filename",
    ["requirement.md", "requirement.txt", "requirement.yaml", "requirement.custom", "requirement"],
)
def test_requirement_file_extension_is_unrestricted(tmp_path: Path, filename: str) -> None:
    requirement = tmp_path / filename
    requirement.write_text("Add CSV export and preserve existing behavior.\n", encoding="utf-8")

    assert _read_requirement_file(requirement) == "Add CSV export and preserve existing behavior."


def test_requirement_file_accepts_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    requirement = specs / "customer.requirement"
    requirement.write_text("Fix reconnect handling.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _read_requirement_file(Path("specs/customer.requirement")) == "Fix reconnect handling."


def test_requirement_file_accepts_home_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requirement = tmp_path / "task"
    requirement.write_text("Add report filtering.\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _read_requirement_file(Path("~/task")) == "Add report filtering."


def test_requirement_file_rejects_binary_content(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.anything"
    requirement.write_bytes(b"Add feature\x00binary")

    with pytest.raises(ValueError, match="appears to be binary"):
        _read_requirement_file(requirement)


def test_requirement_file_rejects_invalid_utf8(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.data"
    requirement.write_bytes(b"\xff\xfe\xfd")

    with pytest.raises(ValueError, match="readable UTF-8 text"):
        _read_requirement_file(requirement)


def test_requirement_file_rejects_sensitive_path(tmp_path: Path) -> None:
    requirement = tmp_path / ".env"
    requirement.write_text("not a requirement", encoding="utf-8")

    with pytest.raises(ValueError, match="sensitive input file"):
        _read_requirement_file(requirement)


def test_requirement_file_is_bounded(tmp_path: Path) -> None:
    requirement = tmp_path / "large.spec"
    requirement.write_bytes(b"x" * 2_000_001)

    with pytest.raises(ValueError, match="exceeds 2000000 bytes"):
        _read_requirement_file(requirement)


def test_benchmark_subcommand_has_dedicated_help(capsys) -> None:
    from devagent.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["benchmark", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "pinned real-world" in output
    assert "--catalog" in output


def test_cli_help_describes_unrestricted_input_path(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--input PATH" in output
    assert "extension is unrestricted" in output


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
        "devagent.cli.prepare_publication",
        lambda _repo, *, explicit_branch=None, remote="origin": PublicationPlan(
            mode="new",
            remote=remote,
            branch=explicit_branch,
            base_commit="baseline",
            expected_remote_head=None,
        ),
    )
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
        mode: str = "new",
        expected_remote_head: str | None = None,
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


def test_current_local_development_branch_is_continued_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    repo, result = _patch_engineering_run(tmp_path, monkeypatch, events)
    result.repository.git_branch = "feature/calculator"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "devagent.cli.prepare_publication",
        lambda _repo, *, explicit_branch=None, remote="origin": PublicationPlan(
            mode="continue",
            remote=remote,
            branch="feature/calculator",
            base_commit="remote123",
            expected_remote_head="remote123",
        ),
    )

    def fake_publish(
        _result: RunResult,
        *,
        branch: str | None = None,
        remote: str = "origin",
        mode: str = "new",
        expected_remote_head: str | None = None,
    ) -> SourceControlResult:
        events.append("publish")
        captured.update(
            branch=branch,
            remote=remote,
            mode=mode,
            expected_remote_head=expected_remote_head,
        )
        return SourceControlResult(
            requested=True,
            remote=remote,
            branch=branch,
            commit="next456",
            committed=True,
            pushed=True,
        )

    monkeypatch.setattr("devagent.cli.publish_verified_branch", fake_publish)

    assert main(["--repo", str(repo), "Add addition support"]) == 0

    output = capsys.readouterr().out
    assert events == ["run", "report", "publish"]
    assert captured == {
        "branch": "feature/calculator",
        "remote": "origin",
        "mode": "continue",
        "expected_remote_head": "remote123",
    }
    assert "Branch: feature/calculator" in output
    assert "Status: PUSHED" in output
