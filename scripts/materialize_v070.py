from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one patch marker, found {count}")
    write(path, text.replace(old, new, 1))


def append_before(path: str, marker: str, content: str) -> None:
    replace_once(path, marker, content + marker)


replace_once(
    "devagent/workspace.py",
    '''    def _structural_file(self, path: str) -> Path:
        lexical = self.root / path
        if lexical.is_symlink():
            raise SafetyError(f"Structural file operations do not follow symlinks: {path}")
        target = self._writable(path)
''',
    '''    def _reject_structural_symlinks(self, path: str) -> None:
        lexical = self.root
        for part in Path(path).parts:
            lexical = lexical / part
            if lexical.is_symlink():
                raise SafetyError(f"Structural file operations do not follow symlinks: {path}")

    def _structural_file(self, path: str) -> Path:
        self._reject_structural_symlinks(path)
        target = self._writable(path)
''',
)
replace_once(
    "devagent/workspace.py",
    '''        destination_lexical = self.root / destination
        if destination_lexical.is_symlink():
            raise SafetyError(f"Structural destination cannot be a symlink: {destination}")
        destination_target = self._writable(destination)
''',
    '''        self._reject_structural_symlinks(destination)
        destination_target = self._writable(destination)
''',
)

replace_once(
    "devagent/orchestrator.py",
    '''_ACTION = {"anyOf": [_REPLACE_ACTION, _COUNTED_REPLACE_ACTION, _WRITE_ACTION]}
''',
    '''_DELETE_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "delete_file"},
        "arguments": {
            "type": "object",
            "properties": {"path": _NON_EMPTY_STRING},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_MOVE_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "move_file"},
        "arguments": {
            "type": "object",
            "properties": {"source": _NON_EMPTY_STRING, "destination": _NON_EMPTY_STRING},
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_RENAME_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "rename_file"},
        "arguments": {
            "type": "object",
            "properties": {"source": _NON_EMPTY_STRING, "destination": _NON_EMPTY_STRING},
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_ACTION = {
    "anyOf": [
        _REPLACE_ACTION,
        _COUNTED_REPLACE_ACTION,
        _WRITE_ACTION,
        _DELETE_ACTION,
        _MOVE_ACTION,
        _RENAME_ACTION,
    ]
}
''',
)

old_execute = '''def _execute_actions(workspace: Workspace, response: dict[str, Any], allowed_paths: set[str]) -> list[str]:
    if not isinstance(response, dict) or set(response) != {"actions", "summary"}:
        raise OrchestrationError("Invalid implement response: expected exactly actions and summary")
    actions = response.get("actions")
    if not isinstance(actions, list) or not actions:
        raise OrchestrationError("Implementation requires at least one structured action")
    changed: list[str] = []
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"tool", "arguments"} or not isinstance(action["arguments"], dict):
            raise OrchestrationError("Every action must contain only tool and arguments")
        tool, arguments = action["tool"], action["arguments"]
        if tool not in {"replace_text", "write_file"}:
            raise OrchestrationError(f"Implementation tool is not allowed: {tool}")
        path = arguments.get("path")
        if not isinstance(path, str) or path not in allowed_paths:
            raise OrchestrationError(f"Action path was not inspected/planned: {path}")
        if tool == "replace_text":
            required = {"path", "old", "new"}
            if frozenset(arguments) not in {frozenset(required), frozenset({*required, "count"})}:
                raise OrchestrationError("replace_text arguments must contain only path, old, new, and optional count")
            if not all(isinstance(arguments[key], str) for key in required) or not arguments["old"]:
                raise OrchestrationError("replace_text requires string path, non-empty old, and string new")
            count = arguments.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise OrchestrationError("replace_text count must be an integer greater than zero")
            workspace.replace_text(path, arguments["old"], arguments["new"], count)
        else:
            if set(arguments) != {"path", "content"} or not isinstance(arguments.get("content"), str):
                raise OrchestrationError("write_file requires only string path and content")
            workspace.write_file(path, arguments["content"])
        changed.append(path)
    return changed
'''
new_execute = '''def _execute_actions(workspace: Workspace, response: dict[str, Any], allowed_paths: set[str]) -> list[str]:
    if not isinstance(response, dict) or set(response) != {"actions", "summary"}:
        raise OrchestrationError("Invalid implement response: expected exactly actions and summary")
    actions = response.get("actions")
    if not isinstance(actions, list) or not actions:
        raise OrchestrationError("Implementation requires at least one structured action")
    changed: list[str] = []
    allowed_tools = {"replace_text", "write_file", "delete_file", "move_file", "rename_file"}
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"tool", "arguments"} or not isinstance(action["arguments"], dict):
            raise OrchestrationError("Every action must contain only tool and arguments")
        tool, arguments = action["tool"], action["arguments"]
        if tool not in allowed_tools:
            raise OrchestrationError(f"Implementation tool is not allowed: {tool}")

        if tool in {"move_file", "rename_file"}:
            if set(arguments) != {"source", "destination"}:
                raise OrchestrationError(f"{tool} requires only string source and destination")
            source = arguments.get("source")
            destination = arguments.get("destination")
            if (
                not isinstance(source, str)
                or not isinstance(destination, str)
                or source not in allowed_paths
                or destination not in allowed_paths
            ):
                raise OrchestrationError(
                    f"Structural source/destination was not inspected/planned: {source} -> {destination}"
                )
            if tool == "move_file":
                workspace.move_file(source, destination)
            else:
                workspace.rename_file(source, destination)
            changed.extend((source, destination))
            continue

        path = arguments.get("path")
        if not isinstance(path, str) or path not in allowed_paths:
            raise OrchestrationError(f"Action path was not inspected/planned: {path}")
        if tool == "replace_text":
            required = {"path", "old", "new"}
            if frozenset(arguments) not in {frozenset(required), frozenset({*required, "count"})}:
                raise OrchestrationError("replace_text arguments must contain only path, old, new, and optional count")
            if not all(isinstance(arguments[key], str) for key in required) or not arguments["old"]:
                raise OrchestrationError("replace_text requires string path, non-empty old, and string new")
            count = arguments.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise OrchestrationError("replace_text count must be an integer greater than zero")
            workspace.replace_text(path, arguments["old"], arguments["new"], count)
        elif tool == "write_file":
            if set(arguments) != {"path", "content"} or not isinstance(arguments.get("content"), str):
                raise OrchestrationError("write_file requires only string path and content")
            workspace.write_file(path, arguments["content"])
        else:
            if set(arguments) != {"path"}:
                raise OrchestrationError("delete_file requires only string path")
            workspace.delete_file(path)
        changed.append(path)
    return list(dict.fromkeys(changed))
'''
replace_once("devagent/orchestrator.py", old_execute, new_execute)

append_before(
    "tests/test_structural_operations.py",
    "\ndef test_structural_operations_reject_directories_and_symlinks",
    '''
def test_structural_operations_reject_symlinked_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    real = tmp_path / "real"
    real.mkdir()
    (real / "source.txt").write_text("payload\\n", encoding="utf-8")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    workspace, _artifacts = _workspace(tmp_path)

    with pytest.raises(SafetyError, match="do not follow symlinks"):
        workspace.delete_file("linked/source.txt")
    with pytest.raises(SafetyError, match="do not follow symlinks"):
        workspace.move_file("real/source.txt", "linked/destination.txt")


''',
)

write(
    "tests/test_v070_engineering_breadth.py",
    r'''from __future__ import annotations

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
''',
)

write(
    "tests/test_structural_devagent_e2e_v070.py",
    r'''from __future__ import annotations

import subprocess
from pathlib import Path

from devagent.models import AcceptanceStatus, Outcome
from devagent.orchestrator import DevAgent
from devagent.providers import ScriptedFakeProvider


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_structural_rename_delete_refactor_is_verified_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "legacy.py").write_text("LEGACY = True\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    before_test = "from service import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    after_test = "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    (tests / "test_service.py").write_text(before_test, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "DevAgent Qualification")
    _git(tmp_path, "config", "user.email", "qualification@example.com")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")

    responses = [
        {
            "_role": "understand",
            "problem": "The calculator implementation is stored under an obsolete module name and an unused legacy file remains.",
            "expected_behavior": "The module is renamed to calculator.py, legacy.py is removed, and existing addition behavior remains unchanged.",
            "affected_paths": ["service.py", "calculator.py", "legacy.py", "tests/test_service.py"],
            "root_cause": "Repository structure still uses service.py and retains legacy.py while the regression test imports the old module.",
            "evidence": [
                {"statement": "service.py contains the addition implementation.", "paths": ["service.py"], "confidence": 1.0},
                {"statement": "legacy.py is the obsolete file requested for removal.", "paths": ["legacy.py"], "confidence": 1.0},
                {"statement": "The test imports service.py.", "paths": ["tests/test_service.py"], "confidence": 1.0},
            ],
            "proposed_solution": [
                "Rename service.py to calculator.py.",
                "Remove legacy.py using the structural delete tool.",
                "Update the regression test import without changing behavior.",
            ],
            "confidence": 0.99,
        },
        {
            "_role": "plan",
            "files_to_inspect": ["service.py", "calculator.py", "legacy.py", "tests/test_service.py"],
            "implementation": [
                "Rename service.py to calculator.py.",
                "Delete legacy.py.",
                "Update the test import.",
            ],
            "verification": [["python", "-m", "pytest", "-q"], ["git", "diff", "--check"]],
            "rationale": "These are the complete evidence-backed structural and test paths.",
        },
        {
            "_role": "implement",
            "actions": [
                {"tool": "rename_file", "arguments": {"source": "service.py", "destination": "calculator.py"}},
                {"tool": "delete_file", "arguments": {"path": "legacy.py"}},
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "tests/test_service.py",
                        "old": before_test,
                        "new": after_test,
                    },
                },
            ],
            "summary": [
                "Renamed the calculator module without rewriting its contents.",
                "Removed the obsolete legacy module.",
                "Updated the existing regression import.",
            ],
        },
        {
            "_role": "review",
            "approved": True,
            "issues": [],
            "summary": "The structural refactor is minimal and preserves verified behavior.",
        },
    ]

    result = DevAgent(ScriptedFakeProvider(responses)).run(
        tmp_path,
        "Refactor the calculator module: rename `service.py` to `calculator.py`, remove `legacy.py`, preserve existing addition behavior, and verify tests.",
    )

    assert result.outcome is Outcome.VERIFIED
    assert all(item.status is AcceptanceStatus.SATISFIED for item in result.task.acceptance_criteria if item.required)
    working = Path(result.working_root)
    assert not (working / "service.py").exists()
    assert (working / "calculator.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert not (working / "legacy.py").exists()
    assert (tmp_path / "service.py").is_file()
    assert (tmp_path / "legacy.py").is_file()
    backups = Path(result.run_dir) / "backups"
    assert (backups / "service.py").is_file()
    assert (backups / "legacy.py").is_file()
    assert any(item.phase == "final" and item.command == ("python", "-m", "pytest", "-q") and item.passed for item in result.verification)
''',
)

write(
    "tests/test_migration_e2e_v070.py",
    r'''from __future__ import annotations

import subprocess
from pathlib import Path

from devagent.models import AcceptanceStatus, Outcome, TaskType
from devagent.orchestrator import DevAgent
from devagent.providers import ScriptedFakeProvider


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_sqlite_migration_forward_rollback_and_existing_state_are_verified(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    before_migration = (
        "def create_schema(conn):\n"
        "    conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')\n"
    )
    after_migration = (
        "def create_schema(conn):\n"
        "    conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')\n\n"
        "def upgrade_add_email(conn):\n"
        "    conn.execute('ALTER TABLE users ADD COLUMN email TEXT')\n\n"
        "def downgrade_add_email(conn):\n"
        "    # Rollback uses a table-copy contract so existing id/name data is preserved.\n"
        "    conn.execute('ALTER TABLE users RENAME TO users_with_email')\n"
        "    conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')\n"
        "    conn.execute('INSERT INTO users (id, name) SELECT id, name FROM users_with_email')\n"
        "    conn.execute('DROP TABLE users_with_email')\n"
    )
    before_test = (
        "import sqlite3\n"
        "from migrations import create_schema\n\n"
        "def test_create_schema():\n"
        "    conn = sqlite3.connect(':memory:')\n"
        "    create_schema(conn)\n"
        "    conn.execute(\"INSERT INTO users (name) VALUES ('Ada')\")\n"
        "    assert conn.execute('SELECT name FROM users').fetchone() == ('Ada',)\n"
    )
    after_test = (
        "import sqlite3\n"
        "from migrations import create_schema, downgrade_add_email, upgrade_add_email\n\n"
        "def test_create_schema():\n"
        "    conn = sqlite3.connect(':memory:')\n"
        "    create_schema(conn)\n"
        "    conn.execute(\"INSERT INTO users (name) VALUES ('Ada')\")\n"
        "    assert conn.execute('SELECT name FROM users').fetchone() == ('Ada',)\n\n"
        "def test_email_migration_preserves_representative_existing_state_and_rolls_back():\n"
        "    conn = sqlite3.connect(':memory:')\n"
        "    create_schema(conn)\n"
        "    conn.execute(\"INSERT INTO users (name) VALUES ('Ada')\")\n"
        "    upgrade_add_email(conn)\n"
        "    columns = [row[1] for row in conn.execute('PRAGMA table_info(users)')]\n"
        "    assert columns == ['id', 'name', 'email']\n"
        "    assert conn.execute('SELECT id, name, email FROM users').fetchone() == (1, 'Ada', None)\n"
        "    conn.execute(\"UPDATE users SET email='ada@example.com' WHERE id=1\")\n"
        "    downgrade_add_email(conn)\n"
        "    columns = [row[1] for row in conn.execute('PRAGMA table_info(users)')]\n"
        "    assert columns == ['id', 'name']\n"
        "    assert conn.execute('SELECT id, name FROM users').fetchone() == (1, 'Ada')\n"
    )
    (tmp_path / "migrations.py").write_text(before_migration, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_migrations.py").write_text(before_test, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "DevAgent Qualification")
    _git(tmp_path, "config", "user.email", "qualification@example.com")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")

    responses = [
        {
            "_role": "understand",
            "problem": "The current users schema has no email field or reversible migration.",
            "expected_behavior": "A forward migration adds nullable email while preserving rows, and a rollback restores the original schema while preserving supported id/name data.",
            "affected_paths": ["migrations.py", "tests/test_migrations.py"],
            "root_cause": "migrations.py only creates the baseline schema and has no forward or downgrade migration functions.",
            "evidence": [
                {"statement": "migrations.py defines only the baseline users table.", "paths": ["migrations.py"], "confidence": 1.0},
                {"statement": "The existing test covers only baseline row storage.", "paths": ["tests/test_migrations.py"], "confidence": 1.0},
            ],
            "proposed_solution": [
                "Add upgrade_add_email and downgrade_add_email.",
                "Exercise forward migration, representative existing data, and rollback in SQLite.",
            ],
            "confidence": 0.99,
        },
        {
            "_role": "plan",
            "files_to_inspect": ["migrations.py", "tests/test_migrations.py"],
            "implementation": [
                "Add explicit forward and rollback migration functions.",
                "Add representative existing-state forward/rollback tests.",
            ],
            "verification": [["python", "-m", "pytest", "-q"], ["git", "diff", "--check"]],
            "rationale": "The migration implementation and its regression test are the complete affected surface.",
        },
        {
            "_role": "implement",
            "actions": [
                {
                    "tool": "replace_text",
                    "arguments": {"path": "migrations.py", "old": before_migration, "new": after_migration},
                },
                {
                    "tool": "replace_text",
                    "arguments": {"path": "tests/test_migrations.py", "old": before_test, "new": after_test},
                },
            ],
            "summary": [
                "Added a nullable-email forward migration.",
                "Added a rollback that restores the original schema while preserving supported existing data.",
                "Added representative SQLite migration coverage.",
            ],
        },
        {
            "_role": "review",
            "approved": True,
            "issues": [],
            "summary": "The migration is explicitly reversible and verified against representative existing data.",
        },
    ]

    result = DevAgent(ScriptedFakeProvider(responses)).run(
        tmp_path,
        "Add a database migration for a nullable `email` column, preserve existing rows, support rollback, and add representative migration tests.",
    )

    assert result.task.task_type is TaskType.MIGRATION
    assert result.outcome is Outcome.VERIFIED
    assert all(item.status is AcceptanceStatus.SATISFIED for item in result.task.acceptance_criteria if item.required)
    assert any(item.phase == "final" and item.command == ("python", "-m", "pytest", "-q") and item.passed for item in result.verification)
    working = Path(result.working_root)
    assert "upgrade_add_email" in (working / "migrations.py").read_text(encoding="utf-8")
    assert "downgrade_add_email" in (working / "migrations.py").read_text(encoding="utf-8")
    assert before_migration == (tmp_path / "migrations.py").read_text(encoding="utf-8")
''',
)

append_before(
    "tests/test_multistack_qualification.py",
    "\ndef test_cpp_make_stack_is_discovered_and_executes",
    r'''
def test_java_maven_stack_is_discovered_and_executes(tmp_path: Path) -> None:
    _production_only()
    _require_tool("java")
    _require_tool("mvn")
    (tmp_path / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        '  <modelVersion>4.0.0</modelVersion>\n'
        '  <groupId>example</groupId><artifactId>qualification-java</artifactId><version>0.0.1</version>\n'
        '  <properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target></properties>\n'
        '  <dependencies>\n'
        '    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><version>5.10.2</version><scope>test</scope></dependency>\n'
        '  </dependencies>\n'
        '  <build><plugins>\n'
        '    <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>\n'
        '  </plugins></build>\n'
        '</project>\n',
        encoding="utf-8",
    )
    source = tmp_path / "src" / "main" / "java" / "example"
    source.mkdir(parents=True)
    (source / "Calculator.java").write_text(
        "package example; public final class Calculator { public static int add(int a, int b) { return a + b; } }\n",
        encoding="utf-8",
    )
    tests = tmp_path / "src" / "test" / "java" / "example"
    tests.mkdir(parents=True)
    (tests / "CalculatorTest.java").write_text(
        "package example; import static org.junit.jupiter.api.Assertions.assertEquals; "
        "import org.junit.jupiter.api.Test; final class CalculatorTest { "
        "@Test void adds() { assertEquals(5, Calculator.add(2, 3)); } }\n",
        encoding="utf-8",
    )

    command = _run_capability(tmp_path, "test")

    assert command == ("mvn", "test")
    repository = discover_repository(tmp_path, probe_capabilities=False)
    assert "java" in _languages(tmp_path)
    assert "maven" in {framework for component in repository.components for framework in component.frameworks}


def test_dotnet_stack_is_discovered_and_executes_build(tmp_path: Path) -> None:
    _production_only()
    _require_tool("dotnet")
    (tmp_path / "Qualification.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        '<TargetFramework>net8.0</TargetFramework><OutputType>Library</OutputType>'
        '</PropertyGroup></Project>\n',
        encoding="utf-8",
    )
    (tmp_path / "Calculator.cs").write_text(
        "namespace Qualification; public static class Calculator { public static int Add(int a, int b) => a + b; }\n",
        encoding="utf-8",
    )

    command = _run_capability(tmp_path, "build")

    assert command[:3] == ("dotnet", "build", "Qualification.csproj")
    assert "c#" in _languages(tmp_path)


''',
)

write(
    "tests/test_huge_monorepo_v070.py",
    r'''from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from devagent.discovery import discover_repository


def _production_only() -> None:
    if os.getenv("DEVAGENT_PRODUCTION_QUALIFICATION") != "1":
        pytest.skip("huge-monorepo stress runs only through the production qualification gate")


def test_deep_dotnet_manifest_is_recovered_beyond_12000_file_walk_frontier(tmp_path: Path) -> None:
    _production_only()
    filler = tmp_path / "aaa"
    filler.mkdir()
    for index in range(12_025):
        (filler / f"f{index:05d}.txt").write_text("", encoding="utf-8")

    deep = tmp_path / "zzz" / "service"
    deep.mkdir(parents=True)
    (deep / "Service.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
        '</PropertyGroup></Project>\n',
        encoding="utf-8",
    )
    (deep / "Service.cs").write_text(
        "namespace Deep; public static class Service { public static int Value => 1; }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    repository = discover_repository(tmp_path, probe_capabilities=False)

    assert repository.inventory_file_count == 12_000
    component = next(item for item in repository.components if item.path == "zzz/service")
    assert "zzz/service/Service.csproj" in component.manifests
    assert "c#" in component.languages
    assert any(item.command == ("dotnet", "build", "zzz/service/Service.csproj") for item in component.capabilities)
''',
)

replace_once("devagent/__init__.py", '__version__ = "0.6.0"', '__version__ = "0.7.0"')
replace_once("pyproject.toml", 'version = "0.6.0"', 'version = "0.7.0"')
replace_once("tests/test_production_v040.py", 'assert version == "0.6.0"', 'assert version == "0.7.0"')
replace_once(
    ".github/workflows/ci.yml",
    '''          make --version | head -1
''',
    '''          make --version | head -1
          java -version
          mvn -version | head -1
          dotnet --version
''',
)
replace_once(
    "CHANGELOG.md",
    "# Changelog\n\n",
    '''# Changelog

## 0.7.0 - Engineering breadth

- Add backup-first structural `delete_file`, `move_file`, and `rename_file` actions with strict planning, dirty-file, path-containment, no-overwrite, and symlink safety.
- Promote structural operations into the strict provider action contract and verify rename/delete refactors end to end in isolated worktrees.
- Add first-class Java Maven/Gradle and .NET solution/project discovery, including C#, F#, Visual Basic, Maven Wrapper, and Gradle Kotlin DSL manifests.
- Add real Java/Maven and .NET build qualification on the production runner.
- Add a real SQLite migration E2E that proves forward migration, representative existing-state preservation, and rollback behavior.
- Recover tracked high-value manifests through the Git index when the normal 12,000-file inventory frontier is reached, with a real >12k-file monorepo stress case.
- Bump the package version to 0.7.0.

''',
)

catalog_path = ROOT / "evaluation/benchmark_v4.json"
catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
for category in ("structural_operations", "migration_runtime"):
    if category not in catalog["required_categories"]:
        catalog["required_categories"].append(category)

new_cases = [
    {
        "id": "structured-structural-tools",
        "category": "structural_operations",
        "pytest_node": "tests/test_v070_engineering_breadth.py::test_structural_actions_are_first_class_strict_provider_contract",
        "expected": "PASS",
    },
    {
        "id": "structural-refactor-e2e",
        "category": "structural_operations",
        "pytest_node": "tests/test_structural_devagent_e2e_v070.py::test_structural_rename_delete_refactor_is_verified_end_to_end",
        "expected": "VERIFIED",
    },
    {
        "id": "java-gradle-discovery",
        "category": "real_stack",
        "pytest_node": "tests/test_v070_engineering_breadth.py::test_maven_and_gradle_kotlin_dsl_are_first_class_java_capabilities",
        "expected": "PASS",
    },
    {
        "id": "dotnet-project-solution-discovery",
        "category": "real_stack",
        "pytest_node": "tests/test_v070_engineering_breadth.py::test_dotnet_project_and_solution_discovery_is_first_class",
        "expected": "PASS",
    },
    {
        "id": "real-stack-java-maven",
        "category": "real_stack",
        "pytest_node": "tests/test_multistack_qualification.py::test_java_maven_stack_is_discovered_and_executes",
        "expected": "PASS",
    },
    {
        "id": "real-stack-dotnet-build",
        "category": "real_stack",
        "pytest_node": "tests/test_multistack_qualification.py::test_dotnet_stack_is_discovered_and_executes_build",
        "expected": "PASS",
    },
    {
        "id": "sqlite-migration-forward-rollback",
        "category": "migration_runtime",
        "pytest_node": "tests/test_migration_e2e_v070.py::test_sqlite_migration_forward_rollback_and_existing_state_are_verified",
        "expected": "VERIFIED",
    },
    {
        "id": "huge-monorepo-deep-manifest",
        "category": "large_repo",
        "pytest_node": "tests/test_huge_monorepo_v070.py::test_deep_dotnet_manifest_is_recovered_beyond_12000_file_walk_frontier",
        "expected": "deep tracked manifest remains discoverable beyond bounded walk frontier",
    },
]
existing_ids = {case["id"] for case in catalog["cases"]}
for case in new_cases:
    if case["id"] in existing_ids:
        raise RuntimeError(f"qualification case already exists: {case['id']}")
catalog["cases"].extend(new_cases)
catalog_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")

for path in (
    "devagent/workspace.py",
    "devagent/orchestrator.py",
    "tests/test_structural_operations.py",
    "tests/test_v070_engineering_breadth.py",
    "tests/test_structural_devagent_e2e_v070.py",
    "tests/test_migration_e2e_v070.py",
    "tests/test_multistack_qualification.py",
    "tests/test_huge_monorepo_v070.py",
):
    subprocess.run(["python", "-m", "py_compile", str(ROOT / path)], check=True)

workflow = ROOT / ".github" / "workflows" / "v070-materialize.yml"
self_path = Path(__file__).resolve()
if workflow.exists():
    workflow.unlink()
self_path.unlink()

subprocess.run(["git", "config", "user.name", "DevAgent Release Engineering"], cwd=ROOT, check=True)
subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], cwd=ROOT, check=True)
subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
if not status.strip():
    raise RuntimeError("v0.7 materializer produced no changes")
subprocess.run(
    ["git", "commit", "-m", "feat(v0.7): complete engineering breadth qualification"],
    cwd=ROOT,
    check=True,
)
