from __future__ import annotations

from dataclasses import replace
from typing import Any

from devagent.plc.agent_harness_v15 import (
    MAX_REVIEW_ITERATIONS,
    critique_requirement_mappings,
    critique_review_candidates,
    trace,
)
from devagent.plc.production_models import (
    EngineeringFinding,
    EvidenceItem,
    PLCRequirement,
    RequirementStatus,
    RequirementVerification,
    Severity,
)
from devagent.plc.production_utils import severity
from devagent.providers import ModelProvider

AI_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "category", "title", "severity", "summary", "recommendation", "evidence_ids", "confidence"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "category": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "severity": {"type": "string", "enum": [item.value for item in Severity]},
                    "summary": {"type": "string", "minLength": 1},
                    "recommendation": {"type": "string", "minLength": 1},
                    "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string", "minLength": 1}},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
            },
        }
    },
}

AI_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mappings"],
    "properties": {
        "mappings": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["requirement_id", "evidence_ids", "summary", "confidence"],
                "properties": {
                    "requirement_id": {"type": "string", "minLength": 1},
                    "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string", "minLength": 1}},
                    "summary": {"type": "string", "minLength": 1},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
            },
        }
    },
}


def _review_payload(engineering, evidence: list[EvidenceItem], deterministic_findings: list[EngineeringFinding]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    project = engineering.project
    bounded = evidence[:220]
    compact_evidence = [
        {"id": item.id, "kind": item.kind, "summary": item.summary, "source_locator": item.source_locator}
        for item in bounded
    ]
    payload = {
        "instruction": (
            "Review only supplied deterministic PLC facts. Findings are review candidates, not proof of runtime behavior. "
            "Cite only supplied evidence_ids. Never claim VERIFIED, PASS, SAFE, safety certification, or release readiness."
        ),
        "project": {
            "vendor": project.metadata.vendor,
            "controller": project.metadata.controller_name,
            "processor_type": project.metadata.processor_type,
            "source_sha256": project.metadata.source_sha256,
            "tags": len(project.tags),
            "rungs": len(project.rungs),
            "st_statements": project.st_statement_total,
            "aois": len(project.aois),
            "branch_coverage": [project.branch_rung_semantic_count, project.branch_rung_total],
            "st_coverage": [project.st_statement_semantic_count, project.st_statement_total],
            "aoi_body_coverage": [project.aoi_internal_modeled_count, project.aoi_internal_total],
            "aoi_call_binding": [project.aoi_call_bound_count, project.aoi_call_total],
            "unknown_instructions": list(project.unknown_instruction_names),
            "partial_instructions": list(project.partially_modeled_instruction_names),
        },
        "deterministic_findings": [
            {
                "id": item.id,
                "title": item.title,
                "severity": item.severity.value,
                "summary": item.summary,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in deterministic_findings
        ],
        "evidence": compact_evidence,
        "evidence_truncated": len(evidence) > len(bounded),
    }
    return payload, compact_evidence


def _validate_review_raw(
    raw_findings: list[dict[str, Any]],
    *,
    valid_ids: set[str],
    allowed_ids: set[str] | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_findings:
        finding_id = str(raw["id"])
        if allowed_ids is not None and finding_id not in allowed_ids:
            warnings.append(f"Discarded AI engineering finding {finding_id} because it was not requested by the bounded revision loop")
            continue
        if finding_id in seen:
            warnings.append(f"Discarded duplicate AI engineering finding ID {finding_id}")
            continue
        seen.add(finding_id)
        cited = [str(item) for item in raw["evidence_ids"]]
        invalid = [item for item in cited if item not in valid_ids]
        if invalid:
            warnings.append(
                f"Discarded AI engineering finding {finding_id} because it cited unknown evidence IDs: {', '.join(invalid)}"
            )
            continue
        valid.append(raw)
    return valid


def _critic_accepts(
    raw: dict[str, Any],
    decision: dict[str, Any] | None,
    *,
    valid_ids: set[str],
    warnings: list[str],
) -> bool:
    finding_id = str(raw["id"])
    if not decision or decision.get("decision") != "ACCEPT":
        return False
    cited = {str(item) for item in raw["evidence_ids"]}
    supported = {str(item) for item in decision.get("supported_evidence_ids", [])}
    if not supported:
        warnings.append(f"Discarded AI engineering finding {finding_id}; critic ACCEPT had no supporting evidence IDs")
        return False
    if not supported <= cited or not supported <= valid_ids:
        warnings.append(f"Discarded AI engineering finding {finding_id}; critic support escaped the candidate evidence boundary")
        return False
    return True


def _as_finding(raw: dict[str, Any]) -> EngineeringFinding:
    return EngineeringFinding(
        id=f"AI-{raw['id']}",
        category=str(raw["category"]),
        title=str(raw["title"]),
        severity=severity(str(raw["severity"])),
        summary=str(raw["summary"]),
        recommendation=str(raw["recommendation"]),
        evidence_ids=tuple(str(item) for item in raw["evidence_ids"]),
        confidence=float(raw["confidence"]),
        origin="AI_CANDIDATE",
    )


def run_ai_review(
    provider: ModelProvider,
    engineering,
    evidence: list[EvidenceItem],
    deterministic_findings: list[EngineeringFinding],
    *,
    trace_sink: list[dict[str, Any]] | None = None,
) -> tuple[list[EngineeringFinding], list[str]]:
    payload, compact_evidence = _review_payload(engineering, evidence, deterministic_findings)
    valid_ids = {item.id for item in evidence}
    warnings: list[str] = []

    trace(
        trace_sink,
        graph="PLC_ENGINEERING_REVIEW",
        node="CONTEXT",
        iteration=1,
        outcome="COMPLETE",
        detail="Prepared bounded deterministic PLC evidence for AI review; proof engine remains authoritative.",
        counts={"evidence": len(compact_evidence), "deterministic_findings": len(deterministic_findings)},
    )
    trace(
        trace_sink,
        graph="PLC_ENGINEERING_REVIEW",
        node="PROPOSE",
        iteration=1,
        outcome="START",
        detail="Request evidence-constrained engineering review candidates.",
    )
    response = provider.request(role="plc_engineering_reviewer", payload=payload, schema=AI_REVIEW_SCHEMA)
    proposed = _validate_review_raw(
        list(response.get("findings", [])),
        valid_ids=valid_ids,
        allowed_ids=None,
        warnings=warnings,
    )
    trace(
        trace_sink,
        graph="PLC_ENGINEERING_REVIEW",
        node="PROPOSE",
        iteration=1,
        outcome="COMPLETE",
        detail="Initial review proposal completed and deterministic evidence-ID validation applied.",
        counts={"valid_candidates": len(proposed)},
    )
    if not proposed:
        trace(
            trace_sink,
            graph="PLC_ENGINEERING_REVIEW",
            node="ACCEPT",
            iteration=1,
            outcome="EMPTY",
            detail="No AI review candidates survived deterministic evidence validation.",
        )
        return [], warnings

    critic = critique_review_candidates(
        provider,
        candidates=proposed,
        evidence=compact_evidence,
        iteration=1,
        trace_sink=trace_sink,
    )
    accepted_raw = [raw for raw in proposed if _critic_accepts(raw, critic.get(str(raw["id"])), valid_ids=valid_ids, warnings=warnings)]

    revise_ids = {
        str(raw["id"])
        for raw in proposed
        if (critic.get(str(raw["id"])) or {}).get("decision") == "REVISE"
    }
    if revise_ids and MAX_REVIEW_ITERATIONS > 1:
        trace(
            trace_sink,
            graph="PLC_ENGINEERING_REVIEW",
            node="REVISE",
            iteration=2,
            outcome="START",
            detail="Run one bounded evaluator-optimizer revision pass for critic-marked candidates.",
            counts={"candidates": len(revise_ids)},
        )
        revision_payload = {
            **payload,
            "instruction": (
                "Revise only the listed candidate IDs using the critic feedback. Keep only evidence-grounded PLC engineering concerns. "
                "Do not introduce new findings. Do not claim VERIFIED, PASS, SAFE, safety certification, runtime proof, or release readiness."
            ),
            "revision_candidates": [raw for raw in proposed if str(raw["id"]) in revise_ids],
            "critic_feedback": [critic[item] for item in sorted(revise_ids) if item in critic],
        }
        revision_response = provider.request(
            role="plc_engineering_reviewer_revision",
            payload=revision_payload,
            schema=AI_REVIEW_SCHEMA,
        )
        revised = _validate_review_raw(
            list(revision_response.get("findings", [])),
            valid_ids=valid_ids,
            allowed_ids=revise_ids,
            warnings=warnings,
        )
        trace(
            trace_sink,
            graph="PLC_ENGINEERING_REVIEW",
            node="REVISE",
            iteration=2,
            outcome="COMPLETE",
            detail="Bounded revision pass completed.",
            counts={"revised_candidates": len(revised)},
        )
        revised_critic = critique_review_candidates(
            provider,
            candidates=revised,
            evidence=compact_evidence,
            iteration=2,
            trace_sink=trace_sink,
        )
        accepted_raw.extend(
            raw
            for raw in revised
            if _critic_accepts(raw, revised_critic.get(str(raw["id"])), valid_ids=valid_ids, warnings=warnings)
        )

    accepted_ids = {str(raw["id"]) for raw in accepted_raw}
    for raw in proposed:
        finding_id = str(raw["id"])
        decision = (critic.get(finding_id) or {}).get("decision")
        if finding_id not in accepted_ids and finding_id not in revise_ids and decision != "ACCEPT":
            warnings.append(f"Discarded AI engineering finding {finding_id} after critic decision {decision or 'MISSING'}")
    findings = [_as_finding(raw) for raw in accepted_raw]
    trace(
        trace_sink,
        graph="PLC_ENGINEERING_REVIEW",
        node="ACCEPT",
        iteration=MAX_REVIEW_ITERATIONS if revise_ids else 1,
        outcome="COMPLETE",
        detail="Only critic-approved, evidence-bounded findings were admitted as AI_CANDIDATE observations.",
        counts={"accepted": len(findings)},
    )
    return findings, warnings


def run_ai_requirement_mapping(
    provider: ModelProvider,
    requirements: list[PLCRequirement],
    verifications: list[RequirementVerification],
    evidence: list[EvidenceItem],
    *,
    trace_sink: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, RequirementVerification], list[str]]:
    unresolved = [
        item for item in verifications
        if item.status in {RequirementStatus.NOT_MAPPED, RequirementStatus.TRACEABLE_NOT_PROVEN}
    ]
    if not unresolved:
        return {}, []
    bounded = unresolved[:50]
    req_by_id = {item.id: item for item in requirements}
    compact_evidence_items = evidence[:260]
    compact_evidence = [{"id": item.id, "kind": item.kind, "summary": item.summary} for item in compact_evidence_items]
    payload = {
        "instruction": (
            "Propose traceability candidates only. Do not claim a requirement is verified, passed, safe, compliant, or release-ready. "
            "Cite only supplied evidence_ids."
        ),
        "requirements": [
            {
                "id": item.requirement_id,
                "text": req_by_id[item.requirement_id].text,
                "current_status": item.status.value,
            }
            for item in bounded
        ],
        "evidence": compact_evidence,
        "evidence_truncated": len(evidence) > len(compact_evidence_items),
    }
    trace(
        trace_sink,
        graph="PLC_REQUIREMENT_MAPPING",
        node="PROPOSE",
        iteration=1,
        outcome="START",
        detail="Request bounded AI traceability candidates for unresolved requirements.",
        counts={"requirements": len(bounded), "evidence": len(compact_evidence)},
    )
    response = provider.request(role="plc_requirement_mapper", payload=payload, schema=AI_REQUIREMENT_SCHEMA)
    valid_evidence = {item.id for item in evidence}
    allowed_requirements = {item.requirement_id for item in bounded}
    warnings: list[str] = []
    valid_raw: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in response.get("mappings", []):
        requirement_id = str(raw["requirement_id"])
        if requirement_id not in allowed_requirements:
            warnings.append(f"Discarded AI requirement mapping for unknown/unrequested requirement {requirement_id}")
            continue
        if requirement_id in seen:
            warnings.append(f"Discarded duplicate AI requirement mapping for {requirement_id}")
            continue
        seen.add(requirement_id)
        cited = tuple(str(value) for value in raw["evidence_ids"])
        invalid = [value for value in cited if value not in valid_evidence]
        if invalid:
            warnings.append(
                f"Discarded AI requirement mapping for {requirement_id}; unknown evidence IDs: {', '.join(invalid)}"
            )
            continue
        valid_raw.append(raw)
    trace(
        trace_sink,
        graph="PLC_REQUIREMENT_MAPPING",
        node="PROPOSE",
        iteration=1,
        outcome="COMPLETE",
        detail="Requirement mapping proposal completed and deterministic identifier validation applied.",
        counts={"valid_mappings": len(valid_raw)},
    )

    decisions = critique_requirement_mappings(
        provider,
        mappings=valid_raw,
        evidence=compact_evidence,
        iteration=1,
        trace_sink=trace_sink,
    )
    updates: dict[str, RequirementVerification] = {}
    for raw in valid_raw:
        requirement_id = str(raw["requirement_id"])
        decision = decisions.get(requirement_id)
        if not decision or decision.get("decision") != "ACCEPT":
            warnings.append(f"Discarded AI requirement mapping for {requirement_id} after critic decision {(decision or {}).get('decision', 'MISSING')}")
            continue
        cited = {str(value) for value in raw["evidence_ids"]}
        supported = {str(value) for value in decision.get("supported_evidence_ids", [])}
        if not supported or not supported <= cited or not supported <= valid_evidence:
            warnings.append(f"Discarded AI requirement mapping for {requirement_id}; critic support escaped the proposed evidence boundary")
            continue
        previous = next(item for item in verifications if item.requirement_id == requirement_id)
        updates[requirement_id] = replace(
            previous,
            status=RequirementStatus.AI_CANDIDATE,
            summary=str(raw["summary"]),
            evidence_ids=tuple(str(value) for value in raw["evidence_ids"]),
            confidence=float(raw["confidence"]),
            ai_assisted=True,
        )
    if len(unresolved) > len(bounded):
        warnings.append(
            f"AI requirement mapping was bounded to {len(bounded)} of {len(unresolved)} unresolved requirements in this run"
        )
    trace(
        trace_sink,
        graph="PLC_REQUIREMENT_MAPPING",
        node="ACCEPT",
        iteration=1,
        outcome="COMPLETE",
        detail="Only critic-approved mappings were admitted, and all remain AI_CANDIDATE rather than proof.",
        counts={"accepted": len(updates)},
    )
    return updates, warnings
