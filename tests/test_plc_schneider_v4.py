from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_graphical_v4 import schneider_capability_profile_v4


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _ld(body: str, variables: list[tuple[str, str]], *, name: str = "LDV4") -> str:
    tags = "\n".join(f'    <variables name="{tag}" typeName="{dtype}" />' for tag, dtype in variables)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<LDExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV4" version="1.0" />
  <program>
    <identProgram name="{name}" type="section" task="MAST" />
    <LDSource nbColumns="11">
      <networkLD>
{body.strip()}
      </networkLD>
    </LDSource>
  </program>
  <dataBlock>
{tags}
  </dataBlock>
</LDExchangeFile>
'''


def _fbd(body: str, variables: list[tuple[str, str]], *, name: str = "FBDV4") -> str:
    tags = "\n".join(f'    <variables name="{tag}" typeName="{dtype}" />' for tag, dtype in variables)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<FBDExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV4" version="1.0" />
  <program>
    <identProgram name="{name}" type="section" task="MAST" />
    <FBDSource nbRows="24" nbColumns="36">
      <networkFBD>
{body.strip()}
      </networkFBD>
    </FBDSource>
  </program>
  <dataBlock>
{tags}
  </dataBlock>
</FBDExchangeFile>
'''


def _gate(instance: str, kind: str, in1: str, in2: str, out: str = "", *, invert1: bool = False) -> str:
    output = f' effectiveParameter="{out}"' if out else ""
    return f'''
        <FFBBlock instanceName="{instance}" typeName="{kind}" additionnalPinNumber="0" enEnO="false" width="8" height="6">
          <descriptionFFB>
            <inputVariable invertedPin="false" formalParameter="EN" />
            <inputVariable invertedPin="{'true' if invert1 else 'false'}" formalParameter="IN1" effectiveParameter="{in1}" />
            <inputVariable invertedPin="false" formalParameter="IN2" effectiveParameter="{in2}" />
            <outputVariable invertedPin="false" formalParameter="ENO" />
            <outputVariable invertedPin="false" formalParameter="OUT"{output} />
          </descriptionFFB>
        </FFBBlock>
'''


def test_schneider_v4_rebuilds_simple_ld_as_whole_graph(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Motor.xld",
        _ld(
            '''
        <typeLine>
          <contact typeContact="openContact" contactVariableName="Start" />
          <contact typeContact="closedContact" contactVariableName="Stop" />
          <HLink nbCells="8" />
          <coil typeCoil="coil" coilVariableName="Run" />
        </typeLine>
''',
            [("Start", "BOOL"), ("Stop", "BOOL"), ("Run", "EBOOL")],
        ),
    )
    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = schneider_capability_profile_v4(project)

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v4"
    assert profile["ld_regions"] == 1
    assert profile["ld_modeled"] == 1
    assert profile["graphical_output_theorems"] == 1
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert len([item for item in project.logic_statements if item.language == "LD"]) == 1
    statement = next(item for item in project.logic_statements if item.language == "LD")
    assert statement.id.startswith("SCHNEIDER-LD4-")
    assert statement.semantic_state is PLCSemanticState.FULL
    logic = next(item for item in project.output_logic if item.origin.startswith("SCHNEIDER_LD_V4:"))
    assert logic.output_tag == "Run"
    assert {term.tag: term.required for term in logic.paths[0].terms} == {"Start": True, "Stop": False}


def test_schneider_v4_ld_short_circuit_parallel_branch_is_or(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Parallel.xld",
        _ld(
            '''
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
''',
            [("A", "BOOL"), ("B", "EBOOL"), ("Y", "BOOL")],
            name="Parallel",
        ),
    )
    result = run_production_verification_v5(source)
    logic = next(item for item in result.engineering.project.output_logic if item.output_tag == "Y")
    assert len(logic.paths) == 2
    normalized = {
        tuple((term.tag, term.required) for term in path.terms)
        for path in logic.paths
    }
    assert normalized == {(('A', True),), (('B', True),)}
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED


def test_schneider_v4_edge_contact_fails_closed_and_generates_fat(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Edge.xld",
        _ld(
            '''
        <typeLine>
          <contact typeContact="PContact" contactVariableName="Pulse" />
          <HLink nbCells="9" />
          <coil typeCoil="coil" coilVariableName="Y" />
        </typeLine>
''',
            [("Pulse", "EBOOL"), ("Y", "BOOL")],
            name="Edge",
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v4(result.engineering.project)
    assert profile["ld_modeled"] == 0
    assert profile["graphical_opaque"] == 1
    assert profile["graphical_output_theorems"] == 0
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(test.scenario == "SCHNEIDER_GRAPHICAL_RUNTIME" for test in result.engineering.fat_tests)
    assert any(risk.category == "SEMANTIC_COVERAGE" for risk in result.risks)


def test_schneider_v4_fbd_and_gate_is_bounded_full(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "And.xbd",
        _fbd(
            _gate(".1", "AND_BOOL", "A", "B", "Y"),
            [("A", "BOOL"), ("B", "EBOOL"), ("Y", "BOOL")],
            name="AndLogic",
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v4(result.engineering.project)
    assert profile["fbd_regions"] == 1
    assert profile["fbd_modeled"] == 1
    assert profile["graphical_output_theorems"] == 1
    logic = next(item for item in result.engineering.project.output_logic if item.origin.startswith("SCHNEIDER_FBD_V4:"))
    assert logic.output_tag == "Y"
    assert {term.tag: term.required for term in logic.paths[0].terms} == {"A": True, "B": True}
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED


def test_schneider_v4_fbd_link_chain_and_inverted_pin(tmp_path: Path) -> None:
    first = _gate(".1", "AND", "A", "B")
    second = '''
        <FFBBlock instanceName=".2" typeName="OR" additionnalPinNumber="0" enEnO="false" width="8" height="6">
          <descriptionFFB>
            <inputVariable invertedPin="false" formalParameter="EN" />
            <inputVariable invertedPin="false" formalParameter="IN1" />
            <inputVariable invertedPin="true" formalParameter="IN2" effectiveParameter="C" />
            <outputVariable invertedPin="false" formalParameter="ENO" />
            <outputVariable invertedPin="false" formalParameter="OUT" effectiveParameter="Y" />
          </descriptionFFB>
        </FFBBlock>
        <linkFB>
          <linkSource parentObjectName=".1" pinName="OUT"><objPosition posX="1" posY="1" /></linkSource>
          <linkDestination parentObjectName=".2" pinName="IN1"><objPosition posX="2" posY="1" /></linkDestination>
        </linkFB>
'''
    source = _write(
        tmp_path / "Chain.xbd",
        _fbd(first + second, [("A", "BOOL"), ("B", "BOOL"), ("C", "BOOL"), ("Y", "EBOOL")], name="Chain"),
    )
    result = run_production_verification_v5(source)
    logic = next(item for item in result.engineering.project.output_logic if item.output_tag == "Y")
    normalized = {
        tuple((term.tag, term.required) for term in path.terms)
        for path in logic.paths
    }
    assert normalized == {
        (('A', True), ('B', True)),
        (('C', False),),
    }


def test_schneider_v4_stateful_fbd_upstream_is_not_projected(tmp_path: Path) -> None:
    timer = '''
        <FFBBlock instanceName="T1" typeName="TON" additionnalPinNumber="0" enEnO="false" width="7" height="6">
          <descriptionFFB>
            <inputVariable invertedPin="false" formalParameter="EN" />
            <inputVariable invertedPin="false" formalParameter="IN" effectiveParameter="A" />
            <inputVariable invertedPin="false" formalParameter="PT" effectiveParameter="t#1s" />
            <outputVariable invertedPin="false" formalParameter="ENO" />
            <outputVariable invertedPin="false" formalParameter="Q" />
            <outputVariable invertedPin="false" formalParameter="ET" />
          </descriptionFFB>
        </FFBBlock>
'''
    gate = '''
        <FFBBlock instanceName=".1" typeName="AND_BOOL" additionnalPinNumber="0" enEnO="false" width="8" height="6">
          <descriptionFFB>
            <inputVariable invertedPin="false" formalParameter="EN" />
            <inputVariable invertedPin="false" formalParameter="IN1" />
            <inputVariable invertedPin="false" formalParameter="IN2" effectiveParameter="B" />
            <outputVariable invertedPin="false" formalParameter="ENO" />
            <outputVariable invertedPin="false" formalParameter="OUT" effectiveParameter="Y" />
          </descriptionFFB>
        </FFBBlock>
        <linkFB>
          <linkSource parentObjectName="T1" pinName="Q"><objPosition posX="1" posY="1" /></linkSource>
          <linkDestination parentObjectName=".1" pinName="IN1"><objPosition posX="2" posY="1" /></linkDestination>
        </linkFB>
'''
    source = _write(
        tmp_path / "Timer.xbd",
        _fbd(timer + gate, [("A", "BOOL"), ("B", "BOOL"), ("Y", "BOOL"), ("T1", "TON")], name="TimerLogic"),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v4(result.engineering.project)
    assert profile["graphical_output_theorems"] == 0
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(test.scenario == "SCHNEIDER_GRAPHICAL_RUNTIME" for test in result.engineering.fat_tests)


def test_schneider_v4_requirement_and_writer_conflict_boundary(tmp_path: Path) -> None:
    root = tmp_path / "export"
    root.mkdir()
    _write(
        root / "Logic.xbd",
        _fbd(_gate(".1", "AND_BOOL", "A", "B", "Y"), [("A", "BOOL"), ("B", "BOOL"), ("Y", "BOOL")], name="FBDLogic"),
    )
    _write(
        root / "Override.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV4" version="1.0" />
  <program><identProgram name="Override" type="section" task="MAST" /><STSource>Y := Override;</STSource></program>
  <dataBlock><variables name="Override" typeName="BOOL" /><variables name="Y" typeName="BOOL" /></dataBlock>
</STExchangeFile>''',
    )
    requirement = _write(tmp_path / "req.md", "REQ-V4-001: When A=TRUE and B=TRUE, Y=TRUE.")
    result = run_production_verification_v5(root, requirement_paths=[requirement])
    profile = schneider_capability_profile_v4(result.engineering.project)
    assert profile["graphical_writer_conflicts"] == ["Y"]
    assert profile["graphical_output_theorems"] == 0
    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert any(risk.category == "MULTIPLE_WRITERS" for risk in result.risks)


def test_schneider_v4_report_exposes_graphical_boundary(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "And.xbd",
        _fbd(_gate(".1", "AND_BOOL", "A", "B", "Y"), [("A", "BOOL"), ("B", "BOOL"), ("Y", "BOOL")]),
    )
    result = run_production_verification_v5(source)
    report = render_production_report(result)
    assert "### Schneider V4 LD/FBD Boolean Theorem" in report
    assert "whole Control Expert cell geometry" in report
    assert "stateless AND/AND_BOOL/OR/OR_BOOL" in report
    assert "Control Expert Simulator" in report
