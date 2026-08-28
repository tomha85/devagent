from __future__ import annotations

from pathlib import Path

from devagent.plc.schneider_real_st_gap_analysis import (
    SCHEMA,
    analyze_schneider_real_st_gaps,
    classify_partial_st,
    render_markdown,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_partial_st_classifier_clusters_common_real_control_expert_shapes() -> None:
    assert classify_partial_st("IF Start AND Ready THEN")[0] == "CONTROL_FLOW"
    assert classify_partial_st("Run := Start AND Ready;")[0] == "BOUNDED_BOOLEAN_SHAPE_PARTIAL"
    assert classify_partial_st("Run := Start XOR Ready;")[0] == "OTHER_ASSIGNMENT"
    assert classify_partial_st("Count := Count + 1;")[0] == "ARITHMETIC_ASSIGNMENT"
    assert classify_partial_st("Timer1(IN := Start, PT := T#1s);")[0] == "CALL_STATEMENT"
    assert classify_partial_st("Buffer[Index] := Value;")[0] == "INDEXED_ASSIGNMENT"


def test_real_st_gap_analysis_counts_only_current_partial_st_statements(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Main.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="RealSTGap" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
Run := Start;
Count := Count + 1;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
    <variables name="Count" typeName="INT" />
  </dataBlock>
</STExchangeFile>
''',
    )

    result = analyze_schneider_real_st_gaps(source, samples_per_cluster=3)

    assert result.schema == SCHEMA
    assert result.total_st_statements == 2
    assert result.partial_st_statements == 1
    assert result.clusters[0].category == "ARITHMETIC_ASSIGNMENT"
    assert result.clusters[0].count == 1
    assert result.clusters[0].samples[0].source_text is None


def test_real_st_gap_report_source_text_is_explicit_opt_in(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Main.xst",
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="RealSTGap" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>Count := Count + 1;</STSource>
  </program>
  <dataBlock>
    <variables name="Count" typeName="INT" />
  </dataBlock>
</STExchangeFile>
''',
    )

    redacted = analyze_schneider_real_st_gaps(source)
    visible = analyze_schneider_real_st_gaps(source, include_source_text=True)

    assert redacted.clusters[0].samples[0].source_text is None
    assert visible.clusters[0].samples[0].source_text == "Count := Count + 1;"
    assert "Count := Count + 1;" not in render_markdown(redacted)
    assert "Count := Count + 1;" in render_markdown(visible)
