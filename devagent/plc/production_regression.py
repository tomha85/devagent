from __future__ import annotations

import re
from collections import Counter, defaultdict
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
                sorted(
                    (
                        _logic_ref_identity(project, logic, term.tag),
                        bool(term.required),
                    )
                    for term in path.terms
                )
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
    """Index a source to a deterministic multiset of FULL output semantics."""
    buckets: dict[tuple, list[tuple[tuple, object]]] = defaultdict(list)
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL:
            continue
        buckets[_logic_source_key(logic)].append((_logic_fingerprint(project, logic), logic))
    return {
        key: tuple(sorted(items, key=lambda item: (repr(item[0]), item[1].id)))
        for key, items in buckets.items()
    }


def _bucket_fingerprints(bucket) -> tuple:
    return tuple(item[0] for item in bucket or ())


def _changed_bucket_entries(before_bucket, after_bucket):
    """Return only semantic entries not shared by the two source buckets."""
    before_common = Counter(item[0] for item in before_bucket or ())
    after_common = Counter(item[0] for item in after_bucket or ())
    common = before_common & after_common

    def remaining(bucket):
        skip = common.copy()
        result = []
        for item in bucket or ():
            fingerprint = item[0]
            if skip[fingerprint] > 0:
                skip[fingerprint] -= 1
                continue
            result.append(item)
        return tuple(result)

    return remaining(before_bucket), remaining(after_bucket)


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


def _bucket_affected_names(current, previous, before_bucket, after_bucket) -> tuple[str, ...]:
    names: set[str] = set()
    for project, bucket in ((previous, before_bucket), (current, after_bucket)):
        for _, logic in bucket or ():
            identity = _logic_ref_identity(project, logic, logic.output_tag)
            if identity_is_resolved(identity):
                names.update(_identity_names(current, identity))
                names.update(_identity_names(previous, identity))
            names.update(_affected_names(logic.output_tag))
    return tuple(sorted(names, key=str.casefold))


def _source_locator(logic) -> str:
    source = logic.source
    return " / ".join(
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
        before_bucket = previous_logic.get(key, ())
        after_bucket = current_logic.get(key, ())
        if _bucket_fingerprints(before_bucket) == _bucket_fingerprints(after_bucket):
            continue

        changed_before, changed_after = _changed_bucket_entries(before_bucket, after_bucket)
        if not before_bucket:
            change_type = "LOGIC_ADDED"
        elif not after_bucket:
            change_type = "LOGIC_REMOVED"
        else:
            change_type = "LOGIC_CHANGED"

        representative = (changed_after or changed_before or after_bucket or before_bucket)[0][1]
        locator = _source_locator(representative)
        affected_tags = _bucket_affected_names(
            current,
            previous,
            changed_before,
            changed_after,
        )
        outputs = sorted(
            {
                logic.output_tag
                for _, logic in (*changed_before, *changed_after)
            },
            key=str.casefold,
        )
        output_label = ", ".join(outputs[:4]) or "output semantics"
        if len(outputs) > 4:
            output_label += f", +{len(outputs) - 4} output(s)"
        evidence_ids = tuple(
            dict.fromkeys(
                logic.id
                for _, logic in (*changed_before, *changed_after)
            )
        )
        changes.append(RegressionChange(
            stable_id("REG-LOGIC", repr(key)),
            change_type,
            f"{locator}::{output_label}",
            f"Modeled output logic changed at {locator} for {output_label}.",
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
