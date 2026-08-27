from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from typing import Callable

from devagent.plc import production_verification as _verification
from devagent.plc import siemens_state_machine_v5 as _v5
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
    RequirementStatus,
    RequirementVerification,
    RiskFinding,
    Severity,
)
from devagent.plc.production_utils import explicit_bool, stable_id, tag_occurs


_INSTALLED = False
_PREVIOUS_ANALYZER = _v5.analyze_siemens_tia_v5
_PREVIOUS_CAPABILITY = _v5.siemens_capability_profile_v5
_PREVIOUS_VERIFY_REQUIREMENT: Callable | None = None

_PERMISSIVE_WORDS = (
    "permissive", "permit", "ready", "healthy", "okay", "available", "enable", "enabled",
)
_INTERLOCK_WORDS = (
    "interlock", "estop", "emergency", "guard", "door", "trip", "fault", "inhibit", "safe", "safety",
)
_RECOVERY_WORDS = (
    "reset", "recover", "recovery", "ack", "acknowledge", "clear", "restart",
)
_MAX_GUARD_TESTS = 512


@dataclass(frozen=True)
class SiemensV6GuardTermFact:
    id: str
    transition_id: str
    machine_id: str
    block: str
    state_tag: str
    source_state: str
    target_state: str
    path_index: int
    tag: str
    required: bool
    role: str
    description: str | None = None


@dataclass(frozen=True)
class SiemensV6TransitionGuardContract:
    id: str
    transition_id: str
    machine_id: str
    block: str
    state_tag: str
    source_state: str
    target_state: str
    source_line: int
    guard_text: str
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    terms: tuple[SiemensV6GuardTermFact, ...]
    runtime_dependencies: tuple[str, ...]
    semantic_state: PLCSemanticState
    reason: str


@dataclass(frozen=True)
class SiemensV6GuardFacts:
    contracts: tuple[SiemensV6TransitionGuardContract, ...]


def _tag_metadata(project) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    for tag in project.tags:
        result.setdefault(tag.name.casefold(), (tag.name, tag.description))
    return result


def _role_for_ref(ref: str, description: str | None) -> str:
    base = ref.split(".", 1)[0]
    haystack = re.sub(r"[^a-z0-9]+", "", f"{base} {description or ''}".casefold())
    if any(word in haystack for word in _RECOVERY_WORDS):
        return "RECOVERY"
    if any(word in haystack for word in _INTERLOCK_WORDS):
        return "INTERLOCK"
    if any(word in haystack for word in _PERMISSIVE_WORDS):
        return "PERMISSIVE"
    return "GUARD"


def _build_guard_facts(project) -> SiemensV6GuardFacts | None:
    state_facts = _v5._facts(project)
    if state_facts is None:
        return None
    metadata = _tag_metadata(project)
    contracts: list[SiemensV6TransitionGuardContract] = []
    for machine in state_facts.machines:
        for transition in machine.transitions:
            terms: list[SiemensV6GuardTermFact] = []
            for path_index, path in enumerate(transition.guard_paths):
                for ref, required in path:
                    base = ref.split(".", 1)[0]
                    canonical, description = metadata.get(
                        base.casefold(), (base, None)
                    )
                    digest = hashlib.sha1(
                        f"{transition.id}:{path_index}:{ref}:{required}".encode()
                    ).hexdigest()[:14]
                    terms.append(
                        SiemensV6GuardTermFact(
                            id=f"SIEMENS-GUARD6-{digest}",
                            transition_id=transition.id,
                            machine_id=machine.id,
                            block=machine.block,
                            state_tag=machine.state_tag,
                            source_state=transition.source_state,
                            target_state=transition.target_state,
                            path_index=path_index,
                            tag=ref if "." in ref else canonical,
                            required=required,
                            role=_role_for_ref(ref, description),
                            description=description,
                        )
                    )
            semantic = (
                PLCSemanticState.FULL
                if machine.semantic_state is PLCSemanticState.FULL
                else PLCSemanticState.PARTIAL
            )
            digest = hashlib.sha1(f"{transition.id}:guard-contract-v6".encode()).hexdigest()[:14]
            contracts.append(
                SiemensV6TransitionGuardContract(
                    id=f"SIEMENS-GC6-{digest}",
                    transition_id=transition.id,
                    machine_id=machine.id,
                    block=machine.block,
                    state_tag=machine.state_tag,
                    source_state=transition.source_state,
                    target_state=transition.target_state,
                    source_line=transition.source_line,
                    guard_text=transition.guard_text,
                    guard_paths=transition.guard_paths,
                    terms=tuple(terms),
                    runtime_dependencies=transition.runtime_dependencies,
                    semantic_state=semantic,
                    reason=(
                        "bounded_transition_guard_binding"
                        if semantic is PLCSemanticState.FULL
                        else "parent_state_machine_partial"
                    ),
                )
            )
    return SiemensV6GuardFacts(tuple(contracts))


def _facts(project):
    return getattr(project, "_siemens_v6_guard_facts", None)


def siemens_capability_profile_v6(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-siemens-tia-capability-v6"
    if facts is None:
        profile.update(
            {
                "transition_guard_contracts": 0,
                "transition_guard_terms": 0,
                "classified_interlock_terms": 0,
                "classified_permissive_terms": 0,
                "classified_recovery_terms": 0,
                "unclassified_guard_terms": 0,
                "runtime_dependent_guard_contracts": 0,
                "guard_contract": "NONE",
                "requirement_guard_proof_contract": "EXPLICIT_ONLY",
            }
        )
        return profile

    contracts = facts.contracts
    terms = [term for contract in contracts for term in contract.terms]
    partial = [
        contract
        for contract in contracts
        if contract.semantic_state is not PLCSemanticState.FULL
    ]
    profile.update(
        {
            "transition_guard_contracts": len(contracts),
            "transition_guard_terms": len(terms),
            "classified_interlock_terms": sum(t.role == "INTERLOCK" for t in terms),
            "classified_permissive_terms": sum(t.role == "PERMISSIVE" for t in terms),
            "classified_recovery_terms": sum(t.role == "RECOVERY" for t in terms),
            "unclassified_guard_terms": sum(t.role == "GUARD" for t in terms),
            "runtime_dependent_guard_contracts": sum(
                bool(contract.runtime_dependencies) for contract in contracts
            ),
            "guard_contract": (
                "COMPLETE"
                if contracts and not partial
                else "PARTIAL_FAIL_CLOSED"
                if contracts
                else "NONE"
            ),
            "requirement_guard_proof_contract": "EXPLICIT_ONLY",
            "bounded_guard_contract": (
                "transition guards are inherited from the V5 bounded CASE theorem; "
                "role labels are deterministic metadata/name classifications only and "
                "never substitute for source guard proof"
            ),
        }
    )
    return profile


def _source_for_contract(project, contract):
    for statement in project.logic_statements:
        try:
            line = int(str(statement.source.line)) if statement.source.line is not None else None
        except ValueError:
            line = None
        block = statement.source.program or statement.owner_name or ""
        if (
            statement.language == "SCL"
            and block.casefold() == contract.block.casefold()
            and line == contract.source_line
        ):
            return statement.source
    for statement in project.logic_statements:
        block = statement.source.program or statement.owner_name or ""
        if statement.language == "SCL" and block.casefold() == contract.block.casefold():
            return statement.source
    return None


def _guard_fat(project, contracts):
    tests: list[FATTestCase] = []
    for contract in contracts:
        source = _source_for_contract(project, contract)
        if source is None:
            continue
        for path_index, path in enumerate(contract.guard_paths):
            if len(tests) >= _MAX_GUARD_TESTS:
                break
            if not path:
                continue
            preconditions = dict(path)
            digest = hashlib.sha1(
                f"{contract.id}:permit:{path_index}".encode()
            ).hexdigest()[:10]
            roles = sorted(
                {
                    term.role
                    for term in contract.terms
                    if term.path_index == path_index and term.role != "GUARD"
                }
            )
            role_text = ", ".join(roles) if roles else "explicit Boolean guard"
            tests.append(
                FATTestCase(
                    id=f"FAT-SIEMENS-GUARD6-{digest}",
                    title=(
                        f"Verify {role_text} path for {contract.state_tag}: "
                        f"{contract.source_state} -> {contract.target_state}"
                    ),
                    source=source,
                    output_tag=contract.state_tag,
                    preconditions=preconditions,
                    expected=(
                        f"Starting from {contract.state_tag}={contract.source_state}, "
                        f"the exact source guard path {dict(path)} enables the modeled "
                        f"transition to {contract.target_state}. Runtime evidence still "
                        "confirms scan/I/O/process behavior."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SIEMENS_GUARD_PERMIT",
                    limitations=(
                        "Role classification comes only from explicit PLC identifiers/descriptions; the Boolean transition guard itself comes from source semantics.",
                        "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
                    ),
                    watch_tags=tuple(
                        dict.fromkeys(
                            (
                                contract.state_tag,
                                *(ref for ref, _ in path),
                            )
                        )
                    ),
                )
            )
            for term_index, (ref, required) in enumerate(path):
                if len(tests) >= _MAX_GUARD_TESTS:
                    break
                blocked = dict(path)
                blocked[ref] = not required
                bdigest = hashlib.sha1(
                    f"{contract.id}:block:{path_index}:{term_index}".encode()
                ).hexdigest()[:10]
                tests.append(
                    FATTestCase(
                        id=f"FAT-SIEMENS-GUARD6-{bdigest}",
                        title=(
                            f"Verify guard-path denial for {contract.state_tag}: "
                            f"{contract.source_state} -> {contract.target_state} when {ref} is inverted"
                        ),
                        source=source,
                        output_tag=contract.state_tag,
                        preconditions=blocked,
                        expected=(
                            f"The selected source guard path is false because {ref}="
                            f"{'TRUE' if not required else 'FALSE'}. This test proves that "
                            "path is denied; any alternative V5 DNF path must be evaluated "
                            "independently before claiming the whole transition is blocked."
                        ),
                        method="RUNTIME_FAT_REQUIRED",
                        scenario="SIEMENS_GUARD_PATH_BLOCK",
                        limitations=(
                            "A denied DNF path does not imply every alternative transition path is denied.",
                            "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
                        ),
                        watch_tags=tuple(
                            dict.fromkeys(
                                (
                                    contract.state_tag,
                                    *(name for name, _ in path),
                                )
                            )
                        ),
                    )
                )
    return enrich_fat_procedures(project, tests)


def _v6_checks(contracts):
    if not contracts:
        return [
            StaticCheck(
                "SIEMENS_V6_GUARD_TRACEABILITY",
                StaticCheckStatus.WARN,
                "No V5 state transition was available for Siemens V6 guard/interlock/permissive traceability.",
            )
        ]
    terms = [term for contract in contracts for term in contract.terms]
    classified = [term for term in terms if term.role != "GUARD"]
    runtime = sum(bool(contract.runtime_dependencies) for contract in contracts)
    partial = sum(
        contract.semantic_state is not PLCSemanticState.FULL for contract in contracts
    )
    evidence = tuple(contract.id for contract in contracts)
    return [
        StaticCheck(
            "SIEMENS_V6_GUARD_TRACEABILITY",
            StaticCheckStatus.PASS if not partial else StaticCheckStatus.WARN,
            (
                f"Bound {len(contracts)} transition guard contract(s) with "
                f"{len(terms)} explicit Boolean guard term(s); parent-partial contracts={partial}."
            ),
            evidence,
        ),
        StaticCheck(
            "SIEMENS_V6_INTERLOCK_PERMISSIVE_CLASSIFICATION",
            StaticCheckStatus.PASS if classified else StaticCheckStatus.WARN,
            (
                f"Deterministically classified {len(classified)}/{len(terms)} guard term(s) "
                "from explicit tag names/descriptions. Unclassified guards remain generic and are not guessed."
            ),
            tuple(term.id for term in classified),
        ),
        StaticCheck(
            "SIEMENS_V6_REQUIREMENT_GUARD_PROOF",
            StaticCheckStatus.NOT_PROVEN,
            (
                f"Requirement proof is available only for an exact source->target transition with "
                f"all Boolean guard conditions explicit; {runtime} runtime-dependent contract(s) remain FAT-only."
            ),
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
    patterns = (
        rf"\bfrom\s+{source}\s+(?:to|into)\s+{target}\b",
        rf"{source}\s*(?:->|→)\s*{target}",
        rf"{source}\s+\bto\b\s+{target}",
        rf"{re.escape(contract.state_tag)}\s*=\s*{source}.*{re.escape(contract.state_tag)}\s*=\s*{target}",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


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


def _enhance_requirement(requirement, engineering, tests, previous):
    if previous.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return previous
    project = engineering.project
    if str(project.metadata.vendor).casefold() != "siemens":
        return previous
    facts = _facts(project)
    if facts is None:
        return previous

    candidates = [
        contract
        for contract in facts.contracts
        if contract.semantic_state is PLCSemanticState.FULL
        and _transition_relation_occurs(requirement.text, contract)
    ]
    if len(candidates) != 1:
        return previous

    contract = candidates[0]
    evidence = tuple(
        dict.fromkeys(
            (
                *previous.evidence_ids,
                contract.id,
                contract.transition_id,
                *(term.id for term in contract.terms),
            )
        )
    )
    matched_tags = tuple(
        dict.fromkeys(
            (
                *previous.matched_tags,
                contract.state_tag,
                *(term.tag for term in contract.terms),
            )
        )
    )

    if contract.runtime_dependencies:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            (
                f"Requirement uniquely maps to Siemens transition {contract.state_tag} "
                f"{contract.source_state}->{contract.target_state}, but runtime dependency "
                f"{', '.join(contract.runtime_dependencies)} prevents static closure."
            ),
            evidence,
            matched_tags,
            previous.linked_test_ids,
        )

    states = [
        _path_requirement_state(requirement.text, path)
        for path in contract.guard_paths
    ]
    matches = [values for status, values in states if status == "MATCH"]
    if len(matches) == 1:
        linked = set(previous.linked_test_ids)
        for test in tests:
            if test.output_tag.casefold() != contract.state_tag.casefold():
                continue
            if test.scenario not in {
                "SIEMENS_STATE_TRANSITION",
                "SIEMENS_GUARD_PERMIT",
            }:
                continue
            if all(
                test.preconditions.get(tag) == value
                for tag, value in matches[0].items()
            ):
                linked.add(test.id)
        return RequirementVerification(
            requirement.id,
            RequirementStatus.STATICALLY_VERIFIED,
            (
                f"Exact Siemens V6 source transition proven: {contract.state_tag} "
                f"{contract.source_state}->{contract.target_state} under the explicitly "
                "stated Boolean guard path. This is a local engineering-source proof; "
                "runtime scan/I/O/process behavior still requires FAT where policy requires it."
            ),
            evidence,
            matched_tags,
            tuple(sorted(linked)),
            confidence=1.0,
            ai_assisted=False,
        )

    if states and all(status == "CONFLICT" for status, _ in states):
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            (
                f"Requirement uniquely maps to Siemens transition {contract.state_tag} "
                f"{contract.source_state}->{contract.target_state}, but its explicit Boolean "
                "conditions contradict every bounded source guard path."
            ),
            evidence,
            matched_tags,
            previous.linked_test_ids,
        )
    return RequirementVerification(
        requirement.id,
        RequirementStatus.TRACEABLE_NOT_PROVEN,
        (
            f"Requirement uniquely maps to Siemens transition {contract.state_tag} "
            f"{contract.source_state}->{contract.target_state}, but not every Boolean guard "
            "condition is explicitly specified."
        ),
        evidence,
        matched_tags,
        previous.linked_test_ids,
    )


def verify_requirement(requirement, engineering, evidence, tests):
    if _PREVIOUS_VERIFY_REQUIREMENT is None:  # pragma: no cover
        raise RuntimeError("Siemens V6 requirement semantics were not installed")
    previous = _PREVIOUS_VERIFY_REQUIREMENT(requirement, engineering, evidence, tests)
    return _enhance_requirement(requirement, engineering, tests, previous)


def analyze_siemens_tia_v6(path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    state_facts = _v5._facts(project)
    if state_facts is None:
        return base

    facts = _build_guard_facts(project)
    assert facts is not None
    setattr(project, "_siemens_v6_guard_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SIEMENS-TIA-EXPORT-V6")

    fat_tests = list(base.fat_tests)
    fat_tests.extend(_guard_fat(project, facts.contracts))
    fat_tests = list({test.id: test for test in fat_tests}.values())

    checks = [item for item in base.static_checks if not item.id.startswith("SIEMENS_V6_")]
    checks.extend(_v6_checks(facts.contracts))

    profile = siemens_capability_profile_v6(project)
    guard_complete = profile["guard_contract"] in {"COMPLETE", "NONE"}
    outcome = base.outcome
    if base.outcome is PLCOutcome.STATICALLY_VERIFIED and not guard_complete:
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = list(base.limitations)
    limitations.append(
        "Siemens V6 binds interlock/permissive/recovery labels only when PLC tag names/descriptions explicitly support that classification; unclassified guards remain generic and are never guessed."
    )
    limitations.append(
        "V6 requirement verification proves only a uniquely identified bounded source transition with every Boolean guard value explicit. It does not prove safety integrity level, process safety, scan timing, I/O behavior, or timer/counter evolution."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        base.graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _semantic_section(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = siemens_capability_profile_v6(project)
    text = (
        "### Siemens V6 Interlocks / Permissives / Requirement Traceability\n\n"
        f"- Transition guard contracts: **{profile['transition_guard_contracts']}**\n"
        f"- Explicit Boolean guard terms: **{profile['transition_guard_terms']}**\n"
        f"- Classified interlock terms: **{profile['classified_interlock_terms']}**\n"
        f"- Classified permissive terms: **{profile['classified_permissive_terms']}**\n"
        f"- Classified recovery terms: **{profile['classified_recovery_terms']}**\n"
        f"- Runtime-dependent guard contracts: **{profile['runtime_dependent_guard_contracts']}**\n"
        "- Classification is metadata/name based only; source transition semantics remain the proof authority.\n\n"
    )
    marker = "### Siemens V5 Sequencing / State Machines"
    return base.replace(marker, text + marker, 1) if marker in base else base + "\n\n" + text


def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    for contract in facts.contracts:
        classified = [term for term in contract.terms if term.role != "GUARD"]
        if contract.terms and not classified:
            result.append(
                RiskFinding(
                    stable_id("RISK", "SIEMENS_GUARD_TRACEABILITY_V6", contract.id),
                    "INTERLOCK_TRACEABILITY",
                    (
                        f"Siemens transition {contract.state_tag} "
                        f"{contract.source_state}->{contract.target_state} has source guards without explicit role metadata"
                    ),
                    Severity.LOW,
                    (
                        "The Boolean guard is source-traceable, but none of its tag names/descriptions explicitly identify "
                        "permissive/interlock/recovery intent."
                    ),
                    "Engineering intent cannot be inferred safely from generic tag names alone.",
                    "Add/confirm tag descriptions or requirement text, then retain the linked FAT evidence.",
                    (contract.id, contract.transition_id),
                )
            )
    return result


def install() -> None:
    global _INSTALLED, _PREVIOUS_VERIFY_REQUIREMENT
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import production as _production
    from devagent.plc import siemens_integration_v1 as _integration
    from devagent.plc import siemens_tia_v1 as _v1

    previous_section = _integration._siemens_semantic_section
    previous_risks = _integration._siemens_detect_risks

    _PREVIOUS_VERIFY_REQUIREMENT = _verification.verify_requirement
    _verification.verify_requirement = verify_requirement
    _production.verify_requirement = verify_requirement

    _v1.analyze_siemens_tia = analyze_siemens_tia_v6
    _v1.siemens_capability_profile = siemens_capability_profile_v6
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v6
    _integration.siemens_capability_profile = siemens_capability_profile_v6

    def semantic_section(project):
        return _semantic_section(previous_section, project)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _risks(previous_risks, engineering, verifications, executions, engineering_findings)

    _integration._siemens_semantic_section = semantic_section
    _integration._siemens_detect_risks = detect_risks
    _INSTALLED = True


__all__ = [
    "SiemensV6GuardFacts",
    "SiemensV6GuardTermFact",
    "SiemensV6TransitionGuardContract",
    "analyze_siemens_tia_v6",
    "install",
    "siemens_capability_profile_v6",
    "verify_requirement",
]
