from __future__ import annotations

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
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        "name: fixture-ci\nsteps:\n  - run: python -m compileall -q .\n",
        encoding="utf-8",
    )
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
        "Add a database migration implementing `upgrade_add_email` and `downgrade_add_email`.",
    )

    diagnostic = [
        (item.description, item.status.value, item.reason, item.evidence)
        for item in result.task.acceptance_criteria
        if item.required
    ]
    assert result.task.task_type is TaskType.MIGRATION
    assert all(item.status is AcceptanceStatus.SATISFIED for item in result.task.acceptance_criteria if item.required), diagnostic
    assert result.outcome is Outcome.VERIFIED, (diagnostic, result.not_run)
    assert any(item.phase == "final" and item.command == ("python", "-m", "pytest", "-q") and item.passed for item in result.verification)
    assert any(
        item.phase == "final"
        and item.command == ("python", "-m", "compileall", "-q", ".")
        and item.passed
        for item in result.verification
    )
    working = Path(result.working_root)
    assert "upgrade_add_email" in (working / "migrations.py").read_text(encoding="utf-8")
    assert "downgrade_add_email" in (working / "migrations.py").read_text(encoding="utf-8")
    assert before_migration == (tmp_path / "migrations.py").read_text(encoding="utf-8")
