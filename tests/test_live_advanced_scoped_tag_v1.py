from types import SimpleNamespace

from devagent.live.advanced_semantics import LiveAdvancedKind, build_live_advanced_coverage
from devagent.live.engineering_context import LiveEngineeringContext, LiveEngineeringTag


def _tag(tag_id: str, name: str, scope: str = "Program:Main") -> LiveEngineeringTag:
    return LiveEngineeringTag(
        id=tag_id,
        name=name,
        scope=scope,
        data_type="BOOL",
        description=None,
        external_access="Read Only",
        alias_for=None,
    )


def test_program_scoped_tag_accessor_is_supported_and_stable():
    tag = _tag("tag-1", "LineReq")
    assert tag.scoped_name == "Program:Main.LineReq"
    assert tag.identity_forms()
    assert _tag("tag-2", "GlobalReady", scope="Controller").scoped_name == "GlobalReady"


def test_advanced_coverage_builds_for_normal_tagged_project_without_attribute_error():
    context = LiveEngineeringContext(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        controller_name="PLC1",
        source_path="project.L5X",
        source_sha256="abc123",
        full_project=True,
        tags=(
            _tag("req", "LineReq"),
            _tag("ack", "LineAck"),
            LiveEngineeringTag(
                id="fault",
                name="DriveFaultCode",
                scope="Program:Main",
                data_type="DINT",
                description="Drive diagnostic code",
                external_access="Read Only",
                alias_for=None,
            ),
        ),
        rules=(),
        statements=(),
        limitations=(),
    )
    project = SimpleNamespace(rungs=(), logic_statements=(), aois=(), data_types=())

    coverage = build_live_advanced_coverage(project, context)

    handshakes = [item for item in coverage.models if item.kind is LiveAdvancedKind.HANDSHAKE]
    faults = [item for item in coverage.models if item.kind is LiveAdvancedKind.FAULT_CODE]
    assert len(handshakes) == 1
    assert handshakes[0].references == ("Program:Main.LineReq", "Program:Main.LineAck")
    assert len(faults) == 1
    assert faults[0].name == "Program:Main.DriveFaultCode"
