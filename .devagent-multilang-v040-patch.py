from pathlib import Path

ROOT = Path(__file__).resolve().parent


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "devagent/orchestrator.py",
    '''def _is_test_path(path: str) -> bool:\n    lowered = path.lower()\n    parts = Path(lowered).parts\n    return (\n        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)\n        or Path(lowered).name.startswith("test_")\n        or ".test." in lowered\n        or ".spec." in lowered\n    )\n''',
    '''def _is_test_path(path: str) -> bool:\n    lowered = path.lower()\n    parts = Path(lowered).parts\n    name = Path(lowered).name\n    return (\n        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)\n        or name.startswith("test_")\n        or name.startswith("test.")\n        or "_test." in name\n        or ".test." in name\n        or ".spec." in name\n    )\n''',
)

replace(
    "devagent/orchestrator.py",
    '''    changed_tests = [path for path in changes.paths if _is_test_path(path)]\n''',
    '''    changed_tests = sorted(\n        set(path for path in changes.paths if _is_test_path(path))\n        | set(developer_review.test_files)\n    )\n''',
)

replace(
    "devagent/technical_review.py",
    '''            match = re.search(\n                r"\\b(?:export\\s+)?(?:const|let|var)\\s+([A-Za-z_$][A-Za-z0-9_$]*)\\s*=\\s*(?:async\\s*)?(?:\\([^)]*\\)|[A-Za-z_$][A-Za-z0-9_$]*)\\s*=>",\n                line,\n            )\n            if match:\n                add(match.group(1), "function", index, is_test=test_file)\n            match = re.search(r"\\bclass\\s+([A-Za-z_$][A-Za-z0-9_$]*)\\b", line)\n''',
    '''            match = re.search(\n                r"\\b(?:export\\s+)?(?:const|let|var)\\s+([A-Za-z_$][A-Za-z0-9_$]*)\\s*=\\s*(?:async\\s*)?(?:\\([^)]*\\)|[A-Za-z_$][A-Za-z0-9_$]*)\\s*=>",\n                line,\n            )\n            if match:\n                add(match.group(1), "function", index, is_test=test_file)\n            match = re.search(\n                r"\\b(?:exports|module\\.exports)\\.([A-Za-z_$][A-Za-z0-9_$]*)\\s*=\\s*(?:async\\s*)?(?:\\([^)]*\\)|[A-Za-z_$][A-Za-z0-9_$]*)\\s*=>",\n                line,\n            )\n            if match:\n                add(match.group(1), "function", index, is_test=test_file)\n            match = re.search(r"\\bclass\\s+([A-Za-z_$][A-Za-z0-9_$]*)\\b", line)\n''',
)

insert_before = '''    {\n      "id": "release-version-consistency",\n'''
new_cases = '''    {\n      "id": "multilang-review-symbol-test-evidence",\n      "category": "real_stack",\n      "pytest_node": "tests/test_multilang_technical_review.py::test_multilang_review_extracts_changed_symbols_and_test_cases",\n      "expected": "PASS"\n    },\n    {\n      "id": "real-multistack-devagent-e2e",\n      "category": "real_stack",\n      "pytest_node": "tests/test_multistack_devagent_e2e.py::test_real_multistack_devagent_feature_patch_is_verified",\n      "expected": "VERIFIED"\n    },\n    {\n      "id": "release-version-consistency",\n'''
replace("evaluation/benchmark_v3.json", insert_before, new_cases)

for path in ("README.md", "evaluation/README.md", "CHANGELOG.md"):
    replace(path, "50 required cases", "52 required cases")
replace(
    "tests/test_functional_qualification.py",
    "    assert len(cases) == 50\n",
    "    assert len(cases) == 52\n",
)

print("multi-language acceptance/review patch applied")
