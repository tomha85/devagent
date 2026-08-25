from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devagent.artifacts import RunArtifacts
from devagent.discovery import discover_repository
from devagent.orchestrator import IMPLEMENT_SCHEMA, OrchestrationError, _execute_actions
from devagent.providers import validate_response
from devagent.workspace import Workspace


def test_structural_actions_are_first_class_strict_provider_contract() -> None:
    response = {
        "actions": [
            {"tool": "rename_file", "arguments": {"source": "old.py", "destination": "new.py"}},
            {"tool": "move_file", "arguments": {"source": "a.txt", "destination": "archive/a.txt"}},
            {"tool": "delete_file", "arguments": {"path": "legacy.txt"}},
        ],
        "summary": "Restructured files safely.",
    }

    assert validate_response("implement", response, IMPLEMENT_SCHEMA) == response


def test_structural_action_executor_requires_both_move_paths_to_be_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    (tmp_path / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path, RunArtifacts(tmp_path, run_id="structural-contract"))
    response = {
        "actions": [
            {"tool": "rename_file", "arguments": {"source": "old.py", "destination": "new.py"}}
        ],
        "summary": "Rename.",
    }

    with pytest.raises(OrchestrationError, match="not inspected/planned"):
        _execute_actions(workspace, response, {"old.py"})

    changed = _execute_actions(workspace, response, {"old.py", "new.py"})
    assert changed == ["old.py", "new.py"]
    assert not (tmp_path / "old.py").exists()
    assert (tmp_path / "new.py").is_file()


def test_maven_and_gradle_kotlin_dsl_are_first_class_java_capabilities(tmp_path: Path) -> None:
    maven = tmp_path / "maven"
    maven.mkdir()
    (maven / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    gradle = tmp_path / "gradle"
    gradle.mkdir()
    (gradle / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")

    repository = discover_repository(tmp_path, probe_capabilities=False)

    maven_component = next(item for item in repository.components if item.path == "maven")
    gradle_component = next(item for item in repository.components if item.path == "gradle")
    assert "java" in maven_component.languages
    assert "maven" in maven_component.frameworks
    assert any(item.command == ("mvn", "-f", "maven/pom.xml", "test") for item in maven_component.capabilities)
    assert "java" in gradle_component.languages
    assert "gradle" in gradle_component.frameworks
    assert any(item.command == ("gradle", "-p", "gradle", "test") for item in gradle_component.capabilities)


def test_dotnet_project_and_solution_discovery_is_first_class(tmp_path: Path) -> None:
    app = tmp_path / "src" / "App"
    app.mkdir(parents=True)
    (app / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n',
        encoding="utf-8",
    )
    tests = tmp_path / "tests" / "App.Tests"
    tests.mkdir(parents=True)
    (tests / "App.Tests.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        '<TargetFramework>net8.0</TargetFramework><IsTestProject>true</IsTestProject>'
        '</PropertyGroup><ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.10.0" />'
        '</ItemGroup></Project>\n',
        encoding="utf-8",
    )
    (tmp_path / "DevAgent.sln").write_text("\n", encoding="utf-8")

    repository = discover_repository(tmp_path, probe_capabilities=False)
    commands = {item.command for item in repository.capabilities}

    assert ("dotnet", "build", "DevAgent.sln") in commands
    assert ("dotnet", "build", "src/App/App.csproj") in commands
    assert ("dotnet", "test", "tests/App.Tests/App.Tests.csproj") in commands
    assert "c#" in {language for component in repository.components for language in component.languages}


def test_huge_monorepo_priority_manifest_recovery_uses_git_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "visible.py").write_text("VALUE = 1\n", encoding="utf-8")
    deep = tmp_path / "deep" / "Service"
    deep.mkdir(parents=True)
    manifest = deep / "Service.csproj"
    manifest.write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
        '</PropertyGroup></Project>\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    monkeypatch.setattr("devagent.discovery._walk", lambda root, limit=12_000: [root / "visible.py"])
    repository = discover_repository(tmp_path, probe_capabilities=False)

    component = next(item for item in repository.components if item.path == "deep/Service")
    assert "deep/Service/Service.csproj" in component.manifests
    assert "c#" in component.languages
    assert any(item.command[:2] == ("dotnet", "build") for item in component.capabilities)
