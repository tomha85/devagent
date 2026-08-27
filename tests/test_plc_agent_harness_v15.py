from __future__ import annotations

from types import SimpleNamespace

from devagent.plc.production_ai import run_ai_requirement_mapping, run_ai_review
from devagent.plc.production_models import (
    EngineeringFinding,
    EvidenceItem,
    PLCRequirement,
    RequirementCriticality,
    RequirementStatus,
    RequirementVerification,
    RequirementVerificationMode,
    Severity,
)
from devagent.providers import ScriptedFakeProvider


def _engineering():
    metadata = SimpleNamespace(
        vendor="Rockwell Automation",
        controller_name="HarnessQualification",
        processor_type="1756-L85E",
        source_sha256="a" * 64,
    )
    project = SimpleNamespace(
        metadata=metadata,
        tags=[SimpleNamespace(name="Start")],
        rungs=[SimpleNamespace()],
        st_statement_total=0,
        aois=[],
        branch_rung_semantic_count=1,
        branch_rung_total=1,
        st_statement_semantic_count=0,
        aoi_internal_modeled_count=0,
        aoi_internal_total=0,
        aoi_call_bound_count=0,
        aoi_call_total=0,
        unknown_instruction_names=(),
        partially_modeled_instruction_names=(),
    )
    return SimpleNamespace(project=project)


def _evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            "RUNG:Main/Logic/0",
            "RUNG",
            "Main/Logic rung 0 uses Start as a condition for Run.",
            "Main/Logic/Rung[0]",
            "a" * 64,
        )
    ]


def _deterministic() -> list[EngineeringFinding]:
    return [
        EngineeringFinding(
            id="DET-1",
            category="CAUSE_EFFECT",
            title="Bounded cause/effect relationship",
            severity=Severity.INFO,
            summary="Start participates in the modeled Run path.",
            recommendation="Use the deterministic evidence for review and FAT planning.",
            evidence_ids=("RUNG:Main/Logic/0",),
        )
    ]


def _candidate(summary: str = "Review the Start-to-Run relationship for commissioning assumptions.") -> dict:
    return {
        "id": "CAND-1",
        "category": "ENGINEERING_REVIEW",
        "title": "Review modeled command relationship",
        "severity": "MEDIUM",
        "summary": summary,
        "recommendation": "Confirm intended behavior and include an engineer-executed FAT check where appropriate.",
        "evidence_ids": ["RUNG:Main/Logic/0"],
        "confidence": 0.81,
    }


def test_v15_review_graph_requires_independent_critic_acceptance() -> None:
    provider = ScriptedFakeProvider(
        [
            {"_role": "plc_engineering_reviewer", "findings": [_candidate()]},
            {
                "_role": "plc_engineering_critic",
                "decisions": [
                    {
                        "finding_id": "CAND-1",
                        "decision": "ACCEPT",
                        "reason": "The concern is bounded to the cited rung and does not claim runtime proof.",
                        "supported_evidence_ids": ["RUNG:Main/Logic/0"],
                    }
                ],
            },
        ]
    )
    trace: list[dict] = []

    findings, warnings = run_ai_review(
        provider,
        _engineering(),
        _evidence(),
        _deterministic(),
        trace_sink=trace,
    )

    assert warnings == []
    assert [item.id for item in findings] == ["AI-CAND-1"]
    assert findings[0].origin == "AI_CANDIDATE"
    assert [call["role"] for call in provider.calls] == [
        "plc_engineering_reviewer",
        "plc_engineering_critic",
    ]
    assert [item["node"] for item in trace] == [
        "CONTEXT",
        "PROPOSE",
        "PROPOSE",
        "CRITIC",
        "CRITIC",
        "ACCEPT",
    ]


def test_v15_review_graph_runs_only_one_bounded_revision_iteration() -> None:
    revised = _candidate("The modeled Start-to-Run relationship should be reviewed; runtime behavior remains unproven.")
    provider = ScriptedFakeProvider(
        [
            {"_role": "plc_engineering_reviewer", "findings": [_candidate("This proves the machine will always run correctly.")]},
            {
                "_role": "plc_engineering_critic",
                "decisions": [
                    {
                        "finding_id": "CAND-1",
                        "decision": "REVISE",
                        "reason": "The candidate overclaims runtime behavior; retain only the evidence-bounded review concern.",
                        "supported_evidence_ids": ["RUNG:Main/Logic/0"],
                    }
                ],
            },
            {"_role": "plc_engineering_reviewer_revision", "findings": [revised]},
            {
                "_role": "plc_engineering_critic",
                "decisions": [
                    {
                        "finding_id": "CAND-1",
                        "decision": "ACCEPT",
                        "reason": "The revised wording stays inside the static evidence boundary.",
                        "supported_evidence_ids": ["RUNG:Main/Logic/0"],
                    }
                ],
            },
        ]
    )
    trace: list[dict] = []

    findings, _ = run_ai_review(
        provider,
        _engineering(),
        _evidence(),
        _deterministic(),
        trace_sink=trace,
    )

    assert len(findings) == 1
    assert "runtime behavior remains unproven" in findings[0].summary
    assert [call["role"] for call in provider.calls] == [
        "plc_engineering_reviewer",
        "plc_engineering_critic",
        "plc_engineering_reviewer_revision",
        "plc_engineering_critic",
    ]
    assert max(item["iteration"] for item in trace) == 2


def test_v15_critic_rejection_cannot_become_engineering_finding() -> None:
    provider = ScriptedFakeProvider(
        [
            {"_role": "plc_engineering_reviewer", "findings": [_candidate()]},
            {
                "_role": "plc_engineering_critic",
                "decisions": [
                    {
                        "finding_id": "CAND-1",
                        "decision": "REJECT",
                        "reason": "Evidence does not support the proposed concern strongly enough.",
                        "supported_evidence_ids": [],
                    }
                ],
            },
        ]
    )

    findings, warnings = run_ai_review(provider, _engineering(), _evidence(), _deterministic())

    assert findings == []
    assert any("critic decision REJECT" in warning for warning in warnings)


def test_v15_requirement_graph_can_only_create_ai_candidate_not_proof() -> None:
    requirement = PLCRequirement(
        id="REQ-1",
        text="The machine shall stop when Guard opens.",
        source_path="requirements.md",
        source_locator="REQ-1",
        source_sha256="b" * 64,
        verification_mode=RequirementVerificationMode.DYNAMIC,
        criticality=RequirementCriticality.CRITICAL,
    )
    verification = RequirementVerification(
        requirement_id="REQ-1",
        status=RequirementStatus.NOT_MAPPED,
        summary="No deterministic mapping found.",
    )
    provider = ScriptedFakeProvider(
        [
            {
                "_role": "plc_requirement_mapper",
                "mappings": [
                    {
                        "requirement_id": "REQ-1",
                        "evidence_ids": ["RUNG:Main/Logic/0"],
                        "summary": "Candidate trace only; deterministic behavior is not proven.",
                        "confidence": 0.64,
                    }
                ],
            },
            {
                "_role": "plc_requirement_critic",
                "decisions": [
                    {
                        "requirement_id": "REQ-1",
                        "decision": "ACCEPT",
                        "reason": "Lexical/engineering trace is plausible, but this is not proof.",
                        "supported_evidence_ids": ["RUNG:Main/Logic/0"],
                    }
                ],
            },
        ]
    )
    trace: list[dict] = []

    updates, warnings = run_ai_requirement_mapping(
        provider,
        [requirement],
        [verification],
        _evidence(),
        trace_sink=trace,
    )

    assert warnings == []
    assert updates["REQ-1"].status is RequirementStatus.AI_CANDIDATE
    assert updates["REQ-1"].ai_assisted is True
    assert updates["REQ-1"].status is not RequirementStatus.STATICALLY_VERIFIED
    assert updates["REQ-1"].status is not RequirementStatus.DYNAMICALLY_VERIFIED
    assert any(item["graph"] == "PLC_REQUIREMENT_MAPPING" and item["node"] == "CRITIC" for item in trace)
