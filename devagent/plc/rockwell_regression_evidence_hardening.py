from __future__ import annotations

from dataclasses import replace

from devagent.plc import production_regression as _regression
from devagent.plc.production_models import EvidenceItem
from devagent.plc.production_utils import source_locator

_ORIGINAL_ANALYZE_REGRESSION = _regression.analyze_regression


def _current_evidence_map(project) -> dict[str, str]:
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


def _baseline_evidence_map(project) -> dict[str, str]:
    prefix = f"BASELINE:{project.metadata.source_sha256}:"
    mapping: dict[str, str] = {
        tag.id: prefix + f"TAG:{tag.scope}:{tag.name}"
        for tag in project.tags
    }
    for item in project.rungs:
        mapping[item.id] = prefix + item.id
    for item in project.logic_statements:
        mapping[item.id] = prefix + item.id
    for item in project.output_logic:
        mapping[item.id] = prefix + item.id
    return mapping


def _remap_change_evidence(change, baseline_map, current_map):
    remapped: list[str] = []
    use_baseline = change.change_type.endswith("REMOVED") or change.change_type.endswith("CHANGED")
    use_current = change.change_type.endswith("ADDED") or change.change_type.endswith("CHANGED")
    for evidence_id in change.evidence_ids:
        if use_baseline:
            packaged = baseline_map.get(evidence_id)
            if packaged and packaged not in remapped:
                remapped.append(packaged)
        if use_current:
            packaged = current_map.get(evidence_id)
            if packaged and packaged not in remapped:
                remapped.append(packaged)
    return replace(change, evidence_ids=tuple(remapped))


def analyze_regression(baseline_path, engineering, verifications):
    """Namespace baseline provenance and preserve current-package evidence IDs."""

    changes, baseline = _ORIGINAL_ANALYZE_REGRESSION(
        baseline_path,
        engineering,
        verifications,
    )
    if baseline is None:
        return changes, baseline
    baseline_map = _baseline_evidence_map(baseline.project)
    current_map = _current_evidence_map(engineering.project)
    return [
        _remap_change_evidence(change, baseline_map, current_map)
        for change in changes
    ], baseline


def baseline_evidence_items(baseline, changes) -> list[EvidenceItem]:
    """Package only baseline objects actually cited by regression changes."""

    if baseline is None:
        return []
    project = baseline.project
    wanted = {
        evidence_id
        for change in changes
        for evidence_id in change.evidence_ids
        if evidence_id.startswith(f"BASELINE:{project.metadata.source_sha256}:")
    }
    if not wanted:
        return []
    mapping = _baseline_evidence_map(project)
    result: list[EvidenceItem] = []

    for tag in project.tags:
        evidence_id = mapping[tag.id]
        if evidence_id in wanted:
            result.append(
                EvidenceItem(
                    evidence_id,
                    "BASELINE_TAG",
                    f"Baseline {tag.scope} tag {tag.name}: {tag.data_type}",
                    project.metadata.source_path,
                    project.metadata.source_sha256,
                    {
                        "tag": tag.name,
                        "scope": tag.scope,
                        "data_type": tag.data_type,
                        "tag_type": tag.tag_type,
                        "alias_for": tag.alias_for,
                        "external_access": tag.external_access,
                        "constant": tag.constant,
                    },
                )
            )
    for item, kind, summary in [
        *[(rung, "BASELINE_RUNG", rung.text[:240]) for rung in project.rungs],
        *[(statement, f"BASELINE_{statement.language}_STATEMENT", statement.text[:240]) for statement in project.logic_statements],
        *[(logic, "BASELINE_OUTPUT_LOGIC", f"{logic.output_tag} via {logic.instruction} ({len(logic.paths)} modeled path(s))") for logic in project.output_logic],
    ]:
        evidence_id = mapping[item.id]
        if evidence_id not in wanted:
            continue
        result.append(
            EvidenceItem(
                evidence_id,
                kind,
                summary,
                source_locator(item.source),
                project.metadata.source_sha256,
                {"baseline": True, "original_id": item.id},
            )
        )
    return result


def install() -> None:
    _regression.analyze_regression = analyze_regression


__all__ = ["analyze_regression", "baseline_evidence_items", "install"]
