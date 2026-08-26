from __future__ import annotations

from collections import Counter, defaultdict

from devagent.plc.models import PLCSemanticState
from devagent.plc.rockwell_compare import compare_models
from devagent.plc.rockwell_general_actions import action_models, action_profile
from devagent.plc.rockwell_l5x import _instruction_semantics
from devagent.plc.rockwell_motion_runtime_v11 import motion_runtime_profile
from devagent.plc.rockwell_state_machine_v11 import state_machine_profile
from devagent.plc.rockwell_stateful_runtime import stateful_profile

_LEVEL_ORDER = (
    "DETERMINISTIC_PATH",
    "BOUNDED_DETERMINISTIC",
    "STRUCTURAL_RW",
    "PARTIAL",
    "UNMODELED",
)


def _source_key(source) -> tuple[str, str, str]:
    return (
        str(source.program or "").casefold(),
        str(source.routine or "").casefold(),
        str(source.rung if source.rung is not None else ""),
    )


def _rung_key(rung) -> tuple[str, str, str]:
    return (
        str(rung.program or "").casefold(),
        str(rung.routine or "").casefold(),
        str(rung.source.rung if rung.source.rung is not None else rung.number),
    )


def build_semantic_coverage_manifest(project) -> dict[str, object]:
    """Describe what DevAgent understands for this exact PLC project.

    Coverage separates proof-grade behavior, bounded instruction-effect theorems,
    structural operand normalization, explicitly partial behavior, and unknown
    behavior. A parser recognizing a token never becomes a behavioral PASS by
    itself.
    """

    deterministic_rung_keys = {
        _source_key(logic.source)
        for logic in project.output_logic
        if logic.semantic_state is PLCSemanticState.FULL
        and logic.language.upper() == "RLL"
        and not logic.origin.startswith("AOI_INTERNAL:")
    }
    bounded_compare_ids = {model.rung_id for model in compare_models(project)}
    bounded_action_ids = {model.rung_id for model in action_models(project)}
    partial_names = {name.upper() for name in project.partially_modeled_instruction_names}
    unknown_names = {name.upper() for name in project.unknown_instruction_names}
    aoi_parameters = {item.name: item.parameters for item in project.aois}

    per_instruction: dict[str, Counter[str]] = defaultdict(Counter)
    totals: Counter[str] = Counter()

    for rung in project.rungs:
        key = _rung_key(rung)
        deterministic_path = key in deterministic_rung_keys
        bounded_compare = rung.id in bounded_compare_ids
        bounded_action = rung.id in bounded_action_ids
        for instruction in rung.instructions:
            name = instruction.name.upper()
            _, _, _, _, structurally_supported = _instruction_semantics(instruction, aoi_parameters)
            if deterministic_path:
                level = "DETERMINISTIC_PATH"
            elif bounded_compare or bounded_action:
                level = "BOUNDED_DETERMINISTIC"
            elif name in partial_names:
                level = "PARTIAL"
            elif structurally_supported:
                level = "STRUCTURAL_RW"
            else:
                level = "UNMODELED"
            per_instruction[name][level] += 1
            totals[level] += 1

    instruction_total = sum(totals.values())
    deterministic_total = totals["DETERMINISTIC_PATH"] + totals["BOUNDED_DETERMINISTIC"]
    recognized_total = deterministic_total + totals["STRUCTURAL_RW"] + totals["PARTIAL"]

    def pct(value: int, total: int) -> float | None:
        return round(100.0 * value / total, 1) if total else None

    instruction_rows = []
    for name in sorted(per_instruction):
        counts = per_instruction[name]
        instruction_rows.append(
            {
                "instruction": name,
                "occurrences": sum(counts.values()),
                "levels": {level: counts[level] for level in _LEVEL_ORDER if counts[level]},
            }
        )

    st_states = Counter(
        statement.semantic_state.value
        for statement in project.logic_statements
        if statement.language.upper() == "ST"
    )
    st_full = st_states[PLCSemanticState.FULL.value]
    aoi_rll_states = Counter(
        statement.semantic_state.value
        for statement in project.logic_statements
        if statement.owner_type == "aoi" and statement.language.upper() == "RLL"
    )
    aoi_st_states = Counter(
        statement.semantic_state.value
        for statement in project.logic_statements
        if statement.owner_type == "aoi" and statement.language.upper() == "ST"
    )
    unsupported_routine_types = Counter(
        routine.routine_type
        for routine in project.routines
        if routine.routine_type.upper() not in {"RLL", "ST"}
    )
    protected_routines = sum(1 for routine in project.routines if routine.source_protected)
    protected_aois = sum(1 for aoi in project.aois if aoi.source_protected)
    action = action_profile(project)
    stateful = stateful_profile(project)
    motion = motion_runtime_profile(project)
    state_machine = state_machine_profile(project)

    return {
        # V11 adds optional fields only. Keep the established v1 schema ID so
        # existing inspect/report consumers remain compatible.
        "schema": "devagent-plc-semantic-coverage-v1",
        "project": {
            "vendor": project.metadata.vendor,
            "engineering_tool": project.metadata.engineering_tool,
            "controller": project.metadata.controller_name,
            "processor_type": project.metadata.processor_type,
            "source_sha256": project.metadata.source_sha256,
        },
        "inventory": {
            "tags": len(project.tags),
            "data_types": len(project.data_types),
            "modules": len(project.modules),
            "tasks": len(project.tasks),
            "scheduled_program_entries": sum(len(task.scheduled_programs) for task in project.tasks),
            "programs": len(project.programs),
            "routines": len(project.routines),
            "program_rll_rungs": len(project.rungs),
            "logic_statements": len(project.logic_statements),
            "structured_text_statements": project.st_statement_total,
            "aois": len(project.aois),
            "output_logic_objects": len(project.output_logic),
        },
        "instruction_summary": {
            "scope": "PROGRAM_RLL",
            "total_occurrences": instruction_total,
            "deterministic_occurrences": deterministic_total,
            "deterministic_pct": pct(deterministic_total, instruction_total),
            "structural_only_occurrences": totals["STRUCTURAL_RW"],
            "structural_or_better_pct": pct(
                deterministic_total + totals["STRUCTURAL_RW"], instruction_total
            ),
            "partial_occurrences": totals["PARTIAL"],
            "unmodeled_occurrences": totals["UNMODELED"],
            "unmodeled_pct": pct(totals["UNMODELED"], instruction_total),
            "recognized_occurrences": recognized_total,
        },
        "instruction_levels": {level: totals[level] for level in _LEVEL_ORDER},
        "instructions": instruction_rows,
        "language_summary": {
            "rll": {
                "program_rungs": len(project.rungs),
                "deterministic_boolean_rungs": len(deterministic_rung_keys),
                "bounded_compare_rungs": len(bounded_compare_ids),
                "bounded_action_rungs": len(bounded_action_ids),
                "branch_rungs": project.branch_rung_total,
                "branch_rungs_modeled": project.branch_rung_semantic_count,
                "branch_coverage_pct": pct(project.branch_rung_semantic_count, project.branch_rung_total),
            },
            "structured_text": {
                "statements": project.st_statement_total,
                "reachable_full_dataflow_statements": st_full,
                "reachable_full_dataflow_pct": pct(st_full, project.st_statement_total),
                "partial_or_unreachable_statements": st_states[PLCSemanticState.PARTIAL.value],
                "opaque_statements": st_states[PLCSemanticState.OPAQUE.value],
            },
            "aoi": {
                "definitions": len(project.aois),
                "protected_definitions": protected_aois,
                "internal_bodies_total": project.aoi_internal_total,
                "internal_bodies_modeled": project.aoi_internal_modeled_count,
                "internal_rll_statements": sum(aoi_rll_states.values()),
                "internal_rll_full_statements": aoi_rll_states[PLCSemanticState.FULL.value],
                "internal_st_statements": sum(aoi_st_states.values()),
                "internal_st_full_statements": aoi_st_states[PLCSemanticState.FULL.value],
                "calls_total": project.aoi_call_total,
                "calls_bound": project.aoi_call_bound_count,
            },
        },
        "action_semantics": action,
        "stateful_runtime_semantics": stateful,
        "motion_runtime_semantics": motion,
        "state_machine_semantics": state_machine,
        "project_boundaries": {
            "protected_routines": protected_routines,
            "unsupported_routine_types": dict(sorted(unsupported_routine_types.items())),
            "partially_modeled_instruction_names": sorted(partial_names),
            "unmodeled_instruction_names": sorted(unknown_names),
            "warnings": list(project.warnings),
        },
        "trust_note": (
            "Deterministic coverage describes bounded reachable software semantics only. "
            "Program RLL instruction coverage and AOI-definition coverage are separate scopes. "
            "Structural coverage means reads/writes/calls are normalized but behavior is not fully proven. "
            "Timer/counter, motion, and runtime-classified state-machine scenarios remain PARTIAL until qualified execution evidence is attached. "
            "Unreachable, partial, or unmodeled behavior is excluded from deterministic verification. "
            "Physical I/O, process physics, safety certification, and runtime behavior require separate evidence."
        ),
    }


__all__ = ["build_semantic_coverage_manifest"]
