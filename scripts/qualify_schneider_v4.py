from __future__ import annotations

import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.schneider_graphical_v4 import schneider_capability_profile_v4


LD = '''<?xml version="1.0" encoding="UTF-8"?>
<LDExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV4Qualification" version="1.0" />
  <program>
    <identProgram name="ParallelLD" type="section" task="MAST" />
    <LDSource nbColumns="11"><networkLD>
      <typeLine>
        <contact typeContact="openContact" contactVariableName="A" />
        <shortCircuit><VLink /><HLink nbCells="1" /></shortCircuit>
        <HLink nbCells="8" />
        <coil typeCoil="coil" coilVariableName="Y" />
      </typeLine>
      <typeLine>
        <contact typeContact="openContact" contactVariableName="B" />
        <HLink nbCells="1" />
        <emptyCell nbCells="9" />
      </typeLine>
    </networkLD></LDSource>
  </program>
  <dataBlock>
    <variables name="A" typeName="BOOL" />
    <variables name="B" typeName="EBOOL" />
    <variables name="Y" typeName="BOOL" />
  </dataBlock>
</LDExchangeFile>
'''

FBD = '''<?xml version="1.0" encoding="UTF-8"?>
<FBDExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV4Qualification" version="1.0" />
  <program>
    <identProgram name="GateFBD" type="section" task="MAST" />
    <FBDSource nbRows="24" nbColumns="36"><networkFBD>
      <FFBBlock instanceName=".1" typeName="AND_BOOL" additionnalPinNumber="0" enEnO="false" width="8" height="6">
        <descriptionFFB>
          <inputVariable invertedPin="false" formalParameter="EN" />
          <inputVariable invertedPin="false" formalParameter="IN1" effectiveParameter="C" />
          <inputVariable invertedPin="false" formalParameter="IN2" effectiveParameter="D" />
          <outputVariable invertedPin="false" formalParameter="ENO" />
          <outputVariable invertedPin="false" formalParameter="OUT" effectiveParameter="Z" />
        </descriptionFFB>
      </FFBBlock>
    </networkFBD></FBDSource>
  </program>
  <dataBlock>
    <variables name="C" typeName="BOOL" />
    <variables name="D" typeName="BOOL" />
    <variables name="Z" typeName="EBOOL" />
  </dataBlock>
</FBDExchangeFile>
'''

REQ = "REQ-SCH-V4-Q01: When C=TRUE and D=TRUE, Z=TRUE.\n"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v4-") as temp:
        root = Path(temp)
        export = root / "export"
        export.mkdir()
        (export / "Parallel.xld").write_text(LD, encoding="utf-8")
        (export / "Gate.xbd").write_text(FBD, encoding="utf-8")
        req = root / "requirements.md"
        req.write_text(REQ, encoding="utf-8")

        result = run_production_verification_v5(export, requirement_paths=[req])
        project = result.engineering.project
        profile = schneider_capability_profile_v4(project)

        assert profile["schema"] == "devagent-schneider-control-expert-capability-v4"
        assert profile["ld_regions"] == 1
        assert profile["ld_modeled"] == 1
        assert profile["fbd_regions"] == 1
        assert profile["fbd_modeled"] == 1
        assert profile["graphical_output_theorems"] == 2
        assert profile["graphical_writer_conflicts"] == []
        assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
        assert all(
            item.semantic_state is PLCSemanticState.FULL
            for item in project.logic_statements
            if item.language in {"LD", "FBD"}
        )
        assert all(test.execution_status == "NOT_RUN" for test in result.engineering.fat_tests)

        payload = {
            "schema": "devagent-schneider-production-qualification-v4",
            "qualified_vendor": "Schneider Electric",
            "engineering_tool": "EcoStruxure Control Expert / Unity Pro XML exchange export",
            "profile": profile,
            "proof_boundary": {
                "ld_whole_network_cell_geometry": True,
                "ld_series_parallel_boolean": True,
                "fbd_stateless_and_or_boolean": True,
                "edge_state_coils": False,
                "timers_counters_dfb_efb_state": False,
                "compare_operate_jump_control": False,
                "external_runtime_execution": False,
            },
        }
        out = Path(".devagent/schneider-production-qualification-v4.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("SCHNEIDER V4 QUALIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
