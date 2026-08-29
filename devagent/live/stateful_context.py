from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from .vendor_qualification import canonical_vendor_name


class LiveStatefulKind(str, Enum):
    TIMER = "TIMER"
    COUNTER = "COUNTER"
    STATE_MACHINE = "STATE_MACHINE"


class LiveStatefulDiagnosisStatus(str, Enum):
    TRANSITION_READY = "TRANSITION_READY"
    TRANSITION_BLOCKED = "TRANSITION_BLOCKED"
    STATE_OBSERVED = "STATE_OBSERVED"
    INDETERMINATE = "INDETERMINATE"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class LiveStatefulTransition:
    source_state: str
    target_state: str
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    runtime_dependencies: tuple[str, ...]
    source_locator: str


@dataclass(frozen=True)
class LiveStatefulModel:
    id: str
    vendor: str
    kind: LiveStatefulKind
    name: str
    instruction: str
    semantic_state: str
    source_locator: str
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...] = ()
    input_refs: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    transitions: tuple[LiveStatefulTransition, ...] = ()
    runtime_dependencies: tuple[str, ...] = ()
    runtime_expectation: str = ""


@dataclass(frozen=True)
class LiveStatefulCoverageReport:
    vendor: str
    models: tuple[LiveStatefulModel, ...]
    limitations: tuple[str, ...]

    @property
    def state_machines(self) -> int:
        return sum(item.kind is LiveStatefulKind.STATE_MACHINE for item in self.models)

    @property
    def timers(self) -> int:
        return sum(item.kind is LiveStatefulKind.TIMER for item in self.models)

    @property
    def counters(self) -> int:
        return sum(item.kind is LiveStatefulKind.COUNTER for item in self.models)

    @property
    def full_models(self) -> int:
        return sum(str(item.semantic_state).upper() == "FULL" for item in self.models)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "devagent-live-stateful-context-v1",
            "mode": "READ_ONLY",
            "vendor": self.vendor,
            "counts": {
                "models": len(self.models),
                "state_machines": self.state_machines,
                "timers": self.timers,
                "counters": self.counters,
                "full_models": self.full_models,
            },
            "models": [
                {
                    "id": item.id,
                    "kind": item.kind.value,
                    "name": item.name,
                    "instruction": item.instruction,
                    "semantic_state": item.semantic_state,
                    "source_locator": item.source_locator,
                    "input_refs": list(item.input_refs),
                    "states": list(item.states),
                    "runtime_dependencies": list(item.runtime_dependencies),
                    "runtime_expectation": item.runtime_expectation,
                    "transitions": [
                        {
                            "source_state": transition.source_state,
                            "target_state": transition.target_state,
                            "guard_paths": [
                                [[name, required] for name, required in path]
                                for path in transition.guard_paths
                            ],
                            "runtime_dependencies": list(transition.runtime_dependencies),
                            "source_locator": transition.source_locator,
                        }
                        for transition in item.transitions
                    ],
                }
                for item in self.models
            ],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class LiveStatefulDiagnosis:
    model_id: str
    name: str
    status: LiveStatefulDiagnosisStatus
    current_state: str | None
    candidate_targets: tuple[str, ...]
    blocking_conditions: tuple[str, ...]
    unknown_conditions: tuple[str, ...]
    source_locators: tuple[str, ...]
    detail: str


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _source_locator(source: Any) -> str:
    return str(getattr(source, "locator", source) or "").strip()


def _guard_paths(raw_paths: Iterable[Any]) -> tuple[tuple[tuple[str, bool], ...], ...]:
    result: list[tuple[tuple[str, bool], ...]] = []
    for raw_path in raw_paths:
        terms = getattr(raw_path, "terms", None)
        if terms is not None:
            result.append(
                tuple(
                    (str(getattr(term, "tag", "") or "").strip(), bool(getattr(term, "required", False)))
                    for term in terms
                    if str(getattr(term, "tag", "") or "").strip()
                )
            )
            continue
        result.append(
            tuple(
                (str(name).strip(), bool(required))
                for name, required in tuple(raw_path or ())
                if str(name).strip()
            )
        )
    return tuple(result)


def _rockwell_models(project: Any) -> tuple[LiveStatefulModel, ...]:
    from devagent.plc.rockwell_stateful_runtime import stateful_models

    result: list[LiveStatefulModel] = []
    for raw in stateful_models(project):
        instruction = str(raw.instruction).upper()
        kind = (
            LiveStatefulKind.TIMER
            if instruction in {"TON", "TOF", "RTO"}
            else LiveStatefulKind.COUNTER
        )
        result.append(
            LiveStatefulModel(
                id=str(raw.id),
                vendor="ROCKWELL",
                kind=kind,
                name=str(raw.structure_tag),
                instruction=instruction,
                semantic_state="RUNTIME_REQUIRED",
                source_locator=_source_locator(raw.source),
                guard_paths=_guard_paths(raw.paths),
                input_refs=tuple(str(item) for item in raw.input_refs),
                runtime_expectation=str(raw.runtime_expectation),
            )
        )
    return tuple(result)


def _siemens_models(project: Any) -> tuple[LiveStatefulModel, ...]:
    facts = getattr(project, "_siemens_v5_state_machine_facts", None)
    if facts is None:
        return ()
    result: list[LiveStatefulModel] = []
    for machine in tuple(getattr(facts, "machines", ()) or ()):
        transitions = tuple(
            LiveStatefulTransition(
                source_state=str(item.source_state),
                target_state=str(item.target_state),
                guard_paths=_guard_paths(item.guard_paths),
                runtime_dependencies=tuple(str(dep) for dep in item.runtime_dependencies),
                source_locator=f"{machine.block}:{item.source_line}",
            )
            for item in machine.transitions
        )
        result.append(
            LiveStatefulModel(
                id=str(machine.id),
                vendor="SIEMENS",
                kind=LiveStatefulKind.STATE_MACHINE,
                name=str(machine.state_tag),
                instruction="CASE_STATE_MACHINE",
                semantic_state=_enum_text(machine.semantic_state),
                source_locator=f"{machine.block}:{machine.case_line}-{machine.end_line}",
                states=tuple(str(item) for item in machine.states),
                transitions=transitions,
                runtime_dependencies=tuple(str(item) for item in machine.runtime_dependencies),
                runtime_expectation=(
                    "Current state and transition guards can be diagnosed from trusted live values; startup/retentivity, scan timing, and timer/counter evolution remain runtime evidence."
                ),
            )
        )
    return tuple(result)


def _schneider_models(project: Any) -> tuple[LiveStatefulModel, ...]:
    facts = getattr(project, "_schneider_v5_facts", None)
    if facts is None:
        return ()
    result: list[LiveStatefulModel] = []
    for machine in tuple(getattr(facts, "machines", ()) or ()):
        transitions = tuple(
            LiveStatefulTransition(
                source_state=str(item.source_state),
                target_state=str(item.target_state),
                guard_paths=_guard_paths(item.guard_paths),
                runtime_dependencies=tuple(str(dep) for dep in item.runtime_dependencies),
                source_locator=f"{machine.relative_path}:{machine.section}:{item.source_line}",
            )
            for item in machine.transitions
        )
        result.append(
            LiveStatefulModel(
                id=str(machine.id),
                vendor="SCHNEIDER",
                kind=LiveStatefulKind.STATE_MACHINE,
                name=str(machine.state_tag),
                instruction="CASE_STATE_MACHINE",
                semantic_state=_enum_text(machine.semantic_state),
                source_locator=(
                    f"{machine.relative_path}:{machine.section}:{machine.case_line}-{machine.end_line}"
                ),
                states=tuple(str(item) for item in machine.states),
                transitions=transitions,
                runtime_dependencies=tuple(str(item) for item in machine.runtime_dependencies),
                runtime_expectation=(
                    "Current state and transition guards can be diagnosed from trusted live values; retained state, scan timing, and timer/counter evolution remain runtime evidence."
                ),
            )
        )
    return tuple(result)


def extract_live_stateful_models(project: Any) -> tuple[LiveStatefulModel, ...]:
    vendor = canonical_vendor_name(project)
    if vendor == "ROCKWELL":
        return _rockwell_models(project)
    if vendor == "SIEMENS":
        return _siemens_models(project)
    if vendor == "SCHNEIDER":
        return _schneider_models(project)
    return ()


def build_live_stateful_coverage(project: Any) -> LiveStatefulCoverageReport:
    vendor = canonical_vendor_name(project)
    models = extract_live_stateful_models(project)
    limitations = [
        "Stateful diagnosis is read-only and evidence bounded.",
        "Timer/counter elapsed time, edge storage, prescan, retained memory, and physical process behavior are never inferred from static code alone.",
    ]
    if vendor == "ROCKWELL":
        limitations.append(
            "Rockwell TON/TOF/RTO/CTU/CTD source enable conditions are modeled; definitive .ACC/.DN/count evolution requires live controller evidence."
        )
    elif vendor in {"SIEMENS", "SCHNEIDER"}:
        limitations.append(
            "Bounded CASE state-machine transitions are consumed from the existing vendor theorem; partial machines remain fail-closed."
        )
    else:
        limitations.append("No supported vendor-specific stateful context was recognized.")
    return LiveStatefulCoverageReport(
        vendor=vendor,
        models=models,
        limitations=tuple(limitations),
    )


def _lookup(observations: Mapping[str, Any], name: str) -> tuple[bool, Any]:
    target = str(name).casefold()
    for key, value in observations.items():
        if str(key).casefold() == target:
            return True, value
    return False, None


def _evaluate_paths(
    paths: tuple[tuple[tuple[str, bool], ...], ...],
    observations: Mapping[str, Any],
) -> tuple[bool | None, tuple[str, ...], tuple[str, ...]]:
    if not paths:
        return None, (), ()
    all_blockers: list[str] = []
    unknowns: list[str] = []
    saw_indeterminate = False
    for path in paths:
        path_blockers: list[str] = []
        path_unknowns: list[str] = []
        for name, required in path:
            present, value = _lookup(observations, name)
            if not present or not isinstance(value, bool):
                path_unknowns.append(name)
            elif value is not required:
                path_blockers.append(f"{name}={value} requires {required}")
        if not path_blockers and not path_unknowns:
            return True, (), ()
        if path_unknowns:
            saw_indeterminate = True
            unknowns.extend(path_unknowns)
        all_blockers.extend(path_blockers)
    if saw_indeterminate:
        return None, tuple(dict.fromkeys(all_blockers)), tuple(dict.fromkeys(unknowns))
    return False, tuple(dict.fromkeys(all_blockers)), ()


def diagnose_live_stateful_model(
    model: LiveStatefulModel,
    observations: Mapping[str, Any],
) -> LiveStatefulDiagnosis:
    if model.kind in {LiveStatefulKind.TIMER, LiveStatefulKind.COUNTER}:
        enabled, blockers, unknown = _evaluate_paths(model.guard_paths, observations)
        if enabled is False:
            return LiveStatefulDiagnosis(
                model_id=model.id,
                name=model.name,
                status=LiveStatefulDiagnosisStatus.TRANSITION_BLOCKED,
                current_state=None,
                candidate_targets=(),
                blocking_conditions=blockers,
                unknown_conditions=(),
                source_locators=(model.source_locator,),
                detail=f"{model.instruction} enable/rung condition is blocked before stateful time/count evolution.",
            )
        if enabled is None:
            return LiveStatefulDiagnosis(
                model_id=model.id,
                name=model.name,
                status=LiveStatefulDiagnosisStatus.INDETERMINATE,
                current_state=None,
                candidate_targets=(),
                blocking_conditions=blockers,
                unknown_conditions=unknown,
                source_locators=(model.source_locator,),
                detail=(
                    f"{model.instruction} source model is known, but live Boolean prerequisites are incomplete."
                ),
            )
        return LiveStatefulDiagnosis(
            model_id=model.id,
            name=model.name,
            status=LiveStatefulDiagnosisStatus.STATE_OBSERVED,
            current_state=None,
            candidate_targets=(),
            blocking_conditions=(),
            unknown_conditions=(),
            source_locators=(model.source_locator,),
            detail=(
                f"{model.instruction} enable condition is currently satisfied. Definitive time/count completion still requires live state pins (.ACC/.DN or vendor equivalent) and history; DevAgent does not infer them."
            ),
        )

    present, raw_state = _lookup(observations, model.name)
    if not present:
        return LiveStatefulDiagnosis(
            model_id=model.id,
            name=model.name,
            status=LiveStatefulDiagnosisStatus.INDETERMINATE,
            current_state=None,
            candidate_targets=(),
            blocking_conditions=(),
            unknown_conditions=(model.name,),
            source_locators=(model.source_locator,),
            detail="Current state-machine value is not available as trusted live evidence.",
        )
    current = str(raw_state)
    outgoing = [
        transition
        for transition in model.transitions
        if transition.source_state.casefold() == current.casefold()
    ]
    if not outgoing:
        return LiveStatefulDiagnosis(
            model_id=model.id,
            name=model.name,
            status=LiveStatefulDiagnosisStatus.STATE_OBSERVED,
            current_state=current,
            candidate_targets=(),
            blocking_conditions=(),
            unknown_conditions=(),
            source_locators=(model.source_locator,),
            detail=(
                f"State {current!r} has no modeled outgoing transition in the bounded canonical state-machine facts."
            ),
        )

    ready: list[str] = []
    blockers: list[str] = []
    unknowns: list[str] = []
    locators: list[str] = [model.source_locator]
    for transition in outgoing:
        value, path_blockers, path_unknowns = _evaluate_paths(
            transition.guard_paths,
            observations,
        )
        locators.append(transition.source_locator)
        if value is True:
            ready.append(transition.target_state)
        elif value is False:
            blockers.extend(path_blockers)
        else:
            blockers.extend(path_blockers)
            unknowns.extend(path_unknowns)

    if ready:
        return LiveStatefulDiagnosis(
            model_id=model.id,
            name=model.name,
            status=LiveStatefulDiagnosisStatus.TRANSITION_READY,
            current_state=current,
            candidate_targets=tuple(dict.fromkeys(ready)),
            blocking_conditions=(),
            unknown_conditions=tuple(dict.fromkeys(unknowns)),
            source_locators=tuple(dict.fromkeys(locators)),
            detail=(
                "At least one modeled transition guard is satisfied. This proves source/live guard readiness, not scan-order execution or physical transition completion."
            ),
        )
    if unknowns:
        return LiveStatefulDiagnosis(
            model_id=model.id,
            name=model.name,
            status=LiveStatefulDiagnosisStatus.INDETERMINATE,
            current_state=current,
            candidate_targets=(),
            blocking_conditions=tuple(dict.fromkeys(blockers)),
            unknown_conditions=tuple(dict.fromkeys(unknowns)),
            source_locators=tuple(dict.fromkeys(locators)),
            detail="No transition can be proven ready because one or more required guard values are unavailable/untrusted.",
        )
    return LiveStatefulDiagnosis(
        model_id=model.id,
        name=model.name,
        status=LiveStatefulDiagnosisStatus.TRANSITION_BLOCKED,
        current_state=current,
        candidate_targets=(),
        blocking_conditions=tuple(dict.fromkeys(blockers)),
        unknown_conditions=(),
        source_locators=tuple(dict.fromkeys(locators)),
        detail="All modeled outgoing transition paths are currently blocked by trusted live guard values.",
    )


__all__ = [
    "LiveStatefulKind",
    "LiveStatefulDiagnosisStatus",
    "LiveStatefulTransition",
    "LiveStatefulModel",
    "LiveStatefulCoverageReport",
    "LiveStatefulDiagnosis",
    "extract_live_stateful_models",
    "build_live_stateful_coverage",
    "diagnose_live_stateful_model",
]
