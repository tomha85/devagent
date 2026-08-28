from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState, StaticCheckStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_identity_types_v8 import schneider_capability_profile_v8


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _xst(body: str, variables: list[tuple[str, str, str | None]], *, name: str = "Main") -> str:
    tags = []
    for tag, dtype, address in variables:
        address_xml = f' topologicalAddress="{address}"' if address else ""
        tags.append(f'    <variables name="{tag}" typeName="{dtype}"{address_xml} />')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8" version="1.0" />
  <program>
    <identProgram name="{name}" type="section" task="MAST" />
    <STSource>
{body.strip()}
    </STSource>
  </program>
  <dataBlock>
{chr(10).join(tags)}
  </dataBlock>
</STExchangeFile>
'''


def _xdd(name: str, members: list[tuple[str, str, str | None]]) -> str:
    rows = []
    for member, dtype, dimension in members:
        dimension_xml = f' dimension="{dimension}"' if dimension else ""
        rows.append(f'    <variables name="{member}" typeName="{dtype}"{dimension_xml} />')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<DDTExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8" version="1.0" />
  <DDT name="{name}">
{chr(10).join(rows)}
  </DDT>
</DDTExchangeFile>
'''


def _xdb() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<FBExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8" version="1.0" />
  <FBSource nameOfFBType="MOTOR_DFB" version="1.0">
    <inputParameters>
      <variables name="Start" typeName="BOOL" />
    </inputParameters>
    <outputParameters>
      <variables name="Run" typeName="BOOL" />
    </outputParameters>
    <privateLocalVariables>
      <variables name="PrivateFlag" typeName="BOOL" />
    </privateLocalVariables>
    <FBProgram name="MOTOR_DFB">
      <STSource>
Run := Start;
      </STSource>
    </FBProgram>
  </FBSource>
</FBExchangeFile>
'''


def test_v8_canonicalizes_ddt_array_members_and_located_io_without_flat_member_leak(tmp_path: Path) -> None:
    root = tmp_path / "identity"
    root.mkdir()
    _write(
        root / "Types.xdd",
        _xdd("AxisData", [("Enabled", "BOOL", None), ("Samples", "DINT", "0..3")]),
    )
    _write(
        root / "Main.xst",
        _xst(
            """
Run := Start;
Axis.Enabled := Start;
""",
            [
                ("Start", "BOOL", "%I0.0"),
                ("Run", "BOOL", "%Q0.0"),
                ("Axis", "AxisData", None),
            ],
        ),
    )

    result = run_production_verification_v5(root)
    project = result.engineering.project
    profile = schneider_capability_profile_v8(project)
    facts = project._schneider_v8_identity_facts

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v8"
    assert profile["ddt_types"] >= 1
    assert profile["array_types"] >= 1
    assert profile["located_io_identities"] >= 2
    assert profile["input_identities"] >= 1
    assert profile["output_identities"] >= 1
    by_display = {(item.scope.casefold(), item.display_path.casefold()): item for item in facts.symbols}
    assert ("controller", "axis.enabled") in by_display
    assert ("controller", "axis.samples[*]") in by_display
    assert ("controller", "enabled") not in by_display
    binding = next(
        item for item in facts.bindings
        if item.access == "WRITE" and item.raw_ref.casefold() == "axis.enabled"
    )
    assert binding.semantic_state is PLCSemanticState.FULL
    assert binding.canonical_display.casefold() == "controller::axis.enabled"
    assert any(edge.kind == "LOCATED_AT" for edge in result.engineering.graph.edges)


def test_v8_direct_located_address_gets_typed_canonical_binding(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "DirectAddress.xst",
        _xst("Run := %I0.1;", [("Run", "BOOL", "%Q0.0")]),
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._schneider_v8_identity_facts
    binding = next(item for item in facts.bindings if item.raw_ref.casefold() == "%i0.1")
    symbol = next(item for item in facts.symbols if item.id == binding.canonical_symbol_id)

    assert binding.resolution == "DIRECT_LOCATED_ADDRESS"
    assert binding.canonical_display.casefold() == "address::%i0.1"
    assert binding.semantic_state is PLCSemanticState.FULL
    assert symbol.data_type == "BOOL"
    assert symbol.io_area == "INPUT"


def test_v8_referenced_physical_address_alias_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Alias.xst",
        _xst(
            """
Y1 := A;
Y2 := B;
""",
            [
                ("A", "BOOL", "%M10"),
                ("B", "BOOL", "%M10"),
                ("Y1", "BOOL", None),
                ("Y2", "BOOL", None),
            ],
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v8(result.engineering.project)

    assert profile["physical_address_aliases"] >= 1
    assert profile["identity_contract"] == "PARTIAL_FAIL_CLOSED"
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(risk.category == "IO_IDENTITY" for risk in result.risks)
    assert any(
        check.id == "SCHNEIDER_V8_IO_ADDRESS_IDENTITY"
        and check.status is StaticCheckStatus.NOT_PROVEN
        for check in result.engineering.static_checks
    )


def test_v8_whole_member_writer_overlap_and_typed_structure_theorem_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "typed"
    root.mkdir()
    _write(root / "Types.xdd", _xdd("PairData", [("Value", "BOOL", None)]))
    _write(
        root / "Main.xst",
        _xst(
            """
Whole := Other;
Whole.Value := Start;
""",
            [
                ("Whole", "PairData", None),
                ("Other", "PairData", None),
                ("Start", "BOOL", None),
            ],
        ),
    )
    result = run_production_verification_v5(root)
    project = result.engineering.project
    profile = schneider_capability_profile_v8(project)

    assert profile["whole_member_writer_overlaps"] >= 1
    assert profile["typed_boolean_theorem_gaps"] >= 1
    assert profile["typed_boolean_contract"] == "PARTIAL_FAIL_CLOSED"
    whole_logic = next(item for item in project.output_logic if item.output_tag.casefold() == "whole")
    assert whole_logic.semantic_state is PLCSemanticState.PARTIAL
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(
        check.id == "SCHNEIDER_V8_TYPED_BOOLEAN_THEOREM"
        and check.status is StaticCheckStatus.NOT_PROVEN
        for check in result.engineering.static_checks
    )
    assert any(risk.category == "TYPE_IDENTITY" for risk in result.risks)


def test_v8_dfb_instance_and_interface_identity_are_canonical(tmp_path: Path) -> None:
    root = tmp_path / "dfb"
    root.mkdir()
    _write(root / "Motor.xdb", _xdb())
    _write(
        root / "Main.xst",
        _xst(
            "Motor1(Start := Start, Run => Run);",
            [
                ("Start", "BOOL", None),
                ("Run", "BOOL", None),
                ("Motor1", "MOTOR_DFB", None),
            ],
        ),
    )
    result = run_production_verification_v5(root)
    profile = schneider_capability_profile_v8(result.engineering.project)
    facts = result.engineering.project._schneider_v8_identity_facts

    assert profile["dfb_identity_types"] >= 1
    assert profile["dfb_instance_identities"] >= 1
    identity = next(item for item in facts.dfb_instances if item.instance_name.casefold() == "motor1")
    assert identity.canonical_symbol_id is not None
    assert identity.type_id is not None
    displays = {(item.scope.casefold(), item.display_path.casefold()) for item in facts.symbols}
    assert ("controller", "motor1.start") in displays
    assert ("controller", "motor1.run") in displays
    assert ("controller", "privateflag") not in displays
    assert ("dfb-type:motor_dfb", "privateflag") in displays


def test_v8_preserves_v7_theorem_provenance_while_adding_identity_facts(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Recovery.xst",
        _xst(
            """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
END_IF
900:
IF ResetCmd THEN
State := 0;
END_IF
END_CASE
""",
            [
                ("State", "INT", None),
                ("FaultDetected", "BOOL", None),
                ("ResetCmd", "BOOL", None),
            ],
        ),
    )
    result = run_production_verification_v5(source)
    project = result.engineering.project

    assert project.metadata.schema_revision == "SCHNEIDER-CONTROL-EXPERT-EXPORT-V7"
    assert hasattr(project, "_schneider_v7_recovery_facts")
    assert hasattr(project, "_schneider_v8_identity_facts")
    assert schneider_capability_profile_v8(project)["schema"] == "devagent-schneider-control-expert-capability-v8"


def test_v8_report_exposes_identity_boundary_without_runtime_claim(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Report.xst",
        _xst("Run := Start;", [("Start", "BOOL", "%I0.0"), ("Run", "BOOL", "%Q0.0")]),
    )
    production = run_production_verification_v5(source)
    report = render_production_report(production)

    assert "### Schneider V8 Canonical Symbols / Types / I/O Identity" in report
    assert "Identity contract" in report
    assert "does not prove wiring" in report
