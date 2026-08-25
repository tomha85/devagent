from __future__ import annotations

from devagent.models import (
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
    TaskType,
    Understanding,
    VerificationResult,
)
from devagent.orchestrator import _support_acceptance_criteria
from devagent.tasking import compile_task, enrich_acceptance_contract


def _repo(language: str = "python") -> RepositoryModel:
    return RepositoryModel(
        root="/repo",
        kind="single-component",
        components=[
            Component(
                path=".",
                languages=[language],
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


def _result(command: tuple[str, ...], *, tests: int | None = None) -> VerificationResult:
    return VerificationResult(
        command=command,
        exit_code=0,
        duration_seconds=0.1,
        stdout="passed",
        stderr="",
        classification=None,
        revision=2,
        phase="final",
        tests_run=tests,
        tests_passed=tests,
    )


def test_rough_matrix_prompt_becomes_precise_python_contract() -> None:
    task = compile_task("add new function addition 2 matrix 2x2")
    assert task.task_type is TaskType.FEATURE
    assert task.goal == "Add a matrix addition function for two 2x2 matrices (matrix inputs)"

    enrich_acceptance_contract(task, _repo())
    assert task.goal == (
        "Add add_matrices_2x2(a, b) to perform element-wise matrix addition "
        "for two 2x2 matrices"
    )
    user = [item for item in task.acceptance_criteria if item.source is AcceptanceSource.USER]
    assert [item.description for item in user] == [task.goal]


def test_requirement_compiler_preserves_explicit_user_callable() -> None:
    requirement = "Add matrix_sum(a, b) for two 2x2 matrices"
    task = compile_task(requirement)
    enrich_acceptance_contract(task, _repo())
    assert task.goal == requirement
    user = [item for item in task.acceptance_criteria if item.source is AcceptanceSource.USER]
    assert [item.description for item in user] == [requirement]


def test_common_typo_is_compiled_without_inventing_behavior() -> None:
    task = compile_task("add new function substraction 2 matrix 2x2")
    enrich_acceptance_contract(task, _repo())
    assert task.goal == (
        "Add subtract_matrices_2x2(a, b) to perform element-wise matrix subtraction "
        "for two 2x2 matrices"
    )
    assert "mutation" not in task.goal.lower()
    assert "invalid" not in task.goal.lower()


def test_repository_language_selects_conventional_java_callable() -> None:
    task = compile_task("add new function addition 2 matrix 2x2")
    enrich_acceptance_contract(task, _repo("java"))
    assert "addMatrices2x2(a, b)" in task.goal


def test_compiled_matrix_contract_links_to_exact_final_symbol_and_tests() -> None:
    task = compile_task("add new function addition 2 matrix 2x2")
    repository = _repo()
    enrich_acceptance_contract(task, repository)

    pytest_result = _result(("python", "-m", "pytest", "-q"), tests=3)
    diff_check = _result(("git", "diff", "--check"))
    understanding = Understanding(
        problem="Matrix addition is missing.",
        expected_behavior="Add two 2x2 matrices element by element.",
        affected_paths=["calculator.py", "test_calculator.py"],
        root_cause="No matrix addition function exists.",
        evidence=[Evidence("calculator.py contains only existing calculator operations.", ("calculator.py",), 1.0)],
        proposed_solution=["Add the compiled matrix function and regression tests."],
        confidence=1.0,
    )
    review_evidence = DeveloperReviewEvidence(
        changed_symbols=[CodeSymbol("calculator.py", "add_matrices_2x2", "function", 5, "ADDED")],
        test_cases=[
            CodeSymbol("test_calculator.py", "test_add_matrices_2x2", "function", 8, "ADDED"),
            CodeSymbol("test_calculator.py", "test_add_matrices_2x2_negative", "function", 12, "ADDED"),
        ],
        test_files=["test_calculator.py"],
    )
    _support_acceptance_criteria(
        task,
        repository,
        understanding,
        ChangeMetrics(2, 20, 0, ["calculator.py", "test_calculator.py"]),
        [pytest_result, diff_check],
        [pytest_result, diff_check],
        ReviewDecision(True, [], "approved"),
        review_evidence,
        ["Added the compiled 2x2 matrix addition function and tests"],
        "+def add_matrices_2x2(a, b):\n+    return [[a[r][c] + b[r][c] for c in range(2)] for r in range(2)]\n",
    )

    required = [item for item in task.acceptance_criteria if item.required]
    assert required
    assert all(item.status is AcceptanceStatus.SATISFIED for item in required)
    user = [item for item in required if item.source is AcceptanceSource.USER]
    assert user[0].evidence
    assert any("add_matrices_2x2" in item for item in user[0].evidence)


def test_compiled_contract_does_not_accept_wrong_operation() -> None:
    task = compile_task("add new function addition 2 matrix 2x2")
    repository = _repo()
    enrich_acceptance_contract(task, repository)

    pytest_result = _result(("python", "-m", "pytest", "-q"), tests=3)
    diff_check = _result(("git", "diff", "--check"))
    understanding = Understanding(
        problem="Matrix addition is missing.",
        expected_behavior="Add two 2x2 matrices element by element.",
        affected_paths=["calculator.py", "test_calculator.py"],
        root_cause="No matrix addition function exists.",
        evidence=[Evidence("calculator.py contains the current calculator implementation.", ("calculator.py",), 1.0)],
        proposed_solution=["Add matrix behavior and tests."],
        confidence=1.0,
    )
    wrong_review_evidence = DeveloperReviewEvidence(
        changed_symbols=[CodeSymbol("calculator.py", "subtract_matrices_2x2", "function", 5, "ADDED")],
        test_cases=[
            CodeSymbol("test_calculator.py", "test_subtract_matrices_2x2", "function", 8, "ADDED"),
        ],
        test_files=["test_calculator.py"],
    )
    _support_acceptance_criteria(
        task,
        repository,
        understanding,
        ChangeMetrics(2, 12, 0, ["calculator.py", "test_calculator.py"]),
        [pytest_result, diff_check],
        [pytest_result, diff_check],
        ReviewDecision(True, [], "approved"),
        wrong_review_evidence,
        ["Added a matrix function and tests"],
        "+def subtract_matrices_2x2(a, b):\n+    return [[a[r][c] - b[r][c] for c in range(2)] for r in range(2)]\n",
    )

    user = [item for item in task.acceptance_criteria if item.source is AcceptanceSource.USER]
    assert user[0].status is AcceptanceStatus.UNPROVEN
    assert user[0].evidence == []
    assert "semantically matches" in (user[0].reason or "")
