from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devagent.technical_review import analyze_developer_review


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _commit(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.name", "Qualification")
    _git(root, "config", "user.email", "qualification@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")


@pytest.mark.parametrize("stack", ["javascript", "go", "rust", "cpp"])
def test_multilang_review_extracts_changed_symbols_and_test_cases(
    tmp_path: Path,
    stack: str,
) -> None:
    if stack == "javascript":
        source = "calculator.js"
        test_path = "tests/calculator.test.js"
        (tmp_path / "tests").mkdir()
        (tmp_path / source).write_text("exports.add = (a, b) => a + b;\n", encoding="utf-8")
        (tmp_path / test_path).write_text(
            "const test = require('node:test');\n"
            "const assert = require('node:assert/strict');\n"
            "const { add } = require('../calculator');\n"
            "test('add values', () => assert.equal(add(2, 3), 5));\n",
            encoding="utf-8",
        )
        _commit(tmp_path)
        (tmp_path / source).write_text(
            "exports.add = (a, b) => a + b;\nexports.subtract = (a, b) => a - b;\n",
            encoding="utf-8",
        )
        (tmp_path / test_path).write_text(
            "const test = require('node:test');\n"
            "const assert = require('node:assert/strict');\n"
            "const { add, subtract } = require('../calculator');\n"
            "test('add values', () => assert.equal(add(2, 3), 5));\n"
            "test('subtract negative result', () => assert.equal(subtract(2, 5), -3));\n",
            encoding="utf-8",
        )
        changed = [source, test_path]
        symbol = "subtract"
        test_name = "subtract negative result"

    elif stack == "go":
        source = "calculator.go"
        test_path = "calculator_test.go"
        (tmp_path / source).write_text(
            "package qualification\n\nfunc Add(a int, b int) int { return a + b }\n",
            encoding="utf-8",
        )
        (tmp_path / test_path).write_text(
            "package qualification\n\nimport \"testing\"\n\n"
            "func TestAdd(t *testing.T) { if Add(2, 3) != 5 { t.Fatal(\"bad add\") } }\n",
            encoding="utf-8",
        )
        _commit(tmp_path)
        (tmp_path / source).write_text(
            "package qualification\n\n"
            "func Add(a int, b int) int { return a + b }\n"
            "func Subtract(a int, b int) int { return a - b }\n",
            encoding="utf-8",
        )
        (tmp_path / test_path).write_text(
            "package qualification\n\nimport \"testing\"\n\n"
            "func TestAdd(t *testing.T) { if Add(2, 3) != 5 { t.Fatal(\"bad add\") } }\n"
            "func TestSubtractNegative(t *testing.T) { if Subtract(2, 5) != -3 { t.Fatal(\"bad subtract\") } }\n",
            encoding="utf-8",
        )
        changed = [source, test_path]
        symbol = "Subtract"
        test_name = "TestSubtractNegative"

    elif stack == "rust":
        source = "src/lib.rs"
        (tmp_path / "src").mkdir()
        (tmp_path / source).write_text(
            "pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
            encoding="utf-8",
        )
        _commit(tmp_path)
        (tmp_path / source).write_text(
            "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
            "pub fn subtract(a: i32, b: i32) -> i32 { a - b }\n\n"
            "#[cfg(test)]\nmod tests {\n"
            "    use super::*;\n"
            "    #[test]\n"
            "    fn subtract_negative() { assert_eq!(subtract(2, 5), -3); }\n"
            "}\n",
            encoding="utf-8",
        )
        changed = [source]
        symbol = "subtract"
        test_name = "subtract_negative"

    else:
        source = "calculator.cpp"
        test_path = "test_calculator.cpp"
        (tmp_path / source).write_text("int add(int a, int b) { return a + b; }\n", encoding="utf-8")
        (tmp_path / test_path).write_text(
            "int add(int, int);\nint main() { return add(2, 3) == 5 ? 0 : 1; }\n",
            encoding="utf-8",
        )
        _commit(tmp_path)
        (tmp_path / source).write_text(
            "int add(int a, int b) { return a + b; }\n"
            "int subtract(int a, int b) { return a - b; }\n",
            encoding="utf-8",
        )
        (tmp_path / test_path).write_text(
            "int add(int, int);\nint subtract(int, int);\n"
            "int test_subtract_negative() { return subtract(2, 5) == -3 ? 0 : 1; }\n"
            "int main() { return add(2, 3) == 5 && test_subtract_negative() == 0 ? 0 : 1; }\n",
            encoding="utf-8",
        )
        changed = [source, test_path]
        symbol = "subtract"
        test_name = "test_subtract_negative"

    review = analyze_developer_review(tmp_path, changed)

    assert any(item.name == symbol and item.change == "ADDED" for item in review.changed_symbols)
    assert any(item.name == test_name for item in review.test_cases)
    assert review.test_files
