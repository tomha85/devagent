from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.siemens_flgnet_extended_v4 import (
    siemens_capability_profile_v4_extended,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _access(uid: int, name: str) -> str:
    return (
        f'<Access Scope="LocalVariable" UId="{uid}">'
        f'<Symbol><Component Name="{name}" /></Symbol></Access>'
    )


def _literal(uid: int, dtype: str, value: str) -> str:
    return (
        f'<Access Scope="LiteralConstant" UId="{uid}"><Constant>'
        f'<ConstantType>{dtype}</ConstantType><ConstantValue>{value}</ConstantValue>'
        f'</Constant></Access>'
    )


def _xml(
    path: Path,
    flgnet: str,
    *,
    name: str = "Main",
    kind: str = "OB",
    language: str = "LAD",
    members: tuple[tuple[str, str], ...] = (),
) -> Path:
    payload = "".join(
        f'<Member Name="{member}" Datatype="{dtype}" />'
        for member, dtype in members
    )
    return _write(
        path,
        f'''
<Document>
  <SW.Blocks.{kind} ID="1">
    <AttributeList>
      <Name>{name}</Name>
      <ProgrammingLanguage>{language}</ProgrammingLanguage>
      <Interface><Sections><Section Name="Temp">{payload}</Section></Sections></Interface>
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


def _set_action(root: Path) -> dict[str, object]:
    flgnet = f'''
<FlgNet><Parts>
  {_access(21, "Start")}{_access(22, "Latch")}
  <Part Name="Contact" UId="23" /><Part Name="SCoil" UId="24" />
</Parts><Wires>
  <Wire><Powerrail /><NameCon UId="23" Name="in" /></Wire>
  <Wire><IdentCon UId="21" /><NameCon UId="23" Name="operand" /></Wire>
  <Wire><NameCon UId="23" Name="out" /><NameCon UId="24" Name="in" /></Wire>
  <Wire><IdentCon UId="22" /><NameCon UId="24" Name="operand" /></Wire>
</Wires></FlgNet>
'''
    source = _xml(
        root / "set.xml",
        flgnet,
        members=(("Start", "Bool"), ("Latch", "Bool")),
    )
    requirement = _write(
        root / "set-requirement.md",
        "REQ-V4-SET: When Start=TRUE, Latch=TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )
    facts = result.engineering.project._siemens_v4_extended_facts
    profile = siemens_capability_profile_v4_extended(result.engineering.project)
    if profile["flgnet_set_actions"] != 1:
        raise RuntimeError(f"SCoil local action was not normalized: {profile}")
    if facts.actions[0].instruction != "SET_BOOL":
        raise RuntimeError(f"unexpected SCoil action: {facts.actions[0]}")
    if result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED:
        raise RuntimeError("retentive SCoil local action incorrectly proved final state")
    if not any(item.scenario == "SIEMENS_LOCAL_ACTION" for item in result.engineering.fat_tests):
        raise RuntimeError("SCoil local action lost engineer FAT")
    return {
        "local_action": facts.actions[0].instruction,
        "final_requirement_status": result.requirement_verification[0].status.value,
        "fat_required": True,
    }


def _typed_move(root: Path) -> dict[str, object]:
    flgnet = f'''
<FlgNet><Parts>
  {_access(31, "Step")}{_literal(32, "Int", "10")}
  {_literal(33, "Int", "20")}{_access(34, "NextStep")}
  <Part Name="Eq" UId="35"><TemplateValue Name="SrcType" Type="Type">Int</TemplateValue></Part>
  <Part Name="Move" UId="36" DisabledENO="true"><TemplateValue Name="Card" Type="Cardinality">1</TemplateValue></Part>
</Parts><Wires>
  <Wire><Powerrail /><NameCon UId="35" Name="pre" /></Wire>
  <Wire><IdentCon UId="31" /><NameCon UId="35" Name="in1" /></Wire>
  <Wire><IdentCon UId="32" /><NameCon UId="35" Name="in2" /></Wire>
  <Wire><NameCon UId="35" Name="out" /><NameCon UId="36" Name="en" /></Wire>
  <Wire><IdentCon UId="33" /><NameCon UId="36" Name="in" /></Wire>
  <Wire><NameCon UId="36" Name="out1" /><IdentCon UId="34" /></Wire>
</Wires></FlgNet>
'''
    source = _xml(
        root / "move.xml",
        flgnet,
        members=(("Step", "Int"), ("NextStep", "Int")),
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._siemens_v4_extended_facts
    profile = siemens_capability_profile_v4_extended(result.engineering.project)
    action = facts.actions[0] if facts.actions else None
    if profile["flgnet_move_actions"] != 1 or profile["flgnet_typed_comparisons"] != 1:
        raise RuntimeError(f"typed Eq/MOVE contract not normalized: {profile}")
    if action is None or action.target != "NextStep" or action.source_value != "20":
        raise RuntimeError(f"typed MOVE action is wrong: {action}")
    if action.comparison != "Step EQ 10":
        raise RuntimeError(f"typed comparison is wrong: {action.comparison}")
    return {
        "target": action.target,
        "source": action.source_value,
        "comparison": action.comparison,
        "local_action_only": True,
    }


def _timer_runtime(root: Path) -> dict[str, object]:
    flgnet = f'''
<FlgNet><Parts>
  {_access(41, "Start")}<Part Name="TON" UId="42" Version="1.0" />
</Parts><Wires>
  <Wire><Powerrail /><NameCon UId="42" Name="in" /></Wire>
  <Wire><IdentCon UId="41" /><NameCon UId="42" Name="IN" /></Wire>
</Wires></FlgNet>
'''
    source = _xml(
        root / "timer.xml",
        flgnet,
        members=(("Start", "Bool"),),
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v4_extended(result.engineering.project)
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("TON runtime contract must remain PARTIALLY_VERIFIED")
    if "TON" not in profile["flgnet_runtime_instructions"]:
        raise RuntimeError(f"TON runtime instruction not classified: {profile}")
    if any(item.semantic_state is PLCSemanticState.FULL for item in result.engineering.project.logic_statements):
        raise RuntimeError("TON network incorrectly received static executable PASS")
    runtime = [item for item in result.engineering.fat_tests if item.scenario == "SIEMENS_TIMER_COUNTER_RUNTIME"]
    if not runtime or any(item.execution_status != "NOT_RUN" for item in runtime):
        raise RuntimeError("TON runtime contract lost NOT_RUN engineer FAT")
    return {
        "outcome": result.engineering.outcome.value,
        "instruction": "TON",
        "runtime_fat": len(runtime),
        "execution_status": runtime[0].execution_status,
    }


def _visual_call(root: Path) -> dict[str, object]:
    bundle = root / "visual-call"
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
    flgnet = f'''
<FlgNet><Parts>
  {_access(51, "MainStart")}{_access(52, "MotorRun")}
  <Call UId="53"><CallInfo Name="LogicFC" BlockType="FC">
    <Parameter Name="Start" Section="Input" Type="Bool" />
    <Parameter Name="Run" Section="Output" Type="Bool" />
  </CallInfo></Call>
</Parts><Wires>
  <Wire><Powerrail /><NameCon UId="53" Name="en" /></Wire>
  <Wire><IdentCon UId="51" /><NameCon UId="53" Name="Start" /></Wire>
  <Wire><NameCon UId="53" Name="Run" /><IdentCon UId="52" /></Wire>
</Wires></FlgNet>
'''
    _xml(
        bundle / "Main.xml",
        flgnet,
        members=(("MainStart", "Bool"), ("MotorRun", "Bool")),
    )
    requirement = _write(
        root / "visual-call-requirement.md",
        "REQ-V4-CALL: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(bundle, requirement_paths=[requirement])
    project = result.engineering.project
    profile = siemens_capability_profile_v4_extended(project)
    facts = project._siemens_v3_facts
    visual = [item for item in facts.calls if item.resolution == "bound_visual_flgnet_call"]
    if profile["flgnet_visual_calls_bound"] != 1 or len(visual) != 1:
        raise RuntimeError(f"visual FC call was not exactly bound: {profile}, {facts.calls}")
    if "LogicFC" not in facts.reachable_blocks or not facts.projected_logic_ids:
        raise RuntimeError("visual FC call did not enter V3 execution closure")
    if result.requirement_verification[0].status is not RequirementStatus.STATICALLY_VERIFIED:
        raise RuntimeError("reachable visual FC theorem did not prove caller requirement")
    return {
        "call_resolution": visual[0].resolution,
        "reachable": "LogicFC" in facts.reachable_blocks,
        "projected_theorems": len(facts.projected_logic_ids),
        "requirement_status": result.requirement_verification[0].status.value,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify DevAgent Siemens TIA extended FlgNet V4 contract"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v4-ext-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v4-extended",
            "contract": (
                "Siemens-only bounded FlgNet local S/R actions, exact-type typed-comparison/MOVE, "
                "unguarded exact-interface visual FB/FC call binding through V3 closure, and explicit "
                "runtime-only timer/counter contracts; unsupported variants fail closed"
            ),
            "set_action": _set_action(root),
            "typed_compare_move": _typed_move(root),
            "timer_runtime_contract": _timer_runtime(root),
            "visual_call_closure": _visual_call(root),
            "result": "PASS",
        }

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
