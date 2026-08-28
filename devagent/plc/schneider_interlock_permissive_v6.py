from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import re

from devagent.plc import schneider_state_machine_v5 as _v5
from devagent.plc.fat_procedure_v12 import enrich_fat_procedures
from devagent.plc.models import (
    FATTestCase,
    PLCEngineeringResult,
    PLCOutcome,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.production_models import (
    EvidenceItem,
    RequirementStatus,
    RequirementVerification,
    RiskFinding,
    Severity,
)
from devagent.plc.production_utils import explicit_bool, stable_id, tag_occurs


_INSTALLED = False
_PREVIOUS_ANALYZER = _v5.analyze_schneider_control_expert_v5
_PREVIOUS_CAPABILITY = _v5.schneider_capability_profile_v5

_PERMISSIVE_TOKENS = {
    "permissive", "permit", "ready", "healthy", "okay", "available", "enable", "enabled",
}
_INTERLOCK_TOKENS = {
    "interlock", "estop", "emergency", "guard", "door", "trip", "tripped",
    "fault", "faulted", "inhibit", "safe", "safety",
}
_RECOVERY_TOKENS = {
    "reset", "recover", "recovery", "ack", "acknowledge", "clear", "cleared", "restart",
}
_MAX_GUARD_TESTS = 1024


@dataclass(frozen=True)
class SchneiderV6GuardTermFact:
    id: str
    contract_id: str
    source_id: str
    path_index: int
    tag: str
    required: bool
    role: str
    description: str | None = None


@dataclass(frozen=True)
class SchneiderV6TransitionGuardContract:
    id: str
    machine_id: str
    transition_ids: tuple[str, ...]
    section: str
    state_tag: str
    source_state: str
    target_state: str
    source_lines: tuple[int, ...]
    guard_text: str
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    terms: tuple[SchneiderV6GuardTermFact, ...]
    all_path_terms: tuple[tuple[str, bool], ...]
    runtime_dependencies: tuple[str, ...]
    semantic_state: PLCSemanticState
    reason: str


@dataclass(frozen=True)
class SchneiderV6OutputGuardContract:
    id: str
    output_logic_id: str
    output_tag: str
    language: str
    source_locator: str
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    terms: tuple[SchneiderV6GuardTermFact, ...]
    all_path_terms: tuple[tuple[str, bool], ...]
    semantic_state: PLCSemanticState
    reason: str


@dataclass(frozen=True)
class SchneiderV6GuardFacts:
    transition_contracts: tuple[SchneiderV6TransitionGuardContract, ...]
    output_contracts: tuple[SchneiderV6OutputGuardContract, ...]


def _semantic_tokens(*values: object) -> set[str]:
    """Tokenize identifiers/descriptions without substring semantics."""
    tokens: set[str] = set()
    for value in values:
        text = str(value or "")
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        tokens.update(
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9]+", text)
            if token
        )
    return tokens


def _tag_metadata(project) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    for tag in project.tags:
        result.setdefault(tag.name.casefold(), (tag.name, tag.description))
    return result


def _role_for_ref(ref: str, description: str | None) -> str:
    base = ref.split(".", 1)[0]
    tokens = _semantic_tokens(base, description)
    if tokens & _RECOVERY_TOKENS:
        return "RECOVERY"
    if tokens & _INTERLOCK_TOKENS:
        return "INTERLOCK"
    if tokens & _PERMISSIVE_TOKENS:
        return "PERMISSIVE"
    return "GUARD"


def _all_path_terms(paths) -> tuple[tuple[str, bool], ...]:
    """Return exact Boolean terms that occur with the same polarity on every path."""
    if not paths:
        return ()
    common = {name.casefold(): (name, required) for name, required in paths[0]}
    for path in paths[1:]:
        current = {name.casefold(): required for name, required in path}
        for key in tuple(common):
            if key not in current or current[key] != common[key][1]:
                common.pop(key, None)
    return tuple(
        sorted(
            [(name, required) for name, required in common.values()],
            key=lambda item: item[0].casefold(),
        )
    )


def _term(*, contract_id: str, source_id: str, path_index: int, ref: str, required: bool, metadata) -> SchneiderV6GuardTermFact:
    base = ref.split(".", 1)[0]
    canonical, description = metadata.get(base.casefold(), (base, None))
    label = ref if "." in ref else canonical
    digest = hashlib.sha1(
        f"{contract_id}:{source_id}:{path_index}:{ref}:{required}".encode()
    ).hexdigest()[:14]
    return SchneiderV6GuardTermFact(
        id=f"SCHNEIDER-GUARD6-{digest}",
        contract_id=contract_id,
        source_id=source_id,
        path_index=path_index,
        tag=label,
        required=required,
        role=_role_for_ref(ref, description),
        description=description,
    )


def _transition_contracts(project, state_facts) -> tuple[SchneiderV6TransitionGuardContract, ...]:
    metadata = _tag_metadata(project)
    result: list[SchneiderV6TransitionGuardContract] = []
    for machine in state_facts.machines:
        grouped: dict[tuple[str, str], list[object]] = defaultdict(list)
        for transition in machine.transitions:
            grouped[(transition.source_state.casefold(), transition.target_state.casefold())].append(transition)
        for (_source_key, _target_key), transitions in grouped.items():
            first = transitions[0]
            digest = hashlib.sha1(
                f"{machine.id}:{first.source_state}:{first.target_state}:guard-v6".encode()
            ).hexdigest()[:14]
            contract_id = f"SCHNEIDER-GC6-{digest}"
            paths: list[tuple[tuple[str, bool], ...]] = []
            terms: list[SchneiderV6GuardTermFact] = []
            path_index = 0
            for transition in transitions:
                for path in transition.guard_paths:
                    normalized = tuple(path)
                    paths.append(normalized)
                    for ref, required in normalized:
                        terms.append(
                            _term(
                                contract_id=contract_id,
                                source_id=transition.id,
                                path_index=path_index,
                                ref=ref,
                                required=required,
                                metadata=metadata,
                            )
                        )
                    path_index += 1
            runtime = tuple(
                sorted(
                    {dep for transition in transitions for dep in transition.runtime_dependencies},
                    key=str.casefold,
                )
            )
            semantic = (
                PLCSemanticState.FULL
                if machine.semantic_state is PLCSemanticState.FULL
                and all(transition.semantic_state is PLCSemanticState.FULL for transition in transitions)
                else PLCSemanticState.PARTIAL
            )
            result.append(
                SchneiderV6TransitionGuardContract(
                    id=contract_id,
                    machine_id=machine.id,
                    transition_ids=tuple(transition.id for transition in transitions),
                    section=machine.section,
                    state_tag=machine.state_tag,
                    source_state=first.source_state,
                    target_state=first.target_state,
                    source_lines=tuple(sorted({transition.source_line for transition in transitions})),
                    guard_text=" OR ".join(f"({transition.guard_text})" for transition in transitions),
                    guard_paths=tuple(paths),
                    terms=tuple(terms),
                    all_path_terms=_all_path_terms(paths),
                    runtime_dependencies=runtime,
                    semantic_state=semantic,
                    reason="bounded_transition_guard_binding" if semantic is PLCSemanticState.FULL else "parent_state_machine_partial",
                )
            )
    return tuple(result)


def _logic_paths(logic) -> tuple[tuple[tuple[str, bool], ...], ...]:
    return tuple(
        tuple((term.tag, bool(term.required)) for term in path.terms)
        for path in logic.paths
    )


def _output_contracts(project) -> tuple[SchneiderV6OutputGuardContract, ...]:
    metadata = _tag_metadata(project)
    counts: dict[str, int] = defaultdict(int)
    for logic in project.output_logic:
        if logic.semantic_state is PLCSemanticState.FULL:
            counts[logic.output_tag.casefold()] += 1
    result: list[SchneiderV6OutputGuardContract] = []
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL:
            continue
        paths = _logic_paths(logic)
        if not paths:
            continue
        digest = hashlib.sha1(f"{logic.id}:output-guard-v6".encode()).hexdigest()[:14]
        contract_id = f"SCHNEIDER-OG6-{digest}"
        terms = tuple(
            _term(
                contract_id=contract_id,
                source_id=logic.id,
                path_index=path_index,
                ref=ref,
                required=required,
                metadata=metadata,
            )
            for path_index, path in enumerate(paths)
            for ref, required in path
        )
        unique = counts[logic.output_tag.casefold()] == 1
        result.append(
            SchneiderV6OutputGuardContract(
                id=contract_id,
                output_logic_id=logic.id,
                output_tag=logic.output_tag,
                language=logic.language,
                source_locator=logic.source.locator,
                guard_paths=paths,
                terms=terms,
                all_path_terms=_all_path_terms(paths) if unique else (),
                semantic_state=PLCSemanticState.FULL if unique else PLCSemanticState.PARTIAL,
                reason="bounded_output_guard_binding" if unique else "ambiguous_multiple_output_theorems",
            )
        )
    return tuple(result)


def _build_guard_facts(project) -> SchneiderV6GuardFacts | None:
    state_facts = _v5._facts(project)
    transitions = _transition_contracts(project, state_facts) if state_facts is not None else ()
    outputs = _output_contracts(project)
    if not transitions and not outputs:
        return None
    return SchneiderV6GuardFacts(transitions, outputs)


def _facts(project) -> SchneiderV6GuardFacts | None:
    return getattr(project, "_schneider_v6_guard_facts", None)


def _all_contracts(facts):
    return (*facts.transition_contracts, *facts.output_contracts)


def schneider_capability_profile_v6(project) -> dict[str, object]:
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-schneider-control-expert-capability-v6"
    if facts is None:
        profile.update(
            {
                "transition_guard_contracts": 0,
                "output_guard_contracts": 0,
                "guard_terms": 0,
                "all_path_guard_terms": 0,
                "all_path_classified_terms": 0,
                "classified_interlock_terms": 0,
                "classified_permissive_terms": 0,
                "classified_recovery_terms": 0,
                "unclassified_guard_terms": 0,
                "runtime_dependent_transition_contracts": 0,
                "guard_contract": "NONE",
                "requirement_guard_proof_contract": "EXPLICIT_EVERY_PATH_ONLY",
            }
        )
        return profile
    contracts = _all_contracts(facts)
    terms = [term for contract in contracts for term in contract.terms]
    partial = [contract for contract in contracts if contract.semantic_state is not PLCSemanticState.FULL]
    metadata = _tag_metadata(project)
    all_path_pairs = [(tag, required) for contract in contracts for tag, required in contract.all_path_terms]
    all_path_classified = 0
    for tag, _required in all_path_pairs:
        base = tag.split(".", 1)[0]
        _canonical, description = metadata.get(base.casefold(), (base, None))
        if _role_for_ref(tag, description) != "GUARD":
            all_path_classified += 1
    profile.update(
        {
            "transition_guard_contracts": len(facts.transition_contracts),
            "output_guard_contracts": len(facts.output_contracts),
            "guard_terms": len(terms),
            "all_path_guard_terms": len(all_path_pairs),
            "all_path_classified_terms": all_path_classified,
            "classified_interlock_terms": sum(term.role == "INTERLOCK" for term in terms),
            "classified_permissive_terms": sum(term.role == "PERMISSIVE" for term in terms),
            "classified_recovery_terms": sum(term.role == "RECOVERY" for term in terms),
            "unclassified_guard_terms": sum(term.role == "GUARD" for term in terms),
            "runtime_dependent_transition_contracts": sum(bool(contract.runtime_dependencies) for contract in facts.transition_contracts),
            "guard_contract": "COMPLETE" if contracts and not partial else "PARTIAL_FAIL_CLOSED" if contracts else "NONE",
            "requirement_guard_proof_contract": "EXPLICIT_EVERY_PATH_ONLY",
            "bounded_guard_contract": (
                "V6 consumes only FULL V1-V5 Boolean/output and CASE transition theorems. "
                "It computes exact DNF path conditions plus their all-path intersection. "
                "Role classification uses exported identifier/description tokens only; "
                "no safe polarity, SIL/PL, or process-safety meaning is inferred."
            ),
        }
    )
    return profile


def _source_for_transition(project, contract):
    lines = set(contract.source_lines)
    for statement in project.logic_statements:
        if statement.language != "ST":
            continue
        owner = statement.source.routine or statement.routine or statement.owner_name or ""
        if owner.casefold() == contract.section.casefold() and _v5._statement_line(statement) in lines:
            return statement.source
    for statement in project.logic_statements:
        if statement.language != "ST":
            continue
        owner = statement.source.routine or statement.routine or statement.owner_name or ""
        if owner.casefold() == contract.section.casefold():
            return statement.source
    return None


def _source_for_output(project, contract):
    for logic in project.output_logic:
        if logic.id == contract.output_logic_id:
            return logic.source
    return None


def _guard_fat(project, facts) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for contract in facts.transition_contracts:
        source = _source_for_transition(project, contract)
        if source is None:
            continue
        for path_index, path in enumerate(contract.guard_paths):
            if len(tests) >= _MAX_GUARD_TESTS:
                break
            if not path:
                continue
            digest = hashlib.sha1(f"{contract.id}:permit:{path_index}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-GUARD6-{digest}",
                    title=f"Verify transition guard path for {contract.state_tag}: {contract.source_state}->{contract.target_state}",
                    source=source,
                    output_tag=contract.state_tag,
                    preconditions=dict(path),
                    expected=(
                        f"Starting from {contract.state_tag}={contract.source_state}, the exact bounded source guard path {dict(path)} "
                        f"permits transition to {contract.target_state}. Runtime evidence confirms actual scan/I/O/process behavior."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_GUARD_PERMIT",
                    limitations=(
                        "Boolean authority comes from Control Expert source semantics; role labels are metadata classifications only.",
                        "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                    ),
                    watch_tags=tuple(dict.fromkeys((contract.state_tag, *(ref for ref, _ in path)))),
                )
            )
            for term_index, (ref, required) in enumerate(path):
                if len(tests) >= _MAX_GUARD_TESTS:
                    break
                blocked = dict(path)
                blocked[ref] = not required
                bdigest = hashlib.sha1(f"{contract.id}:path-block:{path_index}:{term_index}".encode()).hexdigest()[:10]
                tests.append(
                    FATTestCase(
                        id=f"FAT-SCHNEIDER-GUARD6-{bdigest}",
                        title=f"Verify one transition path is denied when {ref} is inverted",
                        source=source,
                        output_tag=contract.state_tag,
                        preconditions=blocked,
                        expected=(
                            f"The selected bounded source path is false because {ref}={'TRUE' if not required else 'FALSE'}. "
                            "Other paths remain possible unless this term is present on every path."
                        ),
                        method="RUNTIME_FAT_REQUIRED",
                        scenario="SCHNEIDER_GUARD_PATH_BLOCK",
                        limitations=(
                            "Denying one DNF path does not deny the whole transition when another source path exists.",
                            "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                        ),
                        watch_tags=tuple(dict.fromkeys((contract.state_tag, *(name for name, _ in path)))),
                    )
                )
        for ref, required in contract.all_path_terms:
            if len(tests) >= _MAX_GUARD_TESTS:
                break
            digest = hashlib.sha1(f"{contract.id}:all-path:{ref}:{required}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-GUARD6-{digest}",
                    title=f"Verify every transition path is denied when {ref} is inverted",
                    source=source,
                    output_tag=contract.state_tag,
                    preconditions={ref: not required},
                    expected=(
                        f"{ref}={'TRUE' if required else 'FALSE'} occurs in every bounded source path for "
                        f"{contract.state_tag} {contract.source_state}->{contract.target_state}; forcing the opposite value denies every statically modeled path."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_GUARD_ALL_PATH_BLOCK",
                    limitations=(
                        "This is bounded source dominance, not runtime or process-safety certification.",
                        "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                    ),
                    watch_tags=(contract.state_tag, ref),
                )
            )
    for contract in facts.output_contracts:
        source = _source_for_output(project, contract)
        if source is None:
            continue
        for path_index, path in enumerate(contract.guard_paths):
            if len(tests) >= _MAX_GUARD_TESTS:
                break
            if not path:
                continue
            digest = hashlib.sha1(f"{contract.id}:output-permit:{path_index}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-GUARD6-{digest}",
                    title=f"Verify bounded enable path for {contract.output_tag}",
                    source=source,
                    output_tag=contract.output_tag,
                    preconditions=dict(path),
                    expected=(
                        f"The exact bounded source path {dict(path)} permits {contract.output_tag}=TRUE in the static Boolean theorem; "
                        "runtime evidence confirms controller/process behavior."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_OUTPUT_GUARD_PERMIT",
                    limitations=(
                        "Static output theorem does not certify field I/O, actuator state, process safety, or SIL/PL.",
                        "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                    ),
                    watch_tags=tuple(dict.fromkeys((contract.output_tag, *(ref for ref, _ in path)))),
                )
            )
        for ref, required in contract.all_path_terms:
            if len(tests) >= _MAX_GUARD_TESTS:
                break
            digest = hashlib.sha1(f"{contract.id}:output-all-path:{ref}:{required}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-GUARD6-{digest}",
                    title=f"Verify {contract.output_tag} is blocked when {ref} is inverted",
                    source=source,
                    output_tag=contract.output_tag,
                    preconditions={ref: not required},
                    expected=(
                        f"{ref}={'TRUE' if required else 'FALSE'} occurs in every bounded TRUE path for {contract.output_tag}; "
                        "forcing the opposite value makes every modeled TRUE path false."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_OUTPUT_GUARD_ALL_PATH_BLOCK",
                    limitations=(
                        "This proves only the bounded Boolean source theorem for the output, not physical de-energization or safety integrity.",
                        "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                    ),
                    watch_tags=(contract.output_tag, ref),
                )
            )
    return enrich_fat_procedures(project, tests)


def _v6_checks(facts) -> list[StaticCheck]:
    contracts = _all_contracts(facts)
    if not contracts:
        return [
            StaticCheck(
                "SCHNEIDER_V6_GUARD_TRACEABILITY",
                StaticCheckStatus.WARN,
                "No FULL Schneider Boolean output or V5 state transition was available for V6 guard analysis.",
            )
        ]
    terms = [term for contract in contracts for term in contract.terms]
    classified = [term for term in terms if term.role != "GUARD"]
    partial = sum(contract.semantic_state is not PLCSemanticState.FULL for contract in contracts)
    all_path = sum(len(contract.all_path_terms) for contract in contracts)
    runtime = sum(bool(contract.runtime_dependencies) for contract in facts.transition_contracts)
    evidence = tuple(contract.id for contract in contracts)
    return [
        StaticCheck(
            "SCHNEIDER_V6_GUARD_TRACEABILITY",
            StaticCheckStatus.PASS if not partial else StaticCheckStatus.NOT_PROVEN,
            f"Bound {len(contracts)} guard contract(s) with {len(terms)} explicit Boolean term occurrence(s); partial contracts={partial}.",
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V6_INTERLOCK_PERMISSIVE_CLASSIFICATION",
            StaticCheckStatus.PASS if classified else StaticCheckStatus.WARN,
            f"Token-classified {len(classified)}/{len(terms)} term occurrence(s) as interlock/permissive/recovery from exported names/descriptions; generic guards are not guessed.",
            tuple(term.id for term in classified),
        ),
        StaticCheck(
            "SCHNEIDER_V6_ALL_PATH_GUARD_DOMINANCE",
            StaticCheckStatus.PASS if all_path else StaticCheckStatus.WARN,
            f"Derived {all_path} exact Boolean condition occurrence(s) that dominate every bounded source path of their contract.",
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V6_REQUIREMENT_GUARD_PROOF",
            StaticCheckStatus.NOT_PROVEN,
            f"Requirement proof is exact and per requirement: one unique output/transition contract plus explicit Boolean conditions are required; {runtime} runtime-dependent transition contract(s) remain FAT-only.",
            evidence,
        ),
    ]


def _value_token_pattern(value: str) -> str:
    escaped = re.escape(value)
    if re.fullmatch(r"[-+]?\d+", value):
        return rf"(?<!\d){escaped}(?!\d)"
    return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"


def _transition_relation_occurs(text: str, contract) -> bool:
    if not tag_occurs(text, contract.state_tag):
        return False
    source = _value_token_pattern(contract.source_state)
    target = _value_token_pattern(contract.target_state)
    state = re.escape(contract.state_tag)
    patterns = (
        rf"\bfrom\s+{source}\s+(?:to|into)\s+{target}\b",
        rf"{source}\s*(?:->|→)\s*{target}",
        rf"{source}\s+\bto\b\s+{target}",
        rf"{state}\s*=\s*{source}.*{state}\s*=\s*{target}",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


def _is_restrictive_requirement(text: str) -> bool:
    return bool(
        re.search(
            r"\b(only\s+(?:when|if)|unless|shall\s+require|requires?|required\s+(?:for|before)|must\s+have|interlocked\s+by)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _path_requirement_state(text: str, path):
    if not path:
        return "MATCH", {}
    values = {}
    missing = False
    mismatch = False
    for ref, required in path:
        if not tag_occurs(text, ref):
            missing = True
            continue
        value = explicit_bool(text, ref)
        if value is None:
            missing = True
            continue
        values[ref] = value
        if value is not required:
            mismatch = True
    if mismatch and not missing:
        return "CONFLICT", values
    if missing:
        return "INCOMPLETE", values
    return "MATCH", values


def _explicit_values(text: str, project, paths, *, exclude: tuple[str, ...] = ()) -> dict[str, bool]:
    refs: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for ref, _required in path:
            if ref.casefold() not in seen:
                seen.add(ref.casefold())
                refs.append(ref)
    for tag in project.tags:
        if tag.name.casefold() not in seen:
            seen.add(tag.name.casefold())
            refs.append(tag.name)
    excluded = {item.casefold() for item in exclude}
    result: dict[str, bool] = {}
    for ref in refs:
        if ref.casefold() in excluded or not tag_occurs(text, ref):
            continue
        value = explicit_bool(text, ref)
        if value is not None:
            result[ref] = value
    return result


def _dominance_verdict(explicit: dict[str, bool], all_path_terms):
    dominant = {name.casefold(): (name, required) for name, required in all_path_terms}
    missing = []
    conflicts = []
    for ref, value in explicit.items():
        item = dominant.get(ref.casefold())
        if item is None:
            missing.append(ref)
        elif item[1] != value:
            conflicts.append(ref)
    return tuple(missing), tuple(conflicts)


def _contract_evidence(previous, contract):
    terms = tuple(term.id for term in contract.terms)
    if isinstance(contract, SchneiderV6TransitionGuardContract):
        source_ids = contract.transition_ids
        subject = contract.state_tag
    else:
        source_ids = (contract.output_logic_id,)
        subject = contract.output_tag
    evidence = tuple(dict.fromkeys((*previous.evidence_ids, contract.id, *source_ids, *terms)))
    matched = tuple(dict.fromkeys((*previous.matched_tags, subject, *(term.tag for term in contract.terms))))
    return evidence, matched


def _restrictive_output_requirement(requirement, engineering, tests, previous, facts):
    candidates = [
        contract
        for contract in facts.output_contracts
        if tag_occurs(requirement.text, contract.output_tag)
        and explicit_bool(requirement.text, contract.output_tag) is True
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            "Restrictive Schneider V6 output requirement maps to multiple bounded output guard contracts; every-path proof is withheld.",
            previous.evidence_ids,
            previous.matched_tags,
            previous.linked_test_ids,
        )
    contract = candidates[0]
    evidence, matched = _contract_evidence(previous, contract)
    if contract.semantic_state is not PLCSemanticState.FULL:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            f"Restrictive Schneider V6 output requirement maps to {contract.output_tag}, but its guard contract is {contract.semantic_state.value}: {contract.reason}.",
            evidence,
            matched,
            previous.linked_test_ids,
        )
    explicit = _explicit_values(
        requirement.text,
        engineering.project,
        contract.guard_paths,
        exclude=(contract.output_tag,),
    )
    if not explicit:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            f"Restrictive requirement maps to {contract.output_tag}=TRUE, but no explicit Boolean guard condition is stated.",
            evidence,
            matched,
            previous.linked_test_ids,
        )
    missing, conflicts = _dominance_verdict(explicit, contract.all_path_terms)
    if conflicts:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            f"Restrictive Schneider V6 requirement for {contract.output_tag}=TRUE contradicts all-path source polarity for: {', '.join(conflicts)}.",
            evidence,
            matched,
            previous.linked_test_ids,
        )
    if missing:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            f"Restrictive Schneider V6 requirement is not enforced on every bounded TRUE path of {contract.output_tag} for: {', '.join(missing)}.",
            evidence,
            matched,
            previous.linked_test_ids,
        )
    linked = set(previous.linked_test_ids)
    for test in tests:
        if test.output_tag.casefold() == contract.output_tag.casefold() and test.scenario == "SCHNEIDER_OUTPUT_GUARD_ALL_PATH_BLOCK":
            linked.add(test.id)
    return RequirementVerification(
        requirement.id,
        RequirementStatus.STATICALLY_VERIFIED,
        (
            f"Exact Schneider V6 every-path output guard theorem proven: every bounded {contract.output_tag}=TRUE source path "
            "carries the explicitly required Boolean condition(s). This is source proof, not physical/process safety certification."
        ),
        evidence,
        matched,
        tuple(sorted(linked)),
        confidence=1.0,
        ai_assisted=False,
    )


def _restrictive_transition_requirement(requirement, engineering, tests, previous, facts):
    candidates = [contract for contract in facts.transition_contracts if _transition_relation_occurs(requirement.text, contract)]
    if not candidates:
        return None
    if len(candidates) != 1:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            "Restrictive Schneider V6 transition requirement does not map to one unique bounded source relation.",
            previous.evidence_ids,
            previous.matched_tags,
            previous.linked_test_ids,
        )
    contract = candidates[0]
    evidence, matched = _contract_evidence(previous, contract)
    if contract.semantic_state is not PLCSemanticState.FULL:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            (
                f"Restrictive Schneider V6 transition requirement maps to {contract.state_tag} "
                f"{contract.source_state}->{contract.target_state}, but its guard contract is {contract.semantic_state.value}: {contract.reason}."
            ),
            evidence,
            matched,
            previous.linked_test_ids,
        )
    if contract.runtime_dependencies:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            (
                f"Requirement maps to Schneider V6 transition {contract.state_tag} {contract.source_state}->{contract.target_state}, "
                f"but runtime dependency {', '.join(contract.runtime_dependencies)} prevents static every-path closure."
            ),
            evidence,
            matched,
            previous.linked_test_ids,
        )
    explicit = _explicit_values(
        requirement.text,
        engineering.project,
        contract.guard_paths,
        exclude=(contract.state_tag,),
    )
    if not explicit:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            f"Restrictive requirement maps to Schneider transition {contract.state_tag} {contract.source_state}->{contract.target_state}, but no explicit Boolean guard value is stated.",
            evidence,
            matched,
            previous.linked_test_ids,
        )
    missing, conflicts = _dominance_verdict(explicit, contract.all_path_terms)
    if conflicts:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            f"Restrictive Schneider V6 transition requirement contradicts all-path source polarity for: {', '.join(conflicts)}.",
            evidence,
            matched,
            previous.linked_test_ids,
        )
    if missing:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            f"Restrictive Schneider V6 transition requirement is not carried by every bounded source path for: {', '.join(missing)}.",
            evidence,
            matched,
            previous.linked_test_ids,
        )
    linked = set(previous.linked_test_ids)
    for test in tests:
        if test.output_tag.casefold() == contract.state_tag.casefold() and test.scenario == "SCHNEIDER_GUARD_ALL_PATH_BLOCK":
            linked.add(test.id)
    return RequirementVerification(
        requirement.id,
        RequirementStatus.STATICALLY_VERIFIED,
        (
            f"Exact Schneider V6 every-path transition guard theorem proven for {contract.state_tag} "
            f"{contract.source_state}->{contract.target_state}: every bounded source path carries the explicitly required Boolean condition(s). "
            "Runtime behavior remains FAT evidence."
        ),
        evidence,
        matched,
        tuple(sorted(linked)),
        confidence=1.0,
        ai_assisted=False,
    )


def _normal_transition_requirement(requirement, engineering, tests, previous, facts):
    if previous.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return previous
    candidates = [
        contract
        for contract in facts.transition_contracts
        if contract.semantic_state is PLCSemanticState.FULL and _transition_relation_occurs(requirement.text, contract)
    ]
    if len(candidates) != 1:
        return previous
    contract = candidates[0]
    evidence, matched = _contract_evidence(previous, contract)
    if contract.runtime_dependencies:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            (
                f"Requirement uniquely maps to Schneider V6 transition {contract.state_tag} {contract.source_state}->{contract.target_state}, "
                f"but runtime dependency {', '.join(contract.runtime_dependencies)} prevents static closure."
            ),
            evidence,
            matched,
            previous.linked_test_ids,
        )
    states = [_path_requirement_state(requirement.text, path) for path in contract.guard_paths]
    matches = [values for status, values in states if status == "MATCH"]
    if matches:
        linked = set(previous.linked_test_ids)
        for test in tests:
            if test.output_tag.casefold() != contract.state_tag.casefold():
                continue
            if test.scenario not in {"SCHNEIDER_STATE_TRANSITION", "SCHNEIDER_GUARD_PERMIT"}:
                continue
            if any(all(test.preconditions.get(tag) == value for tag, value in values.items()) for values in matches):
                linked.add(test.id)
        return RequirementVerification(
            requirement.id,
            RequirementStatus.STATICALLY_VERIFIED,
            (
                f"Exact Schneider V6 source transition proven: {contract.state_tag} {contract.source_state}->{contract.target_state} "
                "under an explicitly stated bounded Boolean guard path. Runtime scan/I/O/process behavior remains FAT evidence."
            ),
            evidence,
            matched,
            tuple(sorted(linked)),
            confidence=1.0,
            ai_assisted=False,
        )
    if states and all(status == "CONFLICT" for status, _values in states):
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            (
                f"Requirement uniquely maps to Schneider V6 transition {contract.state_tag} {contract.source_state}->{contract.target_state}, "
                "but its explicit Boolean conditions contradict every bounded source guard path."
            ),
            evidence,
            matched,
            previous.linked_test_ids,
        )
    return RequirementVerification(
        requirement.id,
        RequirementStatus.TRACEABLE_NOT_PROVEN,
        (
            f"Requirement uniquely maps to Schneider V6 transition {contract.state_tag} {contract.source_state}->{contract.target_state}, "
            "but the Boolean guard conditions needed for bounded proof are incomplete."
        ),
        evidence,
        matched,
        previous.linked_test_ids,
    )


def _enhance_requirement(requirement, engineering, tests, previous):
    project = engineering.project
    if not str(project.metadata.vendor).casefold().startswith("schneider"):
        return previous
    facts = _facts(project)
    if facts is None:
        return previous
    if _is_restrictive_requirement(requirement.text):
        output_result = _restrictive_output_requirement(requirement, engineering, tests, previous, facts)
        if output_result is not None:
            return output_result
        transition_result = _restrictive_transition_requirement(requirement, engineering, tests, previous, facts)
        if transition_result is not None:
            return transition_result
    return _normal_transition_requirement(requirement, engineering, tests, previous, facts)


def analyze_schneider_control_expert_v6(path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    facts = _build_guard_facts(project)
    if facts is None:
        return base
    setattr(project, "_schneider_v6_guard_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SCHNEIDER-CONTROL-EXPERT-EXPORT-V6")
    fat_tests = list(base.fat_tests)
    fat_tests.extend(_guard_fat(project, facts))
    fat_tests = list({test.id: test for test in fat_tests}.values())
    checks = [item for item in base.static_checks if not item.id.startswith("SCHNEIDER_V6_")]
    checks.extend(_v6_checks(facts))
    profile = schneider_capability_profile_v6(project)
    guard_complete = profile["guard_contract"] in {"COMPLETE", "NONE"}
    outcome = base.outcome
    if base.outcome is PLCOutcome.STATICALLY_VERIFIED and not guard_complete:
        outcome = PLCOutcome.PARTIALLY_VERIFIED
    limitations = list(base.limitations)
    limitations.append(
        "Schneider V6 classifies interlock/permissive/recovery intent only from exported identifier/description tokens. "
        "Generic guard terms remain unclassified and no safe Boolean polarity is inferred from the role label."
    )
    limitations.append(
        "V6 every-path proof is limited to exact Boolean terms present with the same polarity on every bounded DNF path of one FULL "
        "output theorem or grouped V5 source->target transition relation. It does not prove SIL/PL, process safety, physical de-energization, "
        "scan/I/O timing, timer/counter evolution, Simulator, HIL, or real PLC behavior."
    )
    return PLCEngineeringResult(outcome, project, base.graph, fat_tests, checks, list(dict.fromkeys(limitations)))


def _v6_evidence(previous, engineering):
    items = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return items
    project = engineering.project
    existing = {item.id for item in items}
    for contract in facts.transition_contracts:
        if contract.id not in existing:
            items.append(
                EvidenceItem(
                    contract.id,
                    "SCHNEIDER_TRANSITION_GUARD_V6",
                    (
                        f"{contract.state_tag} {contract.source_state}->{contract.target_state}: {len(contract.guard_paths)} bounded path(s), "
                        f"{len(contract.all_path_terms)} all-path term(s), {contract.semantic_state.value}."
                    ),
                    f"{contract.section}:{','.join(str(line) for line in contract.source_lines)}",
                    project.metadata.source_sha256,
                    {
                        "machine_id": contract.machine_id,
                        "transition_ids": list(contract.transition_ids),
                        "section": contract.section,
                        "state_tag": contract.state_tag,
                        "source_state": contract.source_state,
                        "target_state": contract.target_state,
                        "source_lines": list(contract.source_lines),
                        "guard_paths": [[{"tag": name, "required": required} for name, required in path] for path in contract.guard_paths],
                        "all_path_terms": [{"tag": name, "required": required} for name, required in contract.all_path_terms],
                        "runtime_dependencies": list(contract.runtime_dependencies),
                        "semantic_state": contract.semantic_state.value,
                    },
                )
            )
        for term in contract.terms:
            if term.id in existing:
                continue
            items.append(
                EvidenceItem(
                    term.id,
                    "SCHNEIDER_GUARD_TERM_V6",
                    f"{term.tag}={'TRUE' if term.required else 'FALSE'} on path {term.path_index}; role={term.role}.",
                    f"{contract.section}:{contract.source_lines[0] if contract.source_lines else ''}",
                    project.metadata.source_sha256,
                    {
                        "contract_id": term.contract_id,
                        "source_id": term.source_id,
                        "path_index": term.path_index,
                        "tag": term.tag,
                        "required": term.required,
                        "role": term.role,
                        "description": term.description,
                    },
                )
            )
    for contract in facts.output_contracts:
        if contract.id not in existing:
            items.append(
                EvidenceItem(
                    contract.id,
                    "SCHNEIDER_OUTPUT_GUARD_V6",
                    (
                        f"{contract.output_tag}=TRUE theorem: {len(contract.guard_paths)} bounded path(s), "
                        f"{len(contract.all_path_terms)} all-path term(s), {contract.semantic_state.value}."
                    ),
                    contract.source_locator,
                    project.metadata.source_sha256,
                    {
                        "output_logic_id": contract.output_logic_id,
                        "output_tag": contract.output_tag,
                        "language": contract.language,
                        "guard_paths": [[{"tag": name, "required": required} for name, required in path] for path in contract.guard_paths],
                        "all_path_terms": [{"tag": name, "required": required} for name, required in contract.all_path_terms],
                        "semantic_state": contract.semantic_state.value,
                        "reason": contract.reason,
                    },
                )
            )
        for term in contract.terms:
            if term.id in existing:
                continue
            items.append(
                EvidenceItem(
                    term.id,
                    "SCHNEIDER_GUARD_TERM_V6",
                    f"{term.tag}={'TRUE' if term.required else 'FALSE'} on output path {term.path_index}; role={term.role}.",
                    contract.source_locator,
                    project.metadata.source_sha256,
                    {
                        "contract_id": term.contract_id,
                        "source_id": term.source_id,
                        "path_index": term.path_index,
                        "tag": term.tag,
                        "required": term.required,
                        "role": term.role,
                        "description": term.description,
                    },
                )
            )
    return items


def _coverage_gap(contract):
    all_path = {name.casefold() for name, _required in contract.all_path_terms}
    return tuple(
        sorted(
            {
                term.tag
                for term in contract.terms
                if term.role in {"INTERLOCK", "PERMISSIVE"} and term.tag.casefold() not in all_path
            },
            key=str.casefold,
        )
    )


def _v6_risks(previous, engineering, verifications, executions, engineering_findings):
    risks = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return risks
    for contract in _all_contracts(facts):
        classified = [term for term in contract.terms if term.role != "GUARD"]
        if contract.terms and not classified:
            subject = (
                f"{contract.state_tag} {contract.source_state}->{contract.target_state}"
                if isinstance(contract, SchneiderV6TransitionGuardContract)
                else f"{contract.output_tag}=TRUE"
            )
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_GUARD_TRACEABILITY_V6", contract.id),
                    "INTERLOCK_TRACEABILITY",
                    f"Schneider guard contract {subject} has no explicit role metadata",
                    Severity.LOW,
                    "Boolean source guards are traceable, but their exported identifier/description tokens do not explicitly identify interlock/permissive/recovery intent.",
                    "Engineering intent cannot be inferred safely from generic tag names alone.",
                    "Add/confirm descriptions or explicit requirements and retain linked FAT evidence.",
                    (contract.id,),
                )
            )
        gaps = _coverage_gap(contract)
        if gaps:
            subject = (
                f"{contract.state_tag} {contract.source_state}->{contract.target_state}"
                if isinstance(contract, SchneiderV6TransitionGuardContract)
                else f"{contract.output_tag}=TRUE"
            )
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_GUARD_COVERAGE_V6", contract.id, *gaps),
                    "INTERLOCK_COVERAGE",
                    f"Schneider guard terms do not dominate every source path for {subject}",
                    Severity.MEDIUM,
                    f"Classified interlock/permissive term(s) occur on only a subset of bounded source paths: {', '.join(gaps)}.",
                    (
                        "An alternate bounded source path can reach the same transition/output without the same classified guard. "
                        "The role label does not establish whether this is intended."
                    ),
                    "Confirm the engineering requirement. If mandatory, place the guard on every relevant source path and rerun V6 analysis/FAT.",
                    (contract.id,),
                )
            )
    return risks


def _v6_render(previous, project) -> str:
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = schneider_capability_profile_v6(project)
    text = (
        "### Schneider V6 Interlocks / Permissives / Every-Path Guard Proof\n\n"
        f"- Transition guard contracts: **{profile['transition_guard_contracts']}**\n"
        f"- Boolean output guard contracts: **{profile['output_guard_contracts']}**\n"
        f"- Explicit guard term occurrences: **{profile['guard_terms']}**\n"
        f"- All-path guard terms: **{profile['all_path_guard_terms']}**\n"
        f"- All-path classified guard terms: **{profile['all_path_classified_terms']}**\n"
        f"- Classified interlock terms: **{profile['classified_interlock_terms']}**\n"
        f"- Classified permissive terms: **{profile['classified_permissive_terms']}**\n"
        f"- Classified recovery terms: **{profile['classified_recovery_terms']}**\n"
        f"- Runtime-dependent transition contracts: **{profile['runtime_dependent_transition_contracts']}**\n"
        "- V6 every-path proof is the exact intersection of Boolean terms across every bounded source path of one FULL contract.\n"
        "- Separate same-state/source/target transition statements are grouped before dominance analysis so alternate transition paths cannot be hidden.\n"
        "- Role classification is token-based metadata only; it never infers safe polarity or replaces source semantics.\n"
        "- SIL/PL, physical/process safety, scan/I/O timing, timer/counter evolution, Control Expert Simulator, HIL, and real Modicon execution remain runtime/engineering evidence.\n\n"
    )
    marker = "### Schneider V5 Sequencing / State Machines"
    return base.replace(marker, text + marker, 1) if marker in base else base + "\n\n" + text


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _v1
    from devagent.plc import schneider_integration_v1 as _integration
    from devagent.plc import schneider_report_install_v1 as _report
    previous_verify = _integration._verify_requirement
    previous_evidence = _integration._evidence_index
    previous_risks = _integration._detect_risks
    previous_render = _report._render
    _v1.analyze_schneider_control_expert = analyze_schneider_control_expert_v6
    _v1.schneider_capability_profile = schneider_capability_profile_v6
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v6
    _integration.schneider_capability_profile = schneider_capability_profile_v6

    def verify_requirement(requirement, engineering, evidence, tests):
        previous = previous_verify(requirement, engineering, evidence, tests)
        return _enhance_requirement(requirement, engineering, tests, previous)

    def evidence_index(engineering):
        return _v6_evidence(previous_evidence, engineering)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v6_risks(previous_risks, engineering, verifications, executions, engineering_findings)

    def render(project):
        return _v6_render(previous_render, project)

    _integration._verify_requirement = verify_requirement
    _integration._evidence_index = evidence_index
    _integration._detect_risks = detect_risks
    _report._render = render
    _INSTALLED = True


__all__ = [
    "SchneiderV6GuardFacts",
    "SchneiderV6GuardTermFact",
    "SchneiderV6OutputGuardContract",
    "SchneiderV6TransitionGuardContract",
    "analyze_schneider_control_expert_v6",
    "install",
    "schneider_capability_profile_v6",
]
