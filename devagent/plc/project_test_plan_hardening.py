from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from devagent.plc.models import plc_jsonable
from devagent.plc.production_models import EvidenceItem, StageRecord, StageStatus
from devagent.plc.project_test_planner import (
    BehaviorKind,
    TestIntentMethod,
    TestIntentTrust,
    plan_project_tests,
)

_INSTALLED = False


def _plan_sha256(plan) -> str:
    payload = json.dumps(
        plc_jsonable(plan),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _install_planner_guards() -> None:
    from devagent.plc import project_test_planner as _planner

    def fat_behavior_kind(test):
        if test.scenario.startswith("THRESHOLD_"):
            return BehaviorKind.THRESHOLD_OUTPUT
        expected = test.expected.casefold()
        if "=true (latched)" in expected or "=false (unlatched)" in expected:
            return BehaviorKind.LATCHED_OUTPUT
        return BehaviorKind.BOOLEAN_OUTPUT

    _planner._fat_behavior_kind = fat_behavior_kind

    original_timer_counter = _planner._timer_counter_behaviors

    def timer_counter_behaviors(engineering):
        behaviors, intents = original_timer_counter(engineering)
        by_id = {item.id: item for item in behaviors}
        guarded = []
        replaced_timers: set[str] = set()
        for intent in intents:
            if intent.kind is not BehaviorKind.TIMER:
                guarded.append(intent)
                continue
            behavior = by_id.get(intent.behavior_id)
            instruction = str((behavior.metadata or {}).get("instruction") or "").upper() if behavior else ""
            if instruction == "TON":
                guarded.append(intent)
                continue
            # TOF and RTO have timing/retentive semantics that are materially
            # different from TON. Until their dedicated theorem is implemented,
            # do not reuse TON boundary expectations merely because PRE is known.
            if intent.behavior_id in replaced_timers:
                continue
            replaced_timers.add(intent.behavior_id)
            guarded.append(
                replace(
                    intent,
                    scenario=f"{instruction or 'TIMER'}_DYNAMIC_BEHAVIOR",
                    title=f"Exercise {instruction or 'timer'} runtime behavior for {intent.subject}",
                    preconditions={},
                    expected=None,
                    method=TestIntentMethod.SIMULATOR,
                    trust=TestIntentTrust.NOT_PROVEN,
                    limitations=(
                        f"{instruction or 'Timer'} timing semantics require a dedicated deterministic theorem or qualified runtime evidence.",
                        "DevAgent intentionally does not substitute TON timing expectations for a different timer instruction.",
                    ),
                )
            )
        return behaviors, guarded

    _planner._timer_counter_behaviors = timer_counter_behaviors


def install() -> None:
    """Wrap V5 once so every production run receives a project-specific test plan."""

    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import production_v5 as _production_v5

    _install_planner_guards()
    original = _production_v5.run_production_verification_v5

    def run_production_verification_v5(*args, **kwargs):
        result = original(*args, **kwargs)
        plan = plan_project_tests(result.engineering)
        result.project_test_plan = plan
        plan_sha = _plan_sha256(plan)

        static_count = sum(
            1 for item in plan.test_intents
            if item.method is TestIntentMethod.STATIC
            and item.trust is TestIntentTrust.STATICALLY_DERIVED
        )
        dynamic_count = sum(
            1 for item in plan.test_intents
            if item.method is TestIntentMethod.SIMULATOR
        )
        not_proven_count = sum(
            1 for item in plan.test_intents
            if item.trust is TestIntentTrust.NOT_PROVEN
        )
        evidence_id = f"PROJECT-TEST-PLAN:{plan_sha}"
        result.evidence.append(
            EvidenceItem(
                evidence_id,
                "PROJECT_TEST_PLAN",
                (
                    f"Project-specific test planning derived {len(plan.behaviors)} behavior(s) and "
                    f"{len(plan.test_intents)} test intent(s) from normalized PLC semantics without domain-name rules."
                ),
                result.engineering.project.metadata.source_path,
                result.engineering.project.metadata.source_sha256,
                {
                    "schema": plan.schema,
                    "plan_sha256": plan_sha,
                    "summary": plan.summary,
                    "behaviors": plc_jsonable(plan.behaviors),
                    "test_intents": plc_jsonable(plan.test_intents),
                },
            )
        )

        if len(result.stages) >= 8:
            result.stages[7] = StageRecord(
                8,
                result.stages[7].name,
                StageStatus.PASS if plan.test_intents else StageStatus.PARTIAL,
                (
                    f"Generated {len(result.engineering.fat_tests)} deterministic executable FAT candidate(s) plus "
                    f"{len(plan.test_intents)} project-specific test intent(s): {static_count} statically derived, "
                    f"{dynamic_count} simulator-oriented, {not_proven_count} explicitly NOT_PROVEN until runtime/context evidence exists."
                ),
                (evidence_id,),
            )
        return result

    _production_v5.run_production_verification_v5 = run_production_verification_v5
    _INSTALLED = True


__all__ = ["install"]
