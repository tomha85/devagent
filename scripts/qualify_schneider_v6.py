from __future__ import annotations

import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.schneider_interlock_permissive_v6 import schneider_capability_profile_v6


SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV6Qualification" version="1.0" />
  <program>
    <identProgram name="GuardQualification" type="section" task="MAST" />
    <STSource>
MotorRun := Start AND DoorInterlock AND MotorReady;
CASE State OF
0:
IF Start AND DoorInterlock THEN
State := 10;
END_IF
IF Auto AND DoorInterlock THEN
State := 10;
END_IF
10:
END_CASE
    </STSource>
  </program>
  <dataBlock>
    <variables name="State" typeName="INT" />
    <variables name="Start" typeName="BOOL" />
    <variables name="Auto" typeName="BOOL" />
    <variables name="DoorInterlock" typeName="BOOL" />
    <variables name="MotorReady" typeName="BOOL" />
    <variables name="MotorRun" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
"""

REQUIREMENTS = """REQ-V6-OUT: MotorRun shall only be TRUE when DoorInterlock = TRUE and MotorReady = TRUE.
REQ-V6-STATE: State from 0 to 10 shall only transition when DoorInterlock = TRUE.
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v6-") as temp:
        root = Path(temp)
        source = root / "GuardQualification.xst"
        req = root / "requirements.txt"
        source.write_text(SOURCE, encoding="utf-8")
        req.write_text(REQUIREMENTS, encoding="utf-8")

        result = run_production_verification_v5(source, requirement_paths=[req])
        project = result.engineering.project
        profile = schneider_capability_profile_v6(project)
        facts = getattr(project, "_schneider_v6_guard_facts")

        assert profile["schema"] == "devagent-schneider-control-expert-capability-v6"
        assert profile["guard_contract"] == "COMPLETE"
        assert profile["transition_guard_contracts"] == 1
        assert profile["output_guard_contracts"] >= 1
        assert profile["classified_interlock_terms"] >= 2
        assert profile["classified_permissive_terms"] >= 1
        assert profile["all_path_guard_terms"] >= 4

        transition = facts.transition_contracts[0]
        assert transition.semantic_state is PLCSemanticState.FULL
        assert len(transition.transition_ids) == 2
        assert dict(transition.all_path_terms) == {"DoorInterlock": True}

        output = next(item for item in facts.output_contracts if item.output_tag == "MotorRun")
        assert output.semantic_state is PLCSemanticState.FULL
        assert dict(output.all_path_terms)["DoorInterlock"] is True
        assert dict(output.all_path_terms)["MotorReady"] is True

        statuses = {
            item.requirement_id: item.status
            for item in result.requirement_verification
        }
        assert statuses == {
            "REQ-V6-OUT": RequirementStatus.STATICALLY_VERIFIED,
            "REQ-V6-STATE": RequirementStatus.STATICALLY_VERIFIED,
        }
        assert any(test.scenario == "SCHNEIDER_GUARD_ALL_PATH_BLOCK" for test in result.engineering.fat_tests)
        assert any(test.scenario == "SCHNEIDER_OUTPUT_GUARD_ALL_PATH_BLOCK" for test in result.engineering.fat_tests)
        assert all(test.execution_status == "NOT_RUN" for test in result.engineering.fat_tests)

        payload = {
            "schema": "devagent-schneider-production-qualification-v6",
            "qualified_vendor": "Schneider Electric",
            "engineering_tool": "EcoStruxure Control Expert / Unity Pro XML exchange export",
            "profile": profile,
            "proof_boundary": {
                "full_v5_transition_guards_consumed": True,
                "full_boolean_output_theorems_consumed": True,
                "same_source_target_transitions_grouped": True,
                "every_path_boolean_guard_dominance": True,
                "token_based_role_classification": True,
                "safe_polarity_inferred_from_role_name": False,
                "runtime_dependent_guard_static_pass": False,
                "sil_or_performance_level_certification": False,
                "physical_process_safety_statically_proven": False,
                "external_runtime_execution": False,
            },
            "external_evidence_gates": {
                "real_control_expert_export_corpus": "REQUIRED_FOR_COMMERCIAL_CLOSEOUT",
                "control_expert_simulator_hil_real_plc_execution": "NOT_EXECUTED",
            },
        }
        out = Path(".devagent/schneider-production-qualification-v6.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("SCHNEIDER V6 QUALIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
