from __future__ import annotations

_INSTALLED = False


def _is_schneider(project) -> bool:
    return str(project.metadata.vendor).casefold().startswith("schneider")


def _render(project) -> str:
    from devagent.plc.models import PLCSemanticState
    from devagent.plc.schneider_control_expert_v1 import schneider_capability_profile

    profile = schneider_capability_profile(project)
    total = len(project.logic_statements)
    full = int(profile.get("full_statements", 0))
    partial = int(profile.get("partial_statements", 0))
    opaque = int(profile.get("opaque_statements", 0))
    pct = "N/A" if total <= 0 else f"{100.0 * full / total:.1f}%"
    languages = sorted({item.language for item in project.logic_statements})
    withheld = sorted({item.language for item in project.logic_statements if item.semantic_state is not PLCSemanticState.FULL})

    support_regions = int(profile.get("support_regions", total))
    support_full = int(profile.get("support_full", full))
    support_partial = int(profile.get("support_partial", partial))
    support_opaque = int(profile.get("support_opaque", opaque))
    support_protected = int(profile.get("support_protected", 0))
    support_contract = str(profile.get("support_contract", profile.get("static_contract", "NOT_DECLARED")))
    coverage_complete = profile.get("coverage_accounting_complete")
    coverage_text = "not declared" if coverage_complete is None else ("yes" if coverage_complete else "no")
    capability_schema = str(profile.get("schema", "not declared"))
    action_schema = profile.get("real_st_action_schema")
    local_actions = int(profile.get("real_st_local_actions", 0))
    local_action_families = profile.get("real_st_local_action_families", {})

    lines = [
        "## Semantic Coverage / Proof Boundary",
        "",
        "> Schneider Control Expert reporting is capability-aware: XML/source recognition, deterministic proof, local-effect modeling, and runtime-required behavior are reported separately. Import recognition is never treated as a behavioral PASS.",
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
        "### Current Schneider Capability Contract",
        "",
        f"- Capability schema: `{capability_schema}`",
        f"- Support contract: **{support_contract}**",
        f"- Coverage accounting complete: **{coverage_text}**",
        f"- Support regions: **{support_regions}**",
        f"  - FULL: **{support_full}**",
        f"  - PARTIAL: **{support_partial}**",
        f"  - OPAQUE: **{support_opaque}**",
        f"  - PROTECTED: **{support_protected}**",
    ]

    if action_schema is not None:
        lines += [
            f"- Real-ST local-action schema: `{action_schema}`",
            f"- Deterministic local ST assignment effects modeled inside already-PARTIAL source: **{local_actions}**",
            f"- Local ST action families: `{local_action_families}`",
            "- Local ST action modeling **does not promote** the enclosing Schneider support region to FULL unless execution condition, writer ownership, type/scope identity, and relevant ordering semantics are independently proven.",
        ]

    lines += [
        "",
        "### Proof-State Meaning",
        "",
        "- **FULL** — the current installed Schneider theorem stack has bounded deterministic semantics for the normalized logic object/support region and all required local proof conditions passed.",
        "- **PARTIAL** — source is traceable and may include modeled local effects or structural facts, but complete behavior proof is withheld because one or more execution/control/type/writer/state boundaries are not proven.",
        "- **OPAQUE** — the exported behavior is recognized as executable engineering content but no safe deterministic theorem covers that region.",
        "- **PROTECTED** — implementation source is unavailable for independent static proof; downstream claims must remain evidence/runtime gated.",
        "",
        "### Current Schneider Boundaries",
        "",
        "- The report reflects the **currently installed qualified Schneider capability stack** rather than hard-coding historical V1/V2 wording. Individual theorem/evidence IDs preserve exact provenance for bounded ST, call/interface closure, LD/FBD, state-machine, interlock/permissive, fault/recovery, canonical identity/type/I/O, support-closeout, and additive real-ST local-action analysis where available.",
        "- Stateful timers/counters/EFB/DFB behavior, edge history, scan/task ordering, physical I/O, field wiring, process physics, and any unsupported control or graphical topology remain runtime/FAT evidence boundaries unless a dedicated deterministic theorem explicitly proves the needed behavior.",
        "- `.STU`/`.STA` work/archive formats are not parsed directly. Export `.XEF`; for `.ZEF`, extract/export the contained supported engineering source before analysis.",
        "- DevAgent does not launch, connect to, write to, or execute EcoStruxure Control Expert Simulator, HIL, or a real Modicon PLC.",
        f"- Withheld languages/surfaces: `{', '.join(withheld) or 'none'}`",
        "",
        "### Trust Boundary",
        "",
        "Static proof is limited to the exact exported XML/source bytes, canonical identity, and bounded deterministic semantics reported above. FAT procedures remain engineer-executed and NOT_RUN until authenticated execution evidence bound to the exact project/test-plan context is imported.",
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
