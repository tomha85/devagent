from __future__ import annotations

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
        """Refactor the calculator module layout.

Acceptance criteria:
- Rename `service.py` to `calculator.py`
- Remove `legacy.py`
- Preserve existing service behavior
- Verify relevant tests
""",
    )

    diagnostic = [
        (item.description, item.status.value, item.reason, item.evidence)
        for item in result.task.acceptance_criteria
        if item.required
    ]
    assert all(item.status is AcceptanceStatus.SATISFIED for item in result.task.acceptance_criteria if item.required), diagnostic
    assert result.outcome is Outcome.VERIFIED, diagnostic
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
