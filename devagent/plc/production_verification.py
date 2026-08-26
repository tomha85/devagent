from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from devagent.plc.execution_trust import (
    ExecutionBackendRegistry,
    require_qualified_backend,
)
from devagent.plc.models import FATTestCase, PLCSemanticState
from devagent.plc.production_models import (
    EvidenceItem,
    ExecutionStatus,
    PLCRequirement,
    RequirementStatus,
    RequirementVerification,
    TestExecutionEvidence,
)
from devagent.plc.production_utils import explicit_bool, tag_occurs, tokens
from devagent.plc.rockwell_compare import verify_typed_compare_requirement

_EXECUTION_SCHEMA = "devagent-plc-execution-results-v1"


def _ote_truth(logic, assignment: dict[str, bool], expected: bool) -> str:
    """Return PROVEN, CONFLICT, or UNKNOWN for a partial Boolean assignment."""
    possible = 0
    definite = 0
    for path in logic.paths:
        contradicted = False
        complete = True
        for term in path.terms:
            if term.tag not in assignment:
                complete = False
                continue
            if assignment[term.tag] != term.required:
                contradicted = True
                break
        if contradicted:
            continue
        possible += 1
        if complete:
            definite += 1
    if expected:
        if definite:
            return "PROVEN"
        if possible == 0:
            return "CONFLICT"
        return "UNKNOWN"
    if possible == 0:
        return "PROVEN"
    if definite:
        return "CONFLICT"
    return "UNKNOWN"


def requirement_candidates(requirement: PLCRequirement, engineering, evidence: list[EvidenceItem]) -> tuple[list[str], list[str]]:
    project = engineering.project
    explicit_tags = sorted(
        {tag.name for tag in project.tags if tag_occurs(requirement.text, tag.name)},
        key=str.casefold,
    )
    evidence_ids = [f"TAG:{tag.scope}:{tag.name}" for tag in project.tags if tag.name in explicit_tags]
    req_tokens = tokens(requirement.text)
    if not explicit_tags and req_tokens:
        scored: list[tuple[int, str]] = []
        for item in evidence:
            overlap = len(req_tokens & tokens(item.summary))
            if overlap >= 2:
                scored.append((overlap, item.id))
        evidence_ids.extend(item_id for _, item_id in sorted(scored, reverse=True)[:6])
    return explicit_tags, list(dict.fromkeys(evidence_ids))


def verify_requirement(requirement: PLCRequirement, engineering, evidence: list[EvidenceItem], tests: list[FATTestCase]) -> RequirementVerification:
    typed_compare = verify_typed_compare_requirement(requirement, engineering, evidence, tests)
    if typed_compare is not None:
        return typed_compare

    matched_tags, evidence_ids = requirement_candidates(requirement, engineering, evidence)
    modeled_outputs = {
        logic.output_tag
        for logic in engineering.project.output_logic
        if logic.semantic_state is PLCSemanticState.FULL and not logic.origin.startswith("AOI_INTERNAL:")
    }
    explicit_outputs = [
        tag for tag in matched_tags
        if tag in modeled_outputs and explicit_bool(requirement.text, tag) is not None
    ]
    if len(explicit_outputs) > 1:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            "Requirement constrains multiple modeled outputs; compound assertion semantics are not yet statically proven.",
            tuple(evidence_ids),
            tuple(matched_tags),
        )
    if len(explicit_outputs) == 1:
        output = explicit_outputs[0]
        expected = explicit_bool(requirement.text, output)
        assert expected is not None
        assignment = {
            tag: value
            for tag in matched_tags
            if tag != output
            for value in [explicit_bool(requirement.text, tag)]
            if value is not None
        }
        matching_logic = [
            logic for logic in engineering.project.output_logic
            if logic.output_tag == output
            and logic.semantic_state is PLCSemanticState.FULL
            and not logic.origin.startswith("AOI_INTERNAL:")
        ]
        logic_evidence = tuple(logic.id for logic in matching_logic)
        combined_evidence = tuple(dict.fromkeys([*evidence_ids, *logic_evidence]))
        if len(matching_logic) != 1:
            return RequirementVerification(
                requirement.id,
                RequirementStatus.TRACEABLE_NOT_PROVEN,
                f"{output} has {len(matching_logic)} modeled writer logic object(s); output state is withheld without deterministic writer-order semantics.",
                combined_evidence,
                tuple(matched_tags),
            )
        logic = matching_logic[0]
        if logic.instruction != "OTE":
            return RequirementVerification(
                requirement.id,
                RequirementStatus.TRACEABLE_NOT_PROVEN,
                f"{output} uses retentive/action instruction {logic.instruction}; static path truth alone cannot prove retained output state.",
                combined_evidence,
                tuple(matched_tags),
            )
        if not assignment:
            return RequirementVerification(
                requirement.id,
                RequirementStatus.TRACEABLE_NOT_PROVEN,
                f"Requirement names {output} but does not provide enough explicit Boolean input conditions for proof.",
                combined_evidence,
                tuple(matched_tags),
            )
        truth = _ote_truth(logic, assignment, expected)
        if truth == "PROVEN":
            linked = [
                test.id for test in tests
                if test.output_tag == output and all(test.preconditions.get(key) == value for key, value in assignment.items())
            ]
            return RequirementVerification(
                requirement.id,
                RequirementStatus.STATICALLY_VERIFIED,
                f"Specified Boolean conditions deterministically imply {output}={'TRUE' if expected else 'FALSE'} in the single-writer modeled OTE logic; runtime behavior still requires execution when policy requires dynamic proof.",
                combined_evidence,
                tuple(matched_tags),
                tuple(linked),
            )
        if truth == "CONFLICT":
            return RequirementVerification(
                requirement.id,
                RequirementStatus.CONFLICT,
                f"Specified Boolean conditions make required {output}={'TRUE' if expected else 'FALSE'} impossible in the single-writer modeled OTE logic.",
                combined_evidence,
                tuple(matched_tags),
            )
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            f"Requirement maps to {output}, but supplied Boolean conditions under-specify one or more modeled dependencies; neither verification nor conflict is proven.",
            combined_evidence,
            tuple(matched_tags),
        )
    if matched_tags:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            "Requirement references known PLC tags but its complete behavior cannot be deterministically proven from the bounded semantics.",
            tuple(evidence_ids),
            tuple(matched_tags),
        )
    if evidence_ids:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            "Requirement has lexical trace candidates, but no explicit PLC identifier mapping is proven.",
            tuple(evidence_ids),
            confidence=0.5,
        )
    return RequirementVerification(requirement.id, RequirementStatus.NOT_MAPPED, "No deterministic PLC implementation mapping was found.")


def generate_requirement_tests(requirements: list[PLCRequirement], verifications: list[RequirementVerification], engineering) -> list[FATTestCase]:
    tests = list(engineering.fat_tests)
    signatures = {(item.output_tag, tuple(sorted(item.preconditions.items()))) for item in tests}
    req_by_id = {item.id: item for item in requirements}
    logic_by_id = {logic.id: logic for logic in engineering.project.output_logic}
    for verification in verifications:
        if verification.status is not RequirementStatus.STATICALLY_VERIFIED:
            continue
        requirement = req_by_id[verification.requirement_id]
        output = next((tag for tag in verification.matched_tags if any(logic.output_tag == tag for logic in engineering.project.output_logic)), None)
        if not output:
            continue
        assignment = {
            tag: value
            for tag in verification.matched_tags
            if tag != output
            for value in [explicit_bool(requirement.text, tag)]
            if value is not None
        }
        signature = (output, tuple(sorted(assignment.items())))
        if not assignment or signature in signatures:
            continue
        logic = next((logic_by_id[item] for item in verification.evidence_ids if item in logic_by_id), None)
        if logic is None or logic.instruction != "OTE":
            continue
        digest = hashlib.sha1(f"{requirement.id}:{output}:{signature}".encode()).hexdigest()[:10]
        expected = explicit_bool(requirement.text, output)
        tests.append(
            FATTestCase(
                id=f"FAT-REQ-{digest}",
                title=f"Verify {requirement.id}: {output}",
                source=logic.source,
                output_tag=output,
                preconditions=dict(sorted(assignment.items())),
                expected=f"Requirement {requirement.id} expects {output}={'TRUE' if expected else 'FALSE'}",
                limitations=("Requirement-derived candidate; requires approved execution backend before PASS can be claimed.",),
                scenario="REQUIREMENT",
            )
        )
        signatures.add(signature)
    engineering.fat_tests = tests
    return tests


def link_tests_to_verifications(verifications: list[RequirementVerification], requirements: list[PLCRequirement], tests: list[FATTestCase]) -> list[RequirementVerification]:
    req_by_id = {item.id: item for item in requirements}
    result: list[RequirementVerification] = []
    for verification in verifications:
        if verification.status is not RequirementStatus.STATICALLY_VERIFIED:
            result.append(verification)
            continue
        requirement = req_by_id[verification.requirement_id]
        linked = set(verification.linked_test_ids)
        for test in tests:
            if test.output_tag not in verification.matched_tags:
                continue
            if explicit_bool(requirement.text, test.output_tag) is None:
                continue
            conditions = {
                tag: value
                for tag in verification.matched_tags
                if tag != test.output_tag
                for value in [explicit_bool(requirement.text, tag)]
                if value is not None
            }
            if conditions and all(test.preconditions.get(tag) == value for tag, value in conditions.items()):
                linked.add(test.id)
        result.append(replace(verification, linked_test_ids=tuple(sorted(linked))))
    return result


def compute_requirements_sha256(requirements: list[PLCRequirement]) -> str:
    payload = [
        {
            "id": item.id,
            "text": item.text,
            "source_sha256": item.source_sha256,
            "source_locator": item.source_locator,
            "verification_mode": item.verification_mode.value,
            "criticality": item.criticality.value,
        }
        for item in requirements
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_test_plan_sha256(tests: list[FATTestCase]) -> str:
    payload = [
        {
            "id": test.id,
            "output_tag": test.output_tag,
            "preconditions": dict(sorted(test.preconditions.items())),
            "expected": test.expected,
            "scenario": test.scenario,
            "source": getattr(test.source, "locator", None),
        }
        for test in sorted(tests, key=lambda item: item.id)
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def compute_verification_context_sha256(
    *,
    project_sha256: str,
    test_plan_sha256: str,
    requirements_sha256: str,
    backend_registry_sha256: str | None,
    baseline_sha256: str | None,
    execution_results_sha256: str | None = None,
    release_policy_sha256: str | None = None,
    trust_store_sha256: str | None = None,
) -> str:
    payload = {
        "schema": "devagent-plc-verification-context-v2",
        "project_sha256": project_sha256,
        "test_plan_sha256": test_plan_sha256,
        "requirements_sha256": requirements_sha256,
        "backend_registry_sha256": backend_registry_sha256,
        "baseline_sha256": baseline_sha256,
        "execution_results_sha256": execution_results_sha256,
        "release_policy_sha256": release_policy_sha256,
        "trust_store_sha256": trust_store_sha256,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_execution_results(
    path: Path | None,
    project_sha256: str,
    plan_sha256: str,
    test_ids: set[str],
    backend_registry: ExecutionBackendRegistry | None = None,
) -> list[TestExecutionEvidence]:
    if path is None:
        return []
    target = path.expanduser().resolve(strict=True)
    if target.stat().st_size > 25 * 1024 * 1024:
        raise ValueError("Execution evidence exceeds 25 MiB production limit")
    loaded = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError("Execution evidence must be a JSON object")
    if loaded.get("schema") != _EXECUTION_SCHEMA:
        raise ValueError(f"Execution evidence schema must be {_EXECUTION_SCHEMA}")
    if str(loaded.get("project_sha256", "")) != project_sha256:
        raise ValueError("Execution evidence project_sha256 does not match the analyzed PLC artifact")
    if str(loaded.get("test_plan_sha256", "")) != plan_sha256:
        raise ValueError("Execution evidence test_plan_sha256 does not match the generated FAT plan")
    backend = str(loaded.get("backend") or "").strip()
    run_id = str(loaded.get("run_id") or "").strip()
    if not backend or not run_id:
        raise ValueError("Execution evidence requires non-empty backend and run_id")
    qualification = require_qualified_backend(backend_registry, backend, project_sha256)
    assert qualification.id == backend
    if loaded.get("backend_registry_sha256") != backend_registry.source_sha256:
        raise ValueError("Execution evidence backend_registry_sha256 does not match the supplied qualification registry")
    rows = loaded.get("results")
    if not isinstance(rows, list):
        raise ValueError("Execution evidence requires a results list")
    result: list[TestExecutionEvidence] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Execution evidence result entries must be objects")
        test_id = str(row.get("test_id") or "")
        if test_id not in test_ids:
            raise ValueError(f"Execution evidence references unknown test_id: {test_id}")
        if test_id in seen:
            raise ValueError(f"Execution evidence contains duplicate test_id: {test_id}")
        seen.add(test_id)
        try:
            status = ExecutionStatus(str(row.get("status") or "").upper())
        except ValueError as exc:
            raise ValueError(f"Unsupported execution status for {test_id}: {row.get('status')}") from exc
        raw_evidence = row.get("evidence", [])
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 32:
            raise ValueError(f"Execution evidence for {test_id} requires an evidence list with at most 32 items")
        evidence_items = tuple(str(item) for item in raw_evidence if str(item).strip())
        if any(len(item) > 2048 for item in evidence_items):
            raise ValueError(f"Execution evidence reference for {test_id} exceeds 2048 characters")
        result.append(
            TestExecutionEvidence(
                test_id,
                status,
                backend[:256],
                run_id[:256],
                str(row.get("observed"))[:8192] if row.get("observed") is not None else None,
                str(row.get("timestamp"))[:128] if row.get("timestamp") is not None else None,
                evidence_items,
            )
        )
    return result


def promote_requirement_execution(verifications: list[RequirementVerification], executions: list[TestExecutionEvidence]) -> list[RequirementVerification]:
    statuses = {item.test_id: item.status for item in executions}
    result: list[RequirementVerification] = []
    for item in verifications:
        if item.status is RequirementStatus.STATICALLY_VERIFIED and item.linked_test_ids:
            known = [statuses.get(test_id, ExecutionStatus.NOT_RUN) for test_id in item.linked_test_ids]
            if known and all(status is ExecutionStatus.PASS for status in known):
                item = replace(item, status=RequirementStatus.DYNAMICALLY_VERIFIED, summary=item.summary + " Linked qualified-backend execution evidence passed.")
            elif any(status is ExecutionStatus.FAIL for status in known):
                item = replace(item, status=RequirementStatus.CONFLICT, summary=item.summary + " Linked qualified-backend execution evidence contains a FAIL result.")
        result.append(item)
    return result
