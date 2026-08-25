from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"hardening anchor missing in {path}: {old[:140]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1) Preserve a public deterministic named-preservation extractor for final baseline checks.
replace(
    "devagent/models.py",
    "def _preserved_subjects(text: str) -> set[str]:\n",
    "def preserved_subjects(text: str) -> set[str]:\n",
)
replace(
    "devagent/models.py",
    "            for subject in _preserved_subjects(corpus)\n",
    "            for subject in preserved_subjects(corpus)\n",
)

# 2) Task classification: a request to add a feature plus regression coverage is still a feature.
replace(
    "devagent/tasking.py",
    '    (TaskType.BUG_FIX, ("fix", "bug", "incorrect", "broken", "regression")),\n',
    '    (TaskType.BUG_FIX, ("fix", "bug", "incorrect", "broken", "regression failure", "regression bug")),\n',
)

# 3) Structured requirements: support Markdown headings, Tests/Verification/Constraints, and never silently truncate user AC.
replace(
    "devagent/tasking.py",
    '_REQUIREMENT_SECTIONS = {"requirements", "required changes", "acceptance criteria", "acceptance"}\n',
    '''_REQUIREMENT_SECTIONS = {\n    "requirements",\n    "required changes",\n    "acceptance criteria",\n    "acceptance",\n    "tests",\n    "verification",\n    "constraints",\n}\n_KNOWN_SECTIONS = _REQUIREMENT_SECTIONS | {\n    "goal",\n    "context",\n    "current behavior",\n    "current behaviour",\n    "non-goals",\n    "non goals",\n    "notes",\n}\n''',
)
old_parser = '''def _user_acceptance_items(requirement: str) -> list[str]:\n    lines = requirement.splitlines()\n    explicit: list[str] = []\n    active = False\n    for raw in lines:\n        stripped = raw.strip()\n        if not stripped:\n            continue\n        heading = stripped.rstrip(":").strip().lower()\n        if stripped.endswith(":"):\n            active = heading in _REQUIREMENT_SECTIONS\n            continue\n        if active and re.match(r"^(?:[-*+]\\s+|\\d+[.)]\\s+)", stripped):\n            explicit.append(_clean_requirement_item(stripped))\n    explicit = _dedupe(explicit)\n    if explicit:\n        return explicit[:24]\n\n    candidates = re.split(r"(?<=[.!?;])\\s+|\\n+", requirement)\n    directives: list[str] = []\n    for candidate in candidates:\n        item = _clean_requirement_item(candidate)\n        item = re.sub(r"^(?:goal|requirement|task)\\s*:\\s*", "", item, flags=re.IGNORECASE)\n        if not item:\n            continue\n        if _DIRECTIVE.match(item) or re.search(r"\\b(?:must|should|shall)\\b", item, re.IGNORECASE):\n            directives.append(item)\n    directives = _dedupe(directives)\n    if directives:\n        return directives[:24]\n    return [re.sub(r"\\s+", " ", requirement).strip()]\n'''
new_parser = '''def _section_header(line: str) -> tuple[str, str] | None:\n    stripped = line.strip()\n    markdown = re.match(r"^#{1,6}\\s+(.+?)\\s*$", stripped)\n    if markdown:\n        return markdown.group(1).strip().rstrip(":").lower(), ""\n    colon = re.match(r"^([A-Za-z][A-Za-z0-9 _/-]{0,80})\\s*:\\s*(.*)$", stripped)\n    if colon and colon.group(1).strip().lower() in _KNOWN_SECTIONS:\n        return colon.group(1).strip().lower(), colon.group(2).strip()\n    return None\n\n\ndef _user_acceptance_items(requirement: str) -> list[str]:\n    lines = requirement.splitlines()\n    explicit: list[str] = []\n    active_section: str | None = None\n    for raw in lines:\n        stripped = raw.strip()\n        if not stripped:\n            continue\n\n        bullet = re.match(r"^(?:[-*+]\\s+|\\d+[.)]\\s+)", stripped)\n        if bullet:\n            if active_section in _REQUIREMENT_SECTIONS:\n                explicit.append(_clean_requirement_item(stripped))\n            continue\n\n        header = _section_header(stripped)\n        if header is not None:\n            name, inline = header\n            active_section = name if name in _REQUIREMENT_SECTIONS else None\n            if active_section is not None and inline:\n                explicit.append(_clean_requirement_item(inline))\n            continue\n\n        if active_section in _REQUIREMENT_SECTIONS:\n            item = _clean_requirement_item(stripped)\n            if (\n                active_section in {"tests", "verification", "constraints"}\n                or _DIRECTIVE.match(item)\n                or re.search(r"\\b(?:must|should|shall)\\b", item, re.IGNORECASE)\n            ):\n                explicit.append(item)\n\n    explicit = _dedupe(explicit)\n    if explicit:\n        return explicit\n\n    candidates = re.split(r"(?<=[.!?;])\\s+|\\n+", requirement)\n    directives: list[str] = []\n    for candidate in candidates:\n        item = _clean_requirement_item(candidate)\n        item = re.sub(r"^(?:goal|requirement|task)\\s*:\\s*", "", item, flags=re.IGNORECASE)\n        if not item:\n            continue\n        if _DIRECTIVE.match(item) or re.search(r"\\b(?:must|should|shall)\\b", item, re.IGNORECASE):\n            directives.append(item)\n    directives = _dedupe(directives)\n    if directives:\n        return directives\n    return [re.sub(r"\\s+", " ", requirement).strip()]\n'''
replace("devagent/tasking.py", old_parser, new_parser)

# 4) Acceptance adjudication: named preservation gets deterministic baseline repository evidence.
replace(
    "devagent/orchestrator.py",
    "    jsonable,\n)\n",
    "    jsonable,\n    preserved_subjects,\n)\n",
)
insert_anchor = '''def _set_acceptance(\n    criterion: Any,\n    status: AcceptanceStatus,\n    evidence: Sequence[str] = (),\n    reason: str | None = None,\n) -> None:\n    criterion.status = status\n    criterion.evidence = list(dict.fromkeys(item for item in evidence if item))\n    criterion.reason = reason\n\n\n'''
insert_new = insert_anchor + '''def _baseline_subject_evidence(root: Path, subject: str, paths: Sequence[str]) -> list[str]:\n    """Find a named preservation subject in the exact Git baseline for affected paths."""\n\n    pattern = re.compile(\n        rf"(?<![A-Za-z0-9_]){re.escape(subject)}(?![A-Za-z0-9_])",\n        re.IGNORECASE,\n    )\n    evidence: list[str] = []\n    for path in sorted(set(paths)):\n        completed = subprocess.run(\n            ["git", "show", f"HEAD:{path}"],\n            cwd=root,\n            capture_output=True,\n            text=True,\n            timeout=10,\n            check=False,\n        )\n        if completed.returncode == 0 and pattern.search(completed.stdout):\n            evidence.append(f"baseline {path} contains named subject {subject}")\n    return evidence\n\n\ndef _final_contract_evidence(root: Path, contract: str, paths: Sequence[str]) -> list[str]:\n    """Find an exact quoted user contract in bounded final relevant files."""\n\n    needle = contract.lower()\n    evidence: list[str] = []\n    for path in sorted(set(paths)):\n        target = root / path\n        if not target.is_file():\n            continue\n        try:\n            text = target.read_text(encoding="utf-8")\n        except (OSError, UnicodeDecodeError):\n            continue\n        if needle in text.lower():\n            evidence.append(f"final {path} contains exact quoted contract")\n    return evidence\n\n\n'''
replace("devagent/orchestrator.py", insert_anchor, insert_new)
replace(
    "devagent/orchestrator.py",
    '''    implementation: Sequence[str],\n    diff_text: str,\n) -> None:\n''',
    '''    implementation: Sequence[str],\n    diff_text: str,\n    working_root: Path | None = None,\n) -> None:\n''',
)
replace(
    "devagent/orchestrator.py",
    '    implementation_text = " ".join(implementation).lower()\n    lower_diff = diff_text.lower()\n',
    '    lower_diff = diff_text.lower()\n',
)
replace(
    "devagent/orchestrator.py",
    '''            elif lowered == "regression coverage protects the refactored behavior":\n                satisfied = bool(changed_tests and passing_tests)\n                evidence = [*changed_tests, *(" ".join(item.command) for item in passing_tests)]\n''',
    '''            elif lowered == "regression coverage protects the refactored behavior":\n                satisfied = review.approved and bool(baseline_tests and passing_tests)\n                evidence = [\n                    "Independent reviewer approved refactor regression coverage",\n                    *("baseline: " + " ".join(item.command) for item in baseline_tests),\n                    *("final: " + " ".join(item.command) for item in passing_tests),\n                ]\n''',
)
replace(
    "devagent/orchestrator.py",
    '''                present = [term for term in strategy_terms if term in implementation_text or term in lower_diff]\n''',
    '''                present = [term for term in strategy_terms if term in lower_diff]\n''',
)
old_quote = '''        quoted_missing = [item for item in quoted_contracts if item.lower() not in lower_diff]\n'''
new_quote = '''        relevant_paths = [*understanding.affected_paths, *changes.paths]\n        quoted_evidence: list[str] = []\n        quoted_missing: list[str] = []\n        for contract in quoted_contracts:\n            hits = (\n                _final_contract_evidence(working_root, contract, relevant_paths)\n                if working_root is not None\n                else []\n            )\n            if hits:\n                quoted_evidence.extend(hits)\n            elif contract.lower() in lower_diff:\n                quoted_evidence.append("Final diff contains exact quoted contract")\n            else:\n                quoted_missing.append(contract)\n'''
replace("devagent/orchestrator.py", old_quote, new_quote)
old_preservation = '''        if preservation and not matched_tests:\n            criterion.reason = "Preservation requirement has no matching regression-test inventory evidence"\n            continue\n        if preservation and not baseline_tests:\n            criterion.reason = "Preservation requirement has no passing baseline test evidence for comparison"\n            continue\n        if tokens and not semantic_tokens:\n'''
new_preservation = '''        if preservation and not matched_tests:\n            criterion.reason = "Preservation requirement has no matching regression-test inventory evidence"\n            continue\n        named_subjects = preserved_subjects(criterion.description) if preservation else set()\n        named_baseline_evidence: list[str] = []\n        if named_subjects:\n            if working_root is None:\n                criterion.reason = "Named preservation requirement has no deterministic baseline repository root"\n                continue\n            missing_subjects: list[str] = []\n            for subject in sorted(named_subjects):\n                hits = _baseline_subject_evidence(working_root, subject, understanding.affected_paths)\n                if hits:\n                    named_baseline_evidence.extend(hits)\n                else:\n                    missing_subjects.append(subject)\n            if missing_subjects:\n                criterion.reason = (\n                    "Named preservation subject is not present in deterministic baseline affected-path evidence: "\n                    + ", ".join(missing_subjects)\n                )\n                continue\n        if preservation and not baseline_tests:\n            criterion.reason = "Preservation requirement has no passing baseline test evidence for comparison"\n            continue\n        if tokens and not semantic_tokens:\n'''
replace("devagent/orchestrator.py", old_preservation, new_preservation)
replace(
    "devagent/orchestrator.py",
    '''        evidence = [*matched]\n        if diff_tokens:\n''',
    '''        evidence = [*matched, *quoted_evidence, *named_baseline_evidence]\n        if diff_tokens:\n''',
)
replace(
    "devagent/orchestrator.py",
    '''                    implementation,\n                    final_diff,\n                )\n''',
    '''                    implementation,\n                    final_diff,\n                    working_root,\n                )\n''',
)

# Refactors require running regression tests, but do not require needless edits to a well-covered test file.
replace(
    "devagent/orchestrator.py",
    '''            if task.requires_tests and task.task_type not in {TaskType.TEST_FAILURE, TaskType.UNIT_TEST} and not any(\n''',
    '''            if task.requires_tests and task.task_type not in {TaskType.TEST_FAILURE, TaskType.UNIT_TEST, TaskType.REFACTOR} and not any(\n''',
)

# Report the new status contract precisely.
replace(
    "devagent/report.py",
    '        f"Required acceptance criteria evidenced: {acceptance_done}/{acceptance_total}",\n',
    '        f"Required acceptance criteria satisfied: {acceptance_done}/{acceptance_total}",\n',
)
replace(
    "devagent/report.py",
    '            f"- Required acceptance evidence: {acceptance_done}/{acceptance_total}",\n',
    '            f"- Required acceptance criteria satisfied: {acceptance_done}/{acceptance_total}",\n',
)
replace(
    "tests/test_developer_review_report.py",
    '    assert "Required acceptance evidence: 2/2" in report\n',
    '    assert "Required acceptance criteria satisfied: 2/2" in report\n',
)
replace(
    "tests/test_developer_review_report.py",
    '    assert "Required acceptance criteria evidenced: 2/2" in report\n',
    '    assert "Required acceptance criteria satisfied: 2/2" in report\n',
)

# Accuracy regression coverage.
test_path = ROOT / "tests/test_acceptance_contract.py"
text = test_path.read_text(encoding="utf-8")
if "test_feature_with_regression_test_language_is_not_misclassified_as_bug_fix" not in text:
    text += '''\n\ndef test_feature_with_regression_test_language_is_not_misclassified_as_bug_fix() -> None:\n    spec = compile_task(\n        "Add average(values). Preserve divide behavior. Add a regression test and verify the application."\n    )\n    assert spec.task_type is TaskType.FEATURE\n\n\ndef test_markdown_sections_keep_tests_verification_constraints_and_more_than_24_items() -> None:\n    requirement_lines = [\n        "# Customer requirement",\n        "## Context",\n        "- Existing service is in production.",\n        "## Requirements",\n        *[f"- Add behavior_{index}()" for index in range(30)],\n        "## Tests",\n        "- Add a regression test for behavior_0().",\n        "## Verification",\n        "- All relevant automated tests must pass.",\n        "## Constraints",\n        "- Do not modify unrelated APIs.",\n        "## Non-goals",\n        "- Replace the entire application.",\n    ]\n    spec = compile_task("\\n".join(requirement_lines))\n    user = [item.description for item in spec.acceptance_criteria if item.source is AcceptanceSource.USER]\n    assert len([item for item in user if item.startswith("Add behavior_")]) == 30\n    assert "Add a regression test for behavior_0()" in user\n    assert "All relevant automated tests must pass" in user\n    assert "Do not modify unrelated APIs" in user\n    assert "Replace the entire application" not in user\n\n\ndef test_named_preservation_requires_deterministic_baseline_subject(\n    tmp_path: Path,\n) -> None:\n    import subprocess\n\n    def git(*args: str) -> None:\n        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)\n\n    git("init")\n    git("config", "user.email", "test@example.com")\n    git("config", "user.name", "Test")\n    (tmp_path / "calculator.py").write_text("def divide(a, b):\\n    return a / b\\n", encoding="utf-8")\n    (tmp_path / "test_calculator.py").write_text(\n        "from calculator import divide\\n\\ndef test_divide():\\n    assert divide(8, 2) == 4\\n",\n        encoding="utf-8",\n    )\n    git("add", ".")\n    git("commit", "-m", "baseline")\n\n    # Final tree can add multiply and a matching test, but that cannot prove it was existing.\n    (tmp_path / "calculator.py").write_text(\n        "def divide(a, b):\\n    return a / b\\n\\ndef multiply(a, b):\\n    return a * b\\n",\n        encoding="utf-8",\n    )\n    (tmp_path / "test_calculator.py").write_text(\n        "from calculator import divide, multiply\\n\\ndef test_divide():\\n    assert divide(8, 2) == 4\\n\\ndef test_multiply():\\n    assert multiply(2, 3) == 6\\n",\n        encoding="utf-8",\n    )\n    task = TaskSpec(\n        task_type=TaskType.FEATURE,\n        goal="Preserve existing multiply behavior",\n        requires_code_change=True,\n        requires_tests=True,\n        acceptance_criteria=[\n            AcceptanceCriterion("Preserve existing multiply behavior", source=AcceptanceSource.USER)\n        ],\n        risk=compile_task("Add feature").risk,\n    )\n    baseline = _result(("python", "-m", "pytest", "-q"), baseline=True, tests=1)\n    final = _result(("python", "-m", "pytest", "-q"), tests=2)\n    review_evidence = DeveloperReviewEvidence(\n        changed_symbols=[CodeSymbol("calculator.py", "multiply", "function", 4, "ADDED")],\n        test_cases=[\n            CodeSymbol("test_calculator.py", "test_divide", "function", 3, "UNCHANGED"),\n            CodeSymbol("test_calculator.py", "test_multiply", "function", 6, "ADDED"),\n        ],\n        test_files=["test_calculator.py"],\n    )\n    understanding = Understanding(\n        problem="Add requested calculator behavior.",\n        expected_behavior="Complete the requested calculator task.",\n        affected_paths=["calculator.py", "test_calculator.py"],\n        root_cause="Requested behavior needs an implementation change.",\n        evidence=[Evidence("Calculator files are relevant.", ("calculator.py", "test_calculator.py"), 1.0)],\n        proposed_solution=["Implement and test the requested change."],\n        confidence=0.99,\n    )\n    _support_acceptance_criteria(\n        task,\n        _repo(),\n        understanding,\n        ChangeMetrics(2, 6, 0, ["calculator.py", "test_calculator.py"]),\n        [baseline, final],\n        [final],\n        ReviewDecision(True, [], "approved"),\n        review_evidence,\n        ["Added multiply"],\n        "+def multiply(a, b):\\n+    return a * b\\n",\n        tmp_path,\n    )\n    criterion = task.acceptance_criteria[0]\n    assert criterion.status is AcceptanceStatus.UNPROVEN\n    assert "not present in deterministic baseline" in (criterion.reason or "")\n\n\ndef test_refactor_policy_accepts_existing_baseline_regression_suite_without_test_edits() -> None:\n    task = compile_task("Refactor parser internals")\n    criterion = next(\n        item\n        for item in task.acceptance_criteria\n        if item.description == "Regression coverage protects the refactored behavior"\n    )\n    baseline = _result(("python", "-m", "pytest", "-q"), baseline=True, tests=20)\n    final = _result(("python", "-m", "pytest", "-q"), tests=20)\n    _support_acceptance_criteria(\n        task,\n        _repo(),\n        _understanding(),\n        ChangeMetrics(1, 3, 3, ["parser.py"]),\n        [baseline, final],\n        [final],\n        ReviewDecision(True, [], "approved"),\n        DeveloperReviewEvidence(changed_symbols=[CodeSymbol("parser.py", "parse", "function", 1, "MODIFIED")]),\n        ["Refactor parser internals without changing behavior"],\n        "-old\\n+new\\n",\n    )\n    assert criterion.status is AcceptanceStatus.SATISFIED\n'''
    text = text.replace("from __future__ import annotations\n", "from __future__ import annotations\n\nfrom pathlib import Path\n", 1)
    test_path.write_text(text, encoding="utf-8")

# Documentation: make no-truncation and deterministic named-preservation behavior explicit.
doc = ROOT / "docs/acceptance-contract.md"
doc_text = doc.read_text(encoding="utf-8")
addition = '''\nStructured Markdown headings are supported for `Requirements`, `Required Changes`, `Acceptance Criteria`, `Tests`, `Verification`, and `Constraints`. DevAgent does not silently truncate explicit user criteria. Context and non-goal sections are not promoted to required acceptance criteria.\n\nFor named preservation claims such as `preserve existing multiply behavior`, final verification also searches the exact Git baseline across the evidence-backed affected paths. A newly added symbol/test cannot be used as proof that the named behavior existed before the run.\n'''
if "does not silently truncate explicit user criteria" not in doc_text:
    doc.write_text(doc_text.rstrip() + "\n" + addition, encoding="utf-8")

print("acceptance accuracy hardening applied")
