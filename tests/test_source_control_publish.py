from __future__ import annotations

import subprocess
from pathlib import Path

from devagent.models import (
    ChangeMetrics,
    FailureClass,
    Outcome,
    RepositoryModel,
    RiskLevel,
    RunResult,
    SourceControlResult,
    TaskSpec,
    TaskType,
    VerificationResult,
)
from devagent.report import render_report
from devagent.source_control import prepare_publication, publish_verified_branch


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def _result(source: Path, working: Path, *, outcome: Outcome = Outcome.VERIFIED) -> RunResult:
    return RunResult(
        outcome=outcome,
        task=TaskSpec(
            task_type=TaskType.FEATURE,
            goal="Add multiplication support",
            requires_code_change=True,
            requires_tests=True,
            acceptance_criteria=[],
            risk=RiskLevel.LOW,
        ),
        repository=RepositoryModel(
            root=str(source),
            kind="single-component",
            components=[],
            facts=[],
            git_branch="master",
            git_head="baseline",
        ),
        run_id="20260825T010000Z-test",
        run_dir=str(source / ".devagent" / "runs" / "test"),
        root_cause="Feature is missing",
        implementation=["Add multiply(a, b) and tests"],
        changes=ChangeMetrics(files_changed=1, lines_added=2, lines_deleted=0, paths=["calculator.py"]),
        verification=[],
        review=None,
        not_run=[],
        recommendations=[],
        state_history=[],
        working_root=str(working),
    )


def _repo_with_bare_remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    working = tmp_path / "working"
    source.mkdir()
    assert _git(source, "init").returncode == 0
    assert _git(source, "config", "user.name", "DevAgent Test").returncode == 0
    assert _git(source, "config", "user.email", "devagent-test@example.com").returncode == 0
    (source / "calculator.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    assert _git(source, "add", "calculator.py").returncode == 0
    assert _git(source, "commit", "-m", "baseline").returncode == 0

    remote.mkdir()
    assert _git(remote, "init", "--bare").returncode == 0
    assert _git(source, "remote", "add", "origin", str(remote)).returncode == 0
    assert _git(source, "worktree", "add", "--detach", str(working), "HEAD").returncode == 0
    return source, working, remote


def test_verified_result_commits_and_pushes_only_new_branch(tmp_path: Path) -> None:
    source, working, remote = _repo_with_bare_remote(tmp_path)
    (working / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    result = _result(source, working)

    publication = publish_verified_branch(result, branch="devagent/multiplication")

    assert publication.requested is True
    assert publication.committed is True
    assert publication.pushed is True
    assert publication.error is None
    assert publication.commit
    assert publication.pull_request_created is False
    assert publication.merged is False
    remote_head = _git(remote, "rev-parse", "refs/heads/devagent/multiplication")
    assert remote_head.returncode == 0
    assert remote_head.stdout.strip() == publication.commit
    assert _git(remote, "show-ref", "--verify", "--quiet", "refs/heads/main").returncode != 0


def test_publish_refuses_protected_branch(tmp_path: Path) -> None:
    source, working, _remote = _repo_with_bare_remote(tmp_path)
    (working / "calculator.py").write_text("changed\n", encoding="utf-8")

    publication = publish_verified_branch(_result(source, working), branch="main")

    assert publication.pushed is False
    assert publication.committed is False
    assert publication.error == "Refusing to publish directly to protected branch: main"


def test_publish_refuses_non_verified_result(tmp_path: Path) -> None:
    source, working, _remote = _repo_with_bare_remote(tmp_path)
    result = _result(source, working, outcome=Outcome.BLOCKED)

    publication = publish_verified_branch(result, branch="devagent/blocked")

    assert publication.pushed is False
    assert publication.error == "Branch publishing is allowed only for VERIFIED runs"


def test_report_includes_failure_detail_recommendation_and_source_control(tmp_path: Path) -> None:
    source, working, _remote = _repo_with_bare_remote(tmp_path)
    result = _result(source, working, outcome=Outcome.PARTIALLY_VERIFIED)
    result.verification.append(
        VerificationResult(
            command=("python", "-m", "pytest", "-q"),
            exit_code=1,
            duration_seconds=0.25,
            stdout="1 failed, 2 passed",
            stderr="AssertionError: expected 12",
            classification=FailureClass.ASSERTION_FAILURE,
            revision=2,
            phase="final",
            tests_run=3,
            tests_passed=2,
        )
    )
    result.source_control = SourceControlResult(
        requested=True,
        remote="origin",
        branch="devagent/multiplication",
        error="Branch publishing is allowed only for VERIFIED runs",
    )

    report = render_report(result)

    assert "Failed checks: 1" in report
    assert "class=ASSERTION_FAILURE" in report
    assert "AssertionError: expected 12" in report
    assert "Inspect the failing assertion" in report
    assert "Branch: devagent/multiplication" in report
    assert "Pushed: NO" in report
    assert "Pull request: NOT CREATED" in report
    assert "Merge: NOT PERFORMED" in report

def test_prepare_publication_continues_current_development_branch(tmp_path: Path) -> None:
    source, _working, _remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0

    plan = prepare_publication(source)

    assert plan.mode == "continue"
    assert plan.branch == "feature/calculator"
    assert plan.base_commit == baseline
    assert plan.expected_remote_head == baseline


def test_prepare_publication_uses_remote_head_when_devagent_branch_is_ahead(tmp_path: Path) -> None:
    source, _working, remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0

    other = tmp_path / "other"
    cloned = subprocess.run(
        ["git", "clone", "--branch", "feature/calculator", str(remote), str(other)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0
    assert _git(other, "config", "user.name", "Other Developer").returncode == 0
    assert _git(other, "config", "user.email", "other@example.com").returncode == 0
    (other / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    assert _git(other, "add", "calculator.py").returncode == 0
    assert _git(other, "commit", "-m", "previous DevAgent result").returncode == 0
    assert _git(other, "push", "origin", "feature/calculator").returncode == 0
    remote_head = _git(other, "rev-parse", "HEAD").stdout.strip()
    assert remote_head != baseline

    plan = prepare_publication(source)

    assert plan.mode == "continue"
    assert plan.branch == "feature/calculator"
    assert plan.base_commit == remote_head
    assert plan.expected_remote_head == remote_head
    assert _git(source, "rev-parse", "HEAD").stdout.strip() == baseline


def test_continue_mode_fast_forward_pushes_same_branch(tmp_path: Path) -> None:
    source, working, remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0
    (working / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    result = _result(source, working)
    result.repository.git_branch = "feature/calculator"
    result.repository.git_head = baseline

    publication = publish_verified_branch(
        result,
        branch="feature/calculator",
        mode="continue",
        expected_remote_head=baseline,
    )

    assert publication.pushed is True
    assert publication.committed is True
    assert publication.error is None
    remote_head = _git(remote, "rev-parse", "refs/heads/feature/calculator").stdout.strip()
    assert remote_head == publication.commit
    parent = _git(remote, "rev-parse", f"{remote_head}^").stdout.strip()
    assert parent == baseline


def test_continue_mode_blocks_when_remote_moves_during_run(tmp_path: Path) -> None:
    source, working, remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0
    (working / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    result = _result(source, working)
    result.repository.git_branch = "feature/calculator"
    result.repository.git_head = baseline

    (source / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\n# concurrent change\n",
        encoding="utf-8",
    )
    assert _git(source, "add", "calculator.py").returncode == 0
    assert _git(source, "commit", "-m", "concurrent update").returncode == 0
    assert _git(source, "push", "origin", "feature/calculator").returncode == 0
    moved = _git(remote, "rev-parse", "refs/heads/feature/calculator").stdout.strip()
    assert moved != baseline

    publication = publish_verified_branch(
        result,
        branch="feature/calculator",
        mode="continue",
        expected_remote_head=baseline,
    )

    assert publication.pushed is False
    assert publication.committed is False
    assert publication.error is not None
    assert "Remote branch changed during run" in publication.error
