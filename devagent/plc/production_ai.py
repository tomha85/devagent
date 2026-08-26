from __future__ import annotations

from dataclasses import replace
from typing import Any

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


def run_ai_review(
    provider: ModelProvider,
    engineering,
    evidence: list[EvidenceItem],
    deterministic_findings: list[EngineeringFinding],
) -> tuple[list[EngineeringFinding], list[str]]:
    project = engineering.project
    bounded = evidence[:220]
    payload = {
        "instruction": "Review only supplied deterministic PLC facts. Findings are review candidates, not proof of runtime behavior. Cite only supplied evidence_ids.",
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
        "evidence": [
            {"id": item.id, "kind": item.kind, "summary": item.summary, "source_locator": item.source_locator}
            for item in bounded
        ],
        "evidence_truncated": len(evidence) > len(bounded),
    }
    response = provider.request(role="plc_engineering_reviewer", payload=payload, schema=AI_REVIEW_SCHEMA)
    valid_ids = {item.id for item in evidence}
    findings: list[EngineeringFinding] = []
    warnings: list[str] = []
    for raw in response.get("findings", []):
        cited = tuple(str(item) for item in raw["evidence_ids"])
        invalid = [item for item in cited if item not in valid_ids]
        if invalid:
            warnings.append(
                f"Discarded AI engineering finding {raw['id']} because it cited unknown evidence IDs: {', '.join(invalid)}"
            )
            continue
        findings.append(EngineeringFinding(
            id=f"AI-{raw['id']}",
            category=str(raw["category"]),
            title=str(raw["title"]),
            severity=severity(str(raw["severity"])),
            summary=str(raw["summary"]),
            recommendation=str(raw["recommendation"]),
            evidence_ids=cited,
            confidence=float(raw["confidence"]),
            origin="AI_CANDIDATE",
        ))
    return findings, warnings


def run_ai_requirement_mapping(
    provider: ModelProvider,
    requirements: list[PLCRequirement],
    verifications: list[RequirementVerification],
    evidence: list[EvidenceItem],
) -> tuple[dict[str, RequirementVerification], list[str]]:
    unresolved = [
        item for item in verifications
        if item.status in {RequirementStatus.NOT_MAPPED, RequirementStatus.TRACEABLE_NOT_PROVEN}
    ]
    if not unresolved:
        return {}, []
    bounded = unresolved[:50]
    req_by_id = {item.id: item for item in requirements}
    compact_evidence = evidence[:260]
    payload = {
        "instruction": "Propose traceability candidates only. Do not claim a requirement is verified. Cite only supplied evidence_ids.",
        "requirements": [
            {
                "id": item.requirement_id,
                "text": req_by_id[item.requirement_id].text,
                "current_status": item.status.value,
            }
            for item in bounded
        ],
        "evidence": [{"id": item.id, "kind": item.kind, "summary": item.summary} for item in compact_evidence],
        "evidence_truncated": len(evidence) > len(compact_evidence),
    }
    response = provider.request(role="plc_requirement_mapper", payload=payload, schema=AI_REQUIREMENT_SCHEMA)
    valid_evidence = {item.id for item in evidence}
    allowed_requirements = {item.requirement_id for item in bounded}
    updates: dict[str, RequirementVerification] = {}
    warnings: list[str] = []
    for raw in response.get("mappings", []):
        requirement_id = str(raw["requirement_id"])
        if requirement_id not in allowed_requirements:
            warnings.append(f"Discarded AI requirement mapping for unknown/unrequested requirement {requirement_id}")
            continue
        cited = tuple(str(value) for value in raw["evidence_ids"])
        invalid = [value for value in cited if value not in valid_evidence]
        if invalid:
            warnings.append(
                f"Discarded AI requirement mapping for {requirement_id}; unknown evidence IDs: {', '.join(invalid)}"
            )
            continue
        previous = next(item for item in verifications if item.requirement_id == requirement_id)
        updates[requirement_id] = replace(
            previous,
            status=RequirementStatus.AI_CANDIDATE,
            summary=str(raw["summary"]),
            evidence_ids=cited,
            confidence=float(raw["confidence"]),
            ai_assisted=True,
        )
    if len(unresolved) > len(bounded):
        warnings.append(
            f"AI requirement mapping was bounded to {len(bounded)} of {len(unresolved)} unresolved requirements in this run"
        )
    return updates, warnings
