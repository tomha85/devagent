from __future__ import annotations

import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.schneider_state_machine_v5 import schneider_capability_profile_v5


SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV5Qualification" version="1.0" />
  <program>
    <identProgram name="MainSequence" type="section" task="MAST" />
    <STSource>
CASE State OF
0:
IF Start THEN
State := 10;
END_IF
10:
IF Done THEN
State := 20;
ELSIF Fault THEN
State := 900;
END_IF
20:
900:
END_CASE
    </STSource>
  </program>
  <dataBlock>
    <variables name="State" typeName="INT" />
    <variables name="Start" typeName="BOOL" />
    <variables name="Done" typeName="BOOL" />
    <variables name="Fault" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v5-") as temp:
        root = Path(temp)
        source = root / "Sequence.xst"
        source.write_text(SOURCE, encoding="utf-8")

        result = run_production_verification_v5(source)
        project = result.engineering.project
        profile = schneider_capability_profile_v5(project)
        facts = getattr(project, "_schneider_v5_facts")
        machine = facts.machines[0]

        assert profile["schema"] == "devagent-schneider-control-expert-capability-v5"
        assert profile["state_machines"] == 1
        assert profile["state_machine_full"] == 1
        assert profile["state_machine_states"] == 4
        assert profile["state_machine_transitions"] == 3
        assert profile["state_machine_dangling_targets"] == 0
        assert profile["state_machine_overlap_conflicts"] == 0
        assert profile["state_machine_writer_conflicts"] == 0
        assert profile["state_machine_contract"] == "COMPLETE"
        assert machine.semantic_state is PLCSemanticState.FULL
        assert all(item.execution_status == "NOT_RUN" for item in result.engineering.fat_tests)
        assert any(item.scenario == "SCHNEIDER_STATE_STARTUP" for item in result.engineering.fat_tests)
        assert len(
            [item for item in result.engineering.fat_tests if item.scenario == "SCHNEIDER_STATE_TRANSITION"]
        ) == 3

        payload = {
            "schema": "devagent-schneider-production-qualification-v5",
            "qualified_vendor": "Schneider Electric",
            "engineering_tool": "EcoStruxure Control Expert / Unity Pro XML exchange export",
            "state_machine_fixture": {
                "profile": profile,
                "machine_id": machine.id,
                "state_tag": machine.state_tag,
                "states": list(machine.states),
                "transitions": len(machine.transitions),
                "semantic_state": machine.semantic_state.value,
            },
            "proof_boundary": {
                "bounded_case_transition_relation": True,
                "boolean_guard_priority": True,
                "dangling_overlap_writer_fail_closed": True,
                "startup_or_retained_state_statically_proven": False,
                "timer_counter_runtime_evolution_statically_proven": False,
                "scan_or_process_timing_statically_proven": False,
                "external_runtime_execution": False,
            },
        }
        out = Path(".devagent/schneider-production-qualification-v5.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("SCHNEIDER V5 QUALIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
