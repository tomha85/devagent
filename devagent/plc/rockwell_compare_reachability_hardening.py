from __future__ import annotations

from dataclasses import replace

from devagent.plc import rockwell_compare as _compare
from devagent.plc import rockwell_compare_hardening as _hardening
from devagent.plc.rockwell_entrypoint_hardening import (
    routine_has_execution_entry,
    rung_has_execution_entry,
)

_ORIGINAL_COMPARE_MODELS = _hardening.compare_models
_INSTALLED = False


def _has_reachable_other_writer(project, model) -> bool:
    """Count only writers that can execute in the authenticated routine closure."""

    target = _hardening._canonical_tag_identity(project, model.output_tag, model.program)
    for rung in project.rungs:
        if rung.id == model.rung_id or not rung_has_execution_entry(project, rung):
            continue
        if any(
            _hardening._canonical_tag_identity(project, write, rung.program) == target
            for write in rung.writes
        ):
            return True

    for statement in project.logic_statements:
        if statement.language == "RLL" and _hardening._same_source(statement, model):
            continue
        statement_program = statement.source.program or (
            statement.owner_name if statement.owner_type == "program" else None
        )
        statement_routine = statement.source.routine
        if statement_program and statement_routine:
            if not routine_has_execution_entry(project, statement_program, statement_routine):
                continue
        # Statements without a concrete program/routine location cannot be
        # proven unreachable; retain them conservatively as possible writers.
        if any(
            _hardening._canonical_tag_identity(project, write, statement_program) == target
            for write in statement.writes
        ):
            return True
    return False


def compare_models(project):
    """Expose compare models and writer uniqueness only inside executable logic."""

    rung_by_id = {rung.id: rung for rung in project.rungs}
    result = []
    for model in _ORIGINAL_COMPARE_MODELS(project):
        rung = rung_by_id.get(model.rung_id)
        if rung is None or not rung_has_execution_entry(project, rung):
            continue
        result.append(
            replace(
                model,
                single_writer=not _has_reachable_other_writer(project, model),
            )
        )
    return result


def install() -> None:
    """Install the reachability gate into both public and hardened compare globals."""

    global _INSTALLED
    if _INSTALLED:
        return
    # The V8 hardening functions resolve ``compare_models`` from their module
    # globals at runtime. Rebinding that symbol makes FAT generation, static
    # checks, and typed requirement verification share the exact same execution
    # reachability model rather than independently rescanning raw rungs.
    _hardening.compare_models = compare_models
    _compare.compare_models = compare_models
    _INSTALLED = True


__all__ = ["compare_models", "install"]
