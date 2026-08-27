from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import re

from devagent.plc import production_evidence as _evidence
from devagent.plc import production_regression as _regression
from devagent.plc import production_review as _review
from devagent.plc.production_models import EvidenceItem, RegressionChange, Severity
from devagent.plc.production_utils import source_locator, stable_id
from devagent.plc.rockwell_motion_runtime_v11 import motion_runtime_models
from devagent.plc.rockwell_state_machine_v11 import state_transitions

_ORIGINAL_ANALYZE_REGRESSION = _regression.analyze_regression
_DOMAIN_EVIDENCE_INSTALLED = False


def _logic_paths_payload(logic) -> list[list[dict[str, object]]]:
    return [
        [
            {"tag": term.tag, "required": term.required}
            for term in path.terms
        ]
        for path in logic.paths
    ]


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
    changed = change.change_type.endswith("CHANGED")
    use_baseline = (
        change.change_type.endswith("REMOVED")
        or change.change_type == "RISK_RESOLVED"
        or changed
    )
    use_current = (
        change.change_type.endswith("ADDED")
        or change.change_type == "RISK_INTRODUCED"
        or changed
    )
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


def baseline_evidence_items(baseline, changes) -> list[EvidenceItem]:
    """Package only baseline objects actually cited by regression changes."""

    if baseline is None:
        return []
    project = baseline.project
    prefix = f"BASELINE:{project.metadata.source_sha256}:"
    wanted = {
        evidence_id
        for change in changes
        for evidence_id in change.evidence_ids
        if evidence_id.startswith(prefix)
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
                        "baseline": True,
                    },
                )
            )
    baseline_objects = [
        *[(rung, "BASELINE_RUNG", rung.text[:240]) for rung in project.rungs],
        *[(statement, f"BASELINE_{statement.language}_STATEMENT", statement.text[:240]) for statement in project.logic_statements],
        *[(logic, "BASELINE_OUTPUT_LOGIC", f"{logic.output_tag} via {logic.instruction} ({len(logic.paths)} modeled path(s))") for logic in project.output_logic],
    ]
    for item, kind, summary in baseline_objects:
        evidence_id = mapping[item.id]
        if evidence_id not in wanted:
            continue
        payload = {"baseline": True, "original_id": item.id}
        if kind == "BASELINE_OUTPUT_LOGIC":
            payload.update(
                {
                    "output_tag": item.output_tag,
                    "instruction": item.instruction,
                    "origin": item.origin,
                    "semantic_state": item.semantic_state.value,
                    "paths": _logic_paths_payload(item),
                }
            )
        result.append(
            EvidenceItem(
                evidence_id,
                kind,
                summary,
                source_locator(item.source),
                project.metadata.source_sha256,
                payload,
            )
        )
    return result


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _root(value: str) -> str:
    return re.split(r"[.\[]", value, maxsplit=1)[0]


def _affected(*groups) -> tuple[str, ...]:
    result: dict[str, str] = {}
    for group in groups:
        for raw in group:
            value = str(raw or "").strip()
            if not value:
                continue
            result.setdefault(value.casefold(), value)
            root = _root(value)
            if root:
                result.setdefault(root.casefold(), root)
    return tuple(sorted(result.values(), key=str.casefold))


def _rung_key(rung):
    return rung.program.casefold(), rung.routine.casefold(), str(rung.number).casefold()


def _statement_key(statement):
    return (
        statement.owner_type.casefold(),
        statement.owner_name.casefold(),
        statement.routine.casefold(),
        str(statement.locator).casefold(),
    )


def _source_surface_changes(baseline, engineering):
    previous = baseline.project
    current = engineering.project
    changes: list[RegressionChange] = []

    before_rungs = {_rung_key(item): item for item in previous.rungs}
    after_rungs = {_rung_key(item): item for item in current.rungs}
    for key in sorted(set(before_rungs) | set(after_rungs)):
        before = before_rungs.get(key)
        after = after_rungs.get(key)
        if before is not None and after is not None and _norm(before.text) == _norm(after.text):
            continue
        representative = after or before
        assert representative is not None
        if before is None:
            change_type = "RUNG_SOURCE_ADDED"
            summary = f"RLL source rung added at {representative.source.locator}."
        elif after is None:
            change_type = "RUNG_SOURCE_REMOVED"
            summary = f"RLL source rung removed from {representative.source.locator}."
        else:
            change_type = "RUNG_SOURCE_CHANGED"
            summary = f"RLL source text changed at {representative.source.locator}; impact analysis includes partial/runtime-required semantics, not only FULL output logic."
        evidence_ids = tuple(item.id for item in (before, after) if item is not None)
        changes.append(
            RegressionChange(
                stable_id("REG-RUNG-SOURCE", repr(key)),
                change_type,
                representative.source.locator,
                summary,
                _affected(
                    before.reads if before else (),
                    before.writes if before else (),
                    before.references if before else (),
                    after.reads if after else (),
                    after.writes if after else (),
                    after.references if after else (),
                ),
                severity=Severity.MEDIUM,
                evidence_ids=evidence_ids,
            )
        )

    before_statements = {_statement_key(item): item for item in previous.logic_statements}
    after_statements = {_statement_key(item): item for item in current.logic_statements}
    for key in sorted(set(before_statements) | set(after_statements)):
        before = before_statements.get(key)
        after = after_statements.get(key)
        same = (
            before is not None
            and after is not None
            and _norm(before.text) == _norm(after.text)
            and before.semantic_state == after.semantic_state
        )
        if same:
            continue
        representative = after or before
        assert representative is not None
        if before is None:
            change_type = "LOGIC_STATEMENT_ADDED"
            summary = f"{representative.language} statement added at {representative.source.locator}."
        elif after is None:
            change_type = "LOGIC_STATEMENT_REMOVED"
            summary = f"{representative.language} statement removed from {representative.source.locator}."
        else:
            change_type = "LOGIC_STATEMENT_CHANGED"
            summary = f"{representative.language} source/semantic state changed at {representative.source.locator}."
        changes.append(
            RegressionChange(
                stable_id("REG-STATEMENT", repr(key)),
                change_type,
                representative.source.locator,
                summary,
                _affected(
                    before.reads if before else (),
                    before.writes if before else (),
                    after.reads if after else (),
                    after.writes if after else (),
                ),
                severity=Severity.MEDIUM,
                evidence_ids=tuple(item.id for item in (before, after) if item is not None),
            )
        )

    before_transitions = {
        (item.source.locator.casefold(), item.state_tag.casefold(), item.from_state): item
        for item in state_transitions(previous)
    }
    after_transitions = {
        (item.source.locator.casefold(), item.state_tag.casefold(), item.from_state): item
        for item in state_transitions(current)
    }
    for key in sorted(set(before_transitions) | set(after_transitions)):
        before = before_transitions.get(key)
        after = after_transitions.get(key)
        same = (
            before is not None
            and after is not None
            and before.to_state == after.to_state
            and before.deterministic_action == after.deterministic_action
        )
        if same:
            continue
        representative = after or before
        assert representative is not None
        if before is None:
            change_type = "STATE_TRANSITION_ADDED"
            summary = f"New discovered state transition {representative.state_tag}: {representative.from_state} -> {representative.to_state}."
        elif after is None:
            change_type = "STATE_TRANSITION_REMOVED"
            summary = f"Previously discovered state transition {representative.state_tag}: {representative.from_state} -> {representative.to_state} is no longer present."
        else:
            change_type = "STATE_TRANSITION_CHANGED"
            summary = f"State transition at {representative.source.locator} changed from {before.from_state}->{before.to_state} to {after.from_state}->{after.to_state}."
        changes.append(
            RegressionChange(
                stable_id("REG-STATE", repr(key)),
                change_type,
                representative.source.locator,
                summary,
                _affected((representative.state_tag,)),
                severity=Severity.HIGH,
                evidence_ids=tuple(item.rung_id for item in (before, after) if item is not None),
            )
        )

    before_motion = {
        (item.source.locator.casefold(), item.instruction): item
        for item in motion_runtime_models(previous)
    }
    after_motion = {
        (item.source.locator.casefold(), item.instruction): item
        for item in motion_runtime_models(current)
    }
    for key in sorted(set(before_motion) | set(after_motion)):
        before = before_motion.get(key)
        after = after_motion.get(key)
        same = (
            before is not None
            and after is not None
            and before.primary_ref.casefold() == after.primary_ref.casefold()
            and tuple(item.casefold() for item in before.input_refs) == tuple(item.casefold() for item in after.input_refs)
        )
        if same:
            continue
        representative = after or before
        assert representative is not None
        if before is None:
            change_type = "MOTION_CONTRACT_ADDED"
            summary = f"New reachable {representative.instruction} motion command requires FAT review at {representative.source.locator}."
        elif after is None:
            change_type = "MOTION_CONTRACT_REMOVED"
            summary = f"Previously reachable {representative.instruction} motion command is no longer present at {representative.source.locator}."
        else:
            change_type = "MOTION_CONTRACT_CHANGED"
            summary = f"Reachable {representative.instruction} motion command references changed at {representative.source.locator}; engineer FAT must be reconsidered."
        changes.append(
            RegressionChange(
                stable_id("REG-MOTION", repr(key)),
                change_type,
                representative.source.locator,
                summary,
                _affected(
                    (before.primary_ref,) if before else (),
                    before.input_refs if before else (),
                    (after.primary_ref,) if after else (),
                    after.input_refs if after else (),
                ),
                severity=Severity.HIGH,
                evidence_ids=tuple(item.rung_id for item in (before, after) if item is not None),
            )
        )
    return changes


def _fat_key(test):
    return (
        test.scenario,
        test.source.locator.casefold(),
        test.output_tag.casefold(),
        tuple(sorted((key.casefold(), bool(value)) for key, value in test.preconditions.items())),
    )


def _fat_plan_changes(baseline, engineering):
    before = {_fat_key(item): item for item in baseline.fat_tests}
    after = {_fat_key(item): item for item in engineering.fat_tests}
    changes = []
    for key in sorted(set(before) | set(after), key=repr):
        previous = before.get(key)
        current = after.get(key)
        if previous is not None and current is not None and previous.expected == current.expected:
            continue
        representative = current or previous
        assert representative is not None
        if previous is None:
            change_type = "FAT_RECOMMENDATION_ADDED"
            summary = f"New FAT procedure {current.id} is recommended because the current PLC revision exposes new/changed behavior."
            affected_test_ids = (current.id,)
        elif current is None:
            change_type = "FAT_RECOMMENDATION_REMOVED"
            summary = f"Previous FAT procedure {previous.id} no longer maps to the current PLC revision; review whether its acceptance intent is still required."
            affected_test_ids = ()
        else:
            change_type = "FAT_RECOMMENDATION_CHANGED"
            summary = f"FAT procedure for {current.source.locator} changed; rerun the current procedure {current.id}."
            affected_test_ids = (current.id,)
        changes.append(
            RegressionChange(
                stable_id("REG-FAT", repr(key)),
                change_type,
                representative.source.locator,
                summary,
                _affected(
                    (representative.output_tag,),
                    representative.preconditions.keys(),
                    representative.watch_tags,
                ),
                affected_test_ids=affected_test_ids,
                severity=Severity.MEDIUM,
            )
        )
    return changes


def _structural_risks(engineering, verifications):
    evidence = _evidence.evidence_index(engineering)
    valid = {item.id for item in evidence}
    findings = _evidence.deterministic_engineering_findings(engineering, valid)
    keep = {
        "SEMANTIC_COVERAGE",
        "MULTIPLE_WRITERS",
        "RETENTIVE_LOGIC",
        "UNREACHABLE_LOGIC",
        "CONTRADICTORY_LOGIC",
        "SEQUENCING",
    }
    return [
        item
        for item in _review.detect_risks(engineering, list(verifications), [], findings)
        if item.category in keep and item.origin == "DETERMINISTIC"
    ]


def _risk_delta_changes(baseline, engineering, verifications):
    before = _structural_risks(baseline, ())
    after = _structural_risks(engineering, verifications)
    before_map = {(item.category, item.title.casefold()): item for item in before}
    after_map = {(item.category, item.title.casefold()): item for item in after}
    changes = []
    for key in sorted(set(before_map) | set(after_map)):
        previous = before_map.get(key)
        current = after_map.get(key)
        if previous is not None and current is not None:
            continue
        if current is not None:
            changes.append(
                RegressionChange(
                    stable_id("REG-RISK-NEW", current.id),
                    "RISK_INTRODUCED",
                    current.title,
                    f"Current PLC revision introduces deterministic review risk: {current.summary}",
                    severity=current.severity,
                    evidence_ids=current.evidence_ids,
                )
            )
        else:
            assert previous is not None
            changes.append(
                RegressionChange(
                    stable_id("REG-RISK-RESOLVED", previous.id),
                    "RISK_RESOLVED",
                    previous.title,
                    f"A deterministic review risk present in the baseline is no longer detected: {previous.summary}",
                    severity=Severity.LOW,
                    evidence_ids=previous.evidence_ids,
                )
            )
    return changes


def _enrich_changes(changes, baseline, engineering, verifications):
    req_by_tag = defaultdict(set)
    test_by_tag = defaultdict(set)
    for verification in verifications:
        for tag in verification.matched_tags:
            req_by_tag[tag.casefold()].add(verification.requirement_id)
            req_by_tag[_root(tag).casefold()].add(verification.requirement_id)
    for test in [*baseline.fat_tests, *engineering.fat_tests]:
        for tag in (test.output_tag, *test.preconditions.keys(), *test.watch_tags):
            test_by_tag[tag.casefold()].add(test.id)
            test_by_tag[_root(tag).casefold()].add(test.id)

    enriched = []
    for change in changes:
        folded = {tag.casefold() for tag in change.affected_tags}
        folded.update(_root(tag).casefold() for tag in change.affected_tags)
        reqs = set(change.affected_requirement_ids)
        tests = set(change.affected_test_ids)
        for tag in folded:
            reqs.update(req_by_tag.get(tag, ()))
            tests.update(test_by_tag.get(tag, ()))
        if change.change_type == "FAT_RECOMMENDATION_REMOVED":
            tests = set(change.affected_test_ids)
        enriched.append(
            replace(
                change,
                affected_requirement_ids=tuple(sorted(reqs)),
                affected_test_ids=tuple(sorted(tests)),
            )
        )
    return enriched


def analyze_regression(baseline_path, engineering, verifications):
    """Compare the full engineering surface, namespace evidence, and identify retest impact."""

    changes, baseline = _ORIGINAL_ANALYZE_REGRESSION(
        baseline_path,
        engineering,
        verifications,
    )
    if baseline is None:
        setattr(engineering, "_v9_baseline_regression_evidence", ())
        return changes, baseline

    extended = [
        *changes,
        *_source_surface_changes(baseline, engineering),
        *_fat_plan_changes(baseline, engineering),
        *_risk_delta_changes(baseline, engineering, verifications),
    ]
    deduped = []
    seen = set()
    for change in extended:
        key = (change.change_type, change.subject.casefold(), change.summary.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(change)
    deduped = _enrich_changes(deduped, baseline, engineering, verifications)

    baseline_map = _baseline_evidence_map(baseline.project)
    current_map = _current_evidence_map(engineering.project)
    remapped = [
        _remap_change_evidence(change, baseline_map, current_map)
        for change in deduped
    ]
    setattr(
        engineering,
        "_v9_baseline_regression_evidence",
        tuple(baseline_evidence_items(baseline, remapped)),
    )
    return remapped, baseline


def install() -> None:
    _regression.analyze_regression = analyze_regression


def install_domain_evidence() -> None:
    """Attach namespaced baseline evidence in both V4 and V5 stage-14 assembly."""

    global _DOMAIN_EVIDENCE_INSTALLED
    if _DOMAIN_EVIDENCE_INSTALLED:
        return
    from devagent.plc import production as _production
    from devagent.plc import production_v5 as _production_v5

    original_append = _production._append_domain_evidence

    def append_domain_evidence(result):
        original_append(result)
        existing = {item.id for item in result.evidence}
        for item in getattr(result.engineering, "_v9_baseline_regression_evidence", ()):
            if item.id not in existing:
                result.evidence.append(item)
                existing.add(item.id)

    # V4 resolves this module global at runtime. V5 imported the original helper
    # by value, so patch both bindings to prevent its evidence rebuild from
    # discarding the baseline provenance added by V4.
    _production._append_domain_evidence = append_domain_evidence
    _production_v5._append_domain_evidence = append_domain_evidence
    _DOMAIN_EVIDENCE_INSTALLED = True


__all__ = [
    "analyze_regression",
    "baseline_evidence_items",
    "install",
    "install_domain_evidence",
]
