from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from devagent.plc.models import PLCSemanticState
from devagent.plc.rockwell_alias_hardening import canonical_tag_identity, identity_is_resolved
from devagent.plc.rockwell_compare_reachability_hardening import canonical_writer_sources
from devagent.plc.rockwell_entrypoint_hardening import rung_has_execution_entry


class BehaviorKind(str, Enum):
    BOOLEAN_OUTPUT = "BOOLEAN_OUTPUT"
    THRESHOLD_OUTPUT = "THRESHOLD_OUTPUT"
    LATCHED_OUTPUT = "LATCHED_OUTPUT"
    TIMER = "TIMER"
    COUNTER = "COUNTER"
    AOI_CALL = "AOI_CALL"
    MULTI_WRITER = "MULTI_WRITER"


class TestIntentMethod(str, Enum):
    STATIC = "STATIC"
    SIMULATOR = "SIMULATOR"
    HARDWARE = "HARDWARE"


class TestIntentTrust(str, Enum):
    STATICALLY_DERIVED = "STATICALLY_DERIVED"
    DYNAMIC_REQUIRED = "DYNAMIC_REQUIRED"
    NOT_PROVEN = "NOT_PROVEN"


@dataclass(frozen=True)
class PLCBehavior:
    id: str
    kind: BehaviorKind
    subject: str
    source_locator: str
    evidence_ids: tuple[str, ...]
    inputs: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PLCTestIntent:
    id: str
    behavior_id: str
    kind: BehaviorKind
    scenario: str
    title: str
    subject: str
    source_locator: str
    preconditions: dict[str, Any]
    expected: str | None
    method: TestIntentMethod
    trust: TestIntentTrust
    evidence_ids: tuple[str, ...]
    linked_fat_test_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PLCProjectTestPlan:
    schema: str
    project_sha256: str
    behaviors: tuple[PLCBehavior, ...]
    test_intents: tuple[PLCTestIntent, ...]
    summary: dict[str, Any]


_TIMER_NAMES = {"TON", "TOF", "RTO"}
_COUNTER_NAMES = {"CTU", "CTD"}
_FIXED_REF = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_:]*(?:\[\d+\])?(?:\.[A-Za-z_][A-Za-z0-9_:]*(?:\[\d+\])?)*$"
)
_NUMERIC = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?$")


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _fixed_ref(value: str) -> str | None:
    stripped = value.strip()
    return stripped if _FIXED_REF.fullmatch(stripped) else None


def _numeric(value: str) -> int | float | None:
    stripped = value.strip()
    if not _NUMERIC.fullmatch(stripped):
        return None
    try:
        return float(stripped) if any(char in stripped.lower() for char in (".", "e")) else int(stripped, 10)
    except ValueError:
        return None


def _rung_contacts(rung) -> dict[str, bool] | None:
    contacts: dict[str, bool] = {}
    for instruction in rung.instructions:
        name = instruction.name.upper()
        if name not in {"XIC", "XIO"}:
            continue
        if len(instruction.arguments) != 1:
            return None
        ref = _fixed_ref(instruction.arguments[0])
        if ref is None:
            return None
        required = name == "XIC"
        if ref in contacts and contacts[ref] != required:
            return None
        contacts[ref] = required
    return dict(sorted(contacts.items()))


def _fat_behavior_kind(test) -> BehaviorKind:
    if test.scenario.startswith("THRESHOLD_"):
        return BehaviorKind.THRESHOLD_OUTPUT
    source_text = " ".join(test.limitations).casefold()
    if "latched" in test.expected.casefold() or "retentive" in source_text:
        return BehaviorKind.LATCHED_OUTPUT
    return BehaviorKind.BOOLEAN_OUTPUT


def _behavior_from_fat(test) -> PLCBehavior:
    kind = _fat_behavior_kind(test)
    behavior_id = _stable_id("BEHAVIOR", kind.value, test.source.locator, test.output_tag)
    return PLCBehavior(
        behavior_id,
        kind,
        test.output_tag,
        test.source.locator,
        (test.source.locator,),
        tuple(sorted(test.preconditions)),
        {"origin": "DETERMINISTIC_FAT", "fat_scenario": test.scenario},
    )


def _intent_from_fat(test, behavior: PLCBehavior) -> PLCTestIntent:
    return PLCTestIntent(
        _stable_id("TEST-INTENT", test.id, test.scenario),
        behavior.id,
        behavior.kind,
        test.scenario,
        test.title,
        test.output_tag,
        test.source.locator,
        dict(test.preconditions),
        test.expected,
        TestIntentMethod.STATIC,
        TestIntentTrust.STATICALLY_DERIVED,
        (test.id,),
        (test.id,),
        tuple(test.limitations),
    )


def _timer_counter_behaviors(engineering) -> tuple[list[PLCBehavior], list[PLCTestIntent]]:
    project = engineering.project
    behaviors: list[PLCBehavior] = []
    intents: list[PLCTestIntent] = []
    reset_sources: dict[tuple[str, str], list[str]] = {}

    for rung in project.rungs:
        if not rung_has_execution_entry(project, rung):
            continue
        for instruction in rung.instructions:
            if instruction.name.upper() != "RES" or not instruction.arguments:
                continue
            target = _fixed_ref(instruction.arguments[0])
            if target is None:
                continue
            identity = canonical_tag_identity(project, target, rung.program)
            if identity_is_resolved(identity):
                reset_sources.setdefault(identity, []).append(rung.id)

    for rung in project.rungs:
        if not rung_has_execution_entry(project, rung):
            continue
        contacts = _rung_contacts(rung)
        for instruction in rung.instructions:
            name = instruction.name.upper()
            if name not in _TIMER_NAMES | _COUNTER_NAMES or not instruction.arguments:
                continue
            target = _fixed_ref(instruction.arguments[0])
            if target is None:
                continue
            identity = canonical_tag_identity(project, target, rung.program)
            if not identity_is_resolved(identity):
                continue
            kind = BehaviorKind.TIMER if name in _TIMER_NAMES else BehaviorKind.COUNTER
            preset = _numeric(instruction.arguments[1]) if len(instruction.arguments) > 1 else None
            behavior_id = _stable_id("BEHAVIOR", kind.value, rung.id, target, name)
            metadata = {
                "instruction": name,
                "preset": preset,
                "reset_evidence_ids": sorted(set(reset_sources.get(identity, []))),
            }
            behavior = PLCBehavior(
                behavior_id,
                kind,
                target,
                rung.source.locator,
                (rung.id,),
                tuple(sorted((contacts or {}).keys())),
                metadata,
            )
            behaviors.append(behavior)

            if kind is BehaviorKind.TIMER:
                base_limitations = (
                    "Timer time progression and scan timing require qualified simulator/HIL/controller execution.",
                    "The expected timing outcome is derived only from the exported timer instruction and literal preset when present.",
                )
                if preset is not None:
                    intents.append(
                        PLCTestIntent(
                            _stable_id("TEST-INTENT", behavior_id, "NOT_EARLY"),
                            behavior_id,
                            kind,
                            "TIMER_NOT_EARLY",
                            f"Verify {target} does not complete before its configured preset",
                            target,
                            rung.source.locator,
                            dict(contacts or {}),
                            f"{target}.DN remains FALSE before {preset} time units of continuous rung-in condition",
                            TestIntentMethod.SIMULATOR,
                            TestIntentTrust.DYNAMIC_REQUIRED,
                            (rung.id,),
                            limitations=base_limitations,
                        )
                    )
                    intents.append(
                        PLCTestIntent(
                            _stable_id("TEST-INTENT", behavior_id, "AT_PRESET"),
                            behavior_id,
                            kind,
                            "TIMER_AT_PRESET",
                            f"Verify {target} completes at/after its configured preset",
                            target,
                            rung.source.locator,
                            dict(contacts or {}),
                            f"{target}.DN becomes TRUE after the rung-in condition remains true for at least {preset} time units",
                            TestIntentMethod.SIMULATOR,
                            TestIntentTrust.DYNAMIC_REQUIRED,
                            (rung.id,),
                            limitations=base_limitations,
                        )
                    )
                else:
                    intents.append(
                        PLCTestIntent(
                            _stable_id("TEST-INTENT", behavior_id, "DYNAMIC"),
                            behavior_id,
                            kind,
                            "TIMER_DYNAMIC_BEHAVIOR",
                            f"Exercise timer behavior for {target}",
                            target,
                            rung.source.locator,
                            dict(contacts or {}),
                            None,
                            TestIntentMethod.SIMULATOR,
                            TestIntentTrust.NOT_PROVEN,
                            (rung.id,),
                            limitations=("Timer preset is not a literal value; expected timing must be resolved from runtime/project data before execution.",),
                        )
                    )
            else:
                direction = "increments" if name == "CTU" else "decrements"
                intents.append(
                    PLCTestIntent(
                        _stable_id("TEST-INTENT", behavior_id, "COUNT"),
                        behavior_id,
                        kind,
                        "COUNTER_STEP",
                        f"Verify one qualifying event {direction} {target} exactly once",
                        target,
                        rung.source.locator,
                        dict(contacts or {}),
                        f"One qualifying false-to-true count event {direction} {target}.ACC by one",
                        TestIntentMethod.SIMULATOR,
                        TestIntentTrust.DYNAMIC_REQUIRED,
                        (rung.id,),
                        limitations=("Counter edge/scan behavior requires qualified simulator/HIL/controller execution.",),
                    )
                )

            for reset_id in sorted(set(reset_sources.get(identity, []))):
                intents.append(
                    PLCTestIntent(
                        _stable_id("TEST-INTENT", behavior_id, "RESET", reset_id),
                        behavior_id,
                        kind,
                        "RESET_PATH",
                        f"Verify reset path clears {target}",
                        target,
                        rung.source.locator,
                        {},
                        f"Executing the evidenced RES path clears the accumulated state of {target}",
                        TestIntentMethod.SIMULATOR,
                        TestIntentTrust.DYNAMIC_REQUIRED,
                        (rung.id, reset_id),
                        limitations=("Reset ordering and scan behavior require qualified simulator/HIL/controller execution.",),
                    )
                )
    return behaviors, intents


def _multi_writer_behaviors(engineering) -> tuple[list[PLCBehavior], list[PLCTestIntent]]:
    project = engineering.project
    candidates: dict[tuple[str, str], tuple[str, str | None]] = {}
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL or logic.origin.startswith("AOI_INTERNAL:"):
            continue
        identity = canonical_tag_identity(project, logic.output_tag, logic.source.program)
        if identity_is_resolved(identity):
            candidates.setdefault(identity, (logic.output_tag, logic.source.program))
    for statement in project.logic_statements:
        if statement.semantic_state is not PLCSemanticState.FULL or statement.owner_type == "aoi":
            continue
        program = statement.source.program or (statement.owner_name if statement.owner_type == "program" else None)
        for output in statement.writes:
            identity = canonical_tag_identity(project, output, program)
            if identity_is_resolved(identity):
                candidates.setdefault(identity, (output, program))

    behaviors: list[PLCBehavior] = []
    intents: list[PLCTestIntent] = []
    for identity, (display, program) in sorted(candidates.items()):
        writers = canonical_writer_sources(project, display, program)
        if len(writers) <= 1:
            continue
        evidence = tuple(sorted(set(writers)))
        behavior_id = _stable_id("BEHAVIOR", "MULTI_WRITER", identity, *writers)
        behavior = PLCBehavior(
            behavior_id,
            BehaviorKind.MULTI_WRITER,
            display,
            "multiple executable sources",
            evidence,
            metadata={"writer_occurrences": len(writers), "distinct_evidence_sources": len(evidence)},
        )
        behaviors.append(behavior)
        intents.append(
            PLCTestIntent(
                _stable_id("TEST-INTENT", behavior_id, "ORDERING"),
                behavior_id,
                BehaviorKind.MULTI_WRITER,
                "MULTI_WRITER_ORDERING",
                f"Verify scan/order behavior for multiple executable writers of {display}",
                display,
                "multiple executable sources",
                {},
                None,
                TestIntentMethod.SIMULATOR,
                TestIntentTrust.NOT_PROVEN,
                evidence,
                limitations=(
                    "Static writer discovery cannot prove final scan-order behavior when multiple executable writes overlap.",
                    "Exercise each writer combination in a qualified simulator/HIL/controller and compare the observed final value with the intended machine sequence.",
                ),
            )
        )
    return behaviors, intents


def _aoi_behaviors(engineering) -> tuple[list[PLCBehavior], list[PLCTestIntent]]:
    project = engineering.project
    definitions = {aoi.name: aoi for aoi in project.aois}
    behaviors: list[PLCBehavior] = []
    intents: list[PLCTestIntent] = []
    for rung in project.rungs:
        if not rung_has_execution_entry(project, rung):
            continue
        for instruction in rung.instructions:
            definition = definitions.get(instruction.name)
            if definition is None:
                continue
            bound = len(instruction.arguments) >= len(definition.parameters)
            behavior_id = _stable_id("BEHAVIOR", "AOI_CALL", rung.id, instruction.name, *instruction.arguments)
            behavior = PLCBehavior(
                behavior_id,
                BehaviorKind.AOI_CALL,
                instruction.name,
                rung.source.locator,
                (rung.id, definition.id),
                tuple(instruction.arguments),
                {
                    "call_bound": bound,
                    "body_modeled": definition.internal_body_modeled,
                    "source_protected": definition.source_protected,
                    "parameter_count": len(definition.parameters),
                },
            )
            behaviors.append(behavior)
            trust = (
                TestIntentTrust.DYNAMIC_REQUIRED
                if bound and definition.internal_body_modeled and not definition.source_protected
                else TestIntentTrust.NOT_PROVEN
            )
            intents.append(
                PLCTestIntent(
                    _stable_id("TEST-INTENT", behavior_id, "INTERFACE"),
                    behavior_id,
                    BehaviorKind.AOI_CALL,
                    "AOI_INTERFACE",
                    f"Exercise bound interface behavior for AOI {instruction.name}",
                    instruction.name,
                    rung.source.locator,
                    {},
                    None,
                    TestIntentMethod.SIMULATOR,
                    trust,
                    (rung.id, definition.id),
                    limitations=(
                        "AOI interface execution should vary each bound input/permissive and observe each bound output against the exported AOI body.",
                        "Expected output values are not invented when the exported AOI body/binding is incomplete or protected.",
                    ),
                )
            )
    return behaviors, intents


def plan_project_tests(engineering) -> PLCProjectTestPlan:
    """Build project-specific test intents from discovered PLC semantics only.

    This planner intentionally contains no equipment-name or industry-name rules.
    Two projects receive different test plans only when their normalized logic,
    dataflow, instructions, execution closure, or writer structure differs.
    """

    behaviors_by_id: dict[str, PLCBehavior] = {}
    intents_by_id: dict[str, PLCTestIntent] = {}

    for fat in engineering.fat_tests:
        behavior = _behavior_from_fat(fat)
        behaviors_by_id.setdefault(behavior.id, behavior)
        intent = _intent_from_fat(fat, behaviors_by_id[behavior.id])
        intents_by_id[intent.id] = intent

    for discover in (_timer_counter_behaviors, _multi_writer_behaviors, _aoi_behaviors):
        behaviors, intents = discover(engineering)
        for behavior in behaviors:
            behaviors_by_id.setdefault(behavior.id, behavior)
        for intent in intents:
            intents_by_id.setdefault(intent.id, intent)

    behaviors = tuple(sorted(behaviors_by_id.values(), key=lambda item: item.id))
    intents = tuple(sorted(intents_by_id.values(), key=lambda item: item.id))
    by_kind: dict[str, int] = {}
    by_method: dict[str, int] = {}
    by_trust: dict[str, int] = {}
    for intent in intents:
        by_kind[intent.kind.value] = by_kind.get(intent.kind.value, 0) + 1
        by_method[intent.method.value] = by_method.get(intent.method.value, 0) + 1
        by_trust[intent.trust.value] = by_trust.get(intent.trust.value, 0) + 1

    return PLCProjectTestPlan(
        "devagent-plc-project-test-plan-v1",
        engineering.project.metadata.source_sha256,
        behaviors,
        intents,
        {
            "behavior_count": len(behaviors),
            "test_intent_count": len(intents),
            "by_kind": dict(sorted(by_kind.items())),
            "by_method": dict(sorted(by_method.items())),
            "by_trust": dict(sorted(by_trust.items())),
            "hardcoded_domain_rules": 0,
        },
    )


__all__ = [
    "BehaviorKind",
    "PLCBehavior",
    "PLCProjectTestPlan",
    "PLCTestIntent",
    "TestIntentMethod",
    "TestIntentTrust",
    "plan_project_tests",
]
