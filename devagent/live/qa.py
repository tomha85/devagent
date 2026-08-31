from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from devagent.providers import ModelProvider, ProviderError

from .diagnosis import LiveCommissioningDiagnosis, LiveDiagnosisStatus


LIVE_COMMISSIONING_QA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "answer",
        "confidence",
        "evidence_ids",
        "next_checks",
        "limitations",
    ],
    "properties": {
        "answer": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "evidence_ids": {
            "type": "array",
            "maxItems": 24,
            "items": {"type": "string", "minLength": 1},
        },
        "next_checks": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1},
        },
        "limitations": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1},
        },
    },
}


@dataclass(frozen=True)
class LiveCommissioningAnswer:
    question: str
    target_output: str
    diagnosis_status: LiveDiagnosisStatus
    answer: str
    confidence: float
    evidence_ids: tuple[str, ...]
    next_checks: tuple[str, ...]
    limitations: tuple[str, ...]
    ai_assisted: bool

    def render_text(self) -> str:
        lines = [
            self.answer,
            "",
            f"Diagnosis: {self.diagnosis_status.value}",
            f"Target: {self.target_output}",
            f"Confidence: {self.confidence:.2f}",
        ]
        if self.next_checks:
            lines.extend(["", "Next checks:"])
            lines.extend(f"- {item}" for item in self.next_checks)
        if self.limitations:
            lines.extend(["", "Limitations:"])
            lines.extend(f"- {item}" for item in self.limitations)
        if self.evidence_ids:
            lines.extend(["", "Evidence:"])
            lines.extend(f"- {item}" for item in self.evidence_ids)
        return "\n".join(lines)


def _deterministic_confidence(diagnosis: LiveCommissioningDiagnosis) -> float:
    if diagnosis.status is LiveDiagnosisStatus.BLOCKER_IDENTIFIED:
        return 0.95 if not diagnosis.limitations else 0.85
    if diagnosis.status is LiveDiagnosisStatus.CONDITIONS_SATISFIED:
        return 0.90 if not diagnosis.limitations else 0.80
    if diagnosis.status is LiveDiagnosisStatus.LOGIC_CONFLICT:
        return 0.75
    if diagnosis.status in {
        LiveDiagnosisStatus.INDETERMINATE,
        LiveDiagnosisStatus.NO_EVALUABLE_RULE,
    }:
        return 0.35
    return 0.20


def _deterministic_answer(
    question: str,
    diagnosis: LiveCommissioningDiagnosis,
) -> LiveCommissioningAnswer:
    return LiveCommissioningAnswer(
        question=question,
        target_output=diagnosis.target_output,
        diagnosis_status=diagnosis.status,
        answer=diagnosis.summary,
        confidence=_deterministic_confidence(diagnosis),
        evidence_ids=diagnosis.evidence_ids,
        next_checks=diagnosis.next_checks,
        limitations=diagnosis.limitations,
        ai_assisted=False,
    )


def _provider_payload(
    question: str,
    diagnosis: LiveCommissioningDiagnosis,
) -> dict[str, Any]:
    paths: list[dict[str, Any]] = []
    for path in diagnosis.paths:
        paths.append(
            {
                "index": path.index,
                "state": path.state.value,
                "conditions": [
                    {
                        "tag": condition.tag_name or condition.tag_reference,
                        "required": condition.required,
                        "observed": condition.observed_value,
                        "state": condition.state.value,
                        "evidence_id": condition.evidence_id,
                        "detail": condition.detail,
                    }
                    for condition in path.conditions
                ],
            }
        )
    return {
        "instruction": (
            "You are the explanation layer for a read-only onsite PLC commissioning assistant. "
            "The deterministic diagnosis below is authoritative. Explain it clearly to the engineer. "
            "Do not invent PLC logic, hidden tags, causes, safety claims, FAT results, or release-readiness claims. "
            "Do not instruct the user to write, force, bypass, reset, download, change mode, or otherwise control the PLC. "
            "Suggested next checks must be read-only observations, engineering source inspection, or approved field diagnostics. "
            "If diagnosis_status is INDETERMINATE or NO_EVALUABLE_RULE, explicitly say the cause is not proven. "
            "Cite only the supplied evidence_ids."
        ),
        "question": question,
        "deterministic_diagnosis": {
            "target_output": diagnosis.target_output,
            "diagnosis_status": diagnosis.status.value,
            "expected_output": diagnosis.expected_output,
            "observed_output": diagnosis.observed_output,
            "summary": diagnosis.summary,
            "rule_ids": list(diagnosis.rule_ids),
            "source_locators": list(diagnosis.source_locators),
            "paths": paths,
            "blockers": [
                {
                    "tag": item.tag_name or item.tag_reference,
                    "required": item.required,
                    "observed": item.observed_value,
                    "evidence_id": item.evidence_id,
                }
                for item in diagnosis.blockers
            ],
            "evidence_ids": list(diagnosis.evidence_ids),
            "limitations": list(diagnosis.limitations),
            "next_checks": list(diagnosis.next_checks),
        },
    }


# Generated prose needs a broader guard than the user-request control detector.
# These phrases are intentionally action/object oriented so normal diagnostic
# wording such as "start by inspecting the safety chain" remains allowed while
# indirect machine-control advice is rejected regardless of sentence grammar.
_FORBIDDEN_CONTROL_PHRASES = (
    "force the",
    "force tag",
    "force output",
    "force coil",
    "write the",
    "write tag",
    "write output",
    "bypass the",
    "bypass safety",
    "bypass interlock",
    "reset the",
    "reset safety",
    "reset fault",
    "reset trip",
    "reset plc",
    "reset controller",
    "reset drive",
    "download to plc",
    "download to the plc",
    "upload to plc",
    "upload to the plc",
    "change plc mode",
    "change the plc mode",
    "change controller mode",
    "change the controller mode",
    "set the output",
    "set output",
    "set the tag",
    "set tag",
    "start the line",
    "start line",
    "start the machine",
    "start machine",
    "start the motor",
    "start motor",
    "start the conveyor",
    "start conveyor",
    "start the equipment",
    "start equipment",
    "stop the line",
    "stop line",
    "stop the machine",
    "stop machine",
    "stop the motor",
    "stop motor",
    "stop the conveyor",
    "stop conveyor",
    "stop the equipment",
    "stop equipment",
    "jog the",
    "jog axis",
    "jog motor",
    "override the",
    "override interlock",
    "override safety",
    "command the motor",
    "command the drive",
    "command the output",
)

_GENERATED_CONTROL_OBJECTS = (
    "line",
    "machine",
    "motor",
    "conveyor",
    "equipment",
    "output",
    "tag",
    "drive",
    "axis",
    "robot",
    "cell",
    "system",
    "plc",
    "controller",
)

_FORBIDDEN_TURN_CONTROL_PHRASES = tuple(
    phrase
    for object_name in _GENERATED_CONTROL_OBJECTS
    for phrase in (
        f"turn on the {object_name}",
        f"turn on {object_name}",
        f"turn off the {object_name}",
        f"turn off {object_name}",
        f"turn the {object_name} on",
        f"turn {object_name} on",
        f"turn the {object_name} off",
        f"turn {object_name} off",
    )
)


def _contains_forbidden_control_advice(text: str) -> bool:
    lowered = " ".join(str(text or "").casefold().split())
    return any(phrase in lowered for phrase in _FORBIDDEN_CONTROL_PHRASES) or any(
        phrase in lowered for phrase in _FORBIDDEN_TURN_CONTROL_PHRASES
    )


def answer_commissioning_question(
    question: str,
    diagnosis: LiveCommissioningDiagnosis,
    *,
    provider: ModelProvider | None = None,
) -> LiveCommissioningAnswer:
    deterministic = _deterministic_answer(question, diagnosis)
    if provider is None:
        return deterministic

    try:
        response = provider.request(
            role="live_commissioning_explainer",
            payload=_provider_payload(question, diagnosis),
            schema=LIVE_COMMISSIONING_QA_SCHEMA,
        )
    except ProviderError as exc:
        return LiveCommissioningAnswer(
            question=deterministic.question,
            target_output=deterministic.target_output,
            diagnosis_status=deterministic.diagnosis_status,
            answer=deterministic.answer,
            confidence=deterministic.confidence,
            evidence_ids=deterministic.evidence_ids,
            next_checks=deterministic.next_checks,
            limitations=tuple(
                dict.fromkeys(
                    (
                        *deterministic.limitations,
                        f"AI explanation unavailable: {exc}",
                    )
                )
            ),
            ai_assisted=False,
        )

    valid_ids = set(diagnosis.evidence_ids)
    cited = tuple(str(item) for item in response.get("evidence_ids", ()))
    if any(item not in valid_ids for item in cited):
        return LiveCommissioningAnswer(
            question=deterministic.question,
            target_output=deterministic.target_output,
            diagnosis_status=deterministic.diagnosis_status,
            answer=deterministic.answer,
            confidence=deterministic.confidence,
            evidence_ids=deterministic.evidence_ids,
            next_checks=deterministic.next_checks,
            limitations=tuple(
                dict.fromkeys(
                    (
                        *deterministic.limitations,
                        "AI explanation was rejected because it cited evidence outside the deterministic diagnosis boundary.",
                    )
                )
            ),
            ai_assisted=False,
        )

    answer = str(response["answer"]).strip()
    next_checks = tuple(
        str(item).strip()
        for item in response.get("next_checks", ())
        if str(item).strip()
    )
    provider_limitations = tuple(
        str(item).strip()
        for item in response.get("limitations", ())
        if str(item).strip()
    )
    generated_text = (answer, *next_checks, *provider_limitations)
    if any(_contains_forbidden_control_advice(item) for item in generated_text):
        return LiveCommissioningAnswer(
            question=deterministic.question,
            target_output=deterministic.target_output,
            diagnosis_status=deterministic.diagnosis_status,
            answer=deterministic.answer,
            confidence=deterministic.confidence,
            evidence_ids=deterministic.evidence_ids,
            next_checks=deterministic.next_checks,
            limitations=tuple(
                dict.fromkeys(
                    (
                        *deterministic.limitations,
                        "AI explanation was rejected because it suggested PLC control/write behavior outside DevAgent Live read-only scope.",
                    )
                )
            ),
            ai_assisted=False,
        )

    return LiveCommissioningAnswer(
        question=question,
        target_output=diagnosis.target_output,
        diagnosis_status=diagnosis.status,
        answer=answer,
        confidence=min(
            float(response["confidence"]),
            _deterministic_confidence(diagnosis),
        ),
        evidence_ids=cited or diagnosis.evidence_ids,
        next_checks=next_checks or diagnosis.next_checks,
        limitations=tuple(
            dict.fromkeys(
                (*diagnosis.limitations, *provider_limitations)
            )
        ),
        ai_assisted=True,
    )


__all__ = [
    "LIVE_COMMISSIONING_QA_SCHEMA",
    "LiveCommissioningAnswer",
    "answer_commissioning_question",
]
