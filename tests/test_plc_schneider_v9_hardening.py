from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_plc_project
from devagent.plc.models import PLCOutcome
from devagent.plc.schneider_closeout_v9 import schneider_capability_profile_v9


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_v9_unknown_source_surface_is_visible_and_closeout_status_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Unknown.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9" version="1.0" />
  <program>
    <identProgram name="UnknownSection" type="section" task="MAST" />
    <CustomSource><node /></CustomSource>
  </program>
</STExchangeFile>
''',
    )

    result = analyze_plc_project(source)
    profile = schneider_capability_profile_v9(result.project)

    assert profile["unknown_executable_source_tags"]
    assert profile["support_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["commercial_closeout_status"] == "PARTIAL_FAIL_CLOSED"
    # There is no supported executable theorem in this artifact. Unknown-only
    # executable source must stay BLOCKED rather than looking partially proven.
    assert result.outcome is PLCOutcome.BLOCKED


def test_v9_fbd_linksource_is_wiring_metadata_not_unknown_executable_source(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Wiring.xbd",
        '''<?xml version="1.0" encoding="UTF-8"?>
<FBDExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9FBD" version="1.0" />
  <program>
    <identProgram name="Mixer" type="section" task="MAST" />
    <FBDSource nbRows="24" nbColumns="36">
      <networkFBD>
        <FFBBlock instanceName="Delay" typeName="TON" />
        <linkFB>
          <linkSource parentObjectName="Delay" pinName="Q" />
          <linkDestination parentObjectName="Delay" pinName="IN" />
        </linkFB>
      </networkFBD>
    </FBDSource>
  </program>
  <dataBlock>
    <variables name="Delay" typeName="TON" />
  </dataBlock>
</FBDExchangeFile>
''',
    )

    result = analyze_plc_project(source)
    profile = schneider_capability_profile_v9(result.project)

    assert profile["unknown_executable_source_tags"] == []
    assert profile["export_metadata_consistent"] is True
    assert profile["support_contract"] == "PARTIAL_FAIL_CLOSED"


def test_v9_non_wiring_linksource_remains_unknown_and_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "UnexpectedLinkSource.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9UnexpectedLink" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>Run := Start;</STSource>
    <linkSource extensionKind="unexpectedExecutableSurface" />
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
''',
    )

    result = analyze_plc_project(source)
    profile = schneider_capability_profile_v9(result.project)

    assert profile["unknown_executable_source_tags"] == [
        "UnexpectedLinkSource.xst:Main:linkSource"
    ]
    assert profile["commercial_closeout_status"] == "PARTIAL_FAIL_CLOSED"
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_v9_required_real_export_corpus_remains_explicit(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Main.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>Run := Start;</STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
''',
    )

    result = analyze_plc_project(source)
    profile = schneider_capability_profile_v9(result.project)

    assert profile["commercial_closeout_status"] == "IMPLEMENTATION_QUALIFIED_PENDING_EXTERNAL_EVIDENCE"
    assert profile["required_external_corpus"] == [
        "M340",
        "M580",
        "legacy Unity Pro",
        "mixed ST+LD+FBD",
        "DFB+DDT",
        "CASE/state-machine",
        "interlock/fault/recovery",
        "large industrial project",
    ]
