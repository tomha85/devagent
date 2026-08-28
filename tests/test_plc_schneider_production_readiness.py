from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from devagent.plc.schneider_production_readiness import (
    SchneiderProductionCorpusError,
    qualify_schneider_production_corpus,
)


def _xst(project: str = "ProductionCorpus") -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="{project}" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
Run := Start;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" topologicalAddress="%I0.0" />
    <variables name="Run" typeName="BOOL" topologicalAddress="%Q0.0" />
  </dataBlock>
</STExchangeFile>
'''


def _write_source(path: Path) -> Path:
    path.write_text(_xst(), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(root: Path, cases: list[dict[str, object]]) -> Path:
    path = root / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-schneider-production-corpus-v1",
                "corpus_id": "schneider-production-test",
                "cases": cases,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _real_case(source: Path, **overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "m340-real",
        "path": source.name,
        "source_kind": "REAL_CONTROL_EXPERT_EXPORT",
        "origin": "LAB",
        "controller_family": "M340",
        "families": ["M340"],
        "bundle_sha256": _sha(source),
        "attested_by": "qualified-engineer@example.invalid",
        "exported_at": "2026-08-28T12:00:00-04:00",
    }
    payload.update(overrides)
    return payload


def test_real_hash_pinned_m340_case_passes_but_corpus_stays_pending(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "M340.xst")
    manifest = _write_manifest(tmp_path, [_real_case(source)])

    result = qualify_schneider_production_corpus(manifest)

    assert result.status == "PENDING_REAL_EXPORT_CORPUS"
    assert result.commercial_static_ready is False
    assert result.real_export_cases == 1
    assert result.distinct_real_bundle_hashes == 1
    assert "M340" in result.covered_real_families
    assert "M580" in result.missing_real_families
    assert result.cases[0].status == "PASS"
    assert result.cases[0].hash_matches is True
    assert result.cases[0].deterministic is True
    assert result.cases[0].accounting_complete is True
    assert result.cases[0].audit_clean is True


def test_synthetic_fixture_never_satisfies_real_export_family_gate(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "Synthetic.xst")
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "id": "synthetic-m340",
                "path": source.name,
                "source_kind": "SYNTHETIC",
                "origin": "INTERNAL",
                "controller_family": "M340",
                "families": ["M340"],
            }
        ],
    )

    result = qualify_schneider_production_corpus(manifest)

    assert result.cases[0].status == "PASS"
    assert result.real_export_cases == 0
    assert result.covered_real_families == ()
    assert "M340" in result.missing_real_families
    assert result.status == "PENDING_REAL_EXPORT_CORPUS"


def test_real_export_without_hash_and_attestation_fails_closed(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "Unattested.xst")
    case = _real_case(source)
    case.pop("bundle_sha256")
    case.pop("attested_by")
    manifest = _write_manifest(tmp_path, [case])

    result = qualify_schneider_production_corpus(manifest)

    assert result.status == "FAIL"
    assert result.cases[0].status == "FAIL"
    assert result.cases[0].hash_matches is False
    assert any("bundle_sha256" in item for item in result.cases[0].findings)
    assert any("attested_by" in item for item in result.cases[0].findings)


def test_declared_mixed_language_family_must_be_observed_in_export(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "OnlyST.xst")
    case = _real_case(
        source,
        id="false-mixed",
        controller_family=None,
        families=["MIXED_ST_LD_FBD"],
    )
    manifest = _write_manifest(tmp_path, [case])

    result = qualify_schneider_production_corpus(manifest)

    assert result.status == "FAIL"
    assert result.cases[0].status == "FAIL"
    assert "MIXED_ST_LD_FBD" not in result.cases[0].observed_families
    assert any("not substantiated" in item for item in result.cases[0].findings)


def test_controller_family_must_match_declared_hardware_family(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "Mismatch.xst")
    case = _real_case(source, controller_family="M580", families=["M340"])
    manifest = _write_manifest(tmp_path, [case])

    with pytest.raises(SchneiderProductionCorpusError, match="must match hardware family"):
        qualify_schneider_production_corpus(manifest)


def test_corpus_case_cannot_escape_corpus_root(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = _write_source(tmp_path / "outside.xst")
    manifest = _write_manifest(
        corpus,
        [
            {
                "id": "escape",
                "path": "../outside.xst",
                "source_kind": "SYNTHETIC",
                "origin": "INTERNAL",
                "controller_family": "M340",
                "families": ["M340"],
            }
        ],
    )

    assert outside.exists()
    with pytest.raises(SchneiderProductionCorpusError, match="escapes corpus root"):
        qualify_schneider_production_corpus(manifest)
