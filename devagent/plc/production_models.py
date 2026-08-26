from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StageStatus(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"


class RequirementStatus(str, Enum):
    STATICALLY_VERIFIED = "STATICALLY_VERIFIED"
    DYNAMICALLY_VERIFIED = "DYNAMICALLY_VERIFIED"
    TRACEABLE_NOT_PROVEN = "TRACEABLE_NOT_PROVEN"
    AI_CANDIDATE = "AI_CANDIDATE"
    NOT_MAPPED = "NOT_MAPPED"
    CONFLICT = "CONFLICT"


class RequirementVerificationMode(str, Enum):
    DYNAMIC = "DYNAMIC"
    STATIC = "STATIC"


class RequirementCriticality(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ExecutionStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"


class ReadinessStatus(str, Enum):
    BLOCKED = "BLOCKED"
    NOT_READY = "NOT_READY"
    CONDITIONALLY_READY = "CONDITIONALLY_READY"
    READY_FOR_ENGINEERING_APPROVAL = "READY_FOR_ENGINEERING_APPROVAL"
    APPROVED_FOR_RELEASE = "APPROVED_FOR_RELEASE"


@dataclass(frozen=True)
class StageRecord:
    number: int
    name: str
    status: StageStatus
    summary: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PLCRequirement:
    id: str
    text: str
    source_path: str
    source_locator: str
    source_sha256: str
    verification_mode: RequirementVerificationMode = RequirementVerificationMode.DYNAMIC
    criticality: RequirementCriticality = RequirementCriticality.HIGH


@dataclass(frozen=True)
class RequirementVerification:
    requirement_id: str
    status: RequirementStatus
    summary: str
    evidence_ids: tuple[str, ...] = ()
    matched_tags: tuple[str, ...] = ()
    linked_test_ids: tuple[str, ...] = ()
    confidence: float = 1.0
    ai_assisted: bool = False


@dataclass(frozen=True)
class EngineeringFinding:
    id: str
    category: str
    title: str
    severity: Severity
    summary: str
    recommendation: str
    evidence_ids: tuple[str, ...]
    confidence: float = 1.0
    origin: str = "DETERMINISTIC"


@dataclass(frozen=True)
class RiskFinding:
    id: str
    category: str
    title: str
    severity: Severity
    summary: str
    consequence: str
    recommendation: str
    evidence_ids: tuple[str, ...]
    confidence: float = 1.0
    origin: str = "DETERMINISTIC"


@dataclass(frozen=True)
class OptimizationCandidate:
    id: str
    category: str
    title: str
    current_state: str
    proposed_change: str
    expected_benefit: str
    change_risk: Severity
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class TestExecutionEvidence:
    test_id: str
    status: ExecutionStatus
    backend: str
    run_id: str
    observed: str | None = None
    timestamp: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegressionChange:
    id: str
    change_type: str
    subject: str
    summary: str
    affected_tags: tuple[str, ...] = ()
    affected_requirement_ids: tuple[str, ...] = ()
    affected_test_ids: tuple[str, ...] = ()
    severity: Severity = Severity.MEDIUM
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    id: str
    priority: Severity
    title: str
    action: str
    rationale: str
    evidence_ids: tuple[str, ...]
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    kind: str
    summary: str
    source_locator: str | None = None
    source_sha256: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReleaseReadiness:
    status: ReadinessStatus
    score: int
    summary: str
    blockers: tuple[str, ...]
    conditions: tuple[str, ...]
    metrics: dict[str, Any]
    human_approval_required: bool = True
    human_approval: dict[str, Any] | None = None


@dataclass
class PLCProductionResult:
    engineering: Any
    stages: list[StageRecord] = field(default_factory=list)
    requirements: list[PLCRequirement] = field(default_factory=list)
    requirement_verification: list[RequirementVerification] = field(default_factory=list)
    engineering_findings: list[EngineeringFinding] = field(default_factory=list)
    risks: list[RiskFinding] = field(default_factory=list)
    optimizations: list[OptimizationCandidate] = field(default_factory=list)
    executions: list[TestExecutionEvidence] = field(default_factory=list)
    regression_changes: list[RegressionChange] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    readiness: ReleaseReadiness | None = None
    warnings: list[str] = field(default_factory=list)
    ai_provider: str | None = None
    ai_model: str | None = None
    execution_backend_registry: dict[str, Any] | None = None
    execution_backend_registry_sha256: str | None = None
    execution_backend_id: str | None = None
    execution_backend_kind: str | None = None
    execution_results_sha256: str | None = None
    baseline_sha256: str | None = None
    release_policy: dict[str, Any] | None = None
    release_policy_sha256: str | None = None
    trust_store: dict[str, Any] | None = None
    trust_store_sha256: str | None = None
    verified_signatures: list[dict[str, Any]] = field(default_factory=list)
    verification_context_sha256: str | None = None
