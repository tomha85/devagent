from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from devagent.models import Outcome, RunResult, jsonable
from devagent.orchestrator import DevAgent
from devagent.providers import ModelProvider


@dataclass(frozen=True)
class EvaluationMetrics:
    task_success: bool
    acceptance_criteria_supported: int
    acceptance_criteria_total: int
    acceptance_coverage: float
    new_regressions: int | None
    files_changed: int
    lines_changed: int
    iterations: int
    model_calls: int
    tool_calls: int
    runtime_seconds: float
    outcome: Outcome
    final_verification_passed: bool
    review_approved: bool
    source_head_unchanged: bool
    source_status_unchanged: bool


@dataclass(frozen=True)
class EvaluationExpectation:
    expected_outcomes: tuple[Outcome, ...] = (Outcome.VERIFIED,)
    max_files_changed: int | None = None
    max_lines_changed: int | None = None
    require_acceptance_evidence: bool = True
    require_review_approval: bool = True
    require_final_verification: bool = True
    require_no_new_regressions: bool = True
    require_source_unchanged: bool = True

    def __post_init__(self) -> None:
        if not self.expected_outcomes:
            raise ValueError("expected_outcomes must contain at least one outcome")
        if self.max_files_changed is not None and self.max_files_changed < 0:
            raise ValueError("max_files_changed must be >= 0")
        if self.max_lines_changed is not None and self.max_lines_changed < 0:
            raise ValueError("max_lines_changed must be >= 0")


@dataclass(frozen=True)
class EvaluationCaseResult:
    name: str
    category: str
    expected_outcomes: tuple[Outcome, ...]
    metrics: EvaluationMetrics
    passed: bool
    false_verified: bool
    unexpected_blocked: bool
    violations: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationSuiteMetrics:
    cases_total: int
    cases_passed: int
    pass_rate: float
    verified: int
    partially_verified: int
    blocked: int
    false_verified: int
    false_verified_rate: float
    unexpected_blocked: int
    acceptance_criteria_supported: int
    acceptance_criteria_total: int
    acceptance_coverage: float
    total_model_calls: int
    total_tool_calls: int
    total_runtime_seconds: float


@dataclass(frozen=True)
class _RepositorySnapshot:
    head: str | None
    status: tuple[str, ...]


class _CountingProvider:
    def __init__(self, inner: ModelProvider) -> None:
        self.inner = inner
        self.calls = 0

    def request(
        self,
        *,
        role: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        return self.inner.request(role=role, payload=payload, schema=schema)


def _generated_status_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip('"')
    parts = tuple(part for part in normalized.split("/") if part)
    return (
        normalized == ".devagent"
        or normalized.startswith(".devagent/")
        or normalized.endswith(".pyc")
        or any(
            part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
            for part in parts
        )
    )


def _repository_snapshot(repository: Path) -> _RepositorySnapshot:
    repository = repository.resolve()
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _RepositorySnapshot(None, ())

    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    status: list[str] = []
    if status_result.returncode == 0:
        for line in status_result.stdout.splitlines():
            rendered = line.rstrip()
            if not rendered:
                continue
            path = rendered[3:] if len(rendered) >= 4 else rendered
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if _generated_status_path(path):
                continue
            status.append(rendered)
    return _RepositorySnapshot(head, tuple(sorted(status)))


def _tool_call_count(run_dir: str) -> int:
    observations = Path(run_dir) / "observations.jsonl"
    if not observations.is_file():
        return 0
    count = 0
    for line in observations.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line).get("event")
        except json.JSONDecodeError:
            continue
        if event in {"command_finished", "file_written", "text_replaced"}:
            count += 1
    return count


def _final_verification_passed(result: RunResult) -> bool:
    final = [item for item in result.verification if item.phase == "final"]
    return bool(final) and all(item.passed for item in final)


def _known_new_regressions(result: RunResult) -> int | None:
    """Count only regressions supported by comparable baseline/final evidence."""
    baseline_by_command = {
        item.command: item.passed
        for item in result.verification
        if item.baseline or item.phase == "baseline"
    }
    final = [item for item in result.verification if item.phase == "final"]
    if not final:
        return None

    regressions = 0
    unknown_failure = False
    for item in final:
        if item.passed:
            continue
        baseline_passed = baseline_by_command.get(item.command)
        if baseline_passed is True:
            regressions += 1
        elif baseline_passed is None:
            unknown_failure = True

    if unknown_failure and regressions == 0:
        return None
    return regressions


def evaluate(
    repository: Path,
    requirement: str,
    provider: ModelProvider,
    *,
    isolate: bool = True,
) -> tuple[RunResult, EvaluationMetrics]:
    """Run one evidence-backed evaluation and capture deterministic production metrics."""
    repository = repository.resolve()
    source_before = _repository_snapshot(repository)
    counted_provider = _CountingProvider(provider)
    started = time.monotonic()
    result = DevAgent(counted_provider, isolate=isolate).run(repository, requirement)
    runtime = time.monotonic() - started
    source_after = _repository_snapshot(repository)

    supported = sum(bool(criterion.evidence) for criterion in result.task.acceptance_criteria)
    total = len(result.task.acceptance_criteria)
    coverage = supported / total if total else 1.0
    metrics = EvaluationMetrics(
        task_success=result.outcome is Outcome.VERIFIED,
        acceptance_criteria_supported=supported,
        acceptance_criteria_total=total,
        acceptance_coverage=coverage,
        new_regressions=_known_new_regressions(result),
        files_changed=result.changes.files_changed,
        lines_changed=result.changes.lines_added + result.changes.lines_deleted,
        iterations=sum(
            state.value in {"IMPLEMENT", "DIAGNOSE"}
            for state in result.state_history
        ),
        model_calls=counted_provider.calls,
        tool_calls=_tool_call_count(result.run_dir),
        runtime_seconds=runtime,
        outcome=result.outcome,
        final_verification_passed=_final_verification_passed(result),
        review_approved=bool(result.review and result.review.approved),
        source_head_unchanged=source_before.head == source_after.head,
        source_status_unchanged=source_before.status == source_after.status,
    )
    return result, metrics


def score_evaluation(
    name: str,
    category: str,
    metrics: EvaluationMetrics,
    expectation: EvaluationExpectation | None = None,
) -> EvaluationCaseResult:
    """Score a run without allowing model prose to determine benchmark success."""
    expected = expectation or EvaluationExpectation()
    violations: list[str] = []
    if metrics.outcome not in expected.expected_outcomes:
        allowed = ",".join(item.value for item in expected.expected_outcomes)
        violations.append(f"unexpected_outcome:{metrics.outcome.value}:expected={allowed}")

    false_verified = (
        metrics.outcome is Outcome.VERIFIED
        and Outcome.VERIFIED not in expected.expected_outcomes
    )
    unexpected_blocked = (
        metrics.outcome is Outcome.BLOCKED
        and Outcome.BLOCKED not in expected.expected_outcomes
    )

    if metrics.outcome is Outcome.VERIFIED:
        if (
            expected.require_acceptance_evidence
            and metrics.acceptance_criteria_supported < metrics.acceptance_criteria_total
        ):
            violations.append("verified_without_complete_acceptance_evidence")
        if expected.require_review_approval and not metrics.review_approved:
            violations.append("verified_without_approved_review")
        if expected.require_final_verification and not metrics.final_verification_passed:
            violations.append("verified_without_final_verification")
        if expected.require_no_new_regressions and metrics.new_regressions is None:
            violations.append("verified_with_unknown_regression_status")

    if (
        expected.require_no_new_regressions
        and metrics.new_regressions not in {None, 0}
    ):
        violations.append(f"new_regressions:{metrics.new_regressions}")
    if expected.require_source_unchanged and not metrics.source_head_unchanged:
        violations.append("source_head_changed")
    if expected.require_source_unchanged and not metrics.source_status_unchanged:
        violations.append("source_status_changed")
    if (
        expected.max_files_changed is not None
        and metrics.files_changed > expected.max_files_changed
    ):
        violations.append(
            f"files_changed:{metrics.files_changed}:max={expected.max_files_changed}"
        )
    if (
        expected.max_lines_changed is not None
        and metrics.lines_changed > expected.max_lines_changed
    ):
        violations.append(
            f"lines_changed:{metrics.lines_changed}:max={expected.max_lines_changed}"
        )

    return EvaluationCaseResult(
        name=name,
        category=category,
        expected_outcomes=expected.expected_outcomes,
        metrics=metrics,
        passed=not violations,
        false_verified=false_verified,
        unexpected_blocked=unexpected_blocked,
        violations=tuple(violations),
    )


def evaluate_case(
    name: str,
    category: str,
    repository: Path,
    requirement: str,
    provider: ModelProvider,
    *,
    expectation: EvaluationExpectation | None = None,
    isolate: bool = True,
) -> tuple[RunResult, EvaluationCaseResult]:
    result, metrics = evaluate(
        repository,
        requirement,
        provider,
        isolate=isolate,
    )
    return result, score_evaluation(name, category, metrics, expectation)


def aggregate_results(results: Iterable[EvaluationCaseResult]) -> EvaluationSuiteMetrics:
    items = list(results)
    total = len(items)
    passed = sum(item.passed for item in items)
    verified = sum(item.metrics.outcome is Outcome.VERIFIED for item in items)
    partially_verified = sum(
        item.metrics.outcome is Outcome.PARTIALLY_VERIFIED for item in items
    )
    blocked = sum(item.metrics.outcome is Outcome.BLOCKED for item in items)
    false_verified = sum(item.false_verified for item in items)
    supported = sum(item.metrics.acceptance_criteria_supported for item in items)
    criteria_total = sum(item.metrics.acceptance_criteria_total for item in items)
    return EvaluationSuiteMetrics(
        cases_total=total,
        cases_passed=passed,
        pass_rate=passed / total if total else 1.0,
        verified=verified,
        partially_verified=partially_verified,
        blocked=blocked,
        false_verified=false_verified,
        false_verified_rate=false_verified / total if total else 0.0,
        unexpected_blocked=sum(item.unexpected_blocked for item in items),
        acceptance_criteria_supported=supported,
        acceptance_criteria_total=criteria_total,
        acceptance_coverage=supported / criteria_total if criteria_total else 1.0,
        total_model_calls=sum(item.metrics.model_calls for item in items),
        total_tool_calls=sum(item.metrics.tool_calls for item in items),
        total_runtime_seconds=sum(item.metrics.runtime_seconds for item in items),
    )


def write_suite_report(
    path: Path,
    results: Iterable[EvaluationCaseResult],
) -> EvaluationSuiteMetrics:
    """Write a machine-readable benchmark report suitable for CI artifacts."""
    items = list(results)
    summary = aggregate_results(items)
    payload = {
        "schema_version": 1,
        "summary": jsonable(summary),
        "cases": jsonable(items),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary
