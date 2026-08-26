from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RegressionChange, RequirementVerification, Severity
from devagent.plc.production_utils import source_locator, stable_id
from devagent.plc.safe_analysis import analyze_rockwell_l5x


def _logic_fingerprint(logic) -> tuple:
    paths = tuple(sorted(tuple((term.tag, term.required) for term in path.terms) for path in logic.paths))
    return logic.output_tag, logic.instruction, paths, logic.origin


def analyze_regression(
    baseline_path: Path | None,
    engineering,
    verifications: list[RequirementVerification],
) -> tuple[list[RegressionChange], object | None]:
    if baseline_path is None:
        return [], None
    baseline = analyze_rockwell_l5x(baseline_path)
    current = engineering.project
    previous = baseline.project
    changes: list[RegressionChange] = []

    current_tags = {(tag.scope, tag.name): tag for tag in current.tags}
    previous_tags = {(tag.scope, tag.name): tag for tag in previous.tags}
    for key in sorted(set(previous_tags) | set(current_tags)):
        before = previous_tags.get(key)
        after = current_tags.get(key)
        subject = f"{key[0]}::{key[1]}"
        if before is None:
            changes.append(RegressionChange(
                stable_id("REG-TAG-ADD", subject),
                "TAG_ADDED",
                subject,
                f"Tag added with data type {after.data_type}.",
                (key[1],),
                severity=Severity.LOW,
            ))
        elif after is None:
            changes.append(RegressionChange(
                stable_id("REG-TAG-DEL", subject),
                "TAG_REMOVED",
                subject,
                f"Tag removed; previous data type was {before.data_type}.",
                (key[1],),
                severity=Severity.HIGH,
            ))
        elif (before.data_type, before.alias_for, before.constant) != (after.data_type, after.alias_for, after.constant):
            changes.append(RegressionChange(
                stable_id("REG-TAG-MOD", subject),
                "TAG_CHANGED",
                subject,
                f"Tag metadata changed from {(before.data_type, before.alias_for, before.constant)} to {(after.data_type, after.alias_for, after.constant)}.",
                (key[1],),
                severity=Severity.HIGH,
            ))

    current_logic = {
        (source_locator(logic.source), logic.output_tag, logic.origin): _logic_fingerprint(logic)
        for logic in current.output_logic
        if logic.semantic_state is PLCSemanticState.FULL
    }
    previous_logic = {
        (source_locator(logic.source), logic.output_tag, logic.origin): _logic_fingerprint(logic)
        for logic in previous.output_logic
        if logic.semantic_state is PLCSemanticState.FULL
    }
    for key in sorted(set(current_logic) | set(previous_logic), key=str):
        if current_logic.get(key) == previous_logic.get(key):
            continue
        output = key[1]
        change_type = "LOGIC_ADDED" if key not in previous_logic else "LOGIC_REMOVED" if key not in current_logic else "LOGIC_CHANGED"
        changes.append(RegressionChange(
            stable_id("REG-LOGIC", repr(key)),
            change_type,
            f"{key[0]}::{output}",
            f"Modeled output logic changed for {output} at {key[0]}.",
            (output,),
            severity=Severity.MEDIUM,
        ))

    req_by_tag: dict[str, set[str]] = defaultdict(set)
    test_by_tag: dict[str, set[str]] = defaultdict(set)
    for verification in verifications:
        for tag in verification.matched_tags:
            req_by_tag[tag].add(verification.requirement_id)
            test_by_tag[tag].update(verification.linked_test_ids)
    for test in engineering.fat_tests:
        test_by_tag[test.output_tag].add(test.id)
        for tag in test.preconditions:
            test_by_tag[tag].add(test.id)

    enriched: list[RegressionChange] = []
    for change in changes:
        reqs = sorted({req for tag in change.affected_tags for req in req_by_tag.get(tag, set())})
        tests = sorted({test for tag in change.affected_tags for test in test_by_tag.get(tag, set())})
        enriched.append(replace(
            change,
            affected_requirement_ids=tuple(reqs),
            affected_test_ids=tuple(tests),
        ))
    return enriched, baseline
