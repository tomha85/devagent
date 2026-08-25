from __future__ import annotations

import re
from typing import Any

from devagent.models import (
    AcceptanceCriterion,
    AcceptanceSource,
    RiskLevel,
    TaskSpec,
    TaskType,
)


_CLASSIFIERS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    (TaskType.BUILD_FAILURE, ("build fail", "compile error", "won't build", "does not build")),
    (TaskType.TEST_FAILURE, ("test fail", "failing test", "pytest error")),
    (TaskType.RUNTIME_ERROR, ("traceback", "exception", "runtime error", "crash")),
    (TaskType.MIGRATION, ("migration", "migrate ", "schema change", "alembic", "database migration")),
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
    "migrate",
    "schema",
    "crypto",
    "credential",
    "public api",
    "concurrency",
}

_REQUIREMENT_SECTIONS = {"requirements", "required changes", "acceptance criteria", "acceptance"}
_DIRECTIVE = re.compile(
    r"^(?:add|implement|support|preserve|keep|ensure|require|must|should|when|do not|don't|"
    r"verify|run|return|raise|allow|prevent|maintain|migrate|refactor|update|fix|handle)\b",
    re.IGNORECASE,
)


def _classify(text: str) -> TaskType:
    lowered = text.lower()
    for task_type, needles in _CLASSIFIERS:
        if any(needle in lowered for needle in needles):
            return task_type
    return TaskType.GENERAL_ENGINEERING_TASK


def _risk(text: str, task_type: TaskType) -> RiskLevel:
    lowered = text.lower()
    if task_type is TaskType.MIGRATION or any(term in lowered for term in _HIGH_RISK):
        return RiskLevel.HIGH
    if task_type in {TaskType.FEATURE, TaskType.PERFORMANCE, TaskType.REFACTOR}:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _clean_requirement_item(value: str) -> str:
    value = re.sub(r"^[-*+]\s+", "", value.strip())
    value = re.sub(r"^\d+[.)]\s+", "", value)
    return re.sub(r"\s+", " ", value).strip().rstrip(".;")


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = re.sub(r"\W+", " ", item.lower()).strip()
        if item and normalized not in seen:
            result.append(item)
            seen.add(normalized)
    return result


def _user_acceptance_items(requirement: str) -> list[str]:
    lines = requirement.splitlines()
    explicit: list[str] = []
    active = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        heading = stripped.rstrip(":").strip().lower()
        if stripped.endswith(":"):
            active = heading in _REQUIREMENT_SECTIONS
            continue
        if active and re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped):
            explicit.append(_clean_requirement_item(stripped))
    explicit = _dedupe(explicit)
    if explicit:
        return explicit[:24]

    candidates = re.split(r"(?<=[.!?;])\s+|\n+", requirement)
    directives: list[str] = []
    for candidate in candidates:
        item = _clean_requirement_item(candidate)
        item = re.sub(r"^(?:goal|requirement|task)\s*:\s*", "", item, flags=re.IGNORECASE)
        if not item:
            continue
        if _DIRECTIVE.match(item) or re.search(r"\b(?:must|should|shall)\b", item, re.IGNORECASE):
            directives.append(item)
    directives = _dedupe(directives)
    if directives:
        return directives[:24]
    return [re.sub(r"\s+", " ", requirement).strip()]


def _append_criterion(
    criteria: list[AcceptanceCriterion],
    description: str,
    *,
    source: AcceptanceSource,
    required: bool = True,
    verification_command: tuple[str, ...] | None = None,
) -> None:
    normalized = re.sub(r"\W+", " ", description.lower()).strip()
    if any(re.sub(r"\W+", " ", item.description.lower()).strip() == normalized for item in criteria):
        return
    criteria.append(
        AcceptanceCriterion(
            description=description,
            required=required,
            source=source,
            verification_command=verification_command,
        )
    )


def compile_task(requirement: str) -> TaskSpec:
    goal = re.sub(r"\s+", " ", requirement).strip()
    if not goal:
        raise ValueError("Engineering requirement cannot be empty")
    task_type = _classify(goal)
    code_change = task_type is not TaskType.UNIT_TEST or "only" not in goal.lower()
    requires_tests = task_type is not TaskType.BUILD_FAILURE

    criteria: list[AcceptanceCriterion] = []
    for item in _user_acceptance_items(requirement):
        _append_criterion(criteria, item, source=AcceptanceSource.USER)

    if task_type in {TaskType.BUG_FIX, TaskType.RUNTIME_ERROR, TaskType.TEST_FAILURE}:
        _append_criterion(criteria, "The root cause is addressed, not merely masked", source=AcceptanceSource.TASK_POLICY)
        _append_criterion(criteria, "Regression coverage exercises the failing case", source=AcceptanceSource.TASK_POLICY)
    elif task_type is TaskType.FEATURE:
        _append_criterion(criteria, "The feature follows existing repository conventions", source=AcceptanceSource.TASK_POLICY)
        _append_criterion(criteria, "Normal and relevant edge-case behavior are covered", source=AcceptanceSource.TASK_POLICY)
    elif task_type is TaskType.REFACTOR:
        _append_criterion(
            criteria,
            "Externally observable behavior remains unchanged unless explicitly requested",
            source=AcceptanceSource.TASK_POLICY,
        )
        _append_criterion(criteria, "Regression coverage protects the refactored behavior", source=AcceptanceSource.TASK_POLICY)
    elif task_type is TaskType.MIGRATION:
        _append_criterion(
            criteria,
            "Migration preserves compatibility with the current supported application contract",
            source=AcceptanceSource.TASK_POLICY,
        )
        _append_criterion(
            criteria,
            "Migration has an explicit forward and rollback or safe non-reversible strategy",
            source=AcceptanceSource.TASK_POLICY,
        )
        _append_criterion(
            criteria,
            "Migration behavior is covered against representative existing state",
            source=AcceptanceSource.TASK_POLICY,
        )
    elif task_type is TaskType.UNIT_TEST:
        _append_criterion(criteria, "Tests assert externally meaningful behavior", source=AcceptanceSource.TASK_POLICY)

    _append_criterion(criteria, "No unrelated behavior changes", source=AcceptanceSource.QUALITY_GATE)
    if requires_tests:
        _append_criterion(criteria, "Relevant automated tests pass", source=AcceptanceSource.QUALITY_GATE)
    _append_criterion(criteria, "Final diff and independent review pass", source=AcceptanceSource.QUALITY_GATE)

    return TaskSpec(
        task_type=task_type,
        goal=goal,
        requires_code_change=code_change,
        requires_tests=requires_tests,
        acceptance_criteria=criteria,
        risk=_risk(goal, task_type),
    )


def enrich_acceptance_contract(task: TaskSpec, repository: Any) -> TaskSpec:
    """Add checks derived from trusted repository capabilities without replacing user intent."""

    seen_commands: set[tuple[str, ...]] = set()
    for capability in repository.capabilities:
        if not capability.trusted:
            continue
        if not (capability.broad or capability.kind in {"build", "lint", "typecheck", "integration"}):
            continue
        if capability.command in seen_commands:
            continue
        seen_commands.add(capability.command)
        _append_criterion(
            task.acceptance_criteria,
            f"Repository-supported {capability.kind} check passes on the final revision",
            source=AcceptanceSource.REPOSITORY,
            verification_command=capability.command,
        )
    return task
