from __future__ import annotations

from devagent.plc import semantic_coverage_report as _semantic_report
from devagent.plc.rockwell_system_service_v17 import system_service_profile

_INSTALLED = False


def _section(project) -> str:
    profile = system_service_profile(project)
    if not profile["occurrences"]:
        return ""
    return "\n".join(
        [
            "### Rockwell System-Service Runtime Boundary",
            "",
            "> GSV/SSV source and operand traceability is normalized, while controller system-attribute behavior remains runtime-dependent and PARTIAL until engineer-executed evidence is attached.",
            "",
            f"- Reachable GSV/SSV occurrences: **{profile['occurrences']}** across **{profile['rungs']}** rung(s)",
            f"- GSV occurrences: **{profile['gsv_occurrences']}**",
            f"- SSV occurrences: **{profile['ssv_occurrences']}**",
            f"- MajorFaultRecord occurrences: **{profile['major_fault_record_occurrences']}**",
            f"- System-service runtime FAT procedures: **{profile['runtime_fat_tests']}**",
            f"- Engineer runtime evidence required: **{'yes' if profile['requires_engineer_runtime_evidence'] else 'no'}**",
            "- Static proof promotion: **none** — V17 adds FAT and risk specificity only",
            "",
        ]
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    original = _semantic_report.render_semantic_coverage_section

    def render_semantic_coverage_section(project) -> str:
        base = original(project)
        section = _section(project)
        if not section:
            return base
        marker = "### Program RLL Instruction Coverage Breakdown"
        if marker in base:
            return base.replace(marker, section + marker, 1)
        return base + "\n\n" + section

    _semantic_report.render_semantic_coverage_section = render_semantic_coverage_section
    _INSTALLED = True


__all__ = ["install"]
