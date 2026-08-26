from __future__ import annotations

import hashlib
import json

from devagent.plc.models import plc_jsonable
from devagent.plc.production_models import EvidenceItem, StageRecord, StageStatus
from devagent.plc.project_test_planner import TestIntentMethod, TestIntentTrust, plan_project_tests

_INSTALLED = False


def _plan_sha256(plan) -> str:
    payload = json.dumps(
        plc_jsonable(plan),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def install() -> None:
    """Wrap V5 once so every production run receives a project-specific test plan."""

    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import production_v5 as _production_v5

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
