from __future__ import annotations

from dataclasses import replace

from devagent.plc import production_regression as _regression

_ORIGINAL_ANALYZE_REGRESSION = _regression.analyze_regression


def _current_evidence_ids(project) -> set[str]:
    ids = {tag.id for tag in project.tags}
    ids.update(rung.id for rung in project.rungs)
    ids.update(statement.id for statement in project.logic_statements)
    ids.update(logic.id for logic in project.output_logic)
    ids.update(program.id for program in project.programs)
    ids.update(routine.id for routine in project.routines)
    ids.update(task.id for task in project.tasks)
    ids.update(aoi.id for aoi in project.aois)
    ids.update(data_type.id for data_type in project.data_types)
    return ids


def analyze_regression(baseline_path, engineering, verifications):
    """Remove baseline-only IDs from regression evidence references.

    Regression claims are deterministic derived artifacts. Their `evidence_ids`
    must only point to objects that are actually packaged in the current run.
    Baseline-only source objects remain represented by the regression change
    itself and the baseline SHA/context; they are never emitted as dangling IDs.
    """

    changes, baseline = _ORIGINAL_ANALYZE_REGRESSION(
        baseline_path,
        engineering,
        verifications,
    )
    valid_ids = _current_evidence_ids(engineering.project)
    sanitized = [
        replace(
            change,
            evidence_ids=tuple(
                evidence_id
                for evidence_id in change.evidence_ids
                if evidence_id in valid_ids
            ),
        )
        for change in changes
    ]
    return sanitized, baseline


def install() -> None:
    _regression.analyze_regression = analyze_regression


__all__ = ["analyze_regression", "install"]
