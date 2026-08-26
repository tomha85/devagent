from __future__ import annotations

import re
from dataclasses import replace

from devagent.plc import rockwell_alias_hardening as _alias
from devagent.plc import rockwell_compare as _compare
from devagent.plc import rockwell_compare_hardening as _hardening
from devagent.plc.rockwell_entrypoint_hardening import (
    routine_has_execution_entry,
    rung_has_execution_entry,
)
from devagent.plc.rockwell_l5x import _instruction_semantics

_ORIGINAL_COMPARE_MODELS = _hardening.compare_models
_INSTALLED = False
_ST_ASSIGNMENT_LHS = re.compile(
    r"(?P<lhs>[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*)\s*:=",
    re.IGNORECASE,
)


def _rung_write_occurrences(project, rung) -> list[str]:
    """Return one item per modeled write instruction operand, preserving multiplicity."""

    aoi_parameters = {aoi.name: aoi.parameters for aoi in project.aois}
    result: list[str] = []
    for instruction in rung.instructions:
        _, writes, _, _, _ = _instruction_semantics(instruction, aoi_parameters)
        result.extend(sorted(writes, key=str.casefold))
    # If an instruction family was only directionally normalized upstream, keep
    # the rung-level writes conservatively rather than losing a possible writer.
    if not result:
        result.extend(rung.writes)
    return result


def _statement_write_occurrences(statement) -> list[str]:
    """Return every assignment occurrence in a statement, including multi-assignment ST lines."""

    if statement.language.upper() == "ST":
        parsed = [match.group("lhs") for match in _ST_ASSIGNMENT_LHS.finditer(statement.text)]
        if parsed:
            return parsed
    return list(statement.writes)


def canonical_writer_sources(project, ref: str, default_program: str | None) -> tuple[str, ...]:
    """Return alias-aware executable writer occurrences from the concrete routine closure.

    The same source ID may intentionally appear more than once when one rung/ST
    line writes overlapping storage multiple times. Callers use occurrence count
    for the single-writer theorem while evidence packaging can still de-duplicate
    the underlying source object ID.
    """

    target = _alias.canonical_tag_identity(project, ref, default_program)
    if not _alias.identity_is_resolved(target):
        return ()
    sources: list[str] = []

    for rung in project.rungs:
        if not rung_has_execution_entry(project, rung):
            continue
        for write in _rung_write_occurrences(project, rung):
            if _alias.storage_identities_overlap(
                _alias.canonical_tag_identity(project, write, rung.program),
                target,
            ):
                sources.append(rung.id)

    for statement in project.logic_statements:
        if statement.owner_type == "aoi":
            continue
        statement_program = statement.source.program or (
            statement.owner_name if statement.owner_type == "program" else None
        )
        statement_routine = statement.source.routine or statement.routine
        if statement_program and statement_routine:
            if not routine_has_execution_entry(project, statement_program, statement_routine):
                continue
        # A statement without a concrete program/routine cannot be proven
        # unreachable, so keep it conservatively as a possible writer.
        for write in _statement_write_occurrences(statement):
            if _alias.storage_identities_overlap(
                _alias.canonical_tag_identity(project, write, statement_program),
                target,
            ):
                sources.append(statement.id)

    return tuple(sorted(sources, key=str.casefold))


def compare_models(project):
    """Expose compare models and alias-aware writer uniqueness only inside executable logic."""

    rung_by_id = {rung.id: rung for rung in project.rungs}
    result = []
    for model in _ORIGINAL_COMPARE_MODELS(project):
        rung = rung_by_id.get(model.rung_id)
        if rung is None or not rung_has_execution_entry(project, rung):
            continue
        identity = _alias.canonical_tag_identity(project, model.output_tag, model.program)
        if not _alias.identity_is_resolved(identity):
            continue
        writers = canonical_writer_sources(project, model.output_tag, model.program)
        result.append(replace(model, single_writer=len(writers) == 1))
    return result


def install() -> None:
    """Install one reachable, alias-aware writer theorem across compare and requirements."""

    global _INSTALLED
    if _INSTALLED:
        return
    # V8 compare hardening resolves ``compare_models`` from module globals at
    # runtime. Requirement hardening imports ``canonical_writer_sources`` from
    # rockwell_alias_hardening after this install runs, so patch the alias module
    # before production verification is imported as well.
    _hardening.compare_models = compare_models
    _compare.compare_models = compare_models
    _alias.canonical_writer_sources = canonical_writer_sources
    _INSTALLED = True


__all__ = ["canonical_writer_sources", "compare_models", "install"]
