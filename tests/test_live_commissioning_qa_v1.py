from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from devagent.live import (
    LiveCommissioningAssistant,
    LiveDiagnosisStatus,
    LiveObservedTag,
    LiveSecurityConfig,
    PlcConnectionSpec,
    diagnose_output,
    load_live_engineering_context,
    resolve_question_target,
)
from devagent.live.manager import ManagedPlcStatus, PlcReadResult, PlcSessionState
from devagent.live.models import BrowseNode, Quality, RuntimeValue
from devagent.live.qa import answer_commissioning_question
from devagent.providers import ScriptedFakeProvider


def _engineering(*, multiple_writers: bool = False, include_rule: bool = True):
    metadata = SimpleNamespace(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        source_path="/tmp/warehouse.L5X",
        source_sha256="a" * 64,
        controller_name="WarehousePLC",
        full_project=True,
    )
    tags = [
        SimpleNamespace(id=tag_id, name=name, scope="Controller", data_type="BOOL",
                        description=None, external_access=None, alias_for=None)
        for tag_id, name in (
            ("tag-start", "StartCmd"),
            ("tag-auto", "AutoMode"),
            ("tag-safe", "SafetyOK"),
            ("tag-ready", "DownstreamReady"),
            ("tag-fault", "DriveFault"),
            ("tag-run", "Conveyor7_Run"),
        )
    ]
    source = SimpleNamespace(locator="WarehousePLC / Conveyors / CV7 / Rung 12")
    path = SimpleNamespace(
        terms=tuple(
            SimpleNamespace(tag=name, required=required)
            for name, required in (
                ("StartCmd", True),
                ("AutoMode", True),
                ("SafetyOK", True),
                ("DownstreamReady", True),
                ("DriveFault", False),
            )
        )
    )
    rules = []
    if include_rule:
        rules.append(
            SimpleNamespace(
                id="logic-cv7",
                output_tag="Conveyor7_Run",
                instruction="OTE",
                paths=(path,),
                source=source,
                language="RLL",
                origin="RUNG",
                semantic_state=SimpleNamespace(value="FULL"),
            )
        )
        if multiple_writers:
            rules.append(
                SimpleNamespace(
                    id="logic-cv7-second",
                    output_tag="Conveyor7_Run",
                    instruction="OTE",
                    paths=(path,),
                    source=source,
                    language="RLL",
                    origin="RUNG",
                    semantic_state=SimpleNamespace(value="FULL"),
                )
            )
    statements = [
        SimpleNamespace(
            id="stmt-cv7",
            language="ST",
            owner_type="PROGRAM",
            owner_name="Conveyors",
            routine="CV7",
            locator="CV7:20",
            text="Conveyor7_Run := StartCmd AND AutoMode;",
            reads=("StartCmd", "AutoMode"),
            writes=("Conveyor7_Run",),
            calls=(),
            semantic_state=SimpleNamespace(value="FULL"),
            source=source,
        )
    ]
    project = SimpleNamespace(
        metadata=metadata,
        tags=tags,
        output_logic=rules,
        logic_statements=statements,
        warnings=[],
    )
    return SimpleNamespace(project=project)


def _context(**kwargs):
    return load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(**kwargs),
    ).context


def _obs(name: str, tag_id: str, value, evidence: str, *, current: bool = True):
    return LiveObservedTag(
        tag_id=tag_id,
        tag_name=name,
        node_id=f"ns=2;s={name}",
        value=value if current else None,
        evidence_id=evidence,
        definitive_current=current,
        mapping_status="AUTO_BOUND",
        limitation=None if current else "Runtime value was not trusted as CURRENT.",
    )


def _observations(*, auto: bool = False, output: bool = False):
    return (
        _obs("StartCmd", "tag-start", True, "E-start"),
        _obs("AutoMode", "tag-auto", auto, "E-auto"),
        _obs("SafetyOK", "tag-safe", True, "E-safe"),
        _obs("DownstreamReady", "tag-ready", True, "E-ready"),
        _obs("DriveFault", "tag-fault", False, "E-fault"),
        _obs("Conveyor7_Run", "tag-run", output, "E-run"),
    )


def test_live_context_consumes_existing_plc_model_without_mutating_it():
    engineering = _engineering()
    original_rules = tuple(engineering.project.output_logic)
    loaded = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: engineering,
    )

    assert loaded.project is engineering.project
    assert tuple(engineering.project.output_logic) == original_rules
    assert loaded.context.vendor == "ROCKWELL"
    assert loaded.context.controller_name == "WarehousePLC"
    assert len(loaded.context.tags) == 6
    assert len(loaded.context.rules) == 1
    assert loaded.context.rules[0].source_locator.endswith("Rung 12")
    assert loaded.context.rules[0].evidence_id.startswith("LIVE-ENG-LOGIC:")


def test_natural_language_question_resolves_output_signal():
    target = resolve_question_target(_context(), "Why is Conveyor 7 not running?")
    assert target.status is None
    assert target.output_tag == "Conveyor7_Run"


def test_diagnosis_identifies_current_blocking_permissive():
    diagnosis = diagnose_output(
        _context(),
        "Conveyor7_Run",
        _observations(auto=False, output=False),
    )
    assert diagnosis.status is LiveDiagnosisStatus.BLOCKER_IDENTIFIED
    assert diagnosis.expected_output is False
    assert diagnosis.observed_output is False
    assert [item.tag_name for item in diagnosis.blockers] == ["AutoMode"]


def test_all_conditions_true_but_output_false_is_conflict_not_fake_cause():
    diagnosis = diagnose_output(
        _context(),
        "Conveyor7_Run",
        _observations(auto=True, output=False),
    )
    assert diagnosis.status is LiveDiagnosisStatus.LOGIC_CONFLICT
    assert diagnosis.expected_output is True
    assert diagnosis.observed_output is False
    assert diagnosis.blockers == ()


def test_untrusted_value_cannot_drive_definitive_diagnosis():
    observations = list(_observations(auto=True, output=False))
    observations[1] = _obs("AutoMode", "tag-auto", True, "E-auto-bad", current=False)

    diagnosis = diagnose_output(_context(), "Conveyor7_Run", observations)

    assert diagnosis.status is LiveDiagnosisStatus.INDETERMINATE
    auto = [
        item
        for path in diagnosis.paths
        for item in path.conditions
        if item.tag_name == "AutoMode"
    ][0]
    assert auto.observed_value is None


def test_multiple_writers_fail_closed():
    diagnosis = diagnose_output(
        _context(multiple_writers=True),
        "Conveyor7_Run",
        _observations(),
    )
    assert diagnosis.status is LiveDiagnosisStatus.INDETERMINATE
    assert len(diagnosis.rule_ids) == 2
    assert any("writers" in item.casefold() for item in diagnosis.limitations)


def test_statement_without_evaluable_rule_is_context_not_proof():
    diagnosis = diagnose_output(
        _context(include_rule=False),
        "Conveyor7_Run",
        _observations(),
    )
    assert diagnosis.status is LiveDiagnosisStatus.NO_EVALUABLE_RULE
    assert diagnosis.source_locators
    assert "cannot deterministically evaluate" in diagnosis.summary


def test_ai_explanation_cannot_raise_confidence_above_deterministic_result():
    diagnosis = diagnose_output(_context(), "Conveyor7_Run", _observations())
    provider = ScriptedFakeProvider(
        [{
            "answer": "AutoMode is the current modeled blocking condition.",
            "confidence": 1.0,
            "evidence_ids": list(diagnosis.evidence_ids),
            "next_checks": ["Inspect why AutoMode is currently false."],
            "limitations": [],
        }]
    )

    answer = answer_commissioning_question(
        "Why is Conveyor 7 not running?",
        diagnosis,
        provider=provider,
    )

    assert answer.ai_assisted is True
    assert answer.confidence <= 0.95
    assert set(answer.evidence_ids) <= set(diagnosis.evidence_ids)


def test_ai_invented_evidence_is_rejected():
    diagnosis = diagnose_output(_context(), "Conveyor7_Run", _observations())
    provider = ScriptedFakeProvider(
        [{
            "answer": "Invented diagnosis.",
            "confidence": 0.9,
            "evidence_ids": ["NOT-REAL"],
            "next_checks": [],
            "limitations": [],
        }]
    )

    answer = answer_commissioning_question("why", diagnosis, provider=provider)

    assert answer.ai_assisted is False
    assert answer.answer == diagnosis.summary
    assert any("outside" in item.casefold() for item in answer.limitations)


def test_ai_write_force_or_bypass_advice_is_rejected():
    diagnosis = diagnose_output(_context(), "Conveyor7_Run", _observations())
    provider = ScriptedFakeProvider(
        [{
            "answer": "Force the tag AutoMode to true.",
            "confidence": 0.9,
            "evidence_ids": list(diagnosis.evidence_ids),
            "next_checks": [],
            "limitations": [],
        }]
    )

    answer = answer_commissioning_question("why", diagnosis, provider=provider)

    assert answer.ai_assisted is False
    assert "Force the tag" not in answer.answer
    assert any("read-only scope" in item for item in answer.limitations)


class _FakeManager:
    def __init__(self):
        self.plc_ids = ("plc1",)
        self._state = PlcSessionState.DISCONNECTED
        self._values = {
            f"ns=2;s={name}": value
            for name, value in (
                ("StartCmd", True),
                ("AutoMode", False),
                ("SafetyOK", True),
                ("DownstreamReady", True),
                ("DriveFault", False),
                ("Conveyor7_Run", False),
            )
        }
        self._nodes = tuple(
            BrowseNode(
                path=f"Objects/Line1/{name}",
                node_id=f"ns=2;s={name}",
                browse_name=name,
                display_name=name,
                node_class="Variable",
                data_type="Boolean",
                user_access=("CurrentRead",),
                readable=True,
                writable=False,
            )
            for name in (
                "StartCmd",
                "AutoMode",
                "SafetyOK",
                "DownstreamReady",
                "DriveFault",
                "Conveyor7_Run",
            )
        )

    def status(self, plc_id):
        assert plc_id == "plc1"
        return ManagedPlcStatus(
            plc_id="plc1",
            plc_name="WarehousePLC",
            endpoint="opc.tcp://127.0.0.1:4840/",
            state=self._state,
            connected=self._state is PlcSessionState.CONNECTED,
            authentication_mode="ANONYMOUS",
            security_summary="NONE",
            successful_connections=int(self._state is PlcSessionState.CONNECTED),
            last_error=None,
            changed_at=datetime.now(timezone.utc),
        )

    def statuses(self):
        return {"plc1": self.status("plc1")}

    async def connect(self, plc_id):
        self._state = PlcSessionState.CONNECTED
        return self.status(plc_id)

    async def disconnect(self, plc_id):
        self._state = PlcSessionState.DISCONNECTED
        return self.status(plc_id)

    async def browse(self, plc_id, *, max_depth, max_nodes):
        assert plc_id == "plc1"
        return self._nodes[:max_nodes]

    async def read_many(self, requests):
        now = datetime.now(timezone.utc)
        values = tuple(
            RuntimeValue(
                node_id=node_id,
                value=self._values[node_id],
                variant_type="Boolean",
                status_code="Good",
                quality=Quality.GOOD,
                source_timestamp=now,
                server_timestamp=now,
                received_at=now,
                age_seconds=0.0,
                stale=False,
            )
            for node_id in requests["plc1"]
        )
        return {
            "plc1": PlcReadResult(
                plc_id="plc1",
                values=values,
                state=PlcSessionState.CONNECTED,
            )
        }


def test_assistant_combines_plc_logic_and_trusted_opcua_values_read_only():
    async def scenario():
        loaded = load_live_engineering_context(
            Path("/tmp/warehouse.L5X"),
            project_loader=lambda _path: _engineering(),
        )
        manager = _FakeManager()
        assistant = LiveCommissioningAssistant(
            loaded,
            PlcConnectionSpec(
                plc_id="plc1",
                plc_name="WarehousePLC",
                endpoint="opc.tcp://127.0.0.1:4840/",
                security=LiveSecurityConfig(),
            ),
            manager=manager,
        )

        reply = await assistant.answer("Why is Conveyor 7 not running?")
        assert reply.diagnosis is not None
        assert reply.diagnosis.status is LiveDiagnosisStatus.BLOCKER_IDENTIFIED
        assert [item.tag_name for item in reply.diagnosis.blockers] == ["AutoMode"]
        assert "AutoMode" in reply.render_text()
        assert not hasattr(assistant.manager, "write")
        assert not hasattr(assistant.manager, "force")

        overview = await assistant.answer("What is this system?")
        assert "WarehousePLC" in overview.render_text()
        assert "Mode: READ ONLY" in overview.render_text()

        await assistant.close()

    asyncio.run(scenario())
