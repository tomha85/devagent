from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_call_graph_v3 import schneider_capability_profile_v3


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _main_xst(body: str, variables: list[tuple[str, str]], *, project: str = "SchneiderV3") -> str:
    tags = "\n".join(
        f'    <variables name="{name}" typeName="{dtype}" />'
        for name, dtype in variables
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="{project}" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
{body.strip()}
    </STSource>
  </program>
  <dataBlock>
{tags}
  </dataBlock>
</STExchangeFile>
'''


def _dfb_xdb(
    name: str,
    body: str,
    *,
    inputs: tuple[tuple[str, str], ...] = (("Start", "BOOL"), ("Guard", "BOOL")),
    outputs: tuple[tuple[str, str], ...] = (("Run", "BOOL"),),
    locals_: tuple[tuple[str, str], ...] = (),
) -> str:
    input_xml = "\n".join(
        f'      <variables name="{param}" typeName="{dtype}" />'
        for param, dtype in inputs
    )
    output_xml = "\n".join(
        f'      <variables name="{param}" typeName="{dtype}" />'
        for param, dtype in outputs
    )
    local_xml = "\n".join(
        f'      <variables name="{param}" typeName="{dtype}" />'
        for param, dtype in locals_
    )
    local_block = f"<publicLocalVariables>\n{local_xml}\n    </publicLocalVariables>" if locals_ else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<FBExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" content="Function Block source file" />
  <contentHeader name="SchneiderV3" version="1.0" />
  <FBSource nameOfFBType="{name}" version="1.0">
    <inputParameters>
{input_xml}
    </inputParameters>
    <outputParameters>
{output_xml}
    </outputParameters>
    {local_block}
    <FBProgram name="{name}">
      <STSource>
{body.strip()}
      </STSource>
    </FBProgram>
  </FBSource>
</FBExchangeFile>
'''


def _bounded_project(root: Path) -> Path:
    root.mkdir()
    _write(
        root / "Main.xst",
        _main_xst(
            "Motor1(Start := Start, Guard := Guard, Run => Run);",
            [
                ("Start", "BOOL"),
                ("Guard", "BOOL"),
                ("Run", "BOOL"),
                ("Motor1", "MOTOR_DFB"),
            ],
        ),
    )
    _write(
        root / "Motor.xdb",
        _dfb_xdb("MOTOR_DFB", "Run := Start AND Guard;"),
    )
    return root


def test_schneider_v3_resolves_dfb_interface_and_projects_boolean_theorem(tmp_path: Path) -> None:
    source = _bounded_project(tmp_path / "export")
    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = schneider_capability_profile_v3(project)

    assert project.metadata.vendor == "Schneider Electric"
    assert profile["schema"] == "devagent-schneider-control-expert-capability-v3"
    assert profile["dfb_types"] == 1
    assert profile["dfb_instances"] >= 1
    assert profile["dfb_calls"] == 1
    assert profile["dfb_calls_bound"] == 1
    assert profile["reachable_dfb_types"] == ["MOTOR_DFB"]
    assert profile["unreachable_dfb_types"] == []
    assert profile["dfb_local_boolean_theorems"] == 1
    assert profile["projected_call_theorems"] == 1
    assert profile["execution_closure"] == "COMPLETE"

    facts = getattr(project, "_schneider_v3_facts")
    call = facts.calls[0]
    assert call.callee_type == "MOTOR_DFB"
    assert call.instance_name == "Motor1"
    assert call.semantic_state is PLCSemanticState.FULL
    assert {(item.formal, item.actual, item.direction, item.operator) for item in call.bindings} == {
        ("Start", "Start", "INPUT", ":="),
        ("Guard", "Guard", "INPUT", ":="),
        ("Run", "Run", "OUTPUT", "=>"),
    }

    projected = next(item for item in project.output_logic if item.id.startswith("SCHNEIDER-CALL3-"))
    assert projected.output_tag == "Run"
    assert projected.origin.startswith("SCHNEIDER_ST_CALL_V3:")
    assert {term.tag: term.required for term in projected.paths[0].terms} == {
        "Start": True,
        "Guard": True,
    }

    statement = next(item for item in project.logic_statements if item.calls == ("MOTOR_DFB",))
    assert statement.semantic_state is PLCSemanticState.PARTIAL
    assert statement.reads == ("Start", "Guard")
    assert statement.writes == ("Run",)

    assert any(edge.kind == "CALLS_DFB" and edge.target == "DFB:MOTOR_DFB" for edge in result.engineering.graph.edges)
    assert any(edge.kind == "DEPENDS_ON" and edge.source == "Run" and edge.target == "Start" for edge in result.engineering.graph.edges)
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED

    report = render_production_report(result)
    assert "### Schneider V3 DFB Call / Interface Closure" in report
    assert "Deterministically bound DFB calls: **1/1**" in report
    assert "bound DFB call is still not arbitrary runtime proof" in report


def test_schneider_v3_requirement_proves_and_conflicts_only_through_projected_call(tmp_path: Path) -> None:
    source = _bounded_project(tmp_path / "export")
    proven_req = _write(
        tmp_path / "requirements-proven.md",
        "REQ-SCH-V3-001: When Start=TRUE and Guard=TRUE, Run=TRUE.",
    )
    conflict_req = _write(
        tmp_path / "requirements-conflict.md",
        "REQ-SCH-V3-002: When Start=TRUE and Guard=FALSE, Run=TRUE.",
    )

    proven = run_production_verification_v5(source, requirement_paths=[proven_req])
    assert proven.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
    assert "Schneider V3" in proven.requirement_verification[0].summary
    assert any(item.startswith("SCHNEIDER-CALL3-") for item in proven.requirement_verification[0].evidence_ids)
    assert any(item.startswith("SCHNEIDER-CALL:") for item in proven.requirement_verification[0].evidence_ids)
    assert proven.requirement_verification[0].linked_test_ids

    conflict = run_production_verification_v5(source, requirement_paths=[conflict_req])
    assert conflict.requirement_verification[0].status is RequirementStatus.CONFLICT
    assert "Schneider V3" in conflict.requirement_verification[0].summary
    assert any(risk.category == "REQUIREMENT" and risk.severity.value == "CRITICAL" for risk in conflict.risks)


def test_schneider_v3_unresolved_instance_fails_closed_and_gets_call_fat(tmp_path: Path) -> None:
    root = tmp_path / "unresolved"
    root.mkdir()
    _write(
        root / "Main.xst",
        _main_xst(
            "Unknown1(Start := Start, Guard := Guard, Run => Run);",
            [("Start", "BOOL"), ("Guard", "BOOL"), ("Run", "BOOL"), ("Unknown1", "UNKNOWN_DFB")],
        ),
    )
    _write(root / "Motor.xdb", _dfb_xdb("MOTOR_DFB", "Run := Start AND Guard;"))

    result = run_production_verification_v5(root)
    profile = schneider_capability_profile_v3(result.engineering.project)
    assert profile["dfb_calls"] == 1
    assert profile["dfb_calls_bound"] == 0
    assert profile["execution_closure"] == "PARTIAL_FAIL_CLOSED"
    assert profile["projected_call_theorems"] == 0
    assert any(test.scenario == "SCHNEIDER_DFB_CALL_RUNTIME" for test in result.engineering.fat_tests)
    assert any(risk.category == "CALL_BINDING" for risk in result.risks)


def test_schneider_v3_call_inside_control_flow_never_projects(tmp_path: Path) -> None:
    root = tmp_path / "guarded"
    root.mkdir()
    _write(
        root / "Main.xst",
        _main_xst(
            '''
IF Enable THEN
    Motor1(Start := Start, Guard := Guard, Run => Run);
ELSE
    Run := FALSE;
END_IF;
''',
            [
                ("Enable", "BOOL"),
                ("Start", "BOOL"),
                ("Guard", "BOOL"),
                ("Run", "BOOL"),
                ("Motor1", "MOTOR_DFB"),
            ],
        ),
    )
    _write(root / "Motor.xdb", _dfb_xdb("MOTOR_DFB", "Run := Start AND Guard;"))

    result = run_production_verification_v5(root)
    facts = getattr(result.engineering.project, "_schneider_v3_facts")
    assert facts.calls[0].semantic_state is PLCSemanticState.PARTIAL
    assert facts.calls[0].resolution == "call_inside_unmodeled_control"
    assert not facts.projected_logic_ids
    assert any(test.scenario == "SCHNEIDER_DFB_CALL_RUNTIME" for test in result.engineering.fat_tests)


def test_schneider_v3_competing_direct_writer_blocks_cross_boundary_requirement_proof(tmp_path: Path) -> None:
    root = tmp_path / "writers"
    root.mkdir()
    _write(
        root / "Main.xst",
        _main_xst(
            '''
Motor1(Start := Start, Guard := Guard, Run => Run);
Run := Override;
''',
            [
                ("Start", "BOOL"),
                ("Guard", "BOOL"),
                ("Override", "BOOL"),
                ("Run", "BOOL"),
                ("Motor1", "MOTOR_DFB"),
            ],
        ),
    )
    _write(root / "Motor.xdb", _dfb_xdb("MOTOR_DFB", "Run := Start AND Guard;"))
    requirement = _write(
        tmp_path / "requirements-writer.md",
        "REQ-SCH-V3-003: When Start=TRUE and Guard=TRUE, Run=TRUE.",
    )

    result = run_production_verification_v5(root, requirement_paths=[requirement])
    profile = schneider_capability_profile_v3(result.engineering.project)
    assert profile["projected_call_theorems"] == 0
    assert profile["cross_boundary_writer_conflicts"] == ["run"]
    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert any(risk.category == "MULTIPLE_WRITERS" for risk in result.risks)


def test_schneider_v3_detects_nested_dfb_reachability_and_recursion_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "recursive"
    root.mkdir()
    _write(
        root / "Main.xst",
        _main_xst(
            "A0(Start := Start, Guard := Guard, Run => Run);",
            [("Start", "BOOL"), ("Guard", "BOOL"), ("Run", "BOOL"), ("A0", "DFB_A")],
        ),
    )
    _write(
        root / "A.xdb",
        _dfb_xdb(
            "DFB_A",
            "B0(Start := Start, Guard := Guard, Run => Run);",
            locals_=(("B0", "DFB_B"),),
        ),
    )
    _write(
        root / "B.xdb",
        _dfb_xdb(
            "DFB_B",
            "A1(Start := Start, Guard := Guard, Run => Run);",
            locals_=(("A1", "DFB_A"),),
        ),
    )

    result = run_production_verification_v5(root)
    profile = schneider_capability_profile_v3(result.engineering.project)
    assert set(profile["reachable_dfb_types"]) == {"DFB_A", "DFB_B"}
    assert set(profile["recursive_dfb_types"]) == {"DFB_A", "DFB_B"}
    assert profile["execution_closure"] == "PARTIAL_FAIL_CLOSED"
    assert any(risk.category == "CALL_RECURSION" for risk in result.risks)


def test_schneider_v3_unreachable_dfb_cannot_prove_active_behavior(tmp_path: Path) -> None:
    root = tmp_path / "unreachable"
    root.mkdir()
    _write(
        root / "Main.xst",
        _main_xst("Run := Start;", [("Start", "BOOL"), ("Run", "BOOL")]),
    )
    _write(root / "Unused.xdb", _dfb_xdb("UNUSED_DFB", "Run := Start AND Guard;"))

    result = run_production_verification_v5(root)
    profile = schneider_capability_profile_v3(result.engineering.project)
    assert profile["reachable_dfb_types"] == []
    assert profile["unreachable_dfb_types"] == ["UNUSED_DFB"]
    assert profile["projected_call_theorems"] == 0
    assert any(risk.category == "UNREACHABLE_LOGIC" for risk in result.risks)
