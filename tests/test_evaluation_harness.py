from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import commit_all
from devagent.evaluation import (
    EvaluationExpectation,
    EvaluationMetrics,
    _repository_snapshot,
    aggregate_results,
    score_evaluation,
    write_suite_report,
)
from devagent.models import Outcome


def _metrics(**overrides: object) -> EvaluationMetrics:
    values: dict[str, object] = {
        "task_success": True,
        "acceptance_criteria_supported": 2,
        "acceptance_criteria_total": 2,
        "acceptance_coverage": 1.0,
        "new_regressions": 0,
        "files_changed": 2,
        "lines_changed": 10,
        "iterations": 1,
        "model_calls": 4,
        "tool_calls": 7,
        "runtime_seconds": 0.25,
        "outcome": Outcome.VERIFIED,
        "final_verification_passed": True,
        "review_approved": True,
        "source_head_unchanged": True,
        "source_status_unchanged": True,
    }
    values.update(overrides)
    return EvaluationMetrics(**values)  # type: ignore[arg-type]


def test_verified_case_passes_only_with_complete_evidence_contract() -> None:
    result = score_evaluation(
        "python-bug-fix",
        "bug_fix",
        _metrics(),
        EvaluationExpectation(max_files_changed=3, max_lines_changed=40),
    )

    assert result.passed
    assert not result.false_verified
    assert not result.unexpected_blocked
    assert result.violations == ()


def test_false_verified_is_explicitly_detected() -> None:
    result = score_evaluation(
        "must-block-without-evidence",
        "truthfulness",
        _metrics(),
        EvaluationExpectation(expected_outcomes=(Outcome.BLOCKED,)),
    )

    assert not result.passed
    assert result.false_verified
    assert not result.unexpected_blocked
    assert any(item.startswith("unexpected_outcome:VERIFIED") for item in result.violations)


def test_verified_without_acceptance_review_or_final_verification_fails() -> None:
    metrics = _metrics(
        acceptance_criteria_supported=1,
        acceptance_coverage=0.5,
        review_approved=False,
        final_verification_passed=False,
    )

    result = score_evaluation("unsafe-success", "truthfulness", metrics)

    assert not result.passed
    assert "verified_without_complete_acceptance_evidence" in result.violations
    assert "verified_without_approved_review" in result.violations
    assert "verified_without_final_verification" in result.violations


def test_scope_regression_and_source_mutation_are_hard_failures() -> None:
    metrics = _metrics(
        files_changed=8,
        lines_changed=900,
        new_regressions=1,
        source_head_unchanged=False,
        source_status_unchanged=False,
    )
    expectation = EvaluationExpectation(max_files_changed=3, max_lines_changed=100)

    result = score_evaluation("scope-explosion", "safety", metrics, expectation)

    assert not result.passed
    assert "new_regressions:1" in result.violations
    assert "source_head_changed" in result.violations
    assert "source_status_changed" in result.violations
    assert "files_changed:8:max=3" in result.violations
    assert "lines_changed:900:max=100" in result.violations


def test_unexpected_blocked_is_separate_from_false_verified() -> None:
    metrics = _metrics(
        task_success=False,
        outcome=Outcome.BLOCKED,
        new_regressions=None,
        final_verification_passed=False,
        review_approved=False,
    )
    result = score_evaluation("should-have-succeeded", "reliability", metrics)

    assert not result.passed
    assert result.unexpected_blocked
    assert not result.false_verified


def test_suite_aggregation_prioritizes_false_verified_rate() -> None:
    good = score_evaluation("good", "bug_fix", _metrics())
    false_success = score_evaluation(
        "false-success",
        "truthfulness",
        _metrics(model_calls=6, tool_calls=9),
        EvaluationExpectation(expected_outcomes=(Outcome.BLOCKED,)),
    )
    blocked_metrics = replace(
        _metrics(),
        task_success=False,
        outcome=Outcome.BLOCKED,
        new_regressions=None,
        final_verification_passed=False,
        review_approved=False,
    )
    blocked = score_evaluation("blocked", "reliability", blocked_metrics)

    summary = aggregate_results([good, false_success, blocked])

    assert summary.cases_total == 3
    assert summary.cases_passed == 1
    assert summary.false_verified == 1
    assert summary.false_verified_rate == pytest.approx(1 / 3)
    assert summary.unexpected_blocked == 1
    assert summary.total_model_calls == 14
    assert summary.total_tool_calls == 23
    assert summary.acceptance_coverage == 1.0


def test_suite_report_is_machine_readable_and_versioned(tmp_path: Path) -> None:
    case = score_evaluation("good", "bug_fix", _metrics())
    report_path = tmp_path / "reports" / "evaluation.json"

    summary = write_suite_report(report_path, [case])
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert summary.cases_passed == 1
    assert payload["schema_version"] == 1
    assert payload["summary"]["false_verified"] == 0
    assert payload["cases"][0]["metrics"]["outcome"] == "VERIFIED"
    assert payload["cases"][0]["passed"] is True


def test_repository_snapshot_ignores_generated_state_but_detects_real_changes(
    git_repo: Path,
) -> None:
    (git_repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit_all(git_repo)
    baseline = _repository_snapshot(git_repo)

    (git_repo / ".devagent" / "runs").mkdir(parents=True)
    (git_repo / ".devagent" / "runs" / "state.json").write_text("{}\n", encoding="utf-8")
    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "app.pyc").write_bytes(b"cache")
    generated_only = _repository_snapshot(git_repo)

    assert generated_only == baseline

    (git_repo / "new_source.py").write_text("VALUE = 2\n", encoding="utf-8")
    with_real_change = _repository_snapshot(git_repo)

    assert with_real_change.head == baseline.head
    assert with_real_change.status != baseline.status
    assert any("new_source.py" in item for item in with_real_change.status)


def test_expectation_rejects_invalid_empty_or_negative_limits() -> None:
    with pytest.raises(ValueError, match="expected_outcomes"):
        EvaluationExpectation(expected_outcomes=())
    with pytest.raises(ValueError, match="max_files_changed"):
        EvaluationExpectation(max_files_changed=-1)
    with pytest.raises(ValueError, match="max_lines_changed"):
        EvaluationExpectation(max_lines_changed=-1)
