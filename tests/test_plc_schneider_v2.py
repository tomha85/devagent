from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_st_control_flow_v2 import schneider_capability_profile_v2


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _xst(st_source: str, *variables: str, project: str = "SchneiderV2", section: str = "Main") -> str:
    tags = "\n".join(
        f'    <variables name="{name}" typeName="BOOL" />'
        for name in variables
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="{project}" version="1.0" />
  <program>
    <identProgram name="{section}" type="section" task="MAST" />
    <STSource>
{st_source.strip()}
    </STSource>
  </program>
  <dataBlock>
{tags}
  </dataBlock>
</STExchangeFile>
'''


def _complete_if_else(path: Path) -> Path:
    return _write(
        path,
        _xst(
            '''
IF Start AND Guard THEN
    Run := TRUE;
ELSE
    Run := FALSE;
END_IF;
''',
            "Start",
            "Guard",
            "Run",
        ),
    )


def test_schneider_v2_complete_if_else_gets_static_proof_fat_and_report(tmp_path: Path) -> None:
    result = run_production_verification_v5(_complete_if_else(tmp_path / "Main.xst"))
    project = result.engineering.project
    profile = schneider_capability_profile_v2(project)

    assert project.metadata.vendor == "Schneider Electric"
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["schema"] == "devagent-schneider-control-expert-capability-v2"
    assert profile["static_contract"] == "COMPLETE"
    assert profile["if_chain_models"] == 1
    assert profile["if_chain_output_logic"] == 1
    assert project.st_statement_total == 4
    assert project.st_statement_semantic_count == 4
    assert all(item.semantic_state is PLCSemanticState.FULL for item in project.logic_statements)

    logic = next(item for item in project.output_logic if item.output_tag == "Run")
    assert logic.instruction == "ASSIGN_BOOL"
    assert logic.origin.startswith("SCHNEIDER_ST_IF_CHAIN:")
    assert len(logic.paths) == 1
    assert {(term.tag, term.required) for term in logic.paths[0].terms} == {
        ("Start", True),
        ("Guard", True),
    }

    assert any(
        edge.kind == "DEPENDS_ON" and edge.source == "Run" and edge.target == "Start"
        for edge in result.engineering.graph.edges
    )
    assert any(
        edge.kind == "DEPENDS_ON" and edge.source == "Run" and edge.target == "Guard"
        for edge in result.engineering.graph.edges
    )
    assert not any(
        risk.category == "MULTIPLE_WRITERS" and "Run" in risk.title
        for risk in result.risks
    )

    fat = [item for item in result.engineering.fat_tests if item.output_tag == "Run"]
    assert len(fat) >= 2
    assert any(item.scenario == "POSITIVE_PATH" for item in fat)
    assert any(item.scenario == "NEGATIVE_PATH" for item in fat)
    assert all(item.execution_status == "NOT_RUN" for item in fat)
    assert all(item.engineer_execution_required is True for item in fat)
    assert all(item.setup_steps and item.action_steps and item.evidence_required for item in fat)

    report = render_production_report(result)
    assert "### Schneider V2 Bounded Control-Flow Theorem" in report
    assert "Modeled complete IF/ELSIF/ELSE chains: **1**" in report
    assert "does not execute Control Expert Simulator, HIL, or a real Modicon PLC" in report


def test_schneider_v2_requirement_is_proven_or_conflicted_deterministically(tmp_path: Path) -> None:
    source = _complete_if_else(tmp_path / "Main.xst")
    proven_req = _write(
        tmp_path / "requirements-proven.md",
        "REQ-SCH-V2-001: When Start=TRUE and Guard=TRUE, Run=TRUE.\n",
    )
    conflict_req = _write(
        tmp_path / "requirements-conflict.md",
        "REQ-SCH-V2-002: When Start=TRUE and Guard=FALSE, Run=TRUE.\n",
    )

    proven = run_production_verification_v5(source, requirement_paths=[proven_req])
    assert proven.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
    assert "Schneider V2 complete IF/ELSIF/ELSE final-value theorem" in proven.requirement_verification[0].summary
    assert proven.requirement_verification[0].linked_test_ids
    assert proven.executions == []
    assert all(test.execution_status == "NOT_RUN" for test in proven.engineering.fat_tests)

    conflict = run_production_verification_v5(source, requirement_paths=[conflict_req])
    assert conflict.requirement_verification[0].status is RequirementStatus.CONFLICT
    assert "Schneider V2 complete IF/ELSIF/ELSE final-value theorem" in conflict.requirement_verification[0].summary
    assert any(
        risk.category == "REQUIREMENT" and risk.severity.value == "CRITICAL"
        for risk in conflict.risks
    )


def test_schneider_v2_elsif_requires_prior_guard_false(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Modes.xst",
        _xst(
            '''
IF ModeA THEN
    Run := TRUE;
ELSIF ModeB THEN
    Run := TRUE;
ELSE
    Run := FALSE;
END_IF;
''',
            "ModeA",
            "ModeB",
            "Run",
        ),
    )
    result = run_production_verification_v5(source)
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED

    logic = next(item for item in result.engineering.project.output_logic if item.output_tag == "Run")
    paths = [
        {term.tag: term.required for term in path.terms}
        for path in logic.paths
    ]
    assert {"ModeA": True} in paths
    assert {"ModeA": False, "ModeB": True} in paths
    assert {"ModeB": True} not in paths


def test_schneider_v2_missing_output_assignment_remains_fail_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Incomplete.xst",
        _xst(
            '''
IF Start THEN
    Run := TRUE;
ELSE
    Aux := FALSE;
END_IF;
''',
            "Start",
            "Run",
            "Aux",
        ),
    )
    result = run_production_verification_v5(source)

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert not any(
        item.origin.startswith("SCHNEIDER_ST_IF_CHAIN:")
        for item in result.engineering.project.output_logic
    )
    runtime = [
        item for item in result.engineering.fat_tests
        if item.scenario == "ST_RUNTIME"
    ]
    assert {item.output_tag for item in runtime} == {"Run", "Aux"}
    assert all(item.execution_status == "NOT_RUN" for item in runtime)
    assert any(risk.category == "SEMANTIC_COVERAGE" for risk in result.risks)


def test_schneider_v2_nested_if_never_gets_inner_independent_proof(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Nested.xst",
        _xst(
            '''
IF Start THEN
    IF Guard THEN
        Run := TRUE;
    ELSE
        Run := FALSE;
    END_IF;
ELSE
    Run := FALSE;
END_IF;
''',
            "Start",
            "Guard",
            "Run",
        ),
    )
    result = run_production_verification_v5(source)

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert not any(
        item.origin.startswith("SCHNEIDER_ST_IF_CHAIN:")
        for item in result.engineering.project.output_logic
    )
    assert any(
        item.scenario == "ST_RUNTIME" and item.output_tag == "Run"
        for item in result.engineering.fat_tests
    )


def test_schneider_v2_if_inside_case_is_not_mined_out_of_opaque_context(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "CaseNested.xst",
        _xst(
            '''
CASE State OF
1:
    IF Start THEN
        Run := TRUE;
    ELSE
        Run := FALSE;
    END_IF;
END_CASE;
''',
            "State",
            "Start",
            "Run",
        ),
    )
    result = run_production_verification_v5(source)

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert not any(
        item.origin.startswith("SCHNEIDER_ST_IF_CHAIN:")
        for item in result.engineering.project.output_logic
    )


def test_schneider_v2_self_referential_output_remains_fail_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "SelfRef.xst",
        _xst(
            '''
IF NOT Run THEN
    Run := TRUE;
ELSE
    Run := FALSE;
END_IF;
''',
            "Run",
        ),
    )
    result = run_production_verification_v5(source)

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert not any(
        item.origin.startswith("SCHNEIDER_ST_IF_CHAIN:")
        for item in result.engineering.project.output_logic
    )


def test_schneider_v2_outside_writer_preserves_multi_writer_risk_and_withholds_proof(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "OutsideWriter.xst",
        _xst(
            '''
IF Start THEN
    Run := Guard;
ELSE
    Run := FALSE;
END_IF;
Run := Override;
''',
            "Start",
            "Guard",
            "Override",
            "Run",
        ),
    )
    requirement = _write(
        tmp_path / "requirements.md",
        "REQ-SCH-V2-003: When Start=TRUE and Guard=TRUE, Run=TRUE.\n",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )

    assert any(
        risk.category == "MULTIPLE_WRITERS" and "Run" in risk.title
        for risk in result.risks
    )
    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert result.executions == []
