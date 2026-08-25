from __future__ import annotations

from devagent.models import FailureClass, Outcome, RunResult, VerificationResult


def _bounded_output(value: str, limit: int = 1200) -> str:
    text = value.strip()
    if not text:
        return "(none)"
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _verification_line(verification: VerificationResult) -> str:
    status = "✓" if verification.passed else "✗"
    command = " ".join(verification.command)
    details = [
        f"phase={verification.phase}",
        f"revision={verification.revision}",
        f"exit={verification.exit_code}",
        f"{verification.duration_seconds:.2f}s",
    ]
    if verification.baseline:
        details.append("baseline=true")
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


def _acceptance_summary(result: RunResult) -> tuple[int, int]:
    required = [criterion for criterion in result.task.acceptance_criteria if criterion.required]
    evidenced = [criterion for criterion in required if criterion.evidence]
    return len(evidenced), len(required)


def _final_verification(result: RunResult) -> list[VerificationResult]:
    final = [verification for verification in result.verification if verification.phase == "final"]
    return final or [verification for verification in result.verification if not verification.baseline]


def _completeness_lines(result: RunResult) -> list[str]:
    acceptance_done, acceptance_total = _acceptance_summary(result)
    final = _final_verification(result)
    final_passed = sum(1 for verification in final if verification.passed)
    final_failed = sum(1 for verification in final if not verification.passed)
    review_status = "APPROVED" if result.review and result.review.approved else "NOT APPROVED"

    if result.outcome is Outcome.VERIFIED:
        verdict = "COMPLETE FOR DEVELOPER REVIEW: all required DevAgent verification/evidence gates passed on the final revision."
    elif result.outcome is Outcome.PARTIALLY_VERIFIED:
        verdict = "NOT FULLY PROVEN: implementation evidence exists, but at least one required verification/evidence gate is incomplete."
    else:
        verdict = "NOT READY FOR MERGE: DevAgent was blocked before it could safely prove the requested change."

    return [
        f"Outcome: {result.outcome.value}",
        f"Required acceptance criteria evidenced: {acceptance_done}/{acceptance_total}",
        f"Final/current verification checks: {final_passed} passed, {final_failed} failed",
        f"Independent review: {review_status}",
        f"Changed files: {result.changes.files_changed}",
        f"Changed source symbols identified: {len(result.developer_review.changed_symbols)}",
        f"Test cases identified in changed test files: {len(result.developer_review.test_cases)}",
        verdict,
    ]


def render_report(result: RunResult) -> str:
    passed = [verification for verification in result.verification if verification.passed]
    failed = [verification for verification in result.verification if not verification.passed]
    lines = [
        "DEVAGENT ENGINEERING REVIEW REPORT",
        "",
        "STATUS",
        result.outcome.value,
        "",
        "TASK",
        result.task.goal,
        f"Task type: {result.task.task_type.value}",
        f"Risk: {result.task.risk.value}",
        "",
        "REPOSITORY",
        result.repository.root,
        f"Source branch: {result.repository.git_branch or '(not a Git branch)'}",
        f"Source HEAD: {result.repository.git_head or '(unknown)'}",
        f"Working root: {result.working_root}",
        "",
        "WHY THIS CHANGE",
        result.root_cause or "Root cause/design reason was not established",
        "",
        "IMPLEMENTATION DECISIONS",
    ]
    lines.extend(f"- {item}" for item in result.implementation or ["No implementation completed"])

    lines.extend(
        [
            "",
            "TECHNICAL CHANGE SUMMARY",
            f"Files changed: {result.changes.files_changed}",
            f"Lines added: {result.changes.lines_added}",
            f"Lines deleted: {result.changes.lines_deleted}",
            "Changed paths:",
        ]
    )
    lines.extend(f"- {path}" for path in result.changes.paths or ["(none)"])

    lines.extend(["", "FUNCTIONS / CLASSES / SYMBOLS CHANGED"])
    if result.developer_review.changed_symbols:
        for symbol in result.developer_review.changed_symbols:
            line = f":{symbol.line}" if symbol.line is not None else ""
            lines.append(f"- {symbol.change} | {symbol.kind} | {symbol.path}{line} | {symbol.name}")
    else:
        lines.append("(none identified)")

    lines.extend(["", "TEST CASES / UNIT TESTS"])
    if result.developer_review.test_cases:
        for test in result.developer_review.test_cases:
            line = f":{test.line}" if test.line is not None else ""
            lines.append(f"- {test.change} | {test.kind} | {test.path}{line} | {test.name}")
    else:
        lines.append("(none identified)")
    if result.developer_review.test_files:
        lines.append("Test files:")
        lines.extend(f"- {path}" for path in result.developer_review.test_files)
    if result.developer_review.notes:
        lines.append("Technical inventory notes:")
        lines.extend(f"- {note}" for note in result.developer_review.notes)

    lines.extend(["", "ACCEPTANCE CRITERIA + EVIDENCE"])
    if result.task.acceptance_criteria:
        for index, criterion in enumerate(result.task.acceptance_criteria, start=1):
            status = "✓" if criterion.evidence else "✗"
            requirement = "REQUIRED" if criterion.required else "OPTIONAL"
            lines.append(f"{status} AC-{index} [{requirement}] {criterion.description}")
            if criterion.evidence:
                for evidence in criterion.evidence:
                    lines.append(f"  evidence: {evidence}")
            else:
                lines.append("  evidence: NONE")
    else:
        lines.append("(no explicit acceptance criteria recorded)")

    lines.extend(
        [
            "",
            "VERIFICATION SUMMARY",
            f"Passed checks: {len(passed)}",
            f"Failed checks: {len(failed)}",
            "",
            "VERIFICATION MATRIX",
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
                    f"  revision: {verification.revision}",
                    f"  classification: {classification}",
                    f"  exit code: {verification.exit_code}",
                    f"  timed out: {'YES' if verification.timed_out else 'NO'}",
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

    lines.extend(["", "COMPLETENESS ASSESSMENT"])
    lines.extend(_completeness_lines(result))

    lines.extend(["", "NEW REGRESSIONS", "0" if result.outcome.value == "VERIFIED" else "Not proven"])
    if result.not_run:
        lines.extend(["", "KNOWN GAPS / NOT RUN"])
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

    lines.extend(
        [
            "",
            "DEVELOPER REVIEW CHECKLIST",
            "1. Confirm the requirement and root-cause/design explanation match the intended product behavior.",
            "2. Inspect every changed path and changed symbol; reject unrelated or unexpectedly broad changes.",
            "3. Confirm each REQUIRED acceptance criterion has concrete evidence on the final revision.",
            "4. Review the listed unit/test cases for happy paths, edge cases, regression coverage, and missing scenarios.",
            "5. Review every failed check, NOT RUN item, review issue, and recommendation before accepting the change.",
            "6. Confirm final verification ran after the last code change and no known new regression remains.",
            "7. If a branch was pushed, inspect that exact commit/branch before creating any human PR or merge.",
        ]
    )
    if source.pushed:
        lines.append(f"Review remote branch: {source.remote}/{source.branch}")
    else:
        lines.append(f"Review local diff: git -C {result.working_root} diff")
    return "\n".join(lines)
