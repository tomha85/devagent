from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TaskType(str, Enum):
    BUG_FIX = "BUG_FIX"
    FEATURE = "FEATURE"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    UNIT_TEST = "UNIT_TEST"
    REFACTOR = "REFACTOR"
    MIGRATION = "MIGRATION"
    PERFORMANCE = "PERFORMANCE"
    GENERAL_ENGINEERING_TASK = "GENERAL_ENGINEERING_TASK"


class AgentState(str, Enum):
    PREFLIGHT = "PREFLIGHT"
    DISCOVER = "DISCOVER"
    UNDERSTAND = "UNDERSTAND"
    TASK_SPEC = "TASK_SPEC"
    BASELINE = "BASELINE"
    PLAN = "PLAN"
    GATHER_CONTEXT = "GATHER_CONTEXT"
    REPRODUCE = "REPRODUCE"
    IMPLEMENT = "IMPLEMENT"
    VERIFY_TARGETED = "VERIFY_TARGETED"
    DIAGNOSE = "DIAGNOSE"
    VERIFY_BROAD = "VERIFY_BROAD"
    REVIEW = "REVIEW"
    QUALITY_CHECK = "QUALITY_CHECK"
    FINAL_VERIFY = "FINAL_VERIFY"
    LEARN = "LEARN"
    REPORT = "REPORT"
    SUCCESS = "SUCCESS"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    BLOCKED = "BLOCKED"


class Outcome(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    BLOCKED = "BLOCKED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AcceptanceSource(str, Enum):
    USER = "USER"
    REPOSITORY = "REPOSITORY"
    TASK_POLICY = "TASK_POLICY"
    QUALITY_GATE = "QUALITY_GATE"


class AcceptanceStatus(str, Enum):
    UNPROVEN = "UNPROVEN"
    SATISFIED = "SATISFIED"
    CONTRADICTED = "CONTRADICTED"


class FailureClass(str, Enum):
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    TYPE_ERROR = "TYPE_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    BUILD_ERROR = "BUILD_ERROR"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    TIMEOUT = "TIMEOUT"
    FLAKY_TEST = "FLAKY_TEST"
    BASELINE_FAILURE = "BASELINE_FAILURE"
    NEW_REGRESSION = "NEW_REGRESSION"
    UNKNOWN = "UNKNOWN"


class CapabilityProvenance(str, Enum):
    EXPLICIT = "EXPLICIT"
    PROBED = "PROBED"
    INFERRED = "INFERRED"


@dataclass(frozen=True)
class Evidence:
    statement: str
    paths: tuple[str, ...]
    confidence: float = 1.0


@dataclass(frozen=True)
class RepositoryFact:
    fact: str
    confidence: float
    evidence: tuple[str, ...]
    fingerprints: dict[str, str]
    learned_at: str


@dataclass(frozen=True)
class Capability:
    kind: str
    command: tuple[str, ...]
    source: str
    component: str = "."
    broad: bool = False
    provenance: CapabilityProvenance = CapabilityProvenance.EXPLICIT
    provenance_detail: str = "declared by repository configuration"
    tests_collected: int | None = None

    @property
    def trusted(self) -> bool:
        return self.provenance in {CapabilityProvenance.EXPLICIT, CapabilityProvenance.PROBED}


@dataclass
class Component:
    path: str
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    test_locations: list[str] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)


@dataclass
class RepositoryModel:
    root: str
    kind: str
    components: list[Component]
    facts: list[RepositoryFact]
    git_branch: str | None = None
    git_head: str | None = None
    dirty_files: list[str] = field(default_factory=list)
    inventory_file_count: int = 0
    capability_diagnostics: list[str] = field(default_factory=list)

    @property
    def capabilities(self) -> list[Capability]:
        return [capability for component in self.components for capability in component.capabilities]


@dataclass
class AcceptanceCriterion:
    description: str
    required: bool = True
    source: AcceptanceSource = AcceptanceSource.TASK_POLICY
    status: AcceptanceStatus = AcceptanceStatus.UNPROVEN
    evidence: list[str] = field(default_factory=list)
    reason: str | None = None
    verification_command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        # Preserve compatibility for callers constructing already-evidenced criteria.
        # Runtime-compiled criteria start without evidence and must be explicitly adjudicated.
        if self.evidence and self.status is AcceptanceStatus.UNPROVEN:
            self.status = AcceptanceStatus.SATISFIED

    @property
    def satisfied(self) -> bool:
        return self.status is AcceptanceStatus.SATISFIED


@dataclass
class TaskSpec:
    task_type: TaskType
    goal: str
    requires_code_change: bool
    requires_tests: bool
    acceptance_criteria: list[AcceptanceCriterion]
    risk: RiskLevel


@dataclass
class EngineeringPlan:
    files_to_inspect: list[str]
    implementation: list[str]
    verification: list[tuple[str, ...]]
    rationale: str


_PRESERVATION_PATTERNS = (
    re.compile(r"\bpreserv(?:e|es|ed|ing)\s+(?:the\s+)?existing\s+([^.;\n]+)", re.IGNORECASE),
    re.compile(
        r"\bexisting\s+([^.;\n]+?)\s+(?:behavior|behaviour|functionality|semantics)\b",
        re.IGNORECASE,
    ),
)
_PRESERVATION_GENERIC_WORDS = frozenset(
    {
        "and",
        "or",
        "the",
        "a",
        "an",
        "current",
        "normal",
        "same",
        "existing",
        "behavior",
        "behaviour",
        "functionality",
        "semantics",
        "code",
        "system",
        "application",
        "tests",
        "test",
    }
)


def preserved_subjects(text: str) -> set[str]:
    subjects: set[str] = set()
    for pattern in _PRESERVATION_PATTERNS:
        for match in pattern.finditer(text):
            clause = re.split(
                r"\b(?:while|but|and\s+(?:add|implement|verify|create|update|fix))\b",
                match.group(1),
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            clause = re.sub(
                r"\b(?:behavior|behaviour|functionality|semantics)\b",
                " ",
                clause,
                flags=re.IGNORECASE,
            )
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", clause):
                lowered = token.lower()
                if len(lowered) > 2 and lowered not in _PRESERVATION_GENERIC_WORDS:
                    subjects.add(lowered)
    return subjects


def _explicitly_absent(text: str, subject: str) -> bool:
    escaped = re.escape(subject)
    patterns = (
        rf"\bno\s+(?:existing\s+)?{escaped}\b",
        rf"\b{escaped}\s+(?:does\s+not|doesn't)\s+exist\b",
        rf"\b{escaped}\s+(?:is\s+)?(?:absent|missing|not\s+present)\b",
        rf"\bwithout\s+(?:an?\s+)?{escaped}\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


@dataclass
class Understanding:
    problem: str
    expected_behavior: str
    affected_paths: list[str]
    root_cause: str
    evidence: list[Evidence]
    proposed_solution: list[str]
    confidence: float

    def preservation_conflicts(self) -> list[str]:
        """Return preserved subjects that the same grounded understanding says are absent."""

        corpus = "\n".join(
            [
                self.problem,
                self.expected_behavior,
                self.root_cause,
                *(item.statement for item in self.evidence),
            ]
        )
        return sorted(
            subject
            for subject in preserved_subjects(corpus)
            if _explicitly_absent(corpus, subject)
        )

    def implementation_ready(self, root: Path) -> bool:
        if self.confidence < 0.6 or not self.root_cause.strip() or not self.proposed_solution:
            return False
        if not self.affected_paths or not self.evidence:
            return False
        if self.preservation_conflicts():
            return False
        return all((root / path).resolve().is_relative_to(root.resolve()) for path in self.affected_paths)


@dataclass
class VerificationResult:
    command: tuple[str, ...]
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    classification: FailureClass | None
    revision: int
    phase: str
    timed_out: bool = False
    baseline: bool = False
    tests_run: int | None = None
    tests_passed: int | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class ReviewIssue:
    severity: str
    reason: str
    path: str | None = None


@dataclass
class ReviewDecision:
    approved: bool
    issues: list[ReviewIssue]
    summary: str


@dataclass
class ChangeMetrics:
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CodeSymbol:
    path: str
    name: str
    kind: str
    line: int | None = None
    change: str = "MODIFIED"


@dataclass
class DeveloperReviewEvidence:
    changed_symbols: list[CodeSymbol] = field(default_factory=list)
    test_cases: list[CodeSymbol] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SourceControlResult:
    requested: bool = False
    remote: str | None = None
    branch: str | None = None
    commit: str | None = None
    committed: bool = False
    pushed: bool = False
    error: str | None = None
    pull_request_created: bool = False
    merged: bool = False


@dataclass
class RunResult:
    outcome: Outcome
    task: TaskSpec
    repository: RepositoryModel
    run_id: str
    run_dir: str
    root_cause: str
    implementation: list[str]
    changes: ChangeMetrics
    verification: list[VerificationResult]
    review: ReviewDecision | None
    not_run: list[str]
    recommendations: list[str]
    state_history: list[AgentState]
    working_root: str
    developer_review: DeveloperReviewEvidence = field(default_factory=DeveloperReviewEvidence)
    source_control: SourceControlResult = field(default_factory=SourceControlResult)


def jsonable(value: Any) -> Any:
    """Convert nested domain objects to JSON-safe primitive values."""
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value