from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.siemens_closeout_v9 import _source_manifest, siemens_capability_profile_v9


def _write(path: Path, text: str, *, crlf: bool = False) -> Path:
    payload = text.strip() + "\n"
    if crlf:
        path.write_bytes(payload.replace("\n", "\r\n").encode("utf-8"))
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def _access(uid: int, name: str) -> str:
    return f'<Access Scope="LocalVariable" UId="{uid}"><Symbol><Component Name="{name}" /></Symbol></Access>'


def _simple_lad() -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "Start")}
    {_access(22, "Run")}
    <Part Name="Contact" UId="23" />
    <Part Name="Coil" UId="24" />
  </Parts>
  <Wires>
    <Wire UId="30"><Powerrail /><NameCon UId="23" Name="in" /></Wire>
    <Wire UId="31"><IdentCon UId="21" /><NameCon UId="23" Name="operand" /></Wire>
    <Wire UId="32"><NameCon UId="23" Name="out" /><NameCon UId="24" Name="in" /></Wire>
    <Wire UId="33"><IdentCon UId="22" /><NameCon UId="24" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''


def _simple_fbd() -> str:
    return f'''
<FlgNet>
  <Parts>
    {_access(21, "A")}
    {_access(22, "B")}
    {_access(23, "Run")}
    <Part Name="A" UId="30"><TemplateValue Name="Card" Type="Cardinality">2</TemplateValue></Part>
    <Part Name="Coil" UId="31" />
  </Parts>
  <Wires>
    <Wire UId="40"><IdentCon UId="21" /><NameCon UId="30" Name="in1" /></Wire>
    <Wire UId="41"><IdentCon UId="22" /><NameCon UId="30" Name="in2" /></Wire>
    <Wire UId="42"><NameCon UId="30" Name="out" /><NameCon UId="31" Name="in" /></Wire>
    <Wire UId="43"><IdentCon UId="23" /><NameCon UId="31" Name="operand" /></Wire>
  </Wires>
</FlgNet>
'''


def _block_xml(*, name: str, language: str, flgnet: str, namespace: str | None = None) -> str:
    xmlns = f' xmlns="{namespace}"' if namespace else ""
    members = (
        '<Member Name="Start" Datatype="Bool" />'
        '<Member Name="A" Datatype="Bool" />'
        '<Member Name="B" Datatype="Bool" />'
        '<Member Name="Run" Datatype="Bool" />'
    )
    return f'''
<Document{xmlns}>
  <SW.Blocks.OB ID="1">
    <AttributeList>
      <Name>{name}</Name>
      <ProgrammingLanguage>{language}</ProgrammingLanguage>
      <Interface><Sections><Section Name="Temp">{members}</Section></Sections></Interface>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="2" CompositionName="CompileUnits">
        <AttributeList>
          <ProgrammingLanguage>{language}</ProgrammingLanguage>
          <NetworkSource>{flgnet}</NetworkSource>
        </AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.OB>
</Document>
'''


def _version_matrix(root: Path) -> dict[str, object]:
    results = {}
    for version in (17, 18, 19, 20):
        path = _write(
            root / f"tia-v{version}.xml",
            _block_xml(
                name=f"MainV{version}",
                language="LAD",
                flgnet=_simple_lad(),
                namespace=f"urn:siemens:automation:engineering:v{version}",
            ),
        )
        engineering = analyze_plc_project(path)
        profile = siemens_capability_profile_v9(engineering.project)
        if profile["coverage_accounting_complete"] is not True:
            raise RuntimeError(f"TIA V{version} namespace fixture lost support accounting: {profile}")
        if not engineering.project.logic_statements:
            raise RuntimeError(f"TIA V{version} namespace fixture imported no executable network")
        results[f"V{version}"] = {
            "statements": len(engineering.project.logic_statements),
            "support_contract": profile["support_contract"],
            "accounting_complete": profile["coverage_accounting_complete"],
        }
    return results


def _mixed_language(root: Path) -> dict[str, object]:
    _write(
        root / "Main.scl",
        '''
ORGANIZATION_BLOCK SclMain
VAR
    Start : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_ORGANIZATION_BLOCK
''',
        crlf=True,
    )
    _write(root / "Ladder.xml", _block_xml(name="LadderMain", language="LAD", flgnet=_simple_lad()))
    _write(root / "Fbd.xml", _block_xml(name="FbdMain", language="FBD", flgnet=_simple_fbd()))
    result = run_production_verification_v5(root)
    profile = siemens_capability_profile_v9(result.engineering.project)
    languages = {statement.language for statement in result.engineering.project.logic_statements}
    if not {"SCL", "LAD", "FBD"}.issubset(languages):
        raise RuntimeError(f"mixed SCL/LAD/FBD import incomplete: {languages}")
    if not profile["coverage_accounting_complete"]:
        raise RuntimeError(f"mixed-language support accounting incomplete: {profile}")
    return {
        "languages": sorted(languages),
        "support_contract": profile["support_contract"],
        "support_by_language": profile["support_by_language"],
        "line_endings": profile["line_endings"],
    }


def _large_project(root: Path) -> dict[str, object]:
    # Stress the executable-block path, not DATA_BLOCK-as-program bookkeeping.
    # Each FB is a real independently parsed engineering block with two scoped
    # interface symbols and one bounded assignment theorem.
    blocks = []
    for index in range(1000):
        blocks.append(
            f'''FUNCTION_BLOCK FB_{index:04d}\nVAR_INPUT\n    InValue : Bool;\nEND_VAR\nVAR_OUTPUT\n    OutValue : Bool;\nEND_VAR\nBEGIN\n    OutValue := InValue;\nEND_FUNCTION_BLOCK'''
        )
    blocks.append(
        '''ORGANIZATION_BLOCK Main\nVAR\n    Start : Bool;\n    Run : Bool;\nEND_VAR\nBEGIN\n    Run := Start;\nEND_ORGANIZATION_BLOCK'''
    )
    _write(root / "Large.scl", "\n\n".join(blocks))

    tags = "".join(
        f'<SW.Tags.PlcTag ID="T{index}"><AttributeList><Name>Global_{index:04d}</Name><DataTypeName>Bool</DataTypeName></AttributeList></SW.Tags.PlcTag>'
        for index in range(1500)
    )
    _write(root / "Tags.xml", f"<Document>{tags}</Document>")

    engineering = analyze_plc_project(root)
    if len(engineering.project.tags) < 2500:
        raise RuntimeError(f"large Siemens qualification imported only {len(engineering.project.tags)} tags")
    if len(engineering.project.programs) < 1000:
        raise RuntimeError(f"large Siemens qualification imported only {len(engineering.project.programs)} executable blocks/programs")
    if len(engineering.project.logic_statements) < 1000:
        raise RuntimeError(f"large Siemens qualification imported only {len(engineering.project.logic_statements)} logic statements")
    profile = siemens_capability_profile_v9(engineering.project)
    if not profile["coverage_accounting_complete"]:
        raise RuntimeError(f"large Siemens support accounting incomplete: {profile}")
    return {
        "blocks_programs": len(engineering.project.programs),
        "logic_statements": len(engineering.project.logic_statements),
        "tags": len(engineering.project.tags),
        "source_files": profile["source_files"],
        "source_bytes": profile["source_bytes"],
        "accounting_complete": profile["coverage_accounting_complete"],
    }


def _malformed_xml(root: Path) -> dict[str, object]:
    path = _write(root / "malformed.xml", '<Document><SW.Blocks.OB><AttributeList><Name>Broken</Name>')
    rejected = None
    try:
        analyze_plc_project(path)
    except Exception as exc:  # qualification asserts fail-closed, not a specific parser exception surface
        rejected = type(exc).__name__
    if rejected is None:
        raise RuntimeError("malformed Siemens XML was accepted instead of failing closed")
    return {"status": "REJECTED", "exception": rejected}


def _unicode_crlf_manifest(root: Path) -> dict[str, object]:
    _write(
        root / "Unicode.scl",
        '''
ORGANIZATION_BLOCK "Mäin_日本"
VAR
    Start : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_ORGANIZATION_BLOCK
''',
        crlf=True,
    )
    first = _source_manifest(root)
    second = _source_manifest(root)
    if first != second:
        raise RuntimeError("Siemens V9 deterministic bundle manifest changed across identical reads")
    if "CRLF" not in first[3] or first[4] is not True:
        raise RuntimeError(f"Unicode/CRLF manifest qualification failed: {first}")
    return {
        "files": first[0],
        "bytes": first[1],
        "manifest_sha256": first[2],
        "line_endings": first[3],
        "unicode_present": first[4],
    }


def _revision_regression(root: Path) -> dict[str, object]:
    old = _write(
        root / "old.scl",
        '''
ORGANIZATION_BLOCK Main
VAR
    Start : Bool;
    Guard : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := Start AND Guard;
END_ORGANIZATION_BLOCK
''',
    )
    new = _write(
        root / "new.scl",
        '''
ORGANIZATION_BLOCK Main
VAR
    Start : Bool;
    Guard : Bool;
    Ready : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := Start AND Guard AND Ready;
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(new, baseline_path=old)
    if not result.regression_changes:
        raise RuntimeError("Siemens old/new revision regression produced no change evidence")
    return {
        "baseline_sha256": result.baseline_sha256,
        "changes": len(result.regression_changes),
        "change_types": sorted({item.change_type for item in result.regression_changes}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify Siemens V9 real-world-shaped commercial closeout")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v9-") as directory:
        root = Path(directory)
        def sub(name: str) -> Path:
            path = root / name
            path.mkdir()
            return path

        report = {
            "schema": "devagent-siemens-production-qualification-v9",
            "contract": (
                "Real-world-shaped offline qualification for TIA V17/V18/V19/V20 namespace variants, mixed SCL/LAD/FBD, "
                "large engineering bundles, malformed XML, Unicode/CRLF, deterministic manifests, revision regression, "
                "and exhaustive FULL/PARTIAL/OPAQUE/PROTECTED support accounting."
            ),
            "tia_version_matrix_synthetic": _version_matrix(sub("versions")),
            "mixed_language": _mixed_language(sub("mixed")),
            "large_project": _large_project(sub("large")),
            "malformed_xml": _malformed_xml(sub("malformed")),
            "unicode_crlf_manifest": _unicode_crlf_manifest(sub("unicode")),
            "revision_regression": _revision_regression(sub("regression")),
            "real_tia_openness_export_corpus": {
                "count": 0,
                "status": "EXTERNAL_CORPUS_REQUIRED",
                "qualified": False,
                "reason": (
                    "No license-safe real TIA Portal Openness export corpus is committed to this repository. "
                    "Synthetic fixtures are never represented as real Siemens exports. Customer/sanitized V17-V20 exports remain the final external qualification evidence gate."
                ),
            },
            "plcsim_hil_real_plc_execution": False,
            "result": "PASS_WITH_EXTERNAL_REAL_EXPORT_EVIDENCE_GATE",
        }

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
