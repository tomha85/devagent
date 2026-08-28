from __future__ import annotations

import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, StaticCheckStatus
from devagent.plc.schneider_fault_recovery_v7 import schneider_capability_profile_v7


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v7-") as tmp:
        path = Path(tmp) / "Recovery.xst"
        path.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV7Qualification" version="1.0" />
  <program>
    <identProgram name="Sequence" type="section" task="MAST" />
    <STSource>
CASE State OF
0:
IF FaultDetected THEN
State := 900;
ELSIF StartCmd THEN
State := 10;
END_IF
10:
IF FaultDetected THEN
State := 900;
END_IF
900:
IF ResetCmd AND DoorInterlock THEN
State := 0;
END_IF
END_CASE
    </STSource>
  </program>
  <dataBlock>
    <variables name="State" typeName="INT" />
    <variables name="FaultDetected" typeName="BOOL" />
    <variables name="StartCmd" typeName="BOOL" />
    <variables name="ResetCmd" typeName="BOOL" />
    <variables name="DoorInterlock" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
""",
            encoding="utf-8",
        )
        result = run_production_verification_v5(path)
        profile = schneider_capability_profile_v7(result.engineering.project)
        checks = {item.id: item.status.value for item in result.engineering.static_checks}
        assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
        assert profile["schema"] == "devagent-schneider-control-expert-capability-v7"
        assert profile["recovery_contract"] == "COMPLETE"
        assert profile["fault_states"] == 1
        assert profile["fault_latched_states"] == 1
        assert profile["recovery_gaps"] == 0
        assert profile["recovery_bypass_exits"] == 0
        assert profile["stale_command_exit_hazards"] == 0
        assert checks["SCHNEIDER_V7_FAULT_RECOVERY_TOPOLOGY"] == StaticCheckStatus.PASS.value
        assert checks["SCHNEIDER_V7_FAULT_LATCH_DOMINANCE"] == StaticCheckStatus.PASS.value
        scenarios = {test.scenario for test in result.engineering.fat_tests}
        assert "SCHNEIDER_FAULT_ENTRY_V7" in scenarios
        assert "SCHNEIDER_FAULT_RECOVERY_V7" in scenarios
        assert "SCHNEIDER_RESTART_RETAINED_STATE_V7" in scenarios
        assert all(test.execution_status == "NOT_RUN" for test in result.engineering.fat_tests)

        payload = {
            "schema": "devagent-schneider-control-expert-production-qualification-v7",
            "qualified": True,
            "profile": profile,
            "checks": checks,
            "proof_boundary": {
                "numeric_state_value_implies_fault": False,
                "fault_identity_requires_positive_fault_token_on_every_incoming_path": True,
                "recovery_requires_reset_recover_ack_clear_all_path_dominance": True,
                "restart_name_is_recovery_authorization": False,
                "stale_command_exit_analysis": True,
                "recovery_overlap_analysis": True,
                "restart_retention_static_pass": False,
                "simulator_hil_real_plc_execution": False,
            },
        }
        out = Path(".devagent/schneider-production-qualification-v7.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
