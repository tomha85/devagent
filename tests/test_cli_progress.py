from __future__ import annotations

from devagent.cli import _ProgressStatus


def test_default_progress_reports_only_stable_engineering_milestones() -> None:
    output: list[str] = []
    progress = _ProgressStatus(output.append)

    events = [
        "[PREFLIGHT]",
        "[WORKTREE] source: /tmp/repo",
        "[DISCOVER]",
        "[DISCOVER] repository files: 120",
        "[UNDERSTAND]",
        "[RETRIEVAL] selected: src/service.py",
        "[TASK_SPEC]",
        "[BASELINE]",
        "[PLAN]",
        "[GATHER_CONTEXT]",
        "[REPRODUCE]",
        "[IMPLEMENT]",
        "[VERIFY_TARGETED]",
        "[DIAGNOSE]",
        "[PLAN]",
        "[GATHER_CONTEXT]",
        "[IMPLEMENT]",
        "[VERIFY_TARGETED]",
        "[VERIFY_BROAD]",
        "[REVIEW]",
        "[QUALITY_CHECK]",
        "[FINAL_VERIFY]",
        "[REPORT]",
    ]
    for event in events:
        progress(event)
    progress.report()

    assert output == [
        "[1/7] DISCOVER / UNDERSTAND",
        "[2/7] REQUIREMENTS / PLAN",
        "[3/7] IMPLEMENT",
        "[4/7] VERIFY / REPAIR IF NEEDED",
        "      ↳ DIAGNOSE",
        "      ↳ REPLAN",
        "      ↳ APPLY CORRECTION",
        "[5/7] INDEPENDENT REVIEW",
        "[6/7] FINAL VERIFICATION",
        "[7/7] ENGINEERING REPORT",
    ]


def test_review_correction_is_named_without_replaying_completed_stages() -> None:
    output: list[str] = []
    progress = _ProgressStatus(output.append)

    for event in (
        "[DISCOVER]",
        "[TASK_SPEC]",
        "[PLAN]",
        "[IMPLEMENT]",
        "[VERIFY_TARGETED]",
        "[REVIEW]",
        "[IMPLEMENT]",
        "[VERIFY_TARGETED]",
        "[REVIEW]",
        "[FINAL_VERIFY]",
    ):
        progress(event)
    progress.report()

    assert "      ↳ APPLY REVIEW FIXES" in output
    assert output.count("[3/7] IMPLEMENT") == 1
    assert output.count("[4/7] VERIFY / REPAIR IF NEEDED") == 1
    assert output.count("[5/7] INDEPENDENT REVIEW") == 1


def test_verbose_progress_preserves_internal_state_and_diagnostics() -> None:
    output: list[str] = []
    progress = _ProgressStatus(output.append, verbose=True)

    events = [
        "[DISCOVER]",
        "[WORKTREE] source: /tmp/repo",
        "[RETRIEVAL] selected: src/service.py",
    ]
    for event in events:
        progress(event)
    progress.report()

    assert output == [*events, "[ENGINEERING_REPORT]"]
