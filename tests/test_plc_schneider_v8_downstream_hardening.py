from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RequirementStatus


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_v8_type_failure_invalidates_v6_output_guard_and_boolean_fat(tmp_path: Path) -> None:
    root = tmp_path / "typed-v6"
    root.mkdir()
    _write(
        root / "Types.xdd",
        '''
<DDTExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8" version="1.0" />
  <DDT name="PairData">
    <variables name="Value" typeName="BOOL" />
  </DDT>
</DDTExchangeFile>
''',
    )
    _write(
        root / "Main.xst",
        '''
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
Whole := Other;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Whole" typeName="PairData" />
    <variables name="Other" typeName="PairData" />
  </dataBlock>
</STExchangeFile>
''',
    )
    requirements = _write(
        tmp_path / "requirements.md",
        "REQ-V8-TYPED-001: Whole shall be TRUE only when Other=TRUE.",
    )

    production = run_production_verification_v5(root, requirement_paths=[requirements])
    project = production.engineering.project
    v6facts = project._schneider_v6_guard_facts
    contract = next(
        item for item in v6facts.output_contracts
        if item.output_tag.casefold() == "whole"
    )

    assert contract.semantic_state is PLCSemanticState.PARTIAL
    assert contract.all_path_terms == ()
    assert contract.reason == "typed_non_boolean_identity_v8"
    assert production.requirement_verification[0].status is not RequirementStatus.STATICALLY_VERIFIED
    assert production.requirement_verification[0].status is not RequirementStatus.CONFLICT
    assert not any(
        test.output_tag.casefold() == "whole"
        and (test.scenario in {"POSITIVE_PATH", "NEGATIVE_PATH"} or test.scenario.startswith("SCHNEIDER_OUTPUT_GUARD_"))
        for test in production.engineering.fat_tests
    )
