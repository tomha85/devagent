from __future__ import annotations

from devagent.models import (
    AcceptanceCriterion,
    AcceptanceSource,
    AcceptanceStatus,
    Capability,
    CapabilityProvenance,
    ChangeMetrics,
    CodeSymbol,
    Component,
    DeveloperReviewEvidence,
    Evidence,
    RepositoryModel,
    ReviewDecision,
    TaskSpec,
    TaskType,
    Understanding,
    VerificationResult,
)
from devagent.orchestrator import _support_acceptance_criteria
from devagent.tasking import compile_task, enrich_acceptance_contract


def _result(command: tuple[str, ...], *, baseline: bool = False, tests: int | None = None) -> VerificationResult:
    return VerificationResult(
        command=command,
        exit_code=0,
        duration_seconds=0.1,
        stdout="passed",
        stderr="",
        classification=None,
        revision=1,
        phase="baseline" if baseline else "final",
        baseline=baseline,
        tests_run=tests,
        tests_passed=tests,
    )


def _repo() -> RepositoryModel:
    return RepositoryModel(
        root="/repo",
        kind="single-component",
        components=[
            Component(
                path=".",
                languages=["python"],
                capabilities=[
                    Capability(
                        kind="test",
                        command=("python", "-m", "pytest", "-q"),
                        source="pyproject.toml",
                        provenance=CapabilityProvenance.EXPLICIT,
                    )
                ],
            )
        ],
        facts=[],
        git_head="abc",
    )


def _understanding() -> Understanding:
    return Understanding(
        problem="A requested calculator behavior is missing.",
        expected_behavior="Implement the requested behavior.",
        affected_paths=["calculator.py", "test_calculator.py"],
        root_cause="The function is not implemented yet.",
        evidence=[Evidence("calculator.py contains the current implementation.", ("calculator.py",), 1.0)],
        proposed_solution=["Implement the requested function and tests."],
        confidence=0.99,
    )


def test_structured_requirement_compiles_atomic_user_acceptance_items() -> None:
    spec = compile_task(
        """Goal:
Add average support.

Requirements:
- Add average(values).
- Preserve existing divide behavior.

Acceptance Criteria:
- Empty input raises ValueError.
- Decimal values are supported.
"""
    )
    user = [item for item in spec.acceptance_criteria if item.source is AcceptanceSource.USER]
    assert [item.description for item in user] == [
        "Add average(values)",
        "Preserve existing divide behavior",
        "Empty input raises ValueError",
        "Decimal values are supported",
    ]
    assert all(item.status is AcceptanceStatus.UNPROVEN for item in user)


def test_migration_is_high_risk_and_requires_verification_contract() -> None:
    spec = compile_task("Migrate users from integer IDs to UUIDs")
    assert spec.task_type is TaskType.MIGRATION
    assert spec.risk.value == "HIGH"
    assert spec.requires_tests is True
    policy = {item.description for item in spec.acceptance_criteria if item.source is AcceptanceSource.TASK_POLICY}
    assert "Migration preserves compatibility with the current supported application contract" in policy
    assert "Migration has an explicit forward and rollback or safe non-reversible strategy" in policy
    assert "Migration behavior is covered against representative existing state" in policy


def test_refactor_requires_tests_and_behavior_preservation() -> None:
    spec = compile_task("Refactor parser internals")
    assert spec.task_type is TaskType.REFACTOR
    assert spec.requires_tests is True
    assert any("Externally observable behavior" in item.description for item in spec.acceptance_criteria)
    assert any("Regression coverage protects" in item.description for item in spec.acceptance_criteria)


def test_repository_contract_adds_only_trusted_broad_checks() -> None:
    repo = _repo()
    repo.components[0].capabilities.append(
        Capability(
            kind="lint",
            command=("ruff", "check", "."),
            source="pyproject.toml",
            broad=True,
            provenance=CapabilityProvenance.EXPLICIT,
        )
    )
    spec = enrich_acceptance_contract(compile_task("Add average(values)"), repo)
    derived = [item for item in spec.acceptance_criteria if item.source is AcceptanceSource.REPOSITORY]
    assert len(derived) == 1
    assert derived[0].verification_command == ("ruff", "check", ".")


def test_passing_tests_do_not_blanket_prove_unrelated_user_requirement() -> None:
    task = TaskSpec(
        task_type=TaskType.FEATURE,
        goal="Preserve existing multiply behavior",
        requires_code_change=True,
        requires_tests=True,
        acceptance_criteria=[
            AcceptanceCriterion("Preserve existing multiply behavior", source=AcceptanceSource.USER)
        ],
        risk=compile_task("Add feature").risk,
    )
    tests = _result(("python", "-m", "pytest", "-q"), tests=5)
    diff_check = _result(("git", "diff", "--check"))
    review_evidence = DeveloperReviewEvidence(
        changed_symbols=[CodeSymbol("calculator.py", "average", "function", 5, "ADDED")],
        test_cases=[CodeSymbol("test_calculator.py", "test_divide", "function", 4, "UNCHANGED")],
        test_files=["test_calculator.py"],
    )
    _support_acceptance_criteria(
        task,
        _repo(),
        _understanding(),
        ChangeMetrics(2, 8, 0, ["calculator.py", "test_calculator.py"]),
        [tests, diff_check],
        [tests, diff_check],
        ReviewDecision(True, [], "approved"),
        review_evidence,
        ["Add average support"],
        "+def average(values):\n+    return sum(values) / len(values)\n",
    )
    criterion = task.acceptance_criteria[0]
    assert criterion.status is AcceptanceStatus.UNPROVEN
    assert criterion.evidence == []
    assert "matching regression-test" in (criterion.reason or "")


def test_semantically_linked_user_behavior_can_be_satisfied() -> None:
    task = TaskSpec(
        task_type=TaskType.FEATURE,
        goal="Add average(values). Support negative values.",
        requires_code_change=True,
        requires_tests=True,
        acceptance_criteria=[
            AcceptanceCriterion("Add average(values)", source=AcceptanceSource.USER),
            AcceptanceCriterion("Support negative values", source=AcceptanceSource.USER),
        ],
        risk=compile_task("Add feature").risk,
    )
    tests = _result(("python", "-m", "pytest", "-q"), tests=5)
    diff_check = _result(("git", "diff", "--check"))
    review_evidence = DeveloperReviewEvidence(
        changed_symbols=[CodeSymbol("calculator.py", "average", "function", 5, "ADDED")],
        test_cases=[CodeSymbol("test_calculator.py", "test_average_negative_values", "function", 8, "ADDED")],
        test_files=["test_calculator.py"],
    )
    _support_acceptance_criteria(
        task,
        _repo(),
        _understanding(),
        ChangeMetrics(2, 8, 0, ["calculator.py", "test_calculator.py"]),
        [tests, diff_check],
        [tests, diff_check],
        ReviewDecision(True, [], "approved"),
        review_evidence,
        ["Add average(values) and negative-value coverage"],
        "+def average(values):\n+    return sum(values) / len(values)\n",
    )
    assert [item.status for item in task.acceptance_criteria] == [
        AcceptanceStatus.SATISFIED,
        AcceptanceStatus.SATISFIED,
    ]


def test_exact_quoted_contract_must_appear_in_final_diff() -> None:
    task = TaskSpec(
        task_type=TaskType.FEATURE,
        goal='Empty values raise ValueError with "average requires at least one value"',
        requires_code_change=True,
        requires_tests=True,
        acceptance_criteria=[
            AcceptanceCriterion(
                'Empty values raise ValueError with "average requires at least one value"',
                source=AcceptanceSource.USER,
            )
        ],
        risk=compile_task("Add feature").risk,
    )
    tests = _result(("python", "-m", "pytest", "-q"), tests=5)
    review_evidence = DeveloperReviewEvidence(
        test_cases=[CodeSymbol("test_calculator.py", "test_average_empty_values", "function", 8, "ADDED")],
        test_files=["test_calculator.py"],
    )
    _support_acceptance_criteria(
        task,
        _repo(),
        _understanding(),
        ChangeMetrics(1, 4, 0, ["test_calculator.py"]),
        [tests],
        [tests],
        ReviewDecision(True, [], "approved"),
        review_evidence,
        ["Handle empty values"],
        "+raise ValueError(\"different message\")\n",
    )
    assert task.acceptance_criteria[0].status is AcceptanceStatus.UNPROVEN
    assert "Exact quoted user contract" in (task.acceptance_criteria[0].reason or "")


def test_generic_regression_verification_intent_uses_changed_tests_and_review() -> None:
    task = TaskSpec(
        task_type=TaskType.BUG_FIX,
        goal="Add a regression test and verify the application",
        requires_code_change=True,
        requires_tests=True,
        acceptance_criteria=[
            AcceptanceCriterion(
                "Add a regression test and verify the application",
                source=AcceptanceSource.USER,
            )
        ],
        risk=compile_task("Fix bug").risk,
    )
    baseline = _result(("python", "-m", "pytest", "-q"), baseline=True, tests=1)
    final = _result(("python", "-m", "pytest", "-q"), tests=2)
    review_evidence = DeveloperReviewEvidence(
        test_cases=[CodeSymbol("test_calculator.py", "test_divide_by_zero", "function", 8, "ADDED")],
        test_files=["test_calculator.py"],
    )
    _support_acceptance_criteria(
        task,
        _repo(),
        _understanding(),
        ChangeMetrics(1, 3, 0, ["test_calculator.py"]),
        [baseline, final],
        [final],
        ReviewDecision(True, [], "approved"),
        review_evidence,
        ["Added regression coverage"],
        "+def test_divide_by_zero():\n+    assert divide(10, 0) is None\n",
    )
    assert task.acceptance_criteria[0].status is AcceptanceStatus.SATISFIED


def test_without_changing_preservation_requires_baseline_and_final_evidence() -> None:
    task = TaskSpec(
        task_type=TaskType.BUG_FIX,
        goal="Handle division by zero safely without changing normal division behavior",
        requires_code_change=True,
        requires_tests=True,
        acceptance_criteria=[
            AcceptanceCriterion(
                "Handle division by zero safely without changing normal division behavior",
                source=AcceptanceSource.USER,
            )
        ],
        risk=compile_task("Fix division bug").risk,
    )
    final = _result(("python", "-m", "pytest", "-q"), tests=2)
    review_evidence = DeveloperReviewEvidence(
        changed_symbols=[CodeSymbol("calculator.py", "divide", "function", 1, "MODIFIED")],
        test_cases=[CodeSymbol("test_calculator.py", "test_divide_by_zero", "function", 8, "ADDED")],
        test_files=["test_calculator.py"],
    )
    common = dict(
        task=task,
        repository=_repo(),
        understanding=_understanding(),
        changes=ChangeMetrics(2, 5, 1, ["calculator.py", "test_calculator.py"]),
        final_results=[final],
        review=ReviewDecision(True, [], "approved"),
        developer_review=review_evidence,
        implementation=["Handle zero divisor while preserving normal division"],
        diff_text="+    if b == 0:\n+        return None\n",
    )
    _support_acceptance_criteria(verification=[final], **common)
    assert task.acceptance_criteria[0].status is AcceptanceStatus.UNPROVEN
    assert "baseline test evidence" in (task.acceptance_criteria[0].reason or "")

    baseline = _result(("python", "-m", "pytest", "-q"), baseline=True, tests=1)
    _support_acceptance_criteria(verification=[baseline, final], **common)
    assert task.acceptance_criteria[0].status is AcceptanceStatus.SATISFIED
