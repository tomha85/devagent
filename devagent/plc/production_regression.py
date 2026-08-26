from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RegressionChange, RequirementVerification, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc.rockwell_alias_hardening import (
    canonical_tag_identity,
    identity_is_resolved,
    storage_identities_overlap,
)
from devagent.plc.safe_analysis import analyze_rockwell_l5x


def _program_from_scope(scope: str) -> str | None:
    prefix = "program:"
    return scope[len(prefix) :] if scope.casefold().startswith(prefix) else None


def _tag_key(tag) -> tuple[str, str]:
    return tag.scope.casefold(), tag.name.casefold()


def _tag_metadata(tag) -> tuple:
    return (
        tag.data_type.casefold(),
        (tag.tag_type or "").casefold(),
        (tag.alias_for or "").casefold(),
        bool(tag.constant),
        (tag.external_access or "").casefold(),
    )


def _logic_source_key(logic) -> tuple[str, str, str, str, str]:
    source = logic.source
    locator = source.rung if source.rung is not None else source.line or ""
    return (
        (source.aoi or "").casefold(),
        (source.program or "").casefold(),
        (source.routine or "").casefold(),
        str(locator).casefold(),
        logic.origin.casefold(),
    )


def _logic_ref_identity(project, logic, ref: str) -> tuple[str, str]:
    if logic.source.aoi and logic.origin.startswith("AOI_INTERNAL:"):
        return f"aoi:{logic.source.aoi.casefold()}", ref.casefold()
    return canonical_tag_identity(project, ref, logic.source.program)


def _logic_fingerprint(project, logic) -> tuple:
    paths = tuple(
        sorted(
            tuple(
                (_logic_ref_identity(project, logic, term.tag), bool(term.required))
                for term in path.terms
            )
            for path in logic.paths
        )
    )
    return (
        _logic_ref_identity(project, logic, logic.output_tag),
        logic.instruction.upper(),
        paths,
        logic.origin.casefold(),
    )


def _logic_index(project):
    result: dict[tuple, tuple[tuple, object]] = {}
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL:
            continue
        identity = _logic_ref_identity(project, logic, logic.output_tag)
        key = (*_logic_source_key(logic), identity)
        result[key] = (_logic_fingerprint(project, logic), logic)
    return result


def _identity_names(project, identity: tuple[str, str]) -> set[str]:
    names: set[str] = set()
    if identity[0].startswith("aoi:"):
        return names
    for tag in project.tags:
        candidate = canonical_tag_identity(project, tag.name, _program_from_scope(tag.scope))
        if identity_is_resolved(candidate) and storage_identities_overlap(candidate, identity):
            names.add(tag.name)
    return names


def _root_name(value: str) -> str:
    return re.split(r"[.\[]", value, maxsplit=1)[0]


def _affected_names(*values: str) -> tuple[str, ...]:
    result: dict[str, str] = {}
    for value in values:
        if not value:
            continue
        result.setdefault(value.casefold(), value)
        root = _root_name(value)
        if root:
            result.setdefault(root.casefold(), root)
    return tuple(sorted(result.values(), key=str.casefold))


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

    # Exported tag identity is Rockwell case-insensitive. Metadata changes are
    # still tracked even when physical AliasFor storage is unchanged.
    current_tags = {_tag_key(tag): tag for tag in current.tags}
    previous_tags = {_tag_key(tag): tag for tag in previous.tags}
    for key in sorted(set(previous_tags) | set(current_tags)):
        before = previous_tags.get(key)
        after = current_tags.get(key)
        representative = after or before
        assert representative is not None
        subject = f"{representative.scope}::{representative.name}"
        affected = list(_affected_names(representative.name, representative.alias_for or ""))
        if before is None:
            changes.append(RegressionChange(
                stable_id("REG-TAG-ADD", subject.casefold()),
                "TAG_ADDED",
                subject,
                f"Tag added with data type {after.data_type}.",
                tuple(affected),
                severity=Severity.LOW,
                evidence_ids=(after.id,),
            ))
        elif after is None:
            changes.append(RegressionChange(
                stable_id("REG-TAG-DEL", subject.casefold()),
                "TAG_REMOVED",
                subject,
                f"Tag removed; previous data type was {before.data_type}.",
                tuple(affected),
                severity=Severity.HIGH,
                evidence_ids=(before.id,),
            ))
        elif _tag_metadata(before) != _tag_metadata(after):
            affected = list(
                _affected_names(
                    before.name,
                    after.name,
                    before.alias_for or "",
                    after.alias_for or "",
                )
            )
            changes.append(RegressionChange(
                stable_id("REG-TAG-MOD", subject.casefold()),
                "TAG_CHANGED",
                subject,
                (
                    "Tag metadata changed from "
                    f"{_tag_metadata(before)} to {_tag_metadata(after)}."
                ),
                tuple(affected),
                severity=Severity.HIGH,
                evidence_ids=(before.id, after.id),
            ))

    current_logic = _logic_index(current)
    previous_logic = _logic_index(previous)
    for key in sorted(set(current_logic) | set(previous_logic), key=repr):
        before_entry = previous_logic.get(key)
        after_entry = current_logic.get(key)
        if before_entry is not None and after_entry is not None and before_entry[0] == after_entry[0]:
            continue

        before_logic = before_entry[1] if before_entry else None
        after_logic = after_entry[1] if after_entry else None
        representative = after_logic or before_logic
        assert representative is not None
        identity = _logic_ref_identity(
            current if after_logic is not None else previous,
            representative,
            representative.output_tag,
        )
        names = set()
        if identity_is_resolved(identity):
            names.update(_identity_names(current, identity))
            names.update(_identity_names(previous, identity))
        names.update(_affected_names(representative.output_tag))
        affected_tags = tuple(sorted(names, key=str.casefold))

        if before_entry is None:
            change_type = "LOGIC_ADDED"
        elif after_entry is None:
            change_type = "LOGIC_REMOVED"
        else:
            change_type = "LOGIC_CHANGED"

        source = representative.source
        locator = " / ".join(
            value
            for value in (
                source.aoi and f"AOI {source.aoi}",
                source.program,
                source.routine,
                source.rung is not None and f"Rung {source.rung}",
                source.line is not None and f"Line {source.line}",
            )
            if value
        )
        evidence_ids = tuple(
            item.id
            for item in (before_logic, after_logic)
            if item is not None
        )
        changes.append(RegressionChange(
            stable_id("REG-LOGIC", repr(key)),
            change_type,
            f"{locator}::{representative.output_tag}",
            f"Modeled output logic changed for {representative.output_tag} at {locator}.",
            affected_tags,
            severity=Severity.MEDIUM,
            evidence_ids=evidence_ids,
        ))

    req_by_tag: dict[str, set[str]] = defaultdict(set)
    test_by_tag: dict[str, set[str]] = defaultdict(set)
    for verification in verifications:
        for tag in verification.matched_tags:
            req_by_tag[tag.casefold()].add(verification.requirement_id)
            test_by_tag[tag.casefold()].update(verification.linked_test_ids)
    for test in engineering.fat_tests:
        test_by_tag[test.output_tag.casefold()].add(test.id)
        for tag in test.preconditions:
            test_by_tag[tag.casefold()].add(test.id)

    enriched: list[RegressionChange] = []
    for change in changes:
        folded_tags = {_root_name(tag).casefold() for tag in change.affected_tags}
        folded_tags.update(tag.casefold() for tag in change.affected_tags)
        reqs = sorted({req for tag in folded_tags for req in req_by_tag.get(tag, set())})
        tests = sorted({test for tag in folded_tags for test in test_by_tag.get(tag, set())})
        enriched.append(replace(
            change,
            affected_requirement_ids=tuple(reqs),
            affected_test_ids=tuple(tests),
        ))
    return enriched, baseline
