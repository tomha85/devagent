from __future__ import annotations

from dataclasses import replace

from devagent.plc import rockwell_alias_hardening as _alias
from devagent.plc import rockwell_compare as _compare
from devagent.plc import rockwell_compare_hardening as _hardening
from devagent.plc.rockwell_entrypoint_hardening import (
    routine_has_execution_entry,
    rung_has_execution_entry,
)

_ORIGINAL_COMPARE_MODELS = _hardening.compare_models
_INSTALLED = False


def canonical_writer_sources(project, ref: str, default_program: str | None) -> tuple[str, ...]:
    """Return alias-aware writer sources only from the executable routine closure."""

    target = _alias.canonical_tag_identity(project, ref, default_program)
    if not _alias.identity_is_resolved(target):
        return ()
    sources: dict[tuple[str, str, str, str], str] = {}

    for rung in project.rungs:
        if not rung_has_execution_entry(project, rung):
            continue
        if not any(
            _alias.storage_identities_overlap(
                _alias.canonical_tag_identity(project, write, rung.program),
                target,
            )
            for write in rung.writes
        ):
            continue
        key = (
            str(rung.source.aoi or ""),
            str(rung.source.program or rung.program or ""),
            str(rung.source.routine or rung.routine or ""),
            str(rung.source.rung if rung.source.rung is not None else rung.number),
        )
        sources.setdefault(key, rung.id)

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
        if not any(
            _alias.storage_identities_overlap(
                _alias.canonical_tag_identity(project, write, statement_program),
                target,
            )
            for write in statement.writes
        ):
            continue
        key = (
            str(statement.source.aoi or ""),
            str(statement.source.program or statement_program or ""),
            str(statement.source.routine or statement.routine or ""),
            str(
                statement.source.rung
                if statement.source.rung is not None
                else statement.source.line
                if statement.source.line is not None
                else statement.locator
            ),
        )
        sources.setdefault(key, statement.id)

    return tuple(sorted(sources.values(), key=str.casefold))


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
