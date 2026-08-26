from __future__ import annotations

from dataclasses import replace

from devagent.plc import production_regression as _regression

_ORIGINAL_ANALYZE_REGRESSION = _regression.analyze_regression


def _packaged_evidence_map(project) -> dict[str, str]:
    """Map current canonical object IDs to IDs actually emitted by evidence_index()."""

    mapping: dict[str, str] = {
        tag.id: f"TAG:{tag.scope}:{tag.name}"
        for tag in project.tags
    }
    for item in project.rungs:
        mapping[item.id] = item.id
    for item in project.logic_statements:
        mapping[item.id] = item.id
    for item in project.output_logic:
        mapping[item.id] = item.id
    return mapping


def analyze_regression(baseline_path, engineering, verifications):
    """Remove or remap regression references not present in the current package.

    Regression claims are deterministic derived artifacts. Their `evidence_ids`
    must resolve against the evidence package emitted for the current run.
    Current tag object IDs are remapped to the package's `TAG:<scope>:<name>` IDs;
    baseline-only source IDs are dropped instead of becoming dangling references.
    """

    changes, baseline = _ORIGINAL_ANALYZE_REGRESSION(
        baseline_path,
        engineering,
        verifications,
    )
    evidence_map = _packaged_evidence_map(engineering.project)
    sanitized = []
    for change in changes:
        remapped: list[str] = []
        for evidence_id in change.evidence_ids:
            packaged = evidence_map.get(evidence_id)
            if packaged and packaged not in remapped:
                remapped.append(packaged)
        sanitized.append(replace(change, evidence_ids=tuple(remapped)))
    return sanitized, baseline


def install() -> None:
    _regression.analyze_regression = analyze_regression


__all__ = ["analyze_regression", "install"]
