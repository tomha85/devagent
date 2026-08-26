from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PLCOutcome(str, Enum):
    STATICALLY_VERIFIED = "STATICALLY_VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    BLOCKED = "BLOCKED"


class StaticCheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    NOT_PROVEN = "NOT_PROVEN"


class PLCSemanticState(str, Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    OPAQUE = "OPAQUE"


@dataclass(frozen=True)
class PLCSourceRef:
    artifact: str
    controller: str
    program: str | None = None
    routine: str | None = None
    rung: str | None = None
    aoi: str | None = None
    line: str | None = None

    @property
    def locator(self) -> str:
        parts = [self.controller]
        if self.aoi:
            parts.append(f"AOI {self.aoi}")
        if self.program:
            parts.append(self.program)
        if self.routine:
            parts.append(self.routine)
        if self.rung is not None:
            parts.append(f"Rung {self.rung}")
        if self.line is not None:
            parts.append(f"Line {self.line}")
        return " / ".join(parts)


@dataclass(frozen=True)
class PLCTag:
    id: str
    name: str
    scope: str
    data_type: str
    tag_type: str | None = None
    alias_for: str | None = None
    external_access: str | None = None
    constant: bool = False
    description: str | None = None


@dataclass(frozen=True)
class PLCDataTypeMember:
    name: str
    data_type: str
    dimension: str | None = None
    radix: str | None = None
    hidden: bool = False
    description: str | None = None


@dataclass(frozen=True)
class PLCDataType:
    id: str
    name: str
    family: str | None = None
    members: tuple[PLCDataTypeMember, ...] = ()


@dataclass(frozen=True)
class PLCModule:
    id: str
    name: str
    catalog_number: str | None = None
    vendor: str | None = None


@dataclass(frozen=True)
class PLCTask:
    id: str
    name: str
    task_type: str | None = None
    priority: str | None = None
    rate: str | None = None
    scheduled_programs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PLCAOIParameter:
    name: str
    usage: str
    data_type: str | None = None
    required: bool = False
    visible: bool = False
    system_defined: bool = False


@dataclass(frozen=True)
class PLCAddOnInstruction:
    id: str
    name: str
    parameters: tuple[PLCAOIParameter, ...] = ()
    source_protected: bool = False
    routine_ids: tuple[str, ...] = ()
    internal_body_modeled: bool = False


@dataclass(frozen=True)
class PLCInstruction:
    name: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class PLCBooleanTerm:
    tag: str
    required: bool


@dataclass(frozen=True)
class PLCLogicPath:
    terms: tuple[PLCBooleanTerm, ...]


@dataclass(frozen=True)
class PLCOutputLogic:
    id: str
    output_tag: str
    instruction: str
    paths: tuple[PLCLogicPath, ...]
    source: PLCSourceRef
    language: str = "RLL"
    origin: str = "RUNG"
    semantic_state: PLCSemanticState = PLCSemanticState.FULL


@dataclass(frozen=True)
class PLCLogicStatement:
    id: str
    language: str
    owner_type: str
    owner_name: str
    routine: str
    locator: str
    text: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    calls: tuple[str, ...]
    semantic_state: PLCSemanticState
    source: PLCSourceRef


@dataclass(frozen=True)
class PLCRung:
    id: str
    program: str
    routine: str
    number: str
    text: str
    comment: str | None
    instructions: tuple[PLCInstruction, ...]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    calls: tuple[str, ...]
    references: tuple[str, ...]
    source: PLCSourceRef


@dataclass(frozen=True)
class PLCRoutine:
    id: str
    program: str
    name: str
    routine_type: str
    source_protected: bool
    rung_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PLCProgram:
    id: str
    name: str
    tag_ids: tuple[str, ...]
    routine_ids: tuple[str, ...]
    main_routine_name: str | None = None
    fault_routine_name: str | None = None


@dataclass(frozen=True)
class PLCProjectMetadata:
    vendor: str
    engineering_tool: str
    source_path: str
    source_sha256: str
    schema_revision: str | None
    software_revision: str | None
    target_type: str | None
    controller_name: str
    processor_type: str | None
    major_revision: str | None
    minor_revision: str | None
    full_project: bool
    major_fault_program: str | None = None


@dataclass
class CanonicalPLCProject:
    metadata: PLCProjectMetadata
    tags: list[PLCTag] = field(default_factory=list)
    data_types: list[PLCDataType] = field(default_factory=list)
    modules: list[PLCModule] = field(default_factory=list)
    tasks: list[PLCTask] = field(default_factory=list)
    aois: list[PLCAddOnInstruction] = field(default_factory=list)
    programs: list[PLCProgram] = field(default_factory=list)
    routines: list[PLCRoutine] = field(default_factory=list)
    rungs: list[PLCRung] = field(default_factory=list)
    logic_statements: list[PLCLogicStatement] = field(default_factory=list)
    output_logic: list[PLCOutputLogic] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown_instruction_names: list[str] = field(default_factory=list)
    partially_modeled_instruction_names: list[str] = field(default_factory=list)
    instruction_total: int = 0
    instruction_semantic_count: int = 0
    st_statement_total: int = 0
    st_statement_semantic_count: int = 0
    branch_rung_total: int = 0
    branch_rung_semantic_count: int = 0
    aoi_internal_total: int = 0
    aoi_internal_modeled_count: int = 0
    aoi_call_total: int = 0
    aoi_call_bound_count: int = 0

    @property
    def instruction_semantic_coverage(self) -> float:
        if self.instruction_total == 0:
            return 1.0
        return self.instruction_semantic_count / self.instruction_total

    @property
    def st_semantic_coverage(self) -> float:
        if self.st_statement_total == 0:
            return 1.0
        return self.st_statement_semantic_count / self.st_statement_total

    @property
    def branch_semantic_coverage(self) -> float:
        if self.branch_rung_total == 0:
            return 1.0
        return self.branch_rung_semantic_count / self.branch_rung_total


@dataclass(frozen=True)
class PLCDependencyEdge:
    source: str
    target: str
    kind: str
    evidence_id: str


@dataclass
class PLCDependencyGraph:
    edges: list[PLCDependencyEdge] = field(default_factory=list)
    unknown_instruction_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FATTestCase:
    id: str
    title: str
    source: PLCSourceRef
    output_tag: str
    preconditions: dict[str, bool]
    expected: str
    method: str = "STATIC_CANDIDATE"
    execution_status: str = "NOT_RUN"
    limitations: tuple[str, ...] = ()
    scenario: str = "POSITIVE_PATH"


@dataclass(frozen=True)
class StaticCheck:
    id: str
    status: StaticCheckStatus
    summary: str
    evidence: tuple[str, ...] = ()


@dataclass
class PLCEngineeringResult:
    outcome: PLCOutcome
    project: CanonicalPLCProject
    graph: PLCDependencyGraph
    fat_tests: list[FATTestCase]
    static_checks: list[StaticCheck]
    limitations: list[str]


def plc_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return plc_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): plc_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plc_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
