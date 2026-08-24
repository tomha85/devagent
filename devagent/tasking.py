from __future__ import annotations

import re

from devagent.models import (
    AcceptanceCriterion,
    RiskLevel,
    TaskSpec,
    TaskType,
)


_CLASSIFIERS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    (TaskType.BUILD_FAILURE, ("build fail", "compile error", "won't build", "does not build")),
    (TaskType.TEST_FAILURE, ("test fail", "failing test", "pytest error")),
    (TaskType.RUNTIME_ERROR, ("traceback", "exception", "runtime error", "crash")),
    (TaskType.PERFORMANCE, ("performance", "optimize", "slow", "latency", "n+1")),
    (TaskType.REFACTOR, ("refactor", "restructure", "cleanup")),
    (TaskType.UNIT_TEST, ("add unit test", "write tests", "test coverage")),
    (TaskType.BUG_FIX, ("fix", "bug", "incorrect", "broken", "regression")),
    (TaskType.FEATURE, ("add ", "implement", "support ", "feature")),
)

_HIGH_RISK = {
    "auth",
    "authentication",
    "authorization",
    "permission",
    "payment",
    "migration",
    "crypto",
    "credential",
    "public api",
    "concurrency",
}


def _classify(text: str) -> TaskType:
    lowered = text.lower()
    for task_type, needles in _CLASSIFIERS:
        if any(needle in lowered for needle in needles):
            return task_type
    return TaskType.GENERAL_ENGINEERING_TASK


def _risk(text: str, task_type: TaskType) -> RiskLevel:
    lowered = text.lower()
    if any(term in lowered for term in _HIGH_RISK):
        return RiskLevel.HIGH
    if task_type in {TaskType.FEATURE, TaskType.PERFORMANCE, TaskType.REFACTOR}:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def compile_task(requirement: str) -> TaskSpec:
    goal = re.sub(r"\s+", " ", requirement).strip()
    if not goal:
        raise ValueError("Engineering requirement cannot be empty")
    task_type = _classify(goal)
    code_change = task_type is not TaskType.UNIT_TEST or "only" not in goal.lower()
    requires_tests = task_type not in {TaskType.BUILD_FAILURE, TaskType.REFACTOR}

    criteria = [AcceptanceCriterion(goal), AcceptanceCriterion("No unrelated behavior changes")]
    if task_type in {TaskType.BUG_FIX, TaskType.RUNTIME_ERROR, TaskType.TEST_FAILURE}:
        criteria.insert(1, AcceptanceCriterion("The root cause is addressed, not merely masked"))
        criteria.insert(2, AcceptanceCriterion("Regression coverage exercises the failing case"))
    elif task_type is TaskType.FEATURE:
        criteria.insert(1, AcceptanceCriterion("The feature follows existing repository conventions"))
        criteria.insert(2, AcceptanceCriterion("Normal and relevant edge-case behavior are covered"))
    elif task_type is TaskType.UNIT_TEST:
        criteria.insert(1, AcceptanceCriterion("Tests assert externally meaningful behavior"))
    if requires_tests:
        criteria.append(AcceptanceCriterion("Relevant automated tests pass"))
    criteria.append(AcceptanceCriterion("Final diff and independent review pass"))

    return TaskSpec(
        task_type=task_type,
        goal=goal,
        requires_code_change=code_change,
        requires_tests=requires_tests,
        acceptance_criteria=criteria,
        risk=_risk(goal, task_type),
    )
