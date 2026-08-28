from __future__ import annotations

import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.schneider_call_graph_v3 import schneider_capability_profile_v3


MAIN = '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV3Qualification" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
Motor1(Start := Start, Guard := Guard, Run => Run);
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Guard" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
    <variables name="Motor1" typeName="MOTOR_DFB" />
  </dataBlock>
</STExchangeFile>
'''

DFB = '''<?xml version="1.0" encoding="UTF-8"?>
<FBExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" content="Function Block source file" />
  <contentHeader name="SchneiderV3Qualification" version="1.0" />
  <FBSource nameOfFBType="MOTOR_DFB" version="1.0">
    <inputParameters>
      <variables name="Start" typeName="BOOL" />
      <variables name="Guard" typeName="BOOL" />
    </inputParameters>
    <outputParameters>
      <variables name="Run" typeName="BOOL" />
    </outputParameters>
    <FBProgram name="MOTOR_DFB">
      <STSource>
Run := Start AND Guard;
      </STSource>
    </FBProgram>
  </FBSource>
</FBExchangeFile>
'''

REQ = "REQ-SCH-V3-Q01: When Start=TRUE and Guard=TRUE, Run=TRUE.\n"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v3-") as temp:
        root = Path(temp)
        export = root / "export"
        export.mkdir()
        (export / "Main.xst").write_text(MAIN, encoding="utf-8")
        (export / "Motor.xdb").write_text(DFB, encoding="utf-8")
        requirement = root / "requirements.md"
        requirement.write_text(REQ, encoding="utf-8")

        result = run_production_verification_v5(export, requirement_paths=[requirement])
        project = result.engineering.project
        profile = schneider_capability_profile_v3(project)
        facts = getattr(project, "_schneider_v3_facts")

        assert profile["schema"] == "devagent-schneider-control-expert-capability-v3"
        assert profile["dfb_types"] == 1
        assert profile["dfb_calls"] == 1
        assert profile["dfb_calls_bound"] == 1
        assert profile["execution_closure"] == "COMPLETE"
        assert profile["projected_call_theorems"] == 1
        assert facts.calls[0].semantic_state is PLCSemanticState.FULL
        assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
        assert any(item.id.startswith("SCHNEIDER-CALL3-") for item in project.output_logic)
        assert all(item.execution_status == "NOT_RUN" for item in result.engineering.fat_tests)

        payload = {
            "schema": "devagent-schneider-production-qualification-v3",
            "qualified_vendor": "Schneider Electric",
            "engineering_tool": "EcoStruxure Control Expert / Unity Pro XML exchange export",
            "dfb_fixture": {
                "profile": profile,
                "calls": len(facts.calls),
                "projected_logic": len(facts.projected_logic_ids),
                "requirement_status": result.requirement_verification[0].status.value,
            },
            "proof_boundary": {
                "binding_identity_is_runtime_behavior_proof": False,
                "guarded_positional_complex_protected_recursive_calls_fail_closed": True,
                "external_runtime_execution": False,
            },
        }
        out = Path(".devagent/schneider-production-qualification-v3.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("SCHNEIDER V3 QUALIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
