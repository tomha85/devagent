from __future__ import annotations

from types import SimpleNamespace

from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.top_engineering_risks_v1 import (
    classify_risk,
    render_top_engineering_risks,
    select_top_engineering_risks,
)


def _risk(
    risk_id: str,
    *,
    category: str,
    title: str,
    summary: str,
    consequence: str,
    recommendation: str,
    severity: Severity = Severity.HIGH,
) -> RiskFinding:
    return RiskFinding(
        id=risk_id,
        category=category,
        title=title,
        severity=severity,
        summary=summary,
        consequence=consequence,
        recommendation=recommendation,
        evidence_ids=(f"EV-{risk_id}",),
    )


def test_classification_is_conservative_fix_vs_fat() -> None:
    writer = _risk(
        "R-WRITER",
        category="MULTIPLE_WRITERS",
        title="Multiple writers on axis command",
        summary="The same command has multiple writers and final value may depend on scan order.",
        consequence="The command can be overwritten unexpectedly at runtime.",
        recommendation="Establish one authoritative writer and FAT-test intentional writer priority.",
    )
    timing = _risk(
        "R-TIMER",
        category="STATEFUL_LOGIC",
        title="Timer-dependent sequence",
        summary="The sequence depends on timer runtime evolution that static analysis cannot prove.",
        consequence="The transition can occur at a different time than intended.",
        recommendation="Execute the generated FAT case in a simulator or HIL environment.",
    )
    review = _risk(
        "R-REVIEW",
        category="ENGINEERING_REVIEW",
        title="Naming pattern deserves review",
        summary="The naming pattern is inconsistent with nearby logic.",
        consequence="Maintenance may be harder for future engineers.",
        recommendation="Review naming with the controls team before changing code.",
        severity=Severity.MEDIUM,
    )

    assert classify_risk(writer) == "FIX_AND_FAT"
    assert classify_risk(timing) == "FAT_REQUIRED"
    assert classify_risk(review) == "REVIEW_REQUIRED"


def test_top_risks_keep_engineering_diversity_instead_of_requirement_spam() -> None:
    requirements = [
        _risk(
            f"REQ-{index}",
            category="REQUIREMENT_VERIFICATION",
            title=f"Requirement REQ-{index} is not deterministically proven",
            summary="Requirement remains traceable but not deterministically proven.",
            consequence="Release evidence cannot claim this requirement is verified.",
            recommendation="Map the requirement to FAT evidence and execute the required runtime test.",
        )
        for index in range(5)
    ]
    writer = _risk(
        "WRITER",
        category="MULTIPLE_WRITERS",
        title="Multiple writers on HBW axis command",
        summary="The axis command is written from multiple source locations.",
        consequence="Final value may depend on execution order.",
        recommendation="Establish authoritative ownership and FAT-test intentional multi-writer behavior.",
    )
    sequence = _risk(
        "SEQ",
        category="STATEFUL_LOGIC",
        title="Warehouse sequence not fully statically proven",
        summary="The sequence depends on runtime state/timing behavior.",
        consequence="Storage/retrieval may stall, skip, or enter an unintended state.",
        recommendation="Execute the generated sequence FAT scenarios in an approved runtime environment.",
    )

    selected = select_top_engineering_risks([*requirements, writer, sequence], limit=7)
    selected_ids = {item.id for item in selected}

    assert writer.id in selected_ids
    assert sequence.id in selected_ids
    assert len([item for item in selected if item.id.startswith("REQ-")]) == 1
    assert len(selected) == 3

    rendered = render_top_engineering_risks(SimpleNamespace(risks=[*requirements, writer, sequence]), limit=7)
    assert "TOP ENGINEERING RISKS" in rendered
    assert "Requirement coverage incomplete (5 related findings)" in rendered
    assert "5 requirement verification finding(s)" in rendered
    assert "Classification:" in rendered
    assert "Why:" in rendered
    assert "Impact:" in rendered
    assert "Recommended Action:" in rendered


def test_unresolved_calls_are_one_root_cause_theme_not_requirement_spam() -> None:
    calls = [
        _risk(
            f"CALL-{index}",
            category="CALL_BINDING",
            title=f"Requirement coverage incomplete at call {index}",
            summary=f"V3 withheld call proof at Siemens Main / Line {7300 + index}: ambiguous_or_unresolved_target.",
            consequence="Downstream block behavior and requirement/FAT traceability may depend on an unresolved target, instance, interface, control context, or recursive path.",
            recommendation="Correct/export the exact call/interface/instance evidence or execute the generated engineer FAT procedure; do not promote this call to static verification.",
        )
        for index in range(5)
    ]
    semantic = _risk(
        "SEM",
        category="SEMANTIC_COVERAGE",
        title="Siemens behavior contains PARTIAL/OPAQUE areas",
        summary="The deterministic Siemens analyzer cannot prove all exported behavior.",
        consequence="Dependencies and generated static tests are intentionally incomplete for withheld semantics.",
        recommendation="Export deeper supported source or execute linked FAT procedures.",
    )
    writer = _risk(
        "WRITER",
        category="MULTIPLE_WRITERS",
        title="Multiple Siemens source writers for AxisCommand",
        summary="AxisCommand is written by 2 normalized Siemens source statements.",
        consequence="Final value may depend on execution order.",
        recommendation="Review ownership and FAT-test intentional arbitration.",
        severity=Severity.MEDIUM,
    )

    selected = select_top_engineering_risks([*calls, semantic, writer], limit=7)
    assert len(selected) == 3
    assert len([item for item in selected if item.id.startswith("CALL-")]) == 1

    rendered = render_top_engineering_risks(SimpleNamespace(risks=[*calls, semantic, writer]), limit=7)
    assert rendered.count("Unresolved/ambiguous Siemens call bindings (5 related findings)") == 1
    assert "5 PLC call finding(s) cannot be deterministically bound" in rendered
    assert "Requirement coverage incomplete (5 related findings)" not in rendered
    assert "Siemens behavior contains PARTIAL/OPAQUE areas" in rendered
    assert "Multiple Siemens source writers for AxisCommand" in rendered
