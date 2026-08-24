from __future__ import annotations

from devagent.models import RunResult


def render_report(result: RunResult) -> str:
    passed = [verification for verification in result.verification if verification.passed]
    failed = [verification for verification in result.verification if not verification.passed]
    lines = [
        "DEVAGENT REPORT",
        "",
        "STATUS",
        result.outcome.value,
        "",
        "TASK",
        result.task.goal,
        "",
        "REPOSITORY",
        result.repository.root,
        "",
        "WORKING BRANCH",
        result.repository.git_branch or "(not a Git branch)",
        "",
        "WORKING ROOT",
        result.working_root,
        "",
        "ROOT CAUSE",
        result.root_cause or "Not established",
        "",
        "IMPLEMENTATION",
    ]
    lines.extend(f"- {item}" for item in result.implementation or ["No implementation completed"])
    lines.extend(["", "FILES CHANGED"])
    lines.extend(result.changes.paths or ["(none)"])
    lines.extend(["", "VERIFICATION"])
    for verification in passed:
        lines.append(f"✓ {' '.join(verification.command)} ({verification.duration_seconds:.2f}s)")
    for verification in failed:
        classification = verification.classification.value if verification.classification else "UNKNOWN"
        lines.append(f"✗ {' '.join(verification.command)} ({classification})")
    if result.review:
        lines.append("✓ independent review" if result.review.approved else "✗ independent review")
    lines.extend(["", "NEW REGRESSIONS", "0" if result.outcome.value == "VERIFIED" else "Not proven"])
    if result.not_run:
        lines.extend(["", "NOT RUN"])
        lines.extend(f"- {reason}" for reason in result.not_run)
    if result.recommendations:
        lines.extend(["", "RECOMMENDATIONS"])
        lines.extend(f"- {item}" for item in result.recommendations)
    lines.extend(["", "SOURCE CONTROL", "No commit", "No push", "No merge", "", "DEVELOPER ACTION", f"Review: git -C {result.working_root} diff"])
    return "\n".join(lines)

