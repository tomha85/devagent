from __future__ import annotations

from dataclasses import replace

_INSTALLED = False


def _ordered_unique(values):
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return tuple(result)


def _source_refs(project, test):
    source = test.source
    refs = []
    for rung in project.rungs:
        if source.program != rung.source.program or source.routine != rung.source.routine:
            continue
        if source.rung is not None and str(source.rung) == str(rung.source.rung):
            refs.extend(rung.reads)
            refs.extend(rung.writes)
            refs.extend(rung.references)
    for statement in project.logic_statements:
        if source.program != statement.source.program or source.routine != statement.source.routine:
            continue
        if source.line is not None and str(source.line) == str(statement.source.line):
            refs.extend(statement.reads)
            refs.extend(statement.writes)
    return _ordered_unique(refs)


def _purpose(test):
    scenario = test.scenario
    if scenario == "NEGATIVE_PATH":
        return f"Verify the negative/inhibited behavior for {test.output_tag} at {test.source.locator}."
    if scenario == "STATEFUL_RUNTIME":
        return f"Verify time-, edge-, or retained-state behavior for {test.output_tag} at {test.source.locator}."
    if scenario == "STATE_TRANSITION_RUNTIME":
        return f"Verify the discovered sequence/state transition affecting {test.output_tag} at {test.source.locator}."
    if scenario == "MOTION_RUNTIME":
        return f"Verify the controller-visible motion-command behavior referenced at {test.source.locator}."
    if scenario == "REQUIREMENT":
        return f"Verify the requirement-linked behavior for {test.output_tag} at {test.source.locator}."
    if scenario == "ACTION_PATH":
        return f"Verify the bounded action-path effect on {test.output_tag} at {test.source.locator}."
    return f"Verify the modeled PLC behavior for {test.output_tag} at {test.source.locator}."


def _setup_steps(test):
    steps = [f"Confirm the PLC revision under test matches the analyzed source at {test.source.locator}."]
    if test.preconditions:
        for tag, value in sorted(test.preconditions.items(), key=lambda item: item[0].casefold()):
            steps.append(f"Set or establish {tag} = {'TRUE' if value else 'FALSE'}.")
    else:
        steps.append(
            "Establish the source-linked enabling conditions and numeric/state values shown in the PLC logic; DevAgent does not fabricate values that are not proven by the project or requirements."
        )
    if test.scenario in {"STATEFUL_RUNTIME", "STATE_TRANSITION_RUNTIME", "MOTION_RUNTIME"}:
        steps.append("Place the test system in a controlled condition where the commanded transition can be observed safely.")
    return tuple(steps)


def _action_steps(test):
    steps = [
        "Apply the listed setup conditions in the engineer-selected test environment.",
        f"Exercise the source logic at {test.source.locator} through the condition or command represented by this FAT case.",
        f"Observe {test.output_tag} and the listed watch tags while the relevant PLC scan/sequence executes.",
        f"Record PASS only when the observed behavior satisfies: {test.expected}",
    ]
    if test.scenario in {"NEGATIVE_PATH", "STATEFUL_RUNTIME", "STATE_TRANSITION_RUNTIME", "MOTION_RUNTIME"}:
        steps.append("Also record the relevant inhibited/fault/timeout or non-transition behavior if the expected result is not reached.")
    return tuple(steps)


def _why_required(test):
    if test.scenario in {"STATEFUL_RUNTIME", "STATE_TRANSITION_RUNTIME", "MOTION_RUNTIME"}:
        return (
            "The source is traceable, but static analysis alone cannot prove the complete scan-time, timing, retained-state, motion, sequencing, or process behavior; engineer-executed FAT is required."
        )
    if test.limitations:
        return "Static analysis produced a bounded candidate with explicit limitations, so this FAT case is recommended to confirm the behavior in the engineer's chosen test environment."
    return "This test confirms the evidence-linked modeled behavior and provides a repeatable regression check after PLC changes."


def _failure_implication(test):
    if test.scenario == "STATE_TRANSITION_RUNTIME":
        return "The sequence may stall, skip, repeat, or enter an unintended state; inspect state writers, enabling conditions, and execution order."
    if test.scenario == "STATEFUL_RUNTIME":
        return "Timer/counter edge, preset, reset, or retained-state behavior may differ from the intended control sequence."
    if test.scenario == "MOTION_RUNTIME":
        return "The motion command may be rejected, fault, remain active unexpectedly, or produce an unintended controller-visible motion state."
    if test.scenario == "NEGATIVE_PATH":
        return "The controlled output may activate when an inhibiting or safety-related permissive is absent."
    return "The implemented PLC behavior may not match the modeled logic or stated requirement and should be reviewed before commissioning."


def enrich_fat_procedures(project, tests):
    """Turn bounded FAT candidates into engineer-ready manual procedures.

    DevAgent remains analysis/planning software. These fields tell the PLC engineer
    what to set up, exercise, watch, and record. They do not authorize or perform
    writes to a simulator, HIL system, or controller.
    """
    enriched = []
    for test in tests:
        refs = _source_refs(project, test)
        watch_tags = _ordered_unique((test.output_tag, *test.preconditions.keys(), *refs))[:16]
        evidence_required = (
            "PLC project/revision identifier used for the test",
            f"Source reference: {test.source.locator}",
            "Initial values for setup/precondition tags",
            "Observed values/trend/watch evidence for the listed watch tags",
            "Engineer-recorded PASS/FAIL result with timestamp and tester identity",
            "Failure notes, screenshots/traces, and disposition when the expected result is not met",
        )
        enriched.append(
            replace(
                test,
                purpose=test.purpose or _purpose(test),
                setup_steps=test.setup_steps or _setup_steps(test),
                action_steps=test.action_steps or _action_steps(test),
                watch_tags=test.watch_tags or watch_tags,
                evidence_required=test.evidence_required or evidence_required,
                why_required=test.why_required or _why_required(test),
                failure_implication=test.failure_implication or _failure_implication(test),
                recommended_environment=(
                    test.recommended_environment
                    or "Engineer-selected simulator, HIL/test bench, or real PLC under approved engineering procedures"
                ),
                engineer_execution_required=True,
            )
        )
    return enriched


def install() -> None:
    """Ensure tests added later by requirement verification get the same procedure contract."""
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import production_verification as _verification

    original = _verification.generate_requirement_tests

    def generate_requirement_tests(requirements, verifications, engineering):
        tests = original(requirements, verifications, engineering)
        tests = enrich_fat_procedures(engineering.project, tests)
        engineering.fat_tests = tests
        return tests

    _verification.generate_requirement_tests = generate_requirement_tests
    _INSTALLED = True


__all__ = ["enrich_fat_procedures", "install"]
