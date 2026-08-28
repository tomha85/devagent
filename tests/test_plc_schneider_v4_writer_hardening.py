from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.schneider_graphical_v4 import schneider_capability_profile_v4


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_schneider_v4_writer_conflict_reconciles_region_evidence_and_fat(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()

    _write(
        export / "Logic.xbd",
        '''<?xml version="1.0" encoding="UTF-8"?>
<FBDExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="WriterHardening" version="1.0" />
  <program>
    <identProgram name="Gate" type="section" task="MAST" />
    <FBDSource nbRows="24" nbColumns="36"><networkFBD>
      <FFBBlock instanceName=".1" typeName="AND_BOOL" additionnalPinNumber="0" enEnO="false" width="8" height="6">
        <descriptionFFB>
          <inputVariable invertedPin="false" formalParameter="EN" />
          <inputVariable invertedPin="false" formalParameter="IN1" effectiveParameter="A" />
          <inputVariable invertedPin="false" formalParameter="IN2" effectiveParameter="B" />
          <outputVariable invertedPin="false" formalParameter="ENO" />
          <outputVariable invertedPin="false" formalParameter="OUT" effectiveParameter="Y" />
        </descriptionFFB>
      </FFBBlock>
    </networkFBD></FBDSource>
  </program>
  <dataBlock>
    <variables name="A" typeName="BOOL" />
    <variables name="B" typeName="BOOL" />
    <variables name="Y" typeName="BOOL" />
  </dataBlock>
</FBDExchangeFile>''',
    )

    _write(
        export / "Override.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="WriterHardening" version="1.0" />
  <program>
    <identProgram name="Override" type="section" task="MAST" />
    <STSource>Y := Override;</STSource>
  </program>
  <dataBlock>
    <variables name="Override" typeName="BOOL" />
    <variables name="Y" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>''',
    )

    requirement = _write(
        tmp_path / "requirements.md",
        "REQ-SCH-V4-W01: When A=TRUE and B=TRUE, Y=TRUE.",
    )

    result = run_production_verification_v5(export, requirement_paths=[requirement])
    project = result.engineering.project
    facts = getattr(project, "_schneider_v4_facts")
    profile = schneider_capability_profile_v4(project)

    assert profile["graphical_writer_conflicts"] == ["Y"]
    assert profile["graphical_output_theorems"] == 0
    assert profile["graphical_full"] == 0
    assert profile["graphical_partial"] == 1
    assert facts.regions[0].semantic_state is PLCSemanticState.PARTIAL
    assert facts.regions[0].reason == "competing_output_writer"
    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert any(
        test.scenario == "SCHNEIDER_GRAPHICAL_RUNTIME"
        and test.output_tag == "Y"
        for test in result.engineering.fat_tests
    )
    assert any(
        check.id == "SCHNEIDER_V4_GRAPHICAL_SEMANTICS"
        and check.status.value == "NOT_PROVEN"
        for check in result.engineering.static_checks
    )
