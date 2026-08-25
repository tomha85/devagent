from __future__ import annotations

from devagent.models import FailureClass, RunResult, VerificationResult


def _bounded_output(value: str, limit: int = 1200) -> str:
    text = value.strip()
    if not text:
        return "(none)"
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _verification_line(verification: VerificationResult) -> str:
    status = "✓" if verification.passed else "✗"
    command = " ".join(verification.command)
    details = [f"phase={verification.phase}", f"exit={verification.exit_code}", f"{verification.duration_seconds:.2f}s"]
    if verification.tests_run is not None:
        details.append(f"tests={verification.tests_passed}/{verification.tests_run}")
    if verification.classification:
        details.append(f"class={verification.classification.value}")
    if verification.timed_out:
        details.append("timed_out=true")
    return f"{status} {command} | " + " | ".join(details)


def _default_recommendation(failure_class: FailureClass | None) -> str:
    return {
        FailureClass.ASSERTION_FAILURE: "Inspect the failing assertion and reproduce the behavior with the smallest targeted test before changing code again.",
        FailureClass.SYNTAX_ERROR: "Fix the syntax error first, then rerun targeted and final verification.",
        FailureClass.TYPE_ERROR: "Resolve the reported type error and rerun the repository-supported type/test checks.",
        FailureClass.IMPORT_ERROR: "Verify imports, package layout, and the active environment before retrying verification.",
        FailureClass.DEPENDENCY_ERROR: "Restore the required dependency/environment outside the DevAgent run, then rerun verification.",
        FailureClass.BUILD_ERROR: "Inspect the build output, address the first actionable build failure, then rerun the build and tests.",
        FailureClass.ENVIRONMENT_ERROR: "Repair the local environment or external prerequisite, then rerun the same verification commands.",
        FailureClass.TIMEOUT: "Reproduce the timeout directly and determine whether the command, test, or external dependency is hanging.",
        FailureClass.FLAKY_TEST: "Repeat the failing test independently and stabilize the nondeterministic behavior before publishing.",
        FailureClass.BASELINE_FAILURE: "Repair or explicitly accept the baseline failure before judging the proposed change.",
        FailureClass.NEW_REGRESSION: "Fix the new regression and rerun the complete final verification set before publishing.",
        FailureClass.UNKNOWN: "Inspect the captured stdout/stderr and classify the failure before another repair attempt.",
        None: "Inspect the captured stdout/stderr and rerun the failing check after addressing the underlying cause.",
    }[failure_class]


def recommendations_for(result: RunResult) -> list[str]:
    recommendations = list(result.recommendations)
    for verification in result.verification:
        if not verification.passed:
            recommendations.append(_default_recommendation(verification.classification))
    if result.review and not result.review.approved:
        recommendations.append("Address every independent-review issue and rerun verification plus independent review before publishing.")
    if result.source_control.error:
        recommendations.append(f"Resolve source-control publication failure: {result.source_control.error}")
    if result.source_control.pushed:
        recommendations.append("Review the pushed DevAgent branch before any human-created pull request or merge.")
    unique: list[str] = []
    for item in recommendations:
        if item and item not in unique:
            unique.append(item)
    return unique


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

    lines.extend(
        [
            "",
            "VERIFICATION SUMMARY",
            f"Passed checks: {len(passed)}",
            f"Failed checks: {len(failed)}",
            "",
            "VERIFICATION RESULTS",
        ]
    )
    lines.extend(_verification_line(verification) for verification in result.verification)
    if not result.verification:
        lines.append("(none)")

    if failed:
        lines.extend(["", "FAILED CHECK DETAILS"])
        for verification in failed:
            classification = verification.classification.value if verification.classification else "UNKNOWN"
            lines.extend(
                [
                    f"- {' '.join(verification.command)}",
                    f"  phase: {verification.phase}",
                    f"  classification: {classification}",
                    f"  exit code: {verification.exit_code}",
                    "  stdout:",
                    "    " + _bounded_output(verification.stdout).replace("\n", "\n    "),
                    "  stderr:",
                    "    " + _bounded_output(verification.stderr).replace("\n", "\n    "),
                ]
            )

    lines.extend(["", "INDEPENDENT REVIEW"])
    if result.review:
        lines.append("APPROVED" if result.review.approved else "REJECTED")
        lines.append(result.review.summary)
        for issue in result.review.issues:
            location = f" [{issue.path}]" if issue.path else ""
            lines.append(f"- {issue.severity.upper()}{location}: {issue.reason}")
    else:
        lines.append("Not completed")

    lines.extend(["", "NEW REGRESSIONS", "0" if result.outcome.value == "VERIFIED" else "Not proven"])
    if result.not_run:
        lines.extend(["", "NOT RUN"])
        lines.extend(f"- {reason}" for reason in result.not_run)

    recommendations = recommendations_for(result)
    if recommendations:
        lines.extend(["", "RECOMMENDATIONS"])
        lines.extend(f"- {item}" for item in recommendations)

    source = result.source_control
    lines.extend(["", "SOURCE CONTROL"])
    if source.requested:
        lines.extend(
            [
                f"Remote: {source.remote or '(none)'}",
                f"Branch: {source.branch or '(none)'}",
                f"Commit: {source.commit or 'NOT CREATED'}",
                f"Committed: {'YES' if source.committed else 'NO'}",
                f"Pushed: {'YES' if source.pushed else 'NO'}",
            ]
        )
        if source.error:
            lines.append(f"Publication error: {source.error}")
    else:
        lines.extend(["Publishing: not requested", "No commit", "No push"])
    lines.extend(["Pull request: NOT CREATED", "Merge: NOT PERFORMED"])

    lines.extend(["", "DEVELOPER ACTION"])
    if source.pushed:
        lines.append(f"Review remote branch: {source.remote}/{source.branch}")
        lines.append("Create a PR or merge only after developer review, if desired.")
    else:
        lines.append(f"Review: git -C {result.working_root} diff")
    return "\n".join(lines)
