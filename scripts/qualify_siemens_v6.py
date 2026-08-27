from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.production_models import RequirementStatus
from devagent.plc.siemens_interlock_permissive_v6 import siemens_capability_profile_v6


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _source(body: str, declarations: str = "") -> str:
    return f"""
ORGANIZATION_BLOCK Main
VAR
    State : Int;
    Start : Bool;
    DoorInterlock : Bool;
    MotorReady : Bool;
    ResetCmd : Bool;
{declarations}
END_VAR
BEGIN
{body}
END_ORGANIZATION_BLOCK
"""


def _guard_contract(root: Path) -> dict[str, object]:
    source = _write(
        root / "guard.scl",
        _source(
            """
CASE State OF
    0:
        IF Start AND DoorInterlock AND MotorReady THEN
            State := 10;
        END_IF;
    10:
        IF ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )
    result = analyze_plc_project(source)
    profile = siemens_capability_profile_v6(result.project)
    if result.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(f"bounded V6 guard project did not close: {result.outcome}")
    if profile["guard_contract"] != "COMPLETE":
        raise RuntimeError(f"V6 guard contract incomplete: {profile}")
    if profile["classified_interlock_terms"] != 1:
        raise RuntimeError(f"interlock classification mismatch: {profile}")
    if profile["classified_permissive_terms"] != 1:
        raise RuntimeError(f"permissive classification mismatch: {profile}")
    if profile["classified_recovery_terms"] != 1:
        raise RuntimeError(f"recovery classification mismatch: {profile}")
    if not any(test.scenario == "SIEMENS_GUARD_PERMIT" for test in result.fat_tests):
        raise RuntimeError("V6 permit FAT candidate missing")
    if not any(test.scenario == "SIEMENS_GUARD_PATH_BLOCK" for test in result.fat_tests):
        raise RuntimeError("V6 guard denial FAT candidate missing")
    return {
        "outcome": result.outcome.value,
        "guard_contract": profile["guard_contract"],
        "guard_terms": profile["transition_guard_terms"],
        "interlocks": profile["classified_interlock_terms"],
        "permissives": profile["classified_permissive_terms"],
        "recovery_terms": profile["classified_recovery_terms"],
    }


def _requirement_proof(root: Path) -> dict[str, object]:
    source = _write(
        root / "requirement.scl",
        _source(
            """
CASE State OF
    0:
        IF Start AND DoorInterlock AND MotorReady THEN
            State := 10;
        END_IF;
    10:
        IF ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )
    requirements = _write(
        root / "requirements.txt",
        "REQ-V6: State from 0 to 10 shall transition when Start = TRUE, DoorInterlock = TRUE, and MotorReady = TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=(requirements,),
    )
    verification = next(
        item for item in result.requirement_verification if item.requirement_id == "REQ-V6"
    )
    if verification.status is not RequirementStatus.STATICALLY_VERIFIED:
        raise RuntimeError(f"exact V6 requirement was not statically verified: {verification}")
    if not verification.linked_test_ids:
        raise RuntimeError("V6 requirement proof lost FAT traceability")
    return {
        "status": verification.status.value,
        "linked_tests": list(verification.linked_test_ids),
        "confidence": verification.confidence,
    }


def _runtime_boundary(root: Path) -> dict[str, object]:
    source = _write(
        root / "runtime.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
    10:
        Delay(IN := TRUE, PT := T#1s);
        IF Delay.Q AND ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
""",
            declarations="    Delay : TON;",
        ),
    )
    requirements = _write(
        root / "runtime-requirements.txt",
        "REQ-V6-RUNTIME: State from 10 to 0 shall transition when Delay.Q = TRUE and ResetCmd = TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=(requirements,),
    )
    verification = next(
        item
        for item in result.requirement_verification
        if item.requirement_id == "REQ-V6-RUNTIME"
    )
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("timer-dependent V6 project incorrectly received static closure")
    if verification.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        raise RuntimeError("runtime-dependent V6 requirement was falsely verified")
    return {
        "outcome": result.engineering.outcome.value,
        "requirement_status": verification.status.value,
        "runtime_boundary": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify DevAgent Siemens TIA V6 interlock/permissive and requirement contract"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v6-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v6",
            "contract": (
                "Siemens-only V5 transition guards with deterministic metadata/name role classification, "
                "engineer FAT permit/denial procedures, and exact explicit requirement-to-transition proof. "
                "Runtime timer/counter dependencies remain fail-closed."
            ),
            "guard_contract": _guard_contract(root),
            "requirement_traceability": _requirement_proof(root),
            "runtime_fail_closed": _runtime_boundary(root),
            "external_execution": False,
            "result": "PASS",
        }

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
