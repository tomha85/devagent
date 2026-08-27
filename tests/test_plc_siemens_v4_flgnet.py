from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.siemens_flgnet_v4 import siemens_capability_profile_v4


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _access(uid: int, name: str, *, scope: str = "LocalVariable") -> str:
    return f'''
<Access Scope="{scope}" UId="{uid}">
  <Symbol><Component Name="{name}" /></Symbol>
</Access>'''


def _block_xml(
    path: Path,
    *,
    language: str,
    flgnet: str,
    kind: str = "OB",
    name: str = "Main",
    inputs: tuple[tuple[str, str], ...] = (),
    outputs: tuple[tuple[str, str], ...] = (),
    temps: tuple[tuple[str, str], ...] = (),
) -> Path:
    sections = []
    for section, members in (("Input", inputs), ("Output", outputs), ("Temp", temps)):
        if not members:
            continue
        body = "".join(
            f'<Member Name="{member}" Datatype="{dtype}" />'
            for member, dtype in members
        )
        sections.append(f'<Section Name="{section}">{body}</Section>')
    interface = "".join(sections)
    return _write(
        path,
        f'''
<Document>
  <SW.Blocks.{kind} ID="1">
    <AttributeList>
      <Name>{name}</Name>
      <ProgrammingLanguage>{language}</ProgrammingLanguage>
      <Interface><Sections>{interface}</Sections></Interface>
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


def _simple_lad(
    *,
    contact: str = "Start",
    output: str = "Run",
    negated: bool = False,
    part_name: str = "Contact",
) -> str:
    negation = '<Negated Name="operand" />' if negated else ""
    return f'''
<FlgNet>
  <Parts>
    {_access(21, contact)}
    {_access(22, output)}
    <Part Name="{part_name}" UId="23">{negation}</Part>
    <Part Name="Coil" UId="24" />
  </Parts>
  <Wires>
    <Wire UId="30"><Powerrail /><NameCon UId="23" Name="in" /></Wire>
    <Wire UId="31"><IdentCon UId="21" /><NameCon UId="23" Name="operand" /></Wire>
    <Wire UId="32"><NameCon UId="23" Name="out" /><NameCon UId="24" Name="in" /></Wire>
    <Wire UId="33"><IdentCon UId="22" /><NameCon UId="24" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''


def _fbd_gate(gate: str) -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "A")}
    {_access(22, "B")}
    {_access(23, "Run")}
    <Part Name="{gate}" UId="30">
      <TemplateValue Name="Card" Type="Cardinality">2</TemplateValue>
    </Part>
    <Part Name="Coil" UId="31" />
  </Parts>
  <Wires>
    <Wire UId="40"><IdentCon UId="21" /><NameCon UId="30" Name="in1" /></Wire>
    <Wire UId="41"><IdentCon UId="22" /><NameCon UId="30" Name="in2" /></Wire>
    <Wire UId="42"><NameCon UId="30" Name="out" /><NameCon UId="31" Name="in" /></Wire>
    <Wire UId="43"><IdentCon UId="23" /><NameCon UId="31" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''


def _path_dicts(logic) -> list[dict[str, bool]]:
    return [
        {term.tag: term.required for term in path.terms}
        for path in logic.paths
    ]


def test_v4_simple_lad_contact_coil_is_bounded_full_and_requirement_proven(
    tmp_path: Path,
) -> None:
    xml = _block_xml(
        tmp_path / "Main.xml",
        language="LAD",
        flgnet=_simple_lad(),
        temps=(("Start", "Bool"), ("Run", "Bool")),
    )
    requirement = _write(
        tmp_path / "requirements.md",
        "REQ-LAD-1: When Start=TRUE, Run=TRUE.",
    )
    result = run_production_verification_v5(
        xml,
        requirement_paths=[requirement],
    )
    profile = siemens_capability_profile_v4(result.engineering.project)

    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["schema"] == "devagent-siemens-tia-capability-v4"
    assert profile["flgnet_networks"] == 1
    assert profile["flgnet_modeled"] == 1
    assert profile["flgnet_withheld"] == 0
    statement = result.engineering.project.logic_statements[0]
    assert statement.semantic_state is PLCSemanticState.FULL
    assert statement.reads == ("Start",)
    assert statement.writes == ("Run",)
    logic = next(
        item for item in result.engineering.project.output_logic
        if item.origin.startswith("SIEMENS_FLGNET_V4:")
    )
    assert logic.language == "LAD"
    assert _path_dicts(logic) == [{"Start": True}]
    assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
    fat = [item for item in result.engineering.fat_tests if item.output_tag == "Run"]
    assert fat and all(item.execution_status == "NOT_RUN" for item in fat)
    assert any("FlgNet" in item.title for item in fat)


def test_v4_lad_negated_contact_is_false_path(tmp_path: Path) -> None:
    xml = _block_xml(
        tmp_path / "Negated.xml",
        language="LAD",
        flgnet=_simple_lad(negated=True),
        temps=(("Start", "Bool"), ("Run", "Bool")),
    )
    engineering = analyze_plc_project(xml)
    logic = next(
        item for item in engineering.project.output_logic
        if item.origin.startswith("SIEMENS_FLGNET_V4:")
    )
    assert _path_dicts(logic) == [{"Start": False}]


def test_v4_lad_series_and_parallel_or_topologies(tmp_path: Path) -> None:
    series = f'''
<FlgNet>
  <Parts>
    {_access(21, "A")}{_access(22, "B")}{_access(23, "Run")}
    <Part Name="Contact" UId="31" />
    <Part Name="Contact" UId="32" />
    <Part Name="Coil" UId="33" />
  </Parts>
  <Wires>
    <Wire><Powerrail /><NameCon UId="31" Name="in" /></Wire>
    <Wire><IdentCon UId="21" /><NameCon UId="31" Name="operand" /></Wire>
    <Wire><NameCon UId="31" Name="out" /><NameCon UId="32" Name="in" /></Wire>
    <Wire><IdentCon UId="22" /><NameCon UId="32" Name="operand" /></Wire>
    <Wire><NameCon UId="32" Name="out" /><NameCon UId="33" Name="in" /></Wire>
    <Wire><IdentCon UId="23" /><NameCon UId="33" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''
    parallel = f'''
<FlgNet>
  <Parts>
    {_access(21, "A")}{_access(22, "B")}{_access(23, "Run")}
    <Part Name="Contact" UId="31" />
    <Part Name="Contact" UId="32" />
    <Part Name="Coil" UId="33" />
  </Parts>
  <Wires>
    <Wire><Powerrail /><NameCon UId="31" Name="in" /><NameCon UId="32" Name="in" /></Wire>
    <Wire><IdentCon UId="21" /><NameCon UId="31" Name="operand" /></Wire>
    <Wire><IdentCon UId="22" /><NameCon UId="32" Name="operand" /></Wire>
    <Wire><NameCon UId="31" Name="out" /><NameCon UId="32" Name="out" /><NameCon UId="33" Name="in" /></Wire>
    <Wire><IdentCon UId="23" /><NameCon UId="33" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''
    series_path = _block_xml(
        tmp_path / "Series.xml",
        language="LAD",
        flgnet=series,
        temps=(("A", "Bool"), ("B", "Bool"), ("Run", "Bool")),
    )
    parallel_path = _block_xml(
        tmp_path / "Parallel.xml",
        language="LAD",
        flgnet=parallel,
        temps=(("A", "Bool"), ("B", "Bool"), ("Run", "Bool")),
    )
    series_logic = next(
        item for item in analyze_plc_project(series_path).project.output_logic
        if item.origin.startswith("SIEMENS_FLGNET_V4:")
    )
    parallel_logic = next(
        item for item in analyze_plc_project(parallel_path).project.output_logic
        if item.origin.startswith("SIEMENS_FLGNET_V4:")
    )
    assert _path_dicts(series_logic) == [{"A": True, "B": True}]
    assert {frozenset(item.items()) for item in _path_dicts(parallel_logic)} == {
        frozenset({("A", True)}),
        frozenset({("B", True)}),
    }


def test_v4_fbd_and_or_gates_are_bounded_boolean_theorems(tmp_path: Path) -> None:
    and_path = _block_xml(
        tmp_path / "And.xml",
        language="FBD",
        flgnet=_fbd_gate("A"),
        temps=(("A", "Bool"), ("B", "Bool"), ("Run", "Bool")),
    )
    or_path = _block_xml(
        tmp_path / "Or.xml",
        language="FBD",
        flgnet=_fbd_gate("O"),
        temps=(("A", "Bool"), ("B", "Bool"), ("Run", "Bool")),
    )
    and_logic = next(
        item for item in analyze_plc_project(and_path).project.output_logic
        if item.origin.startswith("SIEMENS_FLGNET_V4:")
    )
    or_logic = next(
        item for item in analyze_plc_project(or_path).project.output_logic
        if item.origin.startswith("SIEMENS_FLGNET_V4:")
    )
    assert and_logic.language == "FBD"
    assert _path_dicts(and_logic) == [{"A": True, "B": True}]
    assert {frozenset(item.items()) for item in _path_dicts(or_logic)} == {
        frozenset({("A", True)}),
        frozenset({("B", True)}),
    }


def test_v4_stateful_timer_fails_closed_to_runtime_fat(tmp_path: Path) -> None:
    timer_path = _block_xml(
        tmp_path / "Timer.xml",
        language="LAD",
        flgnet=_simple_lad(part_name="TON"),
        temps=(("Start", "Bool"), ("Run", "Bool")),
    )
    result = run_production_verification_v5(timer_path)
    profile = siemens_capability_profile_v4(result.engineering.project)
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert profile["flgnet_withheld"] == 1
    assert any(
        reason.startswith("unsupported_part:")
        for reason in profile["flgnet_withheld_reasons"]
    )
    assert result.engineering.project.logic_statements[0].semantic_state is PLCSemanticState.OPAQUE
    runtime = [
        item for item in result.engineering.fat_tests
        if item.scenario == "SIEMENS_FLGNET_RUNTIME"
    ]
    assert len(runtime) == 1
    assert runtime[0].method == "RUNTIME_FAT_REQUIRED"
    assert runtime[0].execution_status == "NOT_RUN"


def test_v4_non_boolean_and_self_referential_lad_fail_closed(tmp_path: Path) -> None:
    non_bool = _block_xml(
        tmp_path / "NonBool.xml",
        language="LAD",
        flgnet=_simple_lad(),
        temps=(("Start", "Int"), ("Run", "Bool")),
    )
    self_ref = _block_xml(
        tmp_path / "Self.xml",
        language="LAD",
        flgnet=_simple_lad(contact="Run", output="Run"),
        temps=(("Run", "Bool"),),
    )
    non_bool_result = analyze_plc_project(non_bool)
    self_result = analyze_plc_project(self_ref)
    non_bool_profile = siemens_capability_profile_v4(non_bool_result.project)
    self_profile = siemens_capability_profile_v4(self_result.project)
    assert any(
        key.startswith("non_boolean_symbol:")
        for key in non_bool_profile["flgnet_withheld_reasons"]
    )
    assert any(
        key.startswith("self_reference:")
        for key in self_profile["flgnet_withheld_reasons"]
    )
    assert non_bool_result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert self_result.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_v4_local_fc_lad_theorem_does_not_prove_active_machine_without_ob_call(
    tmp_path: Path,
) -> None:
    xml = _block_xml(
        tmp_path / "LogicFC.xml",
        language="LAD",
        flgnet=_simple_lad(),
        kind="FC",
        name="LogicFC",
        inputs=(("Start", "Bool"),),
        outputs=(("Run", "Bool"),),
    )
    requirement = _write(
        tmp_path / "requirements.md",
        "REQ-FC-LOCAL: When Start=TRUE, Run=TRUE.",
    )
    result = run_production_verification_v5(
        xml,
        requirement_paths=[requirement],
    )
    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    facts = result.engineering.project._siemens_v3_facts
    assert "LogicFC" in facts.unreachable_blocks


def test_v4_fc_lad_theorem_projects_through_v3_reachable_call(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "Main.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    LogicFC(Start := MainStart, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    _block_xml(
        tmp_path / "LogicFC.xml",
        language="LAD",
        flgnet=_simple_lad(),
        kind="FC",
        name="LogicFC",
        inputs=(("Start", "Bool"),),
        outputs=(("Run", "Bool"),),
    )
    requirement = _write(
        tmp_path / "requirements.md",
        "REQ-FC-CALL: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(
        tmp_path,
        requirement_paths=[requirement],
    )
    facts = result.engineering.project._siemens_v3_facts
    assert facts.calls and facts.calls[0].semantic_state is PLCSemanticState.FULL
    assert facts.projected_logic_ids
    projected = [
        item for item in result.engineering.project.output_logic
        if item.id in set(facts.projected_logic_ids)
        and item.output_tag == "MotorRun"
    ]
    assert projected
    assert _path_dicts(projected[0]) == [{"MainStart": True}]
    assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED


def test_v4_report_and_checks_make_flgnet_boundary_explicit(tmp_path: Path) -> None:
    xml = _block_xml(
        tmp_path / "Main.xml",
        language="LAD",
        flgnet=_simple_lad(),
        temps=(("Start", "Bool"), ("Run", "Bool")),
    )
    result = run_production_verification_v5(xml)
    assert any(
        item.id == "SIEMENS_V4_FLGNET_SEMANTICS"
        and item.status.value == "PASS"
        for item in result.engineering.static_checks
    )
    from devagent.plc.production_report import render_production_report

    report = render_production_report(result)
    assert "Siemens V4 LAD/FBD FlgNet Boolean Theorem" in report
    assert "does not execute PLCSIM, HIL, or a real PLC" in report
