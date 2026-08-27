from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.siemens_flgnet_v4 import siemens_capability_profile_v4


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _access(uid: int, name: str) -> str:
    return (
        f'<Access Scope="LocalVariable" UId="{uid}">'
        f'<Symbol><Component Name="{name}" /></Symbol></Access>'
    )


def _xml(
    path: Path,
    *,
    language: str,
    flgnet: str,
    kind: str = "OB",
    name: str = "Main",
    input_name: str = "Start",
    output_name: str = "Run",
) -> Path:
    return _write(
        path,
        f'''
<Document>
  <SW.Blocks.{kind} ID="1">
    <AttributeList>
      <Name>{name}</Name>
      <ProgrammingLanguage>{language}</ProgrammingLanguage>
      <Interface><Sections>
        <Section Name="Input"><Member Name="{input_name}" Datatype="Bool" /></Section>
        <Section Name="Output"><Member Name="{output_name}" Datatype="Bool" /></Section>
      </Sections></Interface>
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


def _lad(part_name: str = "Contact") -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "Start")}
    {_access(22, "Run")}
    <Part Name="{part_name}" UId="23" />
    <Part Name="Coil" UId="24" />
  </Parts>
  <Wires>
    <Wire><Powerrail /><NameCon UId="23" Name="in" /></Wire>
    <Wire><IdentCon UId="21" /><NameCon UId="23" Name="operand" /></Wire>
    <Wire><NameCon UId="23" Name="out" /><NameCon UId="24" Name="in" /></Wire>
    <Wire><IdentCon UId="22" /><NameCon UId="24" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''


def _fbd() -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "Start")}
    {_access(22, "Guard")}
    {_access(23, "Run")}
    <Part Name="A" UId="30"><TemplateValue Name="Card" Type="Cardinality">2</TemplateValue></Part>
    <Part Name="Coil" UId="31" />
  </Parts>
  <Wires>
    <Wire><IdentCon UId="21" /><NameCon UId="30" Name="in1" /></Wire>
    <Wire><IdentCon UId="22" /><NameCon UId="30" Name="in2" /></Wire>
    <Wire><NameCon UId="30" Name="out" /><NameCon UId="31" Name="in" /></Wire>
    <Wire><IdentCon UId="23" /><NameCon UId="31" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''


def _qualified_lad(root: Path) -> dict[str, object]:
    source = _xml(root / "lad.xml", language="LAD", flgnet=_lad())
    requirement = _write(
        root / "lad-requirements.md",
        "REQ-V4-LAD: When Start=TRUE, Run=TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )
    profile = siemens_capability_profile_v4(result.engineering.project)
    if result.engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(f"bounded LAD should be statically verified: {profile}")
    if profile["flgnet_modeled"] != 1 or profile["flgnet_withheld"] != 0:
        raise RuntimeError(f"bounded LAD coverage is wrong: {profile}")
    if result.requirement_verification[0].status is not RequirementStatus.STATICALLY_VERIFIED:
        raise RuntimeError("bounded LAD requirement was not proven")
    return {
        "outcome": result.engineering.outcome.value,
        "modeled": profile["flgnet_modeled"],
        "withheld": profile["flgnet_withheld"],
        "requirement_status": result.requirement_verification[0].status.value,
    }


def _qualified_fbd(root: Path) -> dict[str, object]:
    source = _write(
        root / "fbd.xml",
        f'''
<Document>
  <SW.Blocks.OB ID="1">
    <AttributeList>
      <Name>Main</Name><ProgrammingLanguage>FBD</ProgrammingLanguage>
      <Interface><Sections><Section Name="Temp">
        <Member Name="Start" Datatype="Bool" />
        <Member Name="Guard" Datatype="Bool" />
        <Member Name="Run" Datatype="Bool" />
      </Section></Sections></Interface>
    </AttributeList>
    <ObjectList><SW.Blocks.CompileUnit ID="2"><AttributeList>
      <ProgrammingLanguage>FBD</ProgrammingLanguage>
      <NetworkSource>{_fbd()}</NetworkSource>
    </AttributeList></SW.Blocks.CompileUnit></ObjectList>
  </SW.Blocks.OB>
</Document>
''',
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v4(result.engineering.project)
    logic = next(
        item
        for item in result.engineering.project.output_logic
        if item.origin.startswith("SIEMENS_FLGNET_V4:")
    )
    if result.engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(f"bounded FBD should be statically verified: {profile}")
    if len(logic.paths) != 1 or len(logic.paths[0].terms) != 2:
        raise RuntimeError(f"FBD AND theorem is wrong: {logic.paths}")
    return {
        "outcome": result.engineering.outcome.value,
        "modeled": profile["fbd_modeled"],
        "paths": len(logic.paths),
    }


def _fail_closed(root: Path) -> dict[str, object]:
    source = _xml(
        root / "timer.xml",
        language="LAD",
        flgnet=_lad("TON"),
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v4(result.engineering.project)
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("TON network must remain PARTIALLY_VERIFIED")
    if result.engineering.project.logic_statements[0].semantic_state is not PLCSemanticState.OPAQUE:
        raise RuntimeError("TON network incorrectly received executable theorem")
    runtime = [
        item
        for item in result.engineering.fat_tests
        if item.scenario == "SIEMENS_FLGNET_RUNTIME"
    ]
    if not runtime or any(item.execution_status != "NOT_RUN" for item in runtime):
        raise RuntimeError("withheld FlgNet network lost engineer runtime FAT")
    return {
        "outcome": result.engineering.outcome.value,
        "reasons": profile["flgnet_withheld_reasons"],
        "runtime_fat": len(runtime),
    }


def _cross_block(root: Path) -> dict[str, object]:
    bundle = root / "cross-block"
    bundle.mkdir()
    _write(
        bundle / "Main.scl",
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
    _xml(
        bundle / "LogicFC.xml",
        language="LAD",
        flgnet=_lad(),
        kind="FC",
        name="LogicFC",
    )
    requirement = _write(
        root / "cross-requirements.md",
        "REQ-V4-CALL: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(
        bundle,
        requirement_paths=[requirement],
    )
    facts = result.engineering.project._siemens_v3_facts
    if not facts.projected_logic_ids:
        raise RuntimeError("V4 local theorem was not projected through V3 call closure")
    if result.requirement_verification[0].status is not RequirementStatus.STATICALLY_VERIFIED:
        raise RuntimeError("projected V4 theorem did not prove caller requirement")
    return {
        "calls_bound": sum(
            item.semantic_state is PLCSemanticState.FULL
            for item in facts.calls
        ),
        "projected_theorems": len(facts.projected_logic_ids),
        "requirement_status": result.requirement_verification[0].status.value,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify DevAgent Siemens TIA LAD/FBD FlgNet V4"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v4-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v4",
            "contract": (
                "bounded LAD Powerrail/contact/normal-coil and FBD A/O/normal-coil "
                "FlgNet Boolean topology with exact symbol/type/wire binding; "
                "unsupported/stateful networks fail closed; no PLC execution"
            ),
            "qualified_lad": _qualified_lad(root),
            "qualified_fbd": _qualified_fbd(root),
            "unsupported_timer_fail_closed": _fail_closed(root),
            "v3_cross_block_projection": _cross_block(root),
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
