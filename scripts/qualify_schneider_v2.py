from __future__ import annotations

import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.schneider_st_control_flow_v2 import schneider_capability_profile_v2


def _xst(st_source: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV2Qualification" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
{st_source.strip()}
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Guard" typeName="BOOL" />
    <variables name="ModeB" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
    <variables name="Aux" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
'''


COMPLETE = _xst(
    '''
IF Start AND Guard THEN
    Run := TRUE;
ELSIF ModeB THEN
    Run := TRUE;
ELSE
    Run := FALSE;
END_IF;
'''
)

INCOMPLETE = _xst(
    '''
IF Start THEN
    Run := TRUE;
ELSE
    Aux := FALSE;
END_IF;
'''
)

NESTED = _xst(
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
'''
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v2-") as temp:
        root = Path(temp)

        complete_path = root / "Complete.xst"
        complete_path.write_text(COMPLETE, encoding="utf-8")
        complete = run_production_verification_v5(complete_path)
        complete_profile = schneider_capability_profile_v2(complete.engineering.project)
        assert complete.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
        assert complete_profile["schema"] == "devagent-schneider-control-expert-capability-v2"
        assert complete_profile["if_chain_models"] == 1
        assert complete_profile["if_chain_output_logic"] == 1
        assert all(
            item.semantic_state is PLCSemanticState.FULL
            for item in complete.engineering.project.logic_statements
        )
        assert not any(
            risk.category == "MULTIPLE_WRITERS" and "Run" in risk.title
            for risk in complete.risks
        )
        assert complete.engineering.fat_tests
        assert all(item.execution_status == "NOT_RUN" for item in complete.engineering.fat_tests)

        incomplete_path = root / "Incomplete.xst"
        incomplete_path.write_text(INCOMPLETE, encoding="utf-8")
        incomplete = run_production_verification_v5(incomplete_path)
        incomplete_profile = schneider_capability_profile_v2(incomplete.engineering.project)
        assert incomplete.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
        assert incomplete_profile["if_chain_models"] == 0
        assert any(
            item.semantic_state is PLCSemanticState.PARTIAL
            for item in incomplete.engineering.project.logic_statements
        )

        nested_path = root / "Nested.xst"
        nested_path.write_text(NESTED, encoding="utf-8")
        nested = run_production_verification_v5(nested_path)
        nested_profile = schneider_capability_profile_v2(nested.engineering.project)
        assert nested.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
        assert nested_profile["if_chain_models"] == 0
        assert not any(
            logic.origin.startswith("SCHNEIDER_ST_IF_CHAIN:")
            for logic in nested.engineering.project.output_logic
        )

        evidence = {
            "schema": "devagent-schneider-production-qualification-v2",
            "qualified_vendor": "Schneider Electric",
            "engineering_tool": "EcoStruxure Control Expert / Unity Pro XML exchange export",
            "theorem": "complete top-level IF/ELSIF/ELSE Boolean final-value chains",
            "complete_fixture": {
                "outcome": complete.engineering.outcome.value,
                "profile": complete_profile,
                "fat_tests": len(complete.engineering.fat_tests),
            },
            "fail_closed_fixtures": {
                "incomplete_branch_assignments": {
                    "outcome": incomplete.engineering.outcome.value,
                    "profile": incomplete_profile,
                },
                "nested_if": {
                    "outcome": nested.engineering.outcome.value,
                    "profile": nested_profile,
                },
            },
            "external_runtime_execution": False,
        }
        out = Path(".devagent/schneider-production-qualification-v2.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("SCHNEIDER V2 QUALIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
