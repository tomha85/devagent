from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.siemens_flgnet_extended_v4 import siemens_capability_profile_v4_extended


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _access(uid: int, name: str, scope: str = "LocalVariable") -> str:
    return (
        f'<Access Scope="{scope}" UId="{uid}">'
        f'<Symbol><Component Name="{name}" /></Symbol></Access>'
    )


def _literal(uid: int, dtype: str, value: str) -> str:
    return (
        f'<Access Scope="LiteralConstant" UId="{uid}"><Constant>'
        f'<ConstantType>{dtype}</ConstantType><ConstantValue>{value}</ConstantValue>'
        f'</Constant></Access>'
    )


def _block_xml(
    path: Path,
    *,
    flgnet: str,
    language: str = "LAD",
    kind: str = "OB",
    name: str = "Main",
    members: tuple[tuple[str, str, str], ...] = (),
) -> Path:
    sections = []
    grouped = {}
    for section, member, dtype in members:
        grouped.setdefault(section, []).append((member, dtype))
    for section, values in grouped.items():
        payload = "".join(
            f'<Member Name="{member}" Datatype="{dtype}" />'
            for member, dtype in values
        )
        sections.append(f'<Section Name="{section}">{payload}</Section>')
    return _write(
        path,
        f'''
<Document>
  <SW.Blocks.{kind} ID="1">
    <AttributeList>
      <Name>{name}</Name>
      <ProgrammingLanguage>{language}</ProgrammingLanguage>
      <Interface><Sections>{''.join(sections)}</Sections></Interface>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="2" CompositionName="CompileUnits">
        <AttributeList>
          <ProgrammingLanguage>{language}</ProgrammingLanguage>
          <NetworkSource>{flgnet}</NetworkSource>
        </AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.{kind}>
</Document>
''',
    )


def _sr_flgnet(coil: str) -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "Start")}
    {_access(22, "Latch")}
    <Part Name="Contact" UId="23" />
    <Part Name="{coil}" UId="24" />
  </Parts>
  <Wires>
    <Wire><Powerrail /><NameCon UId="23" Name="in" /></Wire>
    <Wire><IdentCon UId="21" /><NameCon UId="23" Name="operand" /></Wire>
    <Wire><NameCon UId="23" Name="out" /><NameCon UId="24" Name="in" /></Wire>
    <Wire><IdentCon UId="22" /><NameCon UId="24" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''


def test_v4_scoil_and_rcoil_are_local_actions_not_final_boolean_requirement_proof(tmp_path: Path) -> None:
    for coil, instruction in (("SCoil", "SET_BOOL"), ("RCoil", "RESET_BOOL")):
        source = _block_xml(
            tmp_path / f"{coil}.xml",
            flgnet=_sr_flgnet(coil),
            members=(
                ("Temp", "Start", "Bool"),
                ("Temp", "Latch", "Bool"),
            ),
        )
        requirement = _write(
            tmp_path / f"{coil}.md",
            "REQ-SR: When Start=TRUE, Latch=TRUE.",
        )
        result = run_production_verification_v5(
            source,
            requirement_paths=[requirement],
        )
        project = result.engineering.project
        profile = siemens_capability_profile_v4_extended(project)
        facts = project._siemens_v4_extended_facts

        assert profile["flgnet_local_actions"] == 1
        assert facts.actions[0].instruction == instruction
        assert facts.actions[0].target == "Latch"
        assert any(
            logic.instruction == instruction
            and logic.output_tag == "Latch"
            and logic.semantic_state is PLCSemanticState.FULL
            for logic in project.output_logic
        )
        assert result.requirement_verification[0].status is not RequirementStatus.STATICALLY_VERIFIED
        assert any(
            test.scenario == "SIEMENS_LOCAL_ACTION"
            and test.output_tag == "Latch"
            for test in result.engineering.fat_tests
        )
        assert any(risk.category == "STATEFUL_LOGIC" for risk in result.risks)


def _eq_move_flgnet() -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "Step")}
    {_literal(22, "Int", "10")}
    {_literal(23, "Int", "20")}
    {_access(24, "NextStep")}
    <Part Name="Eq" UId="26"><TemplateValue Name="SrcType" Type="Type">Int</TemplateValue></Part>
    <Part Name="Move" UId="28" DisabledENO="true"><TemplateValue Name="Card" Type="Cardinality">1</TemplateValue></Part>
  </Parts>
  <Wires>
    <Wire><Powerrail /><NameCon UId="26" Name="pre" /></Wire>
    <Wire><IdentCon UId="21" /><NameCon UId="26" Name="in1" /></Wire>
    <Wire><IdentCon UId="22" /><NameCon UId="26" Name="in2" /></Wire>
    <Wire><NameCon UId="26" Name="out" /><NameCon UId="28" Name="en" /></Wire>
    <Wire><IdentCon UId="23" /><NameCon UId="28" Name="in" /></Wire>
    <Wire><NameCon UId="28" Name="out1" /><IdentCon UId="24" /></Wire>
  </Wires>
</FlgNet>
'''


def test_v4_typed_eq_move_normalizes_local_data_action_and_stays_fat_bounded(tmp_path: Path) -> None:
    source = _block_xml(
        tmp_path / "eq_move.xml",
        flgnet=_eq_move_flgnet(),
        members=(
            ("Temp", "Step", "Int"),
            ("Temp", "NextStep", "Int"),
        ),
    )
    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = siemens_capability_profile_v4_extended(project)
    facts = project._siemens_v4_extended_facts

    assert profile["flgnet_move_actions"] == 1
    assert profile["flgnet_typed_comparisons"] == 1
    action = facts.actions[0]
    assert action.instruction == "MOVE"
    assert action.target == "NextStep"
    assert action.source_value == "20"
    assert action.comparison == "Step EQ 10"
    statement = next(item for item in project.logic_statements if item.id == action.statement_id)
    assert statement.semantic_state is PLCSemanticState.FULL
    assert statement.writes == ("NextStep",)
    assert "Step" in statement.reads
    assert any(
        test.scenario == "SIEMENS_LOCAL_ACTION"
        and test.output_tag == "NextStep"
        for test in result.engineering.fat_tests
    )


def _timer_flgnet(name: str) -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "Start")}
    <Part Name="{name}" UId="22" Version="1.0" />
  </Parts>
  <Wires>
    <Wire><Powerrail /><NameCon UId="22" Name="in" /></Wire>
    <Wire><IdentCon UId="21" /><NameCon UId="22" Name="IN" /></Wire>
  </Wires>
</FlgNet>
'''


def test_v4_timer_counter_family_is_normalized_only_as_runtime_contract(tmp_path: Path) -> None:
    for name in ("TON", "TOF", "TP", "CTU", "CTD"):
        source = _block_xml(
            tmp_path / f"{name}.xml",
            flgnet=_timer_flgnet(name),
            members=(("Temp", "Start", "Bool"),),
        )
        result = run_production_verification_v5(source)
        project = result.engineering.project
        profile = siemens_capability_profile_v4_extended(project)

        assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
        assert name in profile["flgnet_runtime_instructions"]
        assert all(
            item.semantic_state is not PLCSemanticState.FULL
            for item in project.logic_statements
        )
        assert any(
            test.scenario == "SIEMENS_TIMER_COUNTER_RUNTIME"
            and name in test.title
            and test.execution_status == "NOT_RUN"
            for test in result.engineering.fat_tests
        )


def _visual_fc_call() -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "MainStart")}
    {_access(22, "MotorRun")}
    <Call UId="24">
      <CallInfo Name="LogicFC" BlockType="FC">
        <Parameter Name="Start" Section="Input" Type="Bool" />
        <Parameter Name="Run" Section="Output" Type="Bool" />
      </CallInfo>
    </Call>
  </Parts>
  <Wires>
    <Wire><Powerrail /><NameCon UId="24" Name="en" /></Wire>
    <Wire><IdentCon UId="21" /><NameCon UId="24" Name="Start" /></Wire>
    <Wire><NameCon UId="24" Name="Run" /><IdentCon UId="22" /></Wire>
  </Wires>
</FlgNet>
'''


def test_v4_visual_fc_call_enters_v3_execution_closure_and_projects_callee_theorem(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(
        bundle / "LogicFC.scl",
        '''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION
''',
    )
    _block_xml(
        bundle / "Main.xml",
        flgnet=_visual_fc_call(),
        members=(
            ("Temp", "MainStart", "Bool"),
            ("Temp", "MotorRun", "Bool"),
        ),
    )
    requirement = _write(
        tmp_path / "requirements.md",
        "REQ-VISUAL-CALL: When MainStart=TRUE, MotorRun=TRUE.",
    )

    result = run_production_verification_v5(
        bundle,
        requirement_paths=[requirement],
    )
    project = result.engineering.project
    profile = siemens_capability_profile_v4_extended(project)
    facts = project._siemens_v3_facts

    assert profile["flgnet_visual_calls_bound"] == 1
    assert facts.calls
    visual = next(call for call in facts.calls if call.resolution == "bound_visual_flgnet_call")
    assert visual.callee_block == "LogicFC"
    assert visual.semantic_state is PLCSemanticState.FULL
    assert "LogicFC" in facts.reachable_blocks
    assert facts.projected_logic_ids
    assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED


def test_v4_guarded_visual_call_remains_opaque_and_cannot_enter_execution_closure(tmp_path: Path) -> None:
    bundle = tmp_path / "guarded"
    bundle.mkdir()
    _write(
        bundle / "LogicFC.scl",
        '''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION
''',
    )
    guarded = _visual_fc_call().replace(
        '<Wire><Powerrail /><NameCon UId="24" Name="en" /></Wire>',
        '<Wire><IdentCon UId="21" /><NameCon UId="24" Name="en" /></Wire>',
    )
    _block_xml(
        bundle / "Main.xml",
        flgnet=guarded,
        members=(
            ("Temp", "MainStart", "Bool"),
            ("Temp", "MotorRun", "Bool"),
        ),
    )
    engineering = analyze_plc_project(bundle)
    project = engineering.project
    profile = siemens_capability_profile_v4_extended(project)

    assert profile.get("flgnet_visual_calls_bound", 0) == 0
    assert any(item.semantic_state is PLCSemanticState.OPAQUE for item in project.logic_statements)
    assert "LogicFC" not in project._siemens_v3_facts.reachable_blocks
