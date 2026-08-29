from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from devagent.live.agent_integration import (
    LiveDataTrustLayer,
    LiveEvidenceDisposition,
    LiveEvidenceStore,
    build_live_agent_evidence_pack,
    run_live_augmented_ai_review,
    run_live_augmented_requirement_mapping,
)
from devagent.live.manager import ManagedPlcStatus, PlcReadResult, PlcSessionState
from devagent.live.models import Quality, RuntimeValue
from devagent.plc.production_models import EvidenceItem

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _value(
    node_id: str,
    value=1,
    *,
    quality: Quality = Quality.GOOD,
    stale: bool = False,
    replayed: bool = False,
) -> RuntimeValue:
    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type="Int32",
        status_code="Good" if quality is Quality.GOOD else quality.value,
        quality=quality,
        source_timestamp=NOW,
        server_timestamp=NOW,
        received_at=NOW,
        age_seconds=99.0 if stale else 0.0,
        stale=stale,
        replayed=replayed,
    )


class _FakeManager:
    def __init__(self, results, states=None):
        self.results = results
        self.states = states or {
            plc_id: PlcSessionState.CONNECTED
            for plc_id in results
        }
        self.requests = None

    def statuses(self):
        return {
            plc_id: ManagedPlcStatus(
                plc_id=plc_id,
                plc_name=plc_id.upper(),
                endpoint=f"opc.tcp://{plc_id}:4840/",
                state=self.states[plc_id],
                connected=self.states[plc_id] is PlcSessionState.CONNECTED,
                authentication_mode="ANONYMOUS",
                security_summary="NONE",
                successful_connections=1,
                last_error=None,
                changed_at=NOW,
            )
            for plc_id in self.results
        }

    async def read_many(self, requests):
        self.requests = requests
        return self.results


def test_trust_layer_admits_only_good_current_nonreplayed() -> None:
    layer = LiveDataTrustLayer()
    assert layer.classify(_value("n1", 1)) is LiveEvidenceDisposition.CURRENT
    assert layer.classify(_value("n1", 1, stale=True)) is LiveEvidenceDisposition.STALE
    assert (
        layer.classify(_value("n1", 1, quality=Quality.UNCERTAIN))
        is LiveEvidenceDisposition.UNCERTAIN
    )
    assert (
        layer.classify(_value("n1", 1, quality=Quality.BAD))
        is LiveEvidenceDisposition.UNTRUSTED
    )
    assert (
        layer.classify(_value("n1", 1, replayed=True))
        is LiveEvidenceDisposition.REPLAYED
    )


def test_current_record_is_agent_eligible_but_not_release_proof() -> None:
    record = LiveDataTrustLayer().record(
        plc_id="plc-a",
        plc_name="Conveyor PLC",
        value=_value("ns=2;s=State", "RUNNING"),
    )
    assert record.agent_eligible is True
    assert record.definitive_current is True
    item = record.as_evidence_item()
    assert item.kind == "LIVE_OPCUA_CURRENT"
    assert "do not by themselves prove FAT PASS" in item.summary


def test_excluded_raw_values_never_enter_agent_evidence() -> None:
    async def scenario() -> None:
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (_value("good", 1),),
                    PlcSessionState.CONNECTED,
                ),
                "b": PlcReadResult(
                    "b",
                    (_value("stale", 999, stale=True),),
                    PlcSessionState.CONNECTED,
                ),
                "c": PlcReadResult(
                    "c",
                    (_value("bad", 777, quality=Quality.BAD),),
                    PlcSessionState.CONNECTED,
                ),
            }
        )
        pack = await build_live_agent_evidence_pack(
            manager,
            {"a": ["good"], "b": ["stale"], "c": ["bad"]},
        )
        agent_items = pack.evidence_for_agent()
        assert any(item.kind == "LIVE_OPCUA_CURRENT" for item in agent_items)
        assert all(item.kind != "LIVE_OPCUA_STALE" for item in agent_items)
        assert all(item.kind != "LIVE_OPCUA_UNTRUSTED" for item in agent_items)
        assert len(pack.excluded_raw_evidence_ids) == 2
        assert len(pack.definitive_current_evidence_ids) == 1

    asyncio.run(scenario())


def test_excluded_value_becomes_limitation_without_raw_value() -> None:
    async def scenario() -> None:
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (_value("stale", 424242, stale=True),),
                    PlcSessionState.CONNECTED,
                )
            }
        )
        pack = await build_live_agent_evidence_pack(manager, {"a": ["stale"]})
        limitations = [
            item for item in pack.evidence_for_agent()
            if item.kind == "LIVE_OPCUA_LIMITATION"
        ]
        assert limitations
        assert all(item.payload["raw_value_included"] is False for item in limitations)
        assert "424242" not in " ".join(item.summary for item in limitations)

    asyncio.run(scenario())


def test_failed_read_creates_generic_limitation_without_error_text() -> None:
    async def scenario() -> None:
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (),
                    PlcSessionState.DEGRADED,
                    "login rejected plc-secret",
                )
            },
            {"a": PlcSessionState.DEGRADED},
        )
        pack = await build_live_agent_evidence_pack(manager, {"a": ["n1"]})
        rendered = " ".join(
            item.summary + repr(item.payload)
            for item in pack.evidence_for_agent()
        )
        assert "plc-secret" not in rendered
        assert "requested live read was unavailable or failed" in rendered
        assert pack.plc_states["a"] == "DEGRADED"

    asyncio.run(scenario())


def test_partial_batch_keeps_current_values_and_marks_missing_nodes() -> None:
    async def scenario() -> None:
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (_value("n1", 10),),
                    PlcSessionState.DEGRADED,
                    "n2 failed",
                )
            },
            {"a": PlcSessionState.DEGRADED},
        )
        pack = await build_live_agent_evidence_pack(
            manager,
            {"a": ["n1", "n2"]},
        )
        assert len(pack.current_records()) == 1
        assert any("a:n2: live read unavailable" == item for item in pack.limitations)

    asyncio.run(scenario())


def test_request_normalization_deduplicates_nodes_and_rejects_blank() -> None:
    async def scenario() -> None:
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (_value("n1", 10),),
                    PlcSessionState.CONNECTED,
                )
            }
        )
        await build_live_agent_evidence_pack(
            manager,
            {"a": ["n1", "n1"]},
        )
        assert manager.requests == {"a": ("n1",)}
        with pytest.raises(ValueError, match="Node id"):
            await build_live_agent_evidence_pack(manager, {"a": [" "]})
        with pytest.raises(ValueError, match="At least one PLC"):
            await build_live_agent_evidence_pack(manager, {})

    asyncio.run(scenario())


def test_evidence_id_binds_snapshot_value_and_metadata() -> None:
    layer = LiveDataTrustLayer()
    first = layer.record(plc_id="a", plc_name="A", value=_value("n1", 1))
    second = layer.record(plc_id="a", plc_name="A", value=_value("n1", 2))
    assert first.evidence_id != second.evidence_id


def test_store_is_bounded_and_lookup_is_by_pack_id() -> None:
    async def scenario() -> None:
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (_value("n1", 1),),
                    PlcSessionState.CONNECTED,
                )
            }
        )
        store = LiveEvidenceStore(max_packs=1)
        first = await build_live_agent_evidence_pack(
            manager,
            {"a": ["n1"]},
            store=store,
        )
        manager.results["a"] = PlcReadResult(
            "a",
            (_value("n1", 2),),
            PlcSessionState.CONNECTED,
        )
        second = await build_live_agent_evidence_pack(
            manager,
            {"a": ["n1"]},
            store=store,
        )
        assert store.latest() is second
        assert store.get(second.pack_id) is second
        assert store.get(first.pack_id) is None
        assert len(store.packs()) == 1

    asyncio.run(scenario())


def test_ai_review_receives_static_current_and_limitations_only(monkeypatch) -> None:
    async def scenario() -> None:
        import devagent.plc.production_ai as production_ai

        captured = {}

        def fake_review(provider, engineering, evidence, deterministic_findings, trace_sink=None):
            captured["evidence"] = evidence
            return ["candidate"], ["warning"]

        monkeypatch.setattr(production_ai, "run_ai_review", fake_review)
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (_value("n1", 1),),
                    PlcSessionState.CONNECTED,
                ),
                "b": PlcReadResult(
                    "b",
                    (_value("n2", 999, stale=True),),
                    PlcSessionState.CONNECTED,
                ),
            }
        )
        static = [EvidenceItem("STATIC:1", "TAG", "static evidence")]
        result = await run_live_augmented_ai_review(
            object(),
            object(),
            static,
            [],
            manager,
            {"a": ["n1"], "b": ["n2"]},
        )
        kinds = [item.kind for item in captured["evidence"]]
        assert result.findings == ("candidate",)
        assert "TAG" in kinds
        assert "LIVE_OPCUA_CURRENT" in kinds
        assert "LIVE_OPCUA_LIMITATION" in kinds
        assert "LIVE_OPCUA_STALE" not in kinds

    asyncio.run(scenario())


def test_requirement_mapping_receives_same_trust_filtered_context(monkeypatch) -> None:
    async def scenario() -> None:
        import devagent.plc.production_ai as production_ai

        captured = {}

        def fake_mapping(provider, requirements, verifications, evidence, trace_sink=None):
            captured["evidence"] = evidence
            return {"REQ-1": "candidate"}, ["warning"]

        monkeypatch.setattr(
            production_ai,
            "run_ai_requirement_mapping",
            fake_mapping,
        )
        manager = _FakeManager(
            {
                "a": PlcReadResult(
                    "a",
                    (_value("n1", 3, replayed=True),),
                    PlcSessionState.CONNECTED,
                )
            }
        )
        result = await run_live_augmented_requirement_mapping(
            object(),
            [],
            [],
            [],
            manager,
            {"a": ["n1"]},
        )
        kinds = [item.kind for item in captured["evidence"]]
        assert result.mappings == {"REQ-1": "candidate"}
        assert "LIVE_OPCUA_REPLAYED" not in kinds
        assert "LIVE_OPCUA_LIMITATION" in kinds

    asyncio.run(scenario())


def test_pack_and_store_public_surfaces_are_read_only() -> None:
    store = LiveEvidenceStore()
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
    ):
        assert not hasattr(store, prohibited)
