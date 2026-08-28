from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_plc_project
from devagent.plc.models import PLCSemanticState
from devagent.plc.schneider_closeout_v9 import schneider_capability_profile_v9


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_v10_models_partial_real_st_assignments_without_promoting_control_flow(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Main.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="RealSTActions" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
IF Gate THEN
Run := Start AND Ready;
Count := Count + 1;
Zero := 0;
Mirror := SourceValue;
END_IF;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Gate" typeName="BOOL" />
    <variables name="Start" typeName="BOOL" />
    <variables name="Ready" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
    <variables name="Count" typeName="INT" />
    <variables name="Zero" typeName="INT" />
    <variables name="Mirror" typeName="INT" />
    <variables name="SourceValue" typeName="INT" />
  </dataBlock>
</STExchangeFile>
''',
    )

    result = analyze_plc_project(source)
    facts = result.project._schneider_real_st_action_facts
    profile = schneider_capability_profile_v9(result.project)

    assert len(facts.actions) == 4
    assert {item.family for item in facts.actions} == {
        "BOOLEAN_ASSIGNMENT",
        "ARITHMETIC_ASSIGNMENT",
        "CONSTANT_ASSIGNMENT",
        "DATA_MOVE",
    }
    assert all(item.execution_condition_proven is False for item in facts.actions)

    modeled_ids = {item.statement_id for item in facts.actions}
    modeled_statements = [item for item in result.project.logic_statements if item.id in modeled_ids]
    assert modeled_statements
    assert all(item.semantic_state is PLCSemanticState.PARTIAL for item in modeled_statements)

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v10"
    assert profile["real_st_local_actions"] == 4
    assert profile["partial_st_with_local_action_semantics"] == 4
    assert profile["real_st_local_actions_promote_v9_support"] is False

    action_tests = [item for item in result.fat_tests if item.scenario == "SCHNEIDER_ST_LOCAL_ACTION"]
    assert len(action_tests) == 4
    assert all(item.method == "RUNTIME_FAT_REQUIRED" for item in action_tests)

    action_edges = [item for item in result.graph.edges if item.kind == "ST_LOCAL_ACTION_DEPENDS_ON"]
    assert any(item.source == "Count" and item.target == "Count" for item in action_edges)
    assert any(item.source == "Run" and item.target == "Start" for item in action_edges)
    assert any(item.source == "Run" and item.target == "Ready" for item in action_edges)


def test_v10_withholds_function_indexed_and_time_literal_assignment_shapes(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Main.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="RealSTActions" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
IF Gate THEN
Converted := INT_TO_REAL(InputValue);
Buffer[Index] := InputValue;
PT := T#5s,;
END_IF;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Gate" typeName="BOOL" />
    <variables name="Converted" typeName="REAL" />
    <variables name="InputValue" typeName="INT" />
    <variables name="Index" typeName="INT" />
    <variables name="PT" typeName="TIME" />
  </dataBlock>
</STExchangeFile>
''',
    )

    result = analyze_plc_project(source)
    facts = result.project._schneider_real_st_action_facts

    assert facts.actions == ()
    check = next(item for item in result.static_checks if item.id == "SCHNEIDER_V10_REAL_ST_LOCAL_ACTIONS")
    assert "0 deterministic local ST assignment effect" in check.summary


def test_v10_top_level_v1_boolean_theorem_does_not_get_duplicate_action_model(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Main.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="RealSTActions" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>Run := Start AND Ready;</STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Ready" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
''',
    )

    result = analyze_plc_project(source)
    facts = result.project._schneider_real_st_action_facts

    assert facts.actions == ()
    assert not [item for item in result.fat_tests if item.scenario == "SCHNEIDER_ST_LOCAL_ACTION"]
