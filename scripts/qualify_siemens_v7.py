from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.siemens_recovery_v7 import siemens_capability_profile_v7


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _source(body: str) -> str:
    return f"""
ORGANIZATION_BLOCK Main
VAR
    State : MachineState;
    TripDetected : Bool;
    ResetCmd : Bool;
END_VAR
BEGIN
{body}
END_ORGANIZATION_BLOCK
"""


def _recovery_complete(root: Path) -> dict[str, object]:
    source = _write(
        root / "recovery-complete.scl",
        _source(
            """
CASE State OF
    IDLE:
        IF TripDetected THEN
            State := FAULT;
        END_IF;
    FAULT:
        IF ResetCmd THEN
            State := IDLE;
        END_IF;
END_CASE;
"""
        ),
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v7(result.engineering.project)
    if result.engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(
            f"bounded named-state recovery topology did not close statically: {result.engineering.outcome}"
        )
    if profile["recovery_contract"] != "COMPLETE":
        raise RuntimeError(f"recovery contract incomplete: {profile}")
    if profile["named_fault_states"] != 1:
        raise RuntimeError(f"named fault-state discovery mismatch: {profile}")
    if profile["recovery_transitions"] != 1:
        raise RuntimeError(f"recovery transition count mismatch: {profile}")
    if profile["fault_recovery_gaps"] != 0:
        raise RuntimeError(f"unexpected fault recovery gap: {profile}")
    if not any(
        test.scenario == "SIEMENS_FAULT_RECOVERY"
        and test.execution_status == "NOT_RUN"
        for test in result.engineering.fat_tests
    ):
        raise RuntimeError("V7 recovery FAT candidate missing or falsely executed")
    if not any(
        test.scenario == "SIEMENS_RESTART_RETAINED_STATE"
        and test.execution_status == "NOT_RUN"
        for test in result.engineering.fat_tests
    ):
        raise RuntimeError("V7 restart/retained-state FAT candidate missing")
    return {
        "outcome": result.engineering.outcome.value,
        "recovery_contract": profile["recovery_contract"],
        "named_fault_states": profile["named_fault_states"],
        "recovery_transitions": profile["recovery_transitions"],
        "restart_retention_contract": profile["restart_retention_contract"],
    }


def _recovery_gap(root: Path) -> dict[str, object]:
    source = _write(
        root / "recovery-gap.scl",
        _source(
            """
CASE State OF
    IDLE:
        IF TripDetected THEN
            State := FAULT;
        END_IF;
    FAULT:
END_CASE;
"""
        ),
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v7(result.engineering.project)
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("named fault state without explicit recovery did not fail closed")
    if profile["fault_recovery_gaps"] != 1:
        raise RuntimeError(f"fault recovery gap was not detected: {profile}")
    if profile["recovery_contract"] != "PARTIAL_FAIL_CLOSED":
        raise RuntimeError(f"fault recovery gap did not mark contract partial: {profile}")
    if not any(
        risk.category == "FAULT_RECOVERY"
        and "without an explicit recovery exit" in risk.title
        for risk in result.risks
    ):
        raise RuntimeError("fault recovery gap lost deterministic risk")
    if not any(
        test.scenario == "SIEMENS_FAULT_RECOVERY_GAP"
        and test.execution_status == "NOT_RUN"
        for test in result.engineering.fat_tests
    ):
        raise RuntimeError("fault recovery gap lost engineer FAT")
    return {
        "outcome": result.engineering.outcome.value,
        "recovery_contract": profile["recovery_contract"],
        "fault_recovery_gaps": profile["fault_recovery_gaps"],
        "risk": "FAULT_RECOVERY",
        "fat_status": "NOT_RUN",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify DevAgent Siemens TIA V7 recovery/reset/restart contract"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v7-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v7",
            "contract": (
                "Siemens-only recovery topology derived from V5 state transitions and explicit V6 "
                "reset/recovery/ack/clear/restart guard metadata. Named fault-state gaps and recovery "
                "conflicts fail closed; cold/warm restart and retained-state behavior remain engineer runtime FAT."
            ),
            "bounded_recovery": _recovery_complete(root),
            "fault_gap_fail_closed": _recovery_gap(root),
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
