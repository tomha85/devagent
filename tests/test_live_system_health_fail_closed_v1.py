from __future__ import annotations

from devagent.live.diagnosis import LiveObservedTag
from devagent.live.engineering_context import (
    LiveEngineeringContext,
    LiveEngineeringTag,
    LiveLogicPath,
    LiveLogicRule,
    LiveLogicTerm,
)
from devagent.live.system_health import (
    LiveSystemHealthStatus,
    build_system_health_scope,
    diagnose_system_health,
)
from devagent.live.tag_reconciliation import (
    LiveTagMapping,
    LiveTagMappingStatus,
    LiveTagReconciliation,
)


def _fixture(*, full_project: bool = True):
    ready = LiveEngineeringTag(
        id="ready",
        name="DownstreamReady",
        scope="Controller",
        data_type="BOOL",
        description=None,
        external_access="Read Only",
        alias_for=None,
    )
    run = LiveEngineeringTag(
        id="run",
        name="RunCmd",
        scope="Controller",
        data_type="BOOL",
        description=None,
        external_access="Read Only",
        alias_for=None,
    )
    rule = LiveLogicRule(
        id="rule-run",
        output_tag="RunCmd",
        instruction="OTE",
        paths=(LiveLogicPath(terms=(LiveLogicTerm("DownstreamReady", True),)),),
        source_locator="Main / Rung 0",
        language="RLL",
        origin="RUNG",
        semantic_state="FULL",
        evidence_id="ENG-RUN",
    )
    context = LiveEngineeringContext(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        controller_name="Demo",
        source_path="/tmp/demo.L5X",
        source_sha256="a" * 64,
        full_project=full_project,
        tags=(ready, run),
        rules=(rule,),
        statements=(),
        limitations=(),
    )
    mappings = tuple(
        LiveTagMapping(
            tag_id=tag.id,
            tag_name=tag.name,
            tag_scope=tag.scope,
            tag_data_type=tag.data_type,
            status=LiveTagMappingStatus.AUTO_BOUND,
            reason="exact",
            candidates=(),
            selected_node_id=f"ns=1;s={tag.name}",
            selected_path=f"Objects.{tag.name}",
            evidence_id=f"MAP-{tag.id}",
        )
        for tag in (ready, run)
    )
    reconciliation = LiveTagReconciliation(plc_id="plc1", mappings=mappings)
    observations = (
        LiveObservedTag(
            tag_id="ready",
            tag_name="DownstreamReady",
            node_id="ns=1;s=DownstreamReady",
            value=True,
            evidence_id="LIVE-ready",
            definitive_current=True,
            mapping_status="AUTO_BOUND",
        ),
        LiveObservedTag(
            tag_id="run",
            tag_name="RunCmd",
            node_id="ns=1;s=RunCmd",
            value=True,
            evidence_id="LIVE-run",
            definitive_current=True,
            mapping_status="AUTO_BOUND",
        ),
    )
    return context, reconciliation, observations


def test_partial_engineering_project_cannot_receive_no_fault_conclusion() -> None:
    context, reconciliation, observations = _fixture(full_project=False)
    scope = build_system_health_scope(context, reconciliation)
    diagnosis = diagnose_system_health(context, reconciliation, observations, scope)

    assert diagnosis.status is LiveSystemHealthStatus.INDETERMINATE
    rendered = diagnosis.render_text()
    assert "not marked as a full project" in rendered
    assert "System-wide health cannot be concluded safely" in rendered


def test_bounded_scope_truncation_fails_closed() -> None:
    context, reconciliation, observations = _fixture(full_project=True)
    scope = build_system_health_scope(context, reconciliation, max_tags=1)
    diagnosis = diagnose_system_health(context, reconciliation, observations, scope)

    assert scope.truncated is True
    assert diagnosis.status is LiveSystemHealthStatus.INDETERMINATE
    assert "bounded to 1 of 2 relevant engineering signals" in diagnosis.render_text()
