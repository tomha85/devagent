from __future__ import annotations

import json
from pathlib import Path

from devagent.discovery import discover_repository
from devagent.memory import RepositoryMemory


def test_python_repository_discovery_is_evidence_backed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    model = discover_repository(tmp_path)
    assert model.components[0].languages == ["python"]
    assert any(cap.kind == "test" and "pytest" in cap.command for cap in model.capabilities)
    assert model.facts and model.facts[0].fingerprints


def test_monorepo_components_and_package_scripts(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (backend / "app.py").write_text("pass\n", encoding="utf-8")
    (frontend / "package.json").write_text(json.dumps({"scripts": {"test:unit": "vitest", "build": "vite build"}, "dependencies": {"react": "1"}}), encoding="utf-8")
    (frontend / "app.tsx").write_text("export {}\n", encoding="utf-8")
    model = discover_repository(tmp_path)
    assert model.kind == "monorepo"
    assert {component.path for component in model.components} >= {"backend", "frontend"}
    assert any(cap.command == ("npm", "--prefix", "frontend", "run", "test:unit") for cap in model.capabilities)


def test_ci_commands_are_preferred_repository_evidence(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("steps:\n  - run: npm run test:unit\n  - run: npm run build\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test:unit": "vitest", "build": "vite build"}}), encoding="utf-8")
    model = discover_repository(tmp_path)
    assert any(cap.source == ".github/workflows/ci.yml" and cap.command == ("npm", "run", "test:unit") for cap in model.capabilities)
    assert any(".github/workflows/ci.yml" in fact.evidence for fact in model.facts)


def test_repository_memory_invalidates_changed_evidence(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    model = discover_repository(tmp_path)
    memory = RepositoryMemory(tmp_path)
    memory.store_facts(model.facts)
    assert memory.load_facts()
    manifest.write_text("[project]\nname='changed'\n", encoding="utf-8")
    assert memory.load_facts() == []
