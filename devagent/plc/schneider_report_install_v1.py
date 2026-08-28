from __future__ import annotations

_INSTALLED = False


def _is_schneider(project) -> bool:
    return str(project.metadata.vendor).casefold().startswith("schneider")


def _render(project) -> str:
    from devagent.plc.models import PLCSemanticState
    from devagent.plc.schneider_control_expert_v1 import schneider_capability_profile

    profile = schneider_capability_profile(project)
    total = len(project.logic_statements)
    full = int(profile["full_statements"])
    partial = int(profile["partial_statements"])
    opaque = int(profile["opaque_statements"])
    pct = "N/A" if total <= 0 else f"{100.0 * full / total:.1f}%"
    languages = sorted({item.language for item in project.logic_statements})
    withheld = sorted({item.language for item in project.logic_statements if item.semantic_state is not PLCSemanticState.FULL})
    lines = [
        "## Semantic Coverage / Proof Boundary",
        "",
        "> Schneider Control Expert V1 separates XML/source traceability from bounded deterministic behavior proof. Import recognition is not a behavioral PASS.",
        "",
        "### Schneider Control Expert Export Inventory",
        "",
        f"- Variables: **{len(project.tags)}**",
        f"- Derived data types: **{len(project.data_types)}**",
        f"- Tasks: **{len(project.tasks)}**",
        f"- Sections / routines: **{len(project.routines)}**",
        f"- Logic objects discovered: **{total}**",
        f"- Bounded FULL logic objects: **{full}/{total} ({pct})**",
        f"- PARTIAL logic objects: **{partial}**",
        f"- OPAQUE logic objects: **{opaque}**",
        f"- Bounded Boolean output-logic objects: **{len(project.output_logic)}**",
        f"- Languages/surfaces discovered: `{', '.join(languages) or 'none'}`",
        "",
        "### Explicit Schneider V1 Boundaries",
        "",
        "- Top-level IEC 61131-3 ST Boolean assignments using identifiers with AND/OR/NOT may receive bounded local deterministic proof.",
        "- Simple series LD networks using normal-open/normal-closed contacts and one normal coil may receive bounded local Boolean proof.",
        "- ST IF/CASE/loops/calls, timer/counter/DFB/EFB state, edge behavior, and complex expressions remain PARTIAL until a dedicated theorem models them.",
        "- Branched/stateful/edge/FFB/control LD and FBD/SFC/IL behavior remain OPAQUE in V1 and require engineer FAT evidence.",
        "- .STU/.STA work/archive formats are not parsed directly. Export .XEF; for .ZEF, extract/export the contained .XEF and analyze that source surface.",
        "- DevAgent does not launch, connect to, write to, or execute EcoStruxure Control Expert Simulator, HIL, or a real Modicon PLC.",
        f"- Withheld languages/surfaces: `{', '.join(withheld) or 'none'}`",
        "",
        "### Trust Boundary",
        "",
        "Static proof is limited to the exact exported XML/source bytes and bounded semantics above. FAT procedures remain engineer-executed and NOT_RUN until authenticated execution evidence is imported.",
        "",
    ]
    return "\n".join(lines)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import semantic_coverage_report as _semantic_report

    previous = _semantic_report.render_semantic_coverage_section

    def render_semantic_coverage_section(project):
        return _render(project) if _is_schneider(project) else previous(project)

    _semantic_report.render_semantic_coverage_section = render_semantic_coverage_section
    _INSTALLED = True


__all__ = ["install"]
