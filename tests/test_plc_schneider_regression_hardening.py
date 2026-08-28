from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.production_models import RequirementStatus
from devagent.plc.schneider_interlock_permissive_v6 import schneider_capability_profile_v6


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _st(body: str, variables: list[tuple[str, str]], *, name: str = "Regression") -> str:
    tags = "\n".join(
        f'    <variables name="{tag}" typeName="{dtype}" />'
        for tag, dtype in variables
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderRegression" version="1.0" />
  <program>
    <identProgram name="{name}" type="section" task="MAST" />
    <STSource>
{body.strip()}
    </STSource>
  </program>
  <dataBlock>
{tags}
  </dataBlock>
</STExchangeFile>
"""


def test_source_filename_provenance_is_not_semantic_guard_metadata(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "OutputGuard.xst",
        _st(
            "MotorRun := Start AND DoorInterlock AND MotorReady;",
            [
                ("Start", "BOOL"),
                ("DoorInterlock", "BOOL"),
                ("MotorReady", "BOOL"),
                ("MotorRun", "BOOL"),
            ],
            name="OutputGuard",
        ),
    )
    result = run_production_verification_v5(source)
    contract = result.engineering.project._schneider_v6_guard_facts.output_contracts[0]
    roles = {term.tag: term.role for term in contract.terms}
    profile = schneider_capability_profile_v6(result.engineering.project)

    assert roles == {
        "DoorInterlock": "INTERLOCK",
        "MotorReady": "PERMISSIVE",
        "Start": "GUARD",
    }
    assert profile["classified_interlock_terms"] == 1
    assert profile["classified_permissive_terms"] == 1


def test_nonrestrictive_state_transition_remains_runtime_evidence(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Sequence.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 1;
END_IF
1:
END_CASE
""",
            [("State", "INT"), ("Start", "BOOL")],
            name="Sequence",
        ),
    )
    requirement = _write(
        tmp_path / "requirements.txt",
        "REQ-SEQ: When Start=TRUE, State shall transition from 0 to 1.",
    )

    result = run_production_verification_v5(source, requirement_paths=[requirement])

    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN


def test_restrictive_v6_output_proof_survives_v8_v9_identity_layers(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "OutputRequirement.xst",
        _st(
            "MotorRun := Start AND DoorInterlock AND MotorReady;",
            [
                ("Start", "BOOL"),
                ("DoorInterlock", "BOOL"),
                ("MotorReady", "BOOL"),
                ("MotorRun", "BOOL"),
            ],
            name="OutputRequirement",
        ),
    )
    requirement = _write(
        tmp_path / "requirements.txt",
        "REQ-OUT: MotorRun shall only be TRUE when DoorInterlock = TRUE and MotorReady = TRUE.",
    )

    result = run_production_verification_v5(source, requirement_paths=[requirement])

    assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED


def test_v4_ld_theorem_keeps_language_specific_instruction_label(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Motor.xld",
        '''<?xml version="1.0" encoding="UTF-8"?>
<LDExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderRegression" version="1.0" />
  <program>
    <identProgram name="Motor" type="section" task="MAST" />
    <LDSource nbColumns="3">
      <networkLD>
        <typeLine>
          <contact typeContact="openContact" contactVariableName="Start" />
          <contact typeContact="closedContact" contactVariableName="Stop" />
          <coil typeCoil="coil" coilVariableName="Run" />
        </typeLine>
      </networkLD>
    </LDSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Stop" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</LDExchangeFile>
''',
    )

    result = run_production_verification_v5(source)
    logic = next(item for item in result.engineering.project.output_logic if item.language == "LD")

    assert logic.instruction == "LD_COIL"
