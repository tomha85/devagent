from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

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
from devagent.plc.models import (
    CanonicalPLCProject,
    PLCEngineeringResult,
    PLCBooleanTerm,
    PLCDependencyGraph,
    PLCLogicPath,
    PLCLogicStatement,
    PLCOutcome,
    PLCOutputLogic,
    PLCProjectMetadata,
    PLCSourceRef,
    PLCSemanticState,
    PLCTag,
)
from devagent.providers import ScriptedFakeProvider


def _engineering(*, multiple_writers: bool = False, include_rule: bool = True):
    metadata = PLCProjectMetadata(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        source_path="/tmp/warehouse.L5X",
        source_sha256="a" * 64,
        schema_revision=None,
        software_revision=None,
        target_type="Controller",
        controller_name="WarehousePLC",
        processor_type="1756-L85E",
        major_revision="36",
        minor_revision="11",
        full_project=True,
    )
    tags = [
        PLCTag("tag-start", "StartCmd", "Controller", "BOOL"),
        PLCTag("tag-auto", "AutoMode", "Controller", "BOOL"),
        PLCTag("tag-safe", "SafetyOK", "Controller", "BOOL"),
        PLCTag("tag-ready", "DownstreamReady", "Controller", "BOOL"),
        PLCTag("tag-fault", "DriveFault", "Controller", "BOOL"),
        PLCTag("tag-run", "Conveyor7_Run", "Controller", "BOOL"),
    ]
    source = PLCSourceRef(
        artifact="warehouse.L5X",
        controller="WarehousePLC",
        program="Conveyors",
        routine="CV7",
        rung="12",
    )
    paths = (
        PLCLogicPath(
            terms=(
                PLCBooleanTerm("StartCmd", True),
                PLCBooleanTerm("AutoMode", True),
                PLCBooleanTerm("SafetyOK", True),
                PLCBooleanTerm("DownstreamReady", True),
                PLCBooleanTerm("DriveFault", False),
            )
        ),
    )
    rules = []
    if include_rule:
        rules.append(
            PLCOutputLogic(
                id="logic-cv7",
                output_tag="Conveyor7_Run",
                instruction="OTE",
                paths=paths,
                source=source,
                semantic_state=PLCSemanticState.FULL,
            )
        )
        if multiple_writers:
            rules.append(
                PLCOutputLogic(
                    id="logic-cv7-second",
                    output_tag="Conveyor7_Run",
                    instruction="OTE",
                    paths=paths,
                    source=source,
                    semantic_state=PLCSemanticState.FULL,
                )
            )
    statements = [
        PLCLogicStatement(
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
            semantic_state=PLCSemanticState.FULL,
            source=source,
        )
    ]
    project = CanonicalPLCProject(
        metadata=metadata,
        tags=tags,
        logic_statements=statements,
        output_logic=rules,
    )
    return PLCEngineeringResult(
        outcome=PLCOutcome.STATICALLY_VERIFIED,
        project=project,
        graph=PLCDependencyGraph(),
        fat_tests=[],
        static_checks=[],
        limitations=[],
    )


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


def test_live_context_is_a_copy_adapter_over_existing_plc_project(tmp_path):
    engineering = _engineering()
    seen = []

    def loader(path: Path):
        seen.append(path)
        return engineering

    loaded = load_live_engineering_context(
        tmp_path / "warehouse.L5X",
        project_loader=loader,
    )

    assert seen
    assert loaded.project is engineering.project
    assert loaded.context.vendor == "ROCKWELL"
    assert loaded.context.controller_name == "WarehousePLC"
    assert len(loaded.context.tags) == 6
    assert len(loaded.context.rules) == 1
    assert loaded.context.rules[0].source_locator.endswith("Rung 12")
    assert loaded.context.rules[0].evidence_id.startswith("LIVE-ENG-LOGIC:")


def test_natural_language_target_resolves_engineering_output():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(),
    ).context

    target = resolve_question_target(context, "Why is Conveyor 7 not running?")

    assert target.status is None
    assert target.output_tag == "Conveyor7_Run"


def test_diagnosis_identifies_current_blocking_permissive():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(),
    ).context

    diagnosis = diagnose_output(
        context,
        "Conveyor7_Run",
        _observations(auto=False, output=False),
    )

    assert diagnosis.status is LiveDiagnosisStatus.BLOCKER_IDENTIFIED
    assert diagnosis.expected_output is False
    assert diagnosis.observed_output is False
    assert [item.tag_name for item in diagnosis.blockers] == ["AutoMode"]
    assert "AutoMode" in diagnosis.summary


def test_conditions_true_but_output_false_is_conflict_not_fake_blocker():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(),
    ).context

    diagnosis = diagnose_output(
        context,
        "Conveyor7_Run",
        _observations(auto=True, output=False),
    )

    assert diagnosis.status is LiveDiagnosisStatus.LOGIC_CONFLICT
    assert diagnosis.expected_output is True
    assert diagnosis.observed_output is False
    assert diagnosis.blockers == ()


def test_untrusted_runtime_value_cannot_drive_definitive_diagnosis():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(),
    ).context
    observations = list(_observations(auto=True, output=False))
    observations[1] = _obs("AutoMode", "tag-auto", True, "E-auto-bad", current=False)

    diagnosis = diagnose_output(context, "Conveyor7_Run", observations)

    assert diagnosis.status is LiveDiagnosisStatus.INDETERMINATE
    auto = [
        item
        for path in diagnosis.paths
        for item in path.conditions
        if item.tag_name == "AutoMode"
    ][0]
    assert auto.observed_value is None


def test_multiple_writers_fail_closed():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(multiple_writers=True),
    ).context

    diagnosis = diagnose_output(context, "Conveyor7_Run", _observations())

    assert diagnosis.status is LiveDiagnosisStatus.INDETERMINATE
    assert len(diagnosis.rule_ids) == 2
    assert any("writers" in item.casefold() for item in diagnosis.limitations)


def test_source_statement_is_context_only_when_boolean_rule_missing():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(include_rule=False),
    ).context

    diagnosis = diagnose_output(context, "Conveyor7_Run", _observations())

    assert diagnosis.status is LiveDiagnosisStatus.NO_EVALUABLE_RULE
    assert diagnosis.source_locators
    assert "cannot deterministically evaluate" in diagnosis.summary


def test_ai_explanation_is_bounded_by_deterministic_evidence():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(),
    ).context
    diagnosis = diagnose_output(context, "Conveyor7_Run", _observations())
    provider = ScriptedFakeProvider(
        [
            {
                "answer": "AutoMode is the current modeled blocking condition.",
                "confidence": 1.0,
                "evidence_ids": list(diagnosis.evidence_ids),
                "next_checks": ["Inspect why AutoMode is currently false."],
                "limitations": [],
            }
        ]
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
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(),
    ).context
    diagnosis = diagnose_output(context, "Conveyor7_Run", _observations())
    provider = ScriptedFakeProvider(
        [
            {
                "answer": "Invented diagnosis.",
                "confidence": 0.9,
                "evidence_ids": ["NOT-REAL"],
                "next_checks": [],
                "limitations": [],
            }
        ]
    )

    answer = answer_commissioning_question("why", diagnosis, provider=provider)

    assert answer.ai_assisted is False
    assert answer.answer == diagnosis.summary
    assert any("outside" in item.casefold() for item in answer.limitations)


def test_ai_write_or_force_advice_is_rejected():
    context = load_live_engineering_context(
        Path("/tmp/warehouse.L5X"),
        project_loader=lambda _path: _engineering(),
    ).context
    diagnosis = diagnose_output(context, "Conveyor7_Run", _observations())
    provider = ScriptedFakeProvider(
        [
            {
                "answer": "Force the tag AutoMode to true.",
                "confidence": 0.9,
                "evidence_ids": list(diagnosis.evidence_ids),
                "next_checks": [],
                "limitations": [],
            }
        ]
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
            "ns=2;s=StartCmd": True,
            "ns=2;s=AutoMode": False,
            "ns=2;s=SafetyOK": True,
            "ns=2;s=DownstreamReady": True,
            "ns=2;s=DriveFault": False,
            "ns=2;s=Conveyor7_Run": False,
        }
        self._nodes = tuple(
            BrowseNode(
                path=f"Objects/Line1/{name}",
                node_id=node_id,
                browse_name=name,
                display_name=name,
                node_class="Variable",
                data_type="Boolean",
                user_access=("CurrentRead",),
                readable=True,
                writable=False,
            )
            for node_id, name in (
                ("ns=2;s=StartCmd", "StartCmd"),
                ("ns=2;s=AutoMode", "AutoMode"),
                ("ns=2;s=SafetyOK", "SafetyOK"),
                ("ns=2;s=DownstreamReady", "DownstreamReady"),
                ("ns=2;s=DriveFault", "DriveFault"),
                ("ns=2;s=Conveyor7_Run", "Conveyor7_Run"),
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
            security_summary="None/None",
            successful_connections=1 if self._state is PlcSessionState.CONNECTED else 0,
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

    async def browse(self, plc_id, *, max_depth=4, max_nodes=500):
        assert plc_id == "plc1"
        return self._nodes[:max_nodes]

    async def read_many(self, node_ids_by_plc):
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
            for node_id in node_ids_by_plc["plc1"]
        )
        return {
            "plc1": PlcReadResult(
                plc_id="plc1",
                values=values,
                state=PlcSessionState.CONNECTED,
            )
        }


@pytest.mark.asyncio
async def test_assistant_combines_plc_logic_and_trusted_opcua_values_without_plc_control():
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
