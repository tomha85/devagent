from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.schneider_interlock_permissive_v6 import schneider_capability_profile_v6


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _source() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV6Hardening" version="1.0" />
  <program>
    <identProgram name="V6Hardening" type="section" task="MAST" />
    <STSource>
MotorRun := Start AND DoorInterlock;
    </STSource>
  </program>
  <dataBlock>
    <variables name="MotorRun" typeName="BOOL" />
    <variables name="Start" typeName="BOOL" />
    <variables name="DoorInterlock" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
"""


def test_v6_capability_evidence_and_findings_are_version_correct(tmp_path: Path) -> None:
    source = _write(tmp_path / "V6Hardening.xst", _source())
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v6(result.engineering.project)
    capability = next(
        item
        for item in result.evidence
        if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE"
    )

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v6"
    assert "V6" in capability.summary
    assert all(
        "Schneider Control Expert V1" not in item.title
        and "Schneider Control Expert V5" not in item.title
        and "Schneider V1" not in item.summary
        and "Schneider V5" not in item.summary
        for item in result.engineering_findings
    )
