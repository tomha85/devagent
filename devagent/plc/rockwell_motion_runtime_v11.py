from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
import hashlib

from devagent.plc.models import FATTestCase, StaticCheck, StaticCheckStatus
from devagent.plc.rockwell_entrypoint_hardening import routine_has_execution_entry
from devagent.plc.v2_semantics import _refs


_MOTION = frozenset({"MAH", "MAJ", "MCPM", "MCS", "MCTO", "MDCC"})


@dataclass(frozen=True)
class RockwellMotionRuntimeModel:
    id: str
    rung_id: str
    instruction: str
    primary_ref: str
    input_refs: tuple[str, ...]
    expectation: str
    source: object


def _expectation(name: str, primary: str) -> str:
    common = (
        "Qualified runtime execution must record command acceptance, completion/in-process state, "
        "fault/error state, and any controller-visible state transition caused by this instruction."
    )
    if name == "MAH":
        return f"Exercise axis-home command for {primary}. {common}"
    if name == "MAJ":
        return f"Exercise axis-jog command for {primary} under the exported command parameters. {common}"
    if name == "MCPM":
        return (
            f"Exercise coordinated path motion for {primary}; observe queue/acceptance, in-process/completion, "
            f"and motion fault behavior. {common}"
        )
    if name == "MCS":
        return f"Exercise coordinated stop for {primary}; observe commanded stop behavior and resulting motion state. {common}"
    if name == "MCTO":
        return f"Exercise coordinate transform command for {primary}; observe activation/readiness/error state. {common}"
    return f"Exercise coordinated motion command {name} for {primary}. {common}"


def motion_runtime_models(project) -> list[RockwellMotionRuntimeModel]:
    result: list[RockwellMotionRuntimeModel] = []
    counts: Counter[tuple[str, str]] = Counter()
    for rung in project.rungs:
        if not routine_has_execution_entry(project, rung.program, rung.routine):
            continue
        for instruction in rung.instructions:
            name = instruction.name.upper()
            if name not in _MOTION or not instruction.arguments:
                continue
            refs: list[str] = []
            for argument in instruction.arguments:
                for ref in _refs(argument):
                    if ref not in refs:
                        refs.append(ref)
            primary = refs[0] if refs else instruction.arguments[0].strip()
            key = (name, primary.casefold())
            counts[key] += 1
            digest = hashlib.sha1(
                f"{rung.id}:{name}:{primary}:{counts[key]}".encode("utf-8")
            ).hexdigest()[:12]
            result.append(
                RockwellMotionRuntimeModel(
                    id=f"MOTION-RUNTIME-{digest}",
                    rung_id=rung.id,
                    instruction=name,
                    primary_ref=primary,
                    input_refs=tuple(refs),
                    expectation=_expectation(name, primary),
                    source=rung.source,
                )
            )
    return result


def generate_motion_runtime_fat_tests(project) -> list[FATTestCase]:
    """Generate traceable motion FAT scenarios without claiming static execution.

    The current generic FAT schema can express Boolean setup directly but cannot
    encode arbitrary motion structures/numeric trajectories as typed inputs.
    Therefore these scenarios deliberately carry no fabricated preconditions.
    A qualified Echo/HIL/controller adapter must obtain the exact instruction
    arguments from the evidence-linked source and return authenticated evidence.
    """
    tests: list[FATTestCase] = []
    for model in motion_runtime_models(project):
        digest = hashlib.sha1(f"{model.id}:runtime".encode("utf-8")).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-MOTION-{digest}",
                title=f"Runtime-check {model.instruction} at {model.source.locator}",
                source=model.source,
                output_tag=model.primary_ref,
                preconditions={},
                expected=model.expectation,
                limitations=(
                    "Motion behavior is not statically executed or certified by DevAgent.",
                    "The current generic FAT schema does not fabricate motion trajectories or numeric setup values; the qualified runtime adapter must use the exact evidence-linked project/instruction configuration.",
                    "PASS requires authenticated qualified Logix Echo/HIL/controller evidence bound to this project hash and test-plan hash.",
                ),
                scenario="MOTION_RUNTIME",
            )
        )
    return tests


def motion_runtime_check(project) -> StaticCheck:
    models = motion_runtime_models(project)
    if not models:
        return StaticCheck(
            id="ROCKWELL_MOTION_RUNTIME_CONTRACT",
            status=StaticCheckStatus.PASS,
            summary="No reachable MAH/MAJ/MCPM/MCS/MCTO/MDCC instruction requires a motion runtime FAT contract.",
        )
    return StaticCheck(
        id="ROCKWELL_MOTION_RUNTIME_CONTRACT",
        status=StaticCheckStatus.WARN,
        summary=(
            f"Generated {len(models)} evidence-linked motion runtime contract(s). "
            "Motion remains PARTIAL until qualified Logix Echo/HIL/controller execution evidence is attached."
        ),
        evidence=tuple(model.rung_id for model in models),
    )


def motion_runtime_profile(project) -> dict[str, object]:
    models = motion_runtime_models(project)
    counts = Counter(model.instruction for model in models)
    return {
        "schema": "devagent-rockwell-motion-runtime-v1",
        "modeled_occurrences": len(models),
        "instructions": dict(sorted(counts.items())),
        "requires_qualified_runtime_evidence": bool(models),
    }


__all__ = [
    "RockwellMotionRuntimeModel",
    "generate_motion_runtime_fat_tests",
    "motion_runtime_check",
    "motion_runtime_models",
    "motion_runtime_profile",
]
