from __future__ import annotations

import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.plc_dispatch import detect_plc_vendor
from devagent.plc.schneider_control_expert_v1 import schneider_capability_profile


ST = """<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="Qualification" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>Run := Start AND NOT Stop;</STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Stop" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
"""

PARTIAL = ST.replace(
    "Run := Start AND NOT Stop;",
    "IF Start THEN\nRun := NOT Stop;\nEND_IF;",
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v1-") as temp:
        root = Path(temp)
        full = root / "Main.xst"
        full.write_text(ST, encoding="utf-8")
        partial = root / "Partial.xst"
        partial.write_text(PARTIAL, encoding="utf-8")

        assert detect_plc_vendor(full) == "SCHNEIDER"
        full_result = run_production_verification_v5(full)
        full_profile = schneider_capability_profile(full_result.engineering.project)
        assert full_result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
        assert full_profile["static_contract"] == "COMPLETE"
        assert full_result.engineering.project.output_logic
        assert full_result.engineering.fat_tests
        assert all(item.execution_status == "NOT_RUN" for item in full_result.engineering.fat_tests)
        assert "Schneider" in full_result.stages[0].summary

        partial_result = run_production_verification_v5(partial)
        partial_profile = schneider_capability_profile(partial_result.engineering.project)
        assert partial_result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
        assert partial_profile["partial_statements"] > 0
        assert any(item.semantic_state is PLCSemanticState.PARTIAL for item in partial_result.engineering.project.logic_statements)
        assert any(test.method == "RUNTIME_FAT_REQUIRED" for test in partial_result.engineering.fat_tests)

        evidence = {
            "schema": "devagent-schneider-production-qualification-v1",
            "qualified_vendor": "Schneider Electric",
            "engineering_tool": "EcoStruxure Control Expert / Unity Pro XML exchange export",
            "static_fixture": {
                "outcome": full_result.engineering.outcome.value,
                "profile": full_profile,
                "fat_tests": len(full_result.engineering.fat_tests),
            },
            "partial_fixture": {
                "outcome": partial_result.engineering.outcome.value,
                "profile": partial_profile,
                "runtime_fat": sum(test.method == "RUNTIME_FAT_REQUIRED" for test in partial_result.engineering.fat_tests),
            },
            "external_runtime_execution": False,
        }
        out = Path(".devagent/schneider-production-qualification-v1.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print("SCHNEIDER V1 QUALIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
