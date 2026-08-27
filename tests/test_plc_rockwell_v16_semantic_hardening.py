from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.production_report import render_production_report
from devagent.plc.semantic_coverage import build_semantic_coverage_manifest


def _project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V16Coverage" TargetType="Controller">
  <Controller Name="V16Coverage" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="A" TagType="Base" DataType="BOOL" />
      <Tag Name="B" TagType="Base" DataType="BOOL" />
      <Tag Name="Y" TagType="Base" DataType="BOOL" />
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Select" TagType="Base" DataType="BOOL" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest2" TagType="Base" DataType="DINT" />
      <Tag Name="Record" TagType="Base" DataType="DINT" />
      <Tag Name="Faulted" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="Main"><Routines>
        <Routine Name="Main" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[[XIC(A),XIC(B)]OTE(Y);]]></Text></Rung>
          <Rung Number="1"><Text><![CDATA[XIC(Enable)[XIC(Select)MOVE(1,Dest),XIO(Select)MOVE(0,Dest)];]]></Text></Rung>
          <Rung Number="2"><Text><![CDATA[GSV(Program,THIS,MajorFaultRecord,Record)[MOVE(Source,Dest2),SSV(Program,THIS,MajorFaultRecord,Record),OTE(Faulted)];]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V16Coverage.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_v16_branch_coverage_unifies_boolean_and_action_theorems(tmp_path: Path) -> None:
    result = run_production_verification_v5(_project(tmp_path))
    project = result.engineering.project
    manifest = build_semantic_coverage_manifest(project)
    rll = manifest["language_summary"]["rll"]
    branch = manifest["branch_semantics"]

    assert project.branch_rung_total == 3
    assert project.branch_rung_semantic_count == 2
    assert rll["branch_rungs"] == 3
    assert rll["branch_rungs_modeled"] == 2
    assert rll["boolean_branch_rungs_modeled"] == 1
    assert rll["action_branch_rungs_modeled"] == 1
    assert rll["branch_rungs_withheld"] == 1
    assert rll["branch_coverage_pct"] == 66.7
    assert branch["withheld_branch_rungs"] == 1

    # The mixed GSV/SSV branch remains fail-closed; V16 must not turn improved
    # accounting into a false full-project verification claim.
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    branch_check = next(item for item in result.engineering.static_checks if item.id == "BRANCH_DEPENDENCY_SEMANTICS")
    assert branch_check.status.value == "WARN"
    assert "Modeled 2/3" in branch_check.summary


def test_v16_report_separates_recognition_from_behavior_proof(tmp_path: Path) -> None:
    result = run_production_verification_v5(_project(tmp_path))
    report = render_production_report(result)

    assert "Directional instruction recognition coverage:" in report
    assert "Instruction semantic coverage:" not in report
    assert "Bounded branch semantic coverage:" in report
    assert "Neutral-text branch rungs with bounded semantics: **2**/3 (66.7%)" in report
    assert "Completely unknown instruction names:" in report
    assert "Known-name instruction occurrences whose behavior is withheld" in report


def test_v16_project_only_report_is_explicit_without_changing_release_policy(tmp_path: Path) -> None:
    result = run_production_verification_v5(_project(tmp_path))
    report = render_production_report(result)

    assert result.requirements == []
    assert "**Review mode:** **PROJECT_ONLY_ENGINEERING_REVIEW**" in report
    assert "**Customer requirements:** **NOT SUPPLIED**" in report
    assert "**Requirement compliance:** **NOT EVALUATED" in report
    assert "**Project engineering review:** **COMPLETED within the declared semantic/proof boundary.**" in report
    assert "NOT EVALUATED — requirements not supplied" in report
    assert "no customer-specification compliance claim" in report

    # Strict release policy remains independent from project-only engineering
    # review and is intentionally not weakened by presentation changes.
    assert result.readiness is not None
    assert result.readiness.status.value == "NOT_READY"
    assert "## Release Readiness" in report
