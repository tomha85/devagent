from __future__ import annotations

import pytest

from devagent.models import AgentState, RiskLevel, TaskType
from devagent.state_machine import InvalidTransition, Lifecycle
from devagent.tasking import compile_task


@pytest.mark.parametrize(
    ("requirement", "task_type"),
    [
        ("Fix login bug", TaskType.BUG_FIX),
        ("Add CSV export", TaskType.FEATURE),
        ("Runtime exception in worker", TaskType.RUNTIME_ERROR),
        ("Build fails on Linux", TaskType.BUILD_FAILURE),
        ("Refactor parser", TaskType.REFACTOR),
        ("Optimize slow query", TaskType.PERFORMANCE),
    ],
)
def test_task_classification(requirement: str, task_type: TaskType) -> None:
    assert compile_task(requirement).task_type is task_type


def test_acceptance_criteria_and_high_risk_are_compiled() -> None:
    spec = compile_task("Fix authorization bypass and add regression tests")
    assert spec.risk is RiskLevel.HIGH
    criteria = " ".join(item.description for item in spec.acceptance_criteria)
    assert "root cause" in criteria
    assert "Regression" in criteria
    assert "review" in criteria


def test_state_machine_allows_diagnosis_correction_but_rejects_wandering() -> None:
    lifecycle = Lifecycle(AgentState.VERIFY_TARGETED, [AgentState.VERIFY_TARGETED])
    lifecycle.transition(AgentState.DIAGNOSE)
    lifecycle.transition(AgentState.IMPLEMENT)
    with pytest.raises(InvalidTransition):
        lifecycle.transition(AgentState.REPORT)

