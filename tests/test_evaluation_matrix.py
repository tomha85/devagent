from __future__ import annotations

import json
from pathlib import Path

import pytest

from devagent.discovery import discover_repository


@pytest.mark.parametrize(
    ("name", "files", "language", "expected_command"),
    [
        ("python", {"pyproject.toml": "[tool.pytest.ini_options]\n", "app.py": "pass\n"}, "python", "pytest"),
        ("typescript", {"package.json": json.dumps({"scripts": {"test": "vitest"}}), "src/app.ts": "export {}"}, "typescript", "npm"),
        ("node", {"package.json": json.dumps({"scripts": {"test": "node --test"}}), "index.js": ""}, "javascript", "npm"),
        ("react", {"package.json": json.dumps({"scripts": {"test": "vitest"}, "dependencies": {"react": "1"}}), "App.tsx": ""}, "typescript", "npm"),
        ("go", {"go.mod": "module fixture\n", "main.go": "package main\n"}, "go", "go"),
        ("rust", {"Cargo.toml": "[package]\nname='x'\nversion='0.1.0'\n", "src/main.rs": "fn main() {}"}, "rust", "cargo"),
        ("cpp", {"CMakeLists.txt": "project(x)\n", "main.cpp": "int main(){}"}, "c++", "ctest"),
        ("java", {"pom.xml": "<project/>", "src/Main.java": "class Main {}"}, "java", "mvn"),
    ],
)
def test_language_fixture_discovery(tmp_path: Path, name: str, files: dict[str, str], language: str, expected_command: str) -> None:
    root = tmp_path / name
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    model = discover_repository(root)
    assert any(language in component.languages for component in model.components)
    assert any(any(token.endswith(expected_command) for token in cap.command) for cap in model.capabilities)


def test_monorepo_evaluation_fixture(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "api" / "go.mod").write_text("module api\n", encoding="utf-8")
    (tmp_path / "api" / "main.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "web" / "package.json").write_text(json.dumps({"scripts": {"build": "vite build", "test": "vitest"}}), encoding="utf-8")
    (tmp_path / "web" / "app.ts").write_text("export {}", encoding="utf-8")
    model = discover_repository(tmp_path)
    assert model.kind == "monorepo"
    assert {component.path for component in model.components} >= {"api", "web"}
