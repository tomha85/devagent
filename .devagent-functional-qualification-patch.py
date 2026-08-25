from pathlib import Path

ROOT = Path(__file__).resolve().parent


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "devagent/evaluation.py",
    "from devagent.models import Outcome, RunResult, jsonable",
    "from devagent.models import AcceptanceStatus, Outcome, RunResult, jsonable",
)

replace(
    "devagent/evaluation.py",
    '''def _final_verification_passed(result: RunResult) -> bool:\n    final = [item for item in result.verification if item.phase == "final"]\n    return bool(final) and all(item.passed for item in final)\n\n\ndef _known_new_regressions(result: RunResult) -> int | None:\n''',
    '''def _final_verification_passed(result: RunResult) -> bool:\n    final = [item for item in result.verification if item.phase == "final"]\n    return bool(final) and all(item.passed for item in final)\n\n\ndef _acceptance_satisfaction(criteria: Iterable[Any]) -> tuple[int, int]:\n    required = [criterion for criterion in criteria if criterion.required]\n    satisfied = sum(\n        criterion.status is AcceptanceStatus.SATISFIED\n        for criterion in required\n    )\n    return satisfied, len(required)\n\n\ndef _known_new_regressions(result: RunResult) -> int | None:\n''',
)

replace(
    "devagent/evaluation.py",
    '''    supported = sum(bool(criterion.evidence) for criterion in result.task.acceptance_criteria)\n    total = len(result.task.acceptance_criteria)\n    coverage = supported / total if total else 1.0\n''',
    '''    supported, total = _acceptance_satisfaction(result.task.acceptance_criteria)\n    coverage = supported / total if total else 1.0\n''',
)

replace(
    "devagent/evaluation.py",
    '''    false_verified = (\n        metrics.outcome is Outcome.VERIFIED\n        and Outcome.VERIFIED not in expected.expected_outcomes\n    )\n    unexpected_blocked = (\n''',
    '''    false_verified = False\n    unexpected_blocked = (\n''',
)

replace(
    "devagent/evaluation.py",
    '''    if (\n        expected.max_lines_changed is not None\n        and metrics.lines_changed > expected.max_lines_changed\n    ):\n        violations.append(\n            f"lines_changed:{metrics.lines_changed}:max={expected.max_lines_changed}"\n        )\n\n    return EvaluationCaseResult(\n''',
    '''    if (\n        expected.max_lines_changed is not None\n        and metrics.lines_changed > expected.max_lines_changed\n    ):\n        violations.append(\n            f"lines_changed:{metrics.lines_changed}:max={expected.max_lines_changed}"\n        )\n\n    false_verified_markers = (\n        "verified_without_complete_acceptance_evidence",\n        "verified_without_approved_review",\n        "verified_without_final_verification",\n        "verified_with_unknown_regression_status",\n        "new_regressions:",\n        "source_head_changed",\n        "source_status_changed",\n    )\n    false_verified = metrics.outcome is Outcome.VERIFIED and (\n        Outcome.VERIFIED not in expected.expected_outcomes\n        or any(\n            violation == marker or violation.startswith(marker)\n            for violation in violations\n            for marker in false_verified_markers\n        )\n    )\n\n    return EvaluationCaseResult(\n''',
)

replace(
    "devagent/evaluation.py",
    '        "schema_version": 1,\n',
    '        "schema_version": 2,\n',
)

replace(
    "tests/test_evaluation_harness.py",
    '''    _repository_snapshot,\n    aggregate_results,\n''',
    '''    _acceptance_satisfaction,\n    _repository_snapshot,\n    aggregate_results,\n''',
)

replace(
    "tests/test_evaluation_harness.py",
    "from devagent.models import Outcome",
    "from devagent.models import AcceptanceCriterion, AcceptanceStatus, Outcome",
)

replace(
    "tests/test_evaluation_harness.py",
    '''    assert not result.passed\n    assert "verified_without_complete_acceptance_evidence" in result.violations\n    assert "verified_without_approved_review" in result.violations\n''',
    '''    assert not result.passed\n    assert result.false_verified\n    assert "verified_without_complete_acceptance_evidence" in result.violations\n    assert "verified_without_approved_review" in result.violations\n''',
)

replace(
    "tests/test_evaluation_harness.py",
    '    assert payload["schema_version"] == 1\n',
    '    assert payload["schema_version"] == 2\n',
)

anchor = '''def test_expectation_rejects_invalid_empty_or_negative_limits() -> None:\n'''
addition = '''def test_acceptance_metrics_use_explicit_status_not_nonempty_evidence() -> None:\n    contradicted = AcceptanceCriterion(\n        "Preserve existing multiply behavior",\n        status=AcceptanceStatus.CONTRADICTED,\n        evidence=["final tests mention multiply"],\n    )\n    satisfied = AcceptanceCriterion(\n        "Relevant tests pass",\n        status=AcceptanceStatus.SATISFIED,\n        evidence=["pytest passed"],\n    )\n\n    supported, total = _acceptance_satisfaction([contradicted, satisfied])\n\n    assert supported == 1\n    assert total == 2\n\n\n'''
replace("tests/test_evaluation_harness.py", anchor, addition + anchor)

replace(
    "evaluation/README.md",
    "- acceptance-criteria evidence coverage",
    "- required acceptance-criteria satisfaction coverage (explicit status, not non-empty evidence)",
)
replace(
    "evaluation/README.md",
    "`benchmark_v1.json` maps the first production invariants to executable pytest coverage already in the repository. The normal Production CI workflow runs these tests on Python 3.10, 3.11, and 3.12.",
    "`benchmark_v1.json` preserves the original seed benchmark. `benchmark_v2.json` is the functional qualification catalog: it requires coverage across end-to-end behavior, truthfulness, acceptance contracts, task scope, provider contracts, model routing, worktree safety, source-control safety, CLI input, review/repair loops, reporting, and evaluation integrity. Production CI runs the full suite on Python 3.10/3.11/3.12 and runs the v2 qualification catalog as a dedicated gate.",
)
replace(
    "evaluation/README.md",
    '''Run only the evaluation contract tests:\n\n```bash\npytest -q tests/test_evaluation_harness.py tests/test_e2e_fake_provider.py\n```\n''',
    '''Run only the evaluation contract tests:\n\n```bash\npytest -q tests/test_evaluation_harness.py tests/test_e2e_fake_provider.py\n```\n\nRun the functional qualification gate:\n\n```bash\npython -m devagent.qualification \\\n  --catalog evaluation/benchmark_v2.json \\\n  --report .devagent/functional-qualification.json\n```\n\nA green qualification report means **100% of the explicitly cataloged functionality passed**. It is not a claim that every possible repository, model response, environment, or unseen engineering task is universally correct.\n''',
)

print("functional qualification truth-semantics patch applied")
