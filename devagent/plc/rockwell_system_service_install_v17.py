from __future__ import annotations

from devagent.plc.models import StaticCheck, StaticCheckStatus
from devagent.plc.rockwell_system_service_v17 import (
    generate_system_service_fat_tests,
    rockwell_system_service_check,
    system_service_risks,
    system_services_explain_current_semantic_gap,
)

_INSTALLED = False


def _replace_check(checks, replacement):
    result = []
    replaced = False
    for check in checks:
        if check.id == replacement.id:
            if not replaced:
                result.append(replacement)
                replaced = True
            continue
        result.append(check)
    if not replaced:
        result.append(replacement)
    return result


def install() -> None:
    """Install V17 before production imports capture analysis/risk functions.

    V17 is intentionally a FAT-planning/risk-specificity layer. It never
    promotes GSV/SSV to deterministic behavior proof and never changes
    PLCOutcome or release-readiness policy.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import production_review as _review
    from devagent.plc import safe_analysis as _safe
    from devagent.plc.fat_procedure_v12 import enrich_fat_procedures

    original_analyze = _safe.analyze_rockwell_l5x
    previous_detect_risks = _review.detect_risks

    def analyze_rockwell_l5x(path):
        result = original_analyze(path)
        project = result.project

        known_ids = {test.id for test in result.fat_tests}
        additions = [
            test
            for test in generate_system_service_fat_tests(project)
            if test.id not in known_ids
        ]
        if additions:
            result.fat_tests = enrich_fat_procedures(
                project,
                [*result.fat_tests, *additions],
            )

        traceable = bool(result.fat_tests) and all(
            test.source.artifact and test.source.routine
            for test in result.fat_tests
        )
        runtime_count = sum(
            test.scenario == "SYSTEM_SERVICE_RUNTIME"
            for test in result.fat_tests
        )
        traceability_check = StaticCheck(
            id="FAT_TEST_TRACEABILITY",
            status=StaticCheckStatus.PASS if traceable else StaticCheckStatus.WARN,
            summary=(
                f"{len(result.fat_tests)} source-traceable engineer FAT procedure(s) were generated, including "
                f"{runtime_count} system-service runtime procedure(s); all remain NOT_RUN until engineer execution evidence is imported."
                if traceable
                else "No source-traceable engineer FAT procedures were generated."
            ),
        )
        result.static_checks = _replace_check(result.static_checks, traceability_check)

        service_check = rockwell_system_service_check(project)
        result.static_checks = _replace_check(result.static_checks, service_check)
        if service_check.status is StaticCheckStatus.WARN:
            limitation = (
                "Reachable Rockwell GSV/SSV system-service behavior remains PARTIAL. DevAgent normalizes the system-object references and generates engineer-run FAT procedures, but controller/system attribute values, side effects, fault-state ordering, and recovery require runtime evidence."
            )
            if limitation not in result.limitations:
                result.limitations.append(limitation)

        # Deliberately do not modify result.outcome. The underlying static proof
        # boundary remains authoritative even when V17 generates a better FAT.
        return result

    def detect_risks(engineering, verifications, executions, engineering_findings):
        risks = list(
            previous_detect_risks(
                engineering,
                verifications,
                executions,
                engineering_findings,
            )
        )
        specific = system_service_risks(engineering.project)
        if not specific:
            return risks

        # Replace a vague umbrella risk only when GSV/SSV demonstrably explains
        # the complete remaining static gap. Otherwise retain both so unrelated
        # unsupported behavior cannot be hidden by the system-service finding.
        if system_services_explain_current_semantic_gap(engineering.project):
            risks = [risk for risk in risks if risk.category != "SEMANTIC_COVERAGE"]

        known = {risk.id for risk in risks}
        risks.extend(risk for risk in specific if risk.id not in known)
        return risks

    _safe.analyze_rockwell_l5x = analyze_rockwell_l5x
    _review.detect_risks = detect_risks
    _INSTALLED = True


__all__ = ["install"]
