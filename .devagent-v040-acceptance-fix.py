from pathlib import Path

ROOT = Path(__file__).resolve().parent


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"fix anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "devagent/orchestrator.py",
    "import json\nimport difflib\nimport subprocess\n",
    "import json\nimport difflib\nimport re\nimport subprocess\n",
)

# Preserve directive-style user intent such as "Handle division by zero ...".
replace(
    "devagent/tasking.py",
    'r"verify|run|return|raise|allow|prevent|maintain|migrate|refactor|update|fix)\\b",',
    'r"verify|run|return|raise|allow|prevent|maintain|migrate|refactor|update|fix|handle)\\b",',
)

# Common grammatical terms must never become accidental semantic evidence tokens.
replace(
    "devagent/orchestrator.py",
    '        "add", "implement", "support", "preserve", "preserving", "existing", "behavior",\n',
    '        "add", "and", "the", "handle", "safely", "changing", "implement", "support", "preserve", "preserving", "existing", "behavior",\n',
)

old = '''        if quoted_missing:\n            criterion.reason = "Exact quoted user contract is not present in the final diff: " + "; ".join(quoted_missing)\n            continue\n        if tokens and not semantic_tokens:\n            criterion.reason = "No changed symbol, test, path, or final diff token semantically matches this user criterion"\n            continue\n        if preservation and not matched_tests:\n            criterion.reason = "Preservation requirement has no matching regression-test inventory evidence"\n            continue\n        if behavior_specific and task.requires_tests and not matched_tests:\n            criterion.reason = "Behavior-specific requirement has no semantically matching test-case evidence"\n            continue\n        if task.requires_tests and not passing_tests:\n            criterion.reason = "User criterion cannot be satisfied because no final evidence-backed tests passed"\n            continue\n        evidence = [*matched]\n        if diff_tokens:\n            evidence.append("Final diff semantic tokens: " + ", ".join(sorted(diff_tokens)))\n        evidence.extend(" ".join(item.command) for item in passing_tests)\n        if review.approved:\n            evidence.append("Independent reviewer approved the final diff")\n        _set_acceptance(\n            criterion,\n            AcceptanceStatus.SATISFIED,\n            evidence,\n            "User criterion is linked to final code/test evidence and current-revision verification",\n        )\n'''
new = '''        if quoted_missing:\n            criterion.reason = "Exact quoted user contract is not present in the final diff: " + "; ".join(quoted_missing)\n            continue\n\n        preservation = preservation or "without changing" in lowered\n        generic_test_intent = (\n            not tokens\n            and any(term in lowered for term in ("regression test", "add test", "add tests", "verify the application", "verify application"))\n        )\n        if generic_test_intent:\n            if changed_tests and passing_tests and review.approved:\n                _set_acceptance(\n                    criterion,\n                    AcceptanceStatus.SATISFIED,\n                    [\n                        *changed_tests,\n                        *(" ".join(item.command) for item in passing_tests),\n                        "Independent reviewer approved the final diff",\n                    ],\n                    "Generic user test/verification intent is proven by changed test coverage, final tests, and independent review",\n                )\n            else:\n                criterion.reason = "Generic user test/verification intent lacks changed tests, passing final tests, or independent review"\n            continue\n\n        if preservation and not matched_tests:\n            criterion.reason = "Preservation requirement has no matching regression-test inventory evidence"\n            continue\n        if preservation and not baseline_tests:\n            criterion.reason = "Preservation requirement has no passing baseline test evidence for comparison"\n            continue\n        if tokens and not semantic_tokens:\n            criterion.reason = "No changed symbol, test, path, or final diff token semantically matches this user criterion"\n            continue\n        if behavior_specific and task.requires_tests and not matched_tests:\n            criterion.reason = "Behavior-specific requirement has no semantically matching test-case evidence"\n            continue\n        if task.requires_tests and not passing_tests:\n            criterion.reason = "User criterion cannot be satisfied because no final evidence-backed tests passed"\n            continue\n        evidence = [*matched]\n        if diff_tokens:\n            evidence.append("Final diff semantic tokens: " + ", ".join(sorted(diff_tokens)))\n        if preservation:\n            evidence.extend("baseline: " + " ".join(item.command) for item in baseline_tests)\n        evidence.extend("final: " + " ".join(item.command) for item in passing_tests)\n        if review.approved:\n            evidence.append("Independent reviewer approved the final diff")\n        _set_acceptance(\n            criterion,\n            AcceptanceStatus.SATISFIED,\n            evidence,\n            "User criterion is linked to final code/test evidence and current-revision verification",\n        )\n'''
replace("devagent/orchestrator.py", old, new)

# Accuracy regressions for generic verification intent and preservation baseline comparison.
test_path = ROOT / "tests/test_acceptance_contract.py"
test_text = test_path.read_text(encoding="utf-8")
extra = '''\n\ndef test_generic_regression_verification_intent_uses_changed_tests_and_review() -> None:\n    task = TaskSpec(\n        task_type=TaskType.BUG_FIX,\n        goal="Add a regression test and verify the application",\n        requires_code_change=True,\n        requires_tests=True,\n        acceptance_criteria=[\n            AcceptanceCriterion(\n                "Add a regression test and verify the application",\n                source=AcceptanceSource.USER,\n            )\n        ],\n        risk=compile_task("Fix bug").risk,\n    )\n    baseline = _result(("python", "-m", "pytest", "-q"), baseline=True, tests=1)\n    final = _result(("python", "-m", "pytest", "-q"), tests=2)\n    review_evidence = DeveloperReviewEvidence(\n        test_cases=[CodeSymbol("test_calculator.py", "test_divide_by_zero", "function", 8, "ADDED")],\n        test_files=["test_calculator.py"],\n    )\n    _support_acceptance_criteria(\n        task,\n        _repo(),\n        _understanding(),\n        ChangeMetrics(1, 3, 0, ["test_calculator.py"]),\n        [baseline, final],\n        [final],\n        ReviewDecision(True, [], "approved"),\n        review_evidence,\n        ["Added regression coverage"],\n        "+def test_divide_by_zero():\\n+    assert divide(10, 0) is None\\n",\n    )\n    assert task.acceptance_criteria[0].status is AcceptanceStatus.SATISFIED\n\n\ndef test_without_changing_preservation_requires_baseline_and_final_evidence() -> None:\n    task = TaskSpec(\n        task_type=TaskType.BUG_FIX,\n        goal="Handle division by zero safely without changing normal division behavior",\n        requires_code_change=True,\n        requires_tests=True,\n        acceptance_criteria=[\n            AcceptanceCriterion(\n                "Handle division by zero safely without changing normal division behavior",\n                source=AcceptanceSource.USER,\n            )\n        ],\n        risk=compile_task("Fix division bug").risk,\n    )\n    final = _result(("python", "-m", "pytest", "-q"), tests=2)\n    review_evidence = DeveloperReviewEvidence(\n        changed_symbols=[CodeSymbol("calculator.py", "divide", "function", 1, "MODIFIED")],\n        test_cases=[CodeSymbol("test_calculator.py", "test_divide_by_zero", "function", 8, "ADDED")],\n        test_files=["test_calculator.py"],\n    )\n    common = dict(\n        task=task,\n        repository=_repo(),\n        understanding=_understanding(),\n        changes=ChangeMetrics(2, 5, 1, ["calculator.py", "test_calculator.py"]),\n        final_results=[final],\n        review=ReviewDecision(True, [], "approved"),\n        developer_review=review_evidence,\n        implementation=["Handle zero divisor while preserving normal division"],\n        diff_text="+    if b == 0:\\n+        return None\\n",\n    )\n    _support_acceptance_criteria(verification=[final], **common)\n    assert task.acceptance_criteria[0].status is AcceptanceStatus.UNPROVEN\n    assert "baseline test evidence" in (task.acceptance_criteria[0].reason or "")\n\n    baseline = _result(("python", "-m", "pytest", "-q"), baseline=True, tests=1)\n    _support_acceptance_criteria(verification=[baseline, final], **common)\n    assert task.acceptance_criteria[0].status is AcceptanceStatus.SATISFIED\n'''
if "test_generic_regression_verification_intent_uses_changed_tests_and_review" not in test_text:
    test_path.write_text(test_text + extra, encoding="utf-8")

print("semantic acceptance fixes applied")
