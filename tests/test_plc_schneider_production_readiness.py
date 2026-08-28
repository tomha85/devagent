from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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


def _xsf() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<SFCExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="ProductionCorpusSFC" version="1.0" />
  <program>
    <identProgram name="Sequence" type="section" task="MAST" />
    <SFCSource><step name="S0" /></SFCSource>
  </program>
</SFCExchangeFile>
'''


def _write_source(path: Path, payload: str | None = None) -> Path:
    path.write_text(payload if payload is not None else _xst(), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_payload(cases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "devagent-schneider-production-corpus-v1",
        "corpus_id": "schneider-production-test",
        "cases": cases,
    }


def _write_manifest(root: Path, cases: list[dict[str, object]]) -> Path:
    path = root / "corpus.json"
    path.write_text(json.dumps(_manifest_payload(cases), indent=2) + "\n", encoding="utf-8")
    return path


def _write_signed_manifest(root: Path, cases: list[dict[str, object]]) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    payload = _manifest_payload(cases)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    payload["signature"] = {
        "algorithm": "ED25519",
        "key_id": "schneider-corpus-test",
        "value_base64": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }
    manifest = root / "corpus.json"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust = root / "trust.json"
    trust.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "test-engineering-authority",
                "approved_at": "2026-08-28T12:00:00-04:00",
                "signers": [
                    {
                        "id": "schneider-corpus-test",
                        "algorithm": "ED25519",
                        "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                        "purposes": ["SCHNEIDER_PRODUCTION_CORPUS"],
                        "status": "TRUSTED",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, trust


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


def test_signed_real_hash_pinned_m340_passes_but_corpus_stays_pending(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "M340.xst")
    manifest, trust = _write_signed_manifest(tmp_path, [_real_case(source)])

    result = qualify_schneider_production_corpus(manifest, trust_store_path=trust)

    assert result.status == "PENDING_REAL_EXPORT_CORPUS"
    assert result.commercial_static_ready is False
    assert result.manifest_signature_verified is True
    assert result.manifest_signer_key_id == "schneider-corpus-test"
    assert result.real_export_cases == 1
    assert result.distinct_real_bundle_hashes == 1
    assert "M340" in result.covered_real_families
    assert "M580" in result.missing_real_families
    assert result.cases[0].status == "PASS"
    assert result.cases[0].engineering_outcome == "STATICALLY_VERIFIED"
    assert result.cases[0].support_contract == "FULL"
    assert result.cases[0].hash_matches is True
    assert result.cases[0].deterministic is True
    assert result.cases[0].accounting_complete is True
    assert result.cases[0].audit_clean is True


def test_unsigned_real_export_manifest_is_rejected_before_qualification(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "Unsigned.xst")
    manifest = _write_manifest(tmp_path, [_real_case(source)])

    with pytest.raises(SchneiderProductionCorpusError, match="requires an Ed25519 signature"):
        qualify_schneider_production_corpus(manifest)


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
    assert result.manifest_signature_verified is False
    assert result.real_export_cases == 0
    assert result.covered_real_families == ()
    assert "M340" in result.missing_real_families
    assert result.status == "PENDING_REAL_EXPORT_CORPUS"


def test_real_export_without_hash_and_attestation_fails_closed(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "Unattested.xst")
    case = _real_case(source)
    case.pop("bundle_sha256")
    case.pop("attested_by")
    manifest, trust = _write_signed_manifest(tmp_path, [case])

    result = qualify_schneider_production_corpus(manifest, trust_store_path=trust)

    assert result.status == "FAIL"
    assert result.cases[0].status == "FAIL"
    assert result.cases[0].hash_matches is False
    assert any("bundle_sha256" in item for item in result.cases[0].findings)
    assert any("attested_by" in item for item in result.cases[0].findings)


def test_non_full_real_export_cannot_satisfy_production_corpus(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "Sequence.xsf", _xsf())
    manifest, trust = _write_signed_manifest(tmp_path, [_real_case(source)])

    result = qualify_schneider_production_corpus(manifest, trust_store_path=trust)

    assert result.status == "FAIL"
    assert result.cases[0].status == "FAIL"
    assert result.cases[0].support_contract == "PARTIAL_FAIL_CLOSED"
    assert result.cases[0].support_opaque >= 1
    assert any("requires FULL" in item for item in result.cases[0].findings)


def test_declared_mixed_language_family_must_be_observed_in_export(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "OnlyST.xst")
    case = _real_case(
        source,
        id="false-mixed",
        controller_family=None,
        families=["MIXED_ST_LD_FBD"],
    )
    manifest, trust = _write_signed_manifest(tmp_path, [case])

    result = qualify_schneider_production_corpus(manifest, trust_store_path=trust)

    assert result.status == "FAIL"
    assert result.cases[0].status == "FAIL"
    assert "MIXED_ST_LD_FBD" not in result.cases[0].observed_families
    assert any("not substantiated" in item for item in result.cases[0].findings)


def test_controller_family_must_match_declared_hardware_family(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "Mismatch.xst")
    case = _real_case(source, controller_family="M580", families=["M340"])
    manifest, trust = _write_signed_manifest(tmp_path, [case])

    with pytest.raises(SchneiderProductionCorpusError, match="must match hardware family"):
        qualify_schneider_production_corpus(manifest, trust_store_path=trust)


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
