from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

from devagent.live.agent_integration import (
    LiveAgentEvidencePack,
    LiveEvidenceDisposition,
    LiveEvidenceRecord,
)
from devagent.live.production_integration import (
    augment_production_result,
    build_live_customer_evidence_artifact,
    render_live_augmented_production_report,
    render_live_commissioning_section,
    summarize_live_production_evidence,
    write_live_production_artifacts,
)

NOW = datetime(2026, 8, 29, 2, 15, tzinfo=timezone.utc)


def _record(
    evidence_id: str,
    plc_id: str,
    node_id: str,
    value,
    *,
    disposition: LiveEvidenceDisposition,
    quality: str,
    trust: str,
    agent_eligible: bool,
    replayed: bool = False,
) -> LiveEvidenceRecord:
    return LiveEvidenceRecord(
        evidence_id=evidence_id,
        plc_id=plc_id,
        plc_name=plc_id.upper(),
        node_id=node_id,
        disposition=disposition,
        quality=quality,
        trust=trust,
        value=value,
        variant_type="String",
        status_code="Good" if quality == "GOOD" else quality,
        source_timestamp=NOW,
        server_timestamp=NOW,
        received_at=NOW,
        age_seconds=0.1 if agent_eligible else 99.0,
        replayed=replayed,
        agent_eligible=agent_eligible,
        definitive_current=agent_eligible,
    )


def _pack() -> LiveAgentEvidencePack:
    current = _record(
        "LIVE:a:current",
        "a",
        "ns=2;s=MachineState",
        "RUNNING",
        disposition=LiveEvidenceDisposition.CURRENT,
        quality="GOOD",
        trust="CURRENT",
        agent_eligible=True,
    )
    stale = _record(
        "LIVE:b:stale",
        "b",
        "ns=2;s=SecretStaleValue",
        "SECRET-STALE-RAW",
        disposition=LiveEvidenceDisposition.STALE,
        quality="GOOD",
        trust="STALE",
        agent_eligible=False,
    )
    return LiveAgentEvidencePack(
        pack_id="LIVE-PACK:test",
        captured_at=NOW,
        records=(current, stale),
        evidence=(),
        agent_evidence_ids=frozenset({current.evidence_id, "LIVE-LIMIT:b:test"}),
        definitive_current_evidence_ids=frozenset({current.evidence_id}),
        excluded_raw_evidence_ids=frozenset({stale.evidence_id}),
        limitations=("b:ns=2;s=SecretStaleValue: excluded STALE live value",),
        plc_states={"a": "CONNECTED", "b": "DEGRADED"},
    )


def _production_result(readiness_status: str = "CONDITIONALLY_READY"):
    readiness = SimpleNamespace(
        status=SimpleNamespace(value=readiness_status),
        score=80,
    )
    metadata = SimpleNamespace(source_sha256="project-sha")
    engineering = SimpleNamespace(project=SimpleNamespace(metadata=metadata))
    return SimpleNamespace(readiness=readiness, engineering=engineering)


def _install_fake_renderer(monkeypatch, text: str) -> None:
    module = ModuleType("devagent.plc.production_report")
    module.render_production_report = lambda result: text
    monkeypatch.setitem(sys.modules, "devagent.plc.production_report", module)


def test_summary_counts_current_excluded_and_plc_health() -> None:
    summary = summarize_live_production_evidence(_pack())
    assert summary.total_records == 2
    assert summary.current_records == 1
    assert summary.excluded_raw_records == 1
    assert summary.limitation_count == 1
    assert summary.all_plcs_connected is False
    assert summary.has_definitive_current_evidence is True


def test_augmented_result_preserves_authoritative_readiness_identity() -> None:
    result = _production_result()
    augmented = augment_production_result(result, _pack())
    assert augmented.production_result is result
    assert augmented.readiness is result.readiness
    assert result.readiness.status.value == "CONDITIONALLY_READY"


def test_customer_artifact_includes_current_but_not_excluded_raw_value() -> None:
    artifact = build_live_customer_evidence_artifact(_pack())
    rendered = json.dumps(artifact, sort_keys=True)
    assert "RUNNING" in rendered
    assert "SECRET-STALE-RAW" not in rendered
    assert artifact["excluded_observations"][0]["raw_value_included"] is False
    assert "value" not in artifact["excluded_observations"][0]


def test_customer_artifact_declares_no_readiness_effect_and_read_only_mode() -> None:
    artifact = build_live_customer_evidence_artifact(_pack())
    assert artifact["mode"] == "READ_ONLY"
    assert artifact["effect_on_release_readiness"] == "NONE"
    assert "do not by themselves" in artifact["verification_boundary"]


def test_markdown_shows_current_value_but_withholds_rejected_raw_value() -> None:
    section = render_live_commissioning_section(_pack())
    assert "RUNNING" in section
    assert "SECRET-STALE-RAW" not in section
    assert "Raw value shown" in section
    assert "STALE" in section
    assert "does not change the deterministic FAT execution status" in section


def test_markdown_records_read_only_verification_boundary() -> None:
    section = render_live_commissioning_section(_pack())
    assert "GOOD + CURRENT + non-stale + non-replayed" in section
    assert "not qualified FAT execution PASS evidence" in section
    assert "adds no PLC write, force, reset, download, mode-change, or method-call path" in section


def test_augmented_report_inserts_live_section_before_release_readiness(monkeypatch) -> None:
    _install_fake_renderer(
        monkeypatch,
        "# Base Report\n\n## Recommendations\n\nBase recommendation.\n\n## Release Readiness\n\n**Status: CONDITIONALLY_READY**\n",
    )
    report = render_live_augmented_production_report(_production_result(), _pack())
    assert report.index("## Live Commissioning Evidence") < report.index("## Release Readiness")
    assert "**Status: CONDITIONALLY_READY**" in report
    assert "SECRET-STALE-RAW" not in report


def test_augmented_report_falls_back_to_append_when_marker_missing(monkeypatch) -> None:
    _install_fake_renderer(monkeypatch, "# Base Report\n")
    report = render_live_augmented_production_report(_production_result(), _pack())
    assert report.startswith("# Base Report")
    assert report.rstrip().endswith(
        "This integration remains read-only and adds no PLC write, force, reset, download, mode-change, or method-call path."
    )


def test_write_artifacts_are_sanitized_and_manifest_preserves_readiness(monkeypatch, tmp_path) -> None:
    _install_fake_renderer(
        monkeypatch,
        "# Base Report\n\n## Release Readiness\n\n**Status: CONDITIONALLY_READY**\n",
    )
    result = _production_result("CONDITIONALLY_READY")
    artifacts = write_live_production_artifacts(tmp_path, result, _pack())

    report_text = artifacts.report_path.read_text(encoding="utf-8")
    evidence_text = artifacts.evidence_path.read_text(encoding="utf-8")
    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))

    assert "SECRET-STALE-RAW" not in report_text
    assert "SECRET-STALE-RAW" not in evidence_text
    assert manifest["production_readiness"] == "CONDITIONALLY_READY"
    assert manifest["production_readiness_modified_by_live_evidence"] is False
    assert manifest["project_sha256"] == "project-sha"
    assert artifacts.report_sha256 == manifest["artifacts"][artifacts.report_path.name]
    assert artifacts.evidence_sha256 == manifest["artifacts"][artifacts.evidence_path.name]


def test_artifact_hashes_change_when_current_observation_changes(monkeypatch, tmp_path) -> None:
    _install_fake_renderer(
        monkeypatch,
        "# Base Report\n\n## Release Readiness\n\n**Status: CONDITIONALLY_READY**\n",
    )
    result = _production_result()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = write_live_production_artifacts(first_dir, result, _pack())

    pack = _pack()
    changed = _record(
        "LIVE:a:changed",
        "a",
        "ns=2;s=MachineState",
        "STOPPED",
        disposition=LiveEvidenceDisposition.CURRENT,
        quality="GOOD",
        trust="CURRENT",
        agent_eligible=True,
    )
    changed_pack = LiveAgentEvidencePack(
        pack_id="LIVE-PACK:changed",
        captured_at=pack.captured_at,
        records=(changed, pack.records[1]),
        evidence=(),
        agent_evidence_ids=frozenset({changed.evidence_id}),
        definitive_current_evidence_ids=frozenset({changed.evidence_id}),
        excluded_raw_evidence_ids=pack.excluded_raw_evidence_ids,
        limitations=pack.limitations,
        plc_states=pack.plc_states,
    )
    second = write_live_production_artifacts(second_dir, result, changed_pack)
    assert first.evidence_sha256 != second.evidence_sha256
    assert first.report_sha256 != second.report_sha256


def test_sidecar_public_surface_has_no_control_operations() -> None:
    augmented = augment_production_result(_production_result(), _pack())
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
        "download",
        "change_mode",
    ):
        assert not hasattr(augmented, prohibited)
