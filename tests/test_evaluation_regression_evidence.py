from __future__ import annotations

from devagent.evaluation import EvaluationMetrics, score_evaluation
from devagent.models import Outcome


def test_verified_case_requires_known_regression_status() -> None:
    metrics = EvaluationMetrics(
        task_success=True,
        acceptance_criteria_supported=2,
        acceptance_criteria_total=2,
        acceptance_coverage=1.0,
        new_regressions=None,
        files_changed=2,
        lines_changed=8,
        iterations=1,
        model_calls=4,
        tool_calls=6,
        runtime_seconds=0.2,
        outcome=Outcome.VERIFIED,
        final_verification_passed=True,
        review_approved=True,
        source_head_unchanged=True,
        source_status_unchanged=True,
    )

    result = score_evaluation("unknown-regression-status", "truthfulness", metrics)

    assert not result.passed
    assert "verified_with_unknown_regression_status" in result.violations
