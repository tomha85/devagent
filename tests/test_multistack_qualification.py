from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devagent.discovery import discover_repository


def _production_only() -> None:
    if os.getenv("DEVAGENT_PRODUCTION_QUALIFICATION") != "1":
        pytest.skip("real toolchain fixtures run only through the production qualification gate")


def _require_tool(name: str) -> None:
    assert shutil.which(name), f"required production qualification tool is unavailable: {name}"


def _run_capability(root: Path, kind: str) -> tuple[str, ...]:
    repository = discover_repository(root, probe_capabilities=False)
    capabilities = [item for item in repository.capabilities if item.kind == kind and item.trusted]
    assert capabilities, f"no trusted {kind} capability discovered: {repository}"
    command = capabilities[0].command
    completed = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, (
        f"{' '.join(command)} failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return command


def test_python_pytest_stack_is_discovered_and_executes(tmp_path: Path) -> None:
    _production_only()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "qualification-python"\nversion = "0.0.1"\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    command = _run_capability(tmp_path, "test")

    assert command[:3] == ("python", "-m", "pytest")
    assert "python" in discover_repository(tmp_path, probe_capabilities=False).languages


def test_node_typescript_stack_is_discovered_and_executes(tmp_path: Path) -> None:
    _production_only()
    _require_tool("node")
    _require_tool("npm")
    (tmp_path / "package.json").write_text(
        '{"name":"qualification-node","version":"0.0.1","scripts":{"test":"node --test tests/calc.test.js"}}\n',
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "calculator.ts").write_text(
        "export const add = (a: number, b: number): number => a + b;\n",
        encoding="utf-8",
    )
    (tmp_path / "calculator.js").write_text("exports.add = (a, b) => a + b;\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "calc.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert/strict');\n"
        "const { add } = require('../calculator');\n"
        "test('add', () => assert.equal(add(2, 3), 5));\n",
        encoding="utf-8",
    )

    command = _run_capability(tmp_path, "test")
    repository = discover_repository(tmp_path, probe_capabilities=False)

    assert command == ("npm", "run", "test")
    assert "typescript" in repository.languages
    assert "javascript" in repository.languages


def test_go_stack_is_discovered_and_executes(tmp_path: Path) -> None:
    _production_only()
    _require_tool("go")
    (tmp_path / "go.mod").write_text("module example.com/qualification\n\ngo 1.20\n", encoding="utf-8")
    (tmp_path / "calculator.go").write_text(
        "package qualification\n\nfunc Add(a int, b int) int { return a + b }\n",
        encoding="utf-8",
    )
    (tmp_path / "calculator_test.go").write_text(
        "package qualification\n\nimport \"testing\"\n\n"
        "func TestAdd(t *testing.T) { if Add(2, 3) != 5 { t.Fatal(\"bad sum\") } }\n",
        encoding="utf-8",
    )

    command = _run_capability(tmp_path, "test")

    assert command == ("go", "test", "./...")
    assert "go" in discover_repository(tmp_path, probe_capabilities=False).languages


def test_rust_stack_is_discovered_and_executes_test_and_build(tmp_path: Path) -> None:
    _production_only()
    _require_tool("cargo")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "qualification-rust"\nversion = "0.0.1"\nedition = "2021"\n',
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n\n"
        "#[cfg(test)]\nmod tests { use super::*; #[test] fn adds() { assert_eq!(add(2, 3), 5); } }\n",
        encoding="utf-8",
    )

    test_command = _run_capability(tmp_path, "test")
    build_command = _run_capability(tmp_path, "build")

    assert test_command == ("cargo", "test")
    assert build_command == ("cargo", "check")
    assert "rust" in discover_repository(tmp_path, probe_capabilities=False).languages


def test_cpp_make_stack_is_discovered_and_executes(tmp_path: Path) -> None:
    _production_only()
    _require_tool("make")
    _require_tool("c++")
    (tmp_path / "calculator.cpp").write_text(
        "int add(int a, int b) { return a + b; }\n",
        encoding="utf-8",
    )
    (tmp_path / "test.cpp").write_text(
        "int add(int, int);\nint main() { return add(2, 3) == 5 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "test:\n\tc++ -std=c++17 calculator.cpp test.cpp -o qualification_test\n\t./qualification_test\n",
        encoding="utf-8",
    )

    command = _run_capability(tmp_path, "test")

    assert command == ("make", "test")
    assert "c++" in discover_repository(tmp_path, probe_capabilities=False).languages
