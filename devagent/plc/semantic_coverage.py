from __future__ import annotations

from collections import Counter, defaultdict

from devagent.plc.models import PLCSemanticState
from devagent.plc.rockwell_compare import compare_models
from devagent.plc.rockwell_l5x import _instruction_semantics

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

    Coverage levels are intentionally trust-oriented:

    * DETERMINISTIC_PATH: the full RLL Boolean path was normalized into FULL
      output logic and can participate in deterministic path/action reasoning.
    * BOUNDED_DETERMINISTIC: the rung matches another explicitly bounded
      deterministic theorem (currently typed linear compare semantics).
    * STRUCTURAL_RW: operand direction/read/write/call structure is understood,
      but behavioral/final-state proof is not claimed.
    * PARTIAL: DevAgent recognizes the family but explicitly withholds complete
      behavior semantics.
    * UNMODELED: the instruction is present but no supported semantics apply.

    The manifest is descriptive evidence, not a claim about physical machine
    behavior, safety certification, simulator execution, or site readiness.
    """

    deterministic_rung_keys = {
        _source_key(logic.source)
        for logic in project.output_logic
        if logic.semantic_state is PLCSemanticState.FULL
        and logic.language.upper() == "RLL"
        and not logic.origin.startswith("AOI_INTERNAL:")
    }
    bounded_compare_ids = {model.rung_id for model in compare_models(project)}
    partial_names = {name.upper() for name in project.partially_modeled_instruction_names}
    unknown_names = {name.upper() for name in project.unknown_instruction_names}
    aoi_parameters = {item.name: item.parameters for item in project.aois}

    per_instruction: dict[str, Counter[str]] = defaultdict(Counter)
    totals: Counter[str] = Counter()

    for rung in project.rungs:
        key = _rung_key(rung)
        deterministic_path = key in deterministic_rung_keys
        bounded_compare = rung.id in bounded_compare_ids
        for instruction in rung.instructions:
            name = instruction.name.upper()
            _, _, _, _, structurally_supported = _instruction_semantics(instruction, aoi_parameters)
            if deterministic_path:
                level = "DETERMINISTIC_PATH"
            elif bounded_compare:
                level = "BOUNDED_DETERMINISTIC"
            elif name in partial_names:
                level = "PARTIAL"
            elif structurally_supported:
                level = "STRUCTURAL_RW"
            elif name in unknown_names:
                level = "UNMODELED"
            else:
                # Unknown/custom instructions not retained by an upstream warning
                # still fail closed rather than being counted as understood.
                level = "UNMODELED"
            per_instruction[name][level] += 1
            totals[level] += 1

    instruction_total = sum(totals.values())
    deterministic_total = totals["DETERMINISTIC_PATH"] + totals["BOUNDED_DETERMINISTIC"]
    understood_total = deterministic_total + totals["STRUCTURAL_RW"] + totals["PARTIAL"]

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

    st_states = Counter(statement.semantic_state.value for statement in project.logic_statements if statement.language.upper() == "ST")
    unsupported_routine_types = Counter(
        routine.routine_type
        for routine in project.routines
        if routine.routine_type.upper() not in {"RLL", "ST"}
    )
    protected_routines = sum(1 for routine in project.routines if routine.source_protected)
    protected_aois = sum(1 for aoi in project.aois if aoi.source_protected)

    return {
        "schema": "devagent-plc-semantic-coverage-v1",
        "project": {
            "vendor": project.metadata.vendor,
            "engineering_tool": project.metadata.engineering_tool,
            "controller": project.metadata.controller_name,
            "processor_type": project.metadata.processor_type,
            "source_sha256": project.metadata.source_sha256,
        },
        "instruction_summary": {
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
            "recognized_or_better_occurrences": understood_total,
        },
        "instruction_levels": {level: totals[level] for level in _LEVEL_ORDER},
        "instructions": instruction_rows,
        "language_summary": {
            "rll": {
                "rungs": len(project.rungs),
                "deterministic_boolean_rungs": len(deterministic_rung_keys),
                "bounded_compare_rungs": len(bounded_compare_ids),
                "branch_rungs": project.branch_rung_total,
                "branch_rungs_modeled": project.branch_rung_semantic_count,
                "branch_coverage_pct": pct(project.branch_rung_semantic_count, project.branch_rung_total),
            },
            "structured_text": {
                "statements": project.st_statement_total,
                "full_dataflow_statements": st_states[PLCSemanticState.FULL.value],
                "partial_statements": st_states[PLCSemanticState.PARTIAL.value],
                "opaque_statements": st_states[PLCSemanticState.OPAQUE.value],
                "existing_semantic_count": project.st_statement_semantic_count,
                "existing_semantic_coverage_pct": pct(
                    project.st_statement_semantic_count, project.st_statement_total
                ),
            },
            "aoi": {
                "definitions": len(project.aois),
                "protected_definitions": protected_aois,
                "internal_bodies_total": project.aoi_internal_total,
                "internal_bodies_modeled": project.aoi_internal_modeled_count,
                "calls_total": project.aoi_call_total,
                "calls_bound": project.aoi_call_bound_count,
            },
        },
        "project_boundaries": {
            "protected_routines": protected_routines,
            "unsupported_routine_types": dict(sorted(unsupported_routine_types.items())),
            "partially_modeled_instruction_names": sorted(partial_names),
            "unmodeled_instruction_names": sorted(unknown_names),
        },
        "trust_note": (
            "Deterministic coverage describes bounded software semantics only. "
            "Structural coverage means reads/writes/calls are normalized but behavior is not fully proven. "
            "Partial or unmodeled behavior is excluded from deterministic verification. "
            "Physical I/O, process physics, safety certification, and runtime behavior require separate evidence."
        ),
    }


__all__ = ["build_semantic_coverage_manifest"]
