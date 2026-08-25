from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from devagent.models import AcceptanceStatus, Outcome
from devagent.orchestrator import DevAgent
from devagent.providers import ScriptedFakeProvider


@dataclass(frozen=True)
class StackCase:
    source_path: str
    test_path: str
    source_before: str
    source_after: str
    test_before: str
    test_after: str
    verification: tuple[str, ...]
    extra_files: dict[str, str]
    required_tools: tuple[str, ...]


def _production_only() -> None:
    if os.getenv("DEVAGENT_PRODUCTION_QUALIFICATION") != "1":
        pytest.skip("real multi-stack DevAgent E2E runs only through production qualification")


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _case(stack: str) -> StackCase:
    if stack == "node":
        return StackCase(
            source_path="calculator.js",
            test_path="tests/calculator.test.js",
            source_before="exports.add = (a, b) => a + b;\n",
            source_after=(
                "exports.add = (a, b) => a + b;\n"
                "exports.subtract = (a, b) => a - b;\n"
            ),
            test_before=(
                "const test = require('node:test');\n"
                "const assert = require('node:assert/strict');\n"
                "const { add } = require('../calculator');\n"
                "test('add values', () => assert.equal(add(2, 3), 5));\n"
            ),
            test_after=(
                "const test = require('node:test');\n"
                "const assert = require('node:assert/strict');\n"
                "const { add, subtract } = require('../calculator');\n"
                "test('add values', () => assert.equal(add(2, 3), 5));\n"
                "test('subtract negative result', () => assert.equal(subtract(2, 5), -3));\n"
            ),
            verification=("npm", "run", "test"),
            extra_files={
                "package.json": (
                    '{"name":"devagent-node-e2e","version":"0.0.1",'
                    '"scripts":{"test":"node --test tests/calculator.test.js"}}\n'
                )
            },
            required_tools=("node", "npm"),
        )
    if stack == "go":
        return StackCase(
            source_path="calculator.go",
            test_path="calculator_test.go",
            source_before=(
                "package qualification\n\n"
                "func Add(a int, b int) int { return a + b }\n"
            ),
            source_after=(
                "package qualification\n\n"
                "func Add(a int, b int) int { return a + b }\n"
                "func Subtract(a int, b int) int { return a - b }\n"
            ),
            test_before=(
                "package qualification\n\nimport \"testing\"\n\n"
                "func TestAdd(t *testing.T) { if Add(2, 3) != 5 { t.Fatal(\"bad add\") } }\n"
            ),
            test_after=(
                "package qualification\n\nimport \"testing\"\n\n"
                "func TestAdd(t *testing.T) { if Add(2, 3) != 5 { t.Fatal(\"bad add\") } }\n"
                "func TestSubtractNegative(t *testing.T) { if Subtract(2, 5) != -3 { t.Fatal(\"bad subtract\") } }\n"
            ),
            verification=("go", "test", "./..."),
            extra_files={"go.mod": "module example.com/devagentqualification\n\ngo 1.20\n"},
            required_tools=("go",),
        )
    if stack == "rust":
        return StackCase(
            source_path="src/lib.rs",
            test_path="tests/calculator.rs",
            source_before="pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
            source_after=(
                "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
                "pub fn subtract(a: i32, b: i32) -> i32 { a - b }\n"
            ),
            test_before=(
                "use devagent_qualification::{add};\n\n"
                "#[test]\nfn add_values() { assert_eq!(add(2, 3), 5); }\n"
            ),
            test_after=(
                "use devagent_qualification::{add, subtract};\n\n"
                "#[test]\nfn add_values() { assert_eq!(add(2, 3), 5); }\n\n"
                "#[test]\nfn subtract_negative() { assert_eq!(subtract(2, 5), -3); }\n"
            ),
            verification=("cargo", "test"),
            extra_files={
                "Cargo.toml": (
                    '[package]\nname = "devagent_qualification"\nversion = "0.0.1"\nedition = "2021"\n'
                ),
                ".gitignore": "target/\nCargo.lock\n",
            },
            required_tools=("cargo",),
        )
    return StackCase(
        source_path="calculator.cpp",
        test_path="test_calculator.cpp",
        source_before="int add(int a, int b) { return a + b; }\n",
        source_after=(
            "int add(int a, int b) { return a + b; }\n"
            "int subtract(int a, int b) { return a - b; }\n"
        ),
        test_before=(
            "int add(int, int);\n"
            "int main() { return add(2, 3) == 5 ? 0 : 1; }\n"
        ),
        test_after=(
            "int add(int, int);\nint subtract(int, int);\n"
            "int test_subtract_negative() { return subtract(2, 5) == -3 ? 0 : 1; }\n"
            "int main() { return add(2, 3) == 5 && test_subtract_negative() == 0 ? 0 : 1; }\n"
        ),
        verification=("make", "test"),
        extra_files={
            "Makefile": (
                "test:\n"
                "\tc++ -std=c++17 calculator.cpp test_calculator.cpp -o /tmp/devagent_cpp_e2e\n"
                "\t/tmp/devagent_cpp_e2e\n"
            )
        },
        required_tools=("make", "c++"),
    )


def _initialize(root: Path, case: StackCase) -> None:
    for path, content in case.extra_files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    source = root / case.source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(case.source_before, encoding="utf-8")
    test = root / case.test_path
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(case.test_before, encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "DevAgent Qualification")
    _git(root, "config", "user.email", "qualification@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")


def _responses(case: StackCase) -> list[dict[str, object]]:
    return [
        {
            "_role": "understand",
            "problem": "The repository has addition support but no subtraction operation.",
            "expected_behavior": "Add subtraction support with a negative-result regression test.",
            "affected_paths": [case.source_path, case.test_path],
            "root_cause": "The calculator source does not define subtraction and its tests do not cover it.",
            "evidence": [
                {
                    "statement": "The calculator source contains the current addition implementation.",
                    "paths": [case.source_path],
                    "confidence": 1.0,
                },
                {
                    "statement": "The existing repository test covers addition but not subtraction.",
                    "paths": [case.test_path],
                    "confidence": 1.0,
                },
            ],
            "proposed_solution": [
                "Add the smallest subtraction function consistent with the repository.",
                "Add a negative-result regression test.",
            ],
            "confidence": 0.99,
        },
        {
            "_role": "plan",
            "files_to_inspect": [case.source_path, case.test_path],
            "implementation": [
                "Add subtraction regression coverage.",
                "Add the minimal subtraction implementation.",
            ],
            "verification": [list(case.verification), ["git", "diff", "--check"]],
            "rationale": "The existing calculator source and repository-native test are the complete affected set.",
        },
        {
            "_role": "implement",
            "actions": [
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": case.test_path,
                        "old": case.test_before,
                        "new": case.test_after,
                    },
                },
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": case.source_path,
                        "old": case.source_before,
                        "new": case.source_after,
                    },
                },
            ],
            "summary": [
                "Added subtraction support.",
                "Added negative subtraction regression coverage.",
            ],
        },
        {
            "_role": "review",
            "approved": True,
            "issues": [],
            "summary": "The change is minimal, follows the local style, and is covered by the repository-native test.",
        },
    ]


@pytest.mark.parametrize("stack", ["node", "go", "rust", "cpp"])
def test_real_multistack_devagent_feature_patch_is_verified(
    tmp_path: Path,
    stack: str,
) -> None:
    _production_only()
    case = _case(stack)
    for tool in case.required_tools:
        assert shutil.which(tool), f"required production qualification tool unavailable: {tool}"
    _initialize(tmp_path, case)

    result = DevAgent(ScriptedFakeProvider(_responses(case))).run(
        tmp_path,
        (
            "Add subtract(a, b) support. Support negative subtraction results. "
            "Add regression tests and verify the application."
        ),
    )

    diagnostic = {
        "stack": stack,
        "outcome": result.outcome.value,
        "states": [state.value for state in result.state_history],
        "not_run": result.not_run,
        "verification": [
            {
                "command": list(item.command),
                "phase": item.phase,
                "exit": item.exit_code,
                "classification": item.classification.value if item.classification else None,
                "stderr": item.stderr[-2000:],
                "stdout": item.stdout[-2000:],
            }
            for item in result.verification
        ],
    }
    assert result.outcome is Outcome.VERIFIED, diagnostic
    assert result.review and result.review.approved
    assert all(
        criterion.status is AcceptanceStatus.SATISFIED
        for criterion in result.task.acceptance_criteria
        if criterion.required
    )
    assert any(
        item.phase == "final" and item.command == case.verification and item.passed
        for item in result.verification
    )
    assert any("subtract" in item.name.lower() for item in result.developer_review.changed_symbols)
    assert any("subtract" in item.name.lower() for item in result.developer_review.test_cases)
    working = Path(result.working_root)
    assert case.source_after == (working / case.source_path).read_text(encoding="utf-8")
    assert case.source_before == (tmp_path / case.source_path).read_text(encoding="utf-8")
