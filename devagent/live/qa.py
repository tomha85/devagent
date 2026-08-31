from __future__ import annotations

import re
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
# The guard normalizes lightweight Markdown/quote delimiters, then requires an
# actual PLC-like or machine action object before treating a control verb as
# prohibited. This blocks quoted canonical references without rejecting safety
# boundary prose such as "Write access is disabled" or "Reset commands are unavailable".
_FORBIDDEN_CONTROL_PHRASES = (
    "download to plc",
    "download to the plc",
    "upload to plc",
    "upload to the plc",
    "change plc mode",
    "change the plc mode",
    "change controller mode",
    "change the controller mode",
)

_PLC_REFERENCE_TOKEN = r"[A-Za-z_][A-Za-z0-9_.:\[\]]*"
_REFERENCE_DELIMITER_RE = re.compile(r"[`\"'“”‘’()]", flags=re.UNICODE)

_SAFE_CONTROL_META_TARGETS = frozenset(
    {
        "access",
        "action",
        "actions",
        "advice",
        "analysis",
        "capability",
        "capabilities",
        "command",
        "commands",
        "diagnostic",
        "diagnostics",
        "documentation",
        "docs",
        "evidence",
        "guidance",
        "instruction",
        "instructions",
        "logging",
        "logs",
        "note",
        "notes",
        "operation",
        "operations",
        "permission",
        "permissions",
        "report",
        "reports",
        "request",
        "requests",
        "scope",
        "support",
    }
)

_GENERIC_MACHINE_CONTROL_TARGETS = frozenset(
    {
        "axis",
        "bit",
        "cell",
        "coil",
        "controller",
        "conveyor",
        "drive",
        "equipment",
        "fan",
        "fault",
        "feeder",
        "interlock",
        "line",
        "machine",
        "mode",
        "motor",
        "output",
        "plc",
        "pump",
        "robot",
        "safety",
        "servo",
        "system",
        "tag",
        "trip",
        "valve",
    }
)

_GENERATED_DIRECT_CONTROL_RE = re.compile(
    rf"\b(?P<verb>force|write|set|reset|bypass|override|jog|command)\s+"
    rf"(?:the\s+)?(?P<target>{_PLC_REFERENCE_TOKEN})\b",
    flags=re.IGNORECASE,
)

_GENERATED_START_STOP_CONTROL_RE = re.compile(
    rf"\b(?P<verb>start|stop)\s+"
    rf"(?!by\b|with\b|after\b|before\b|when\b|if\b|because\b)"
    rf"(?:the\s+)?(?P<target>{_PLC_REFERENCE_TOKEN})\b",
    flags=re.IGNORECASE,
)

_GENERATED_TURN_PREFIX_RE = re.compile(
    rf"\bturn\s+(?:on|off)\s+(?:the\s+)?(?P<target>{_PLC_REFERENCE_TOKEN})\b",
    flags=re.IGNORECASE,
)

_GENERATED_TURN_SUFFIX_RE = re.compile(
    rf"\bturn\s+(?:the\s+)?(?P<target>{_PLC_REFERENCE_TOKEN})\s+(?:on|off)\b",
    flags=re.IGNORECASE,
)

_GENERATED_PLC_TRANSFER_RE = re.compile(
    r"\b(?:download|upload)\b[^.!?\n]{0,120}\b(?:to|into|onto)\s+"
    r"(?:the\s+)?(?:plc|controller)\b",
    flags=re.IGNORECASE,
)


def _looks_like_plc_action_object(value: str) -> bool:
    token = str(value or "").strip()
    lowered = token.casefold()
    if not token or lowered in _SAFE_CONTROL_META_TARGETS:
        return False
    if lowered in _GENERIC_MACHINE_CONTROL_TARGETS:
        return True
    if any(char in token for char in "_.:[]"):
        return True
    if any(char.isdigit() for char in token):
        return True
    if len(token) > 1 and token.isupper():
        return True
    return any(char.isupper() for char in token[1:]) and any(
        char.islower() for char in token
    )


def _has_generated_control_target_match(
    pattern: re.Pattern[str],
    text: str,
) -> bool:
    return any(
        _looks_like_plc_action_object(match.group("target"))
        for match in pattern.finditer(text)
    )


def _contains_forbidden_control_advice(text: str) -> bool:
    normalized = " ".join(str(text or "").split())
    comparable = _REFERENCE_DELIMITER_RE.sub("", normalized)
    lowered = comparable.casefold()
    return (
        any(phrase in lowered for phrase in _FORBIDDEN_CONTROL_PHRASES)
        or _GENERATED_PLC_TRANSFER_RE.search(comparable) is not None
        or _has_generated_control_target_match(
            _GENERATED_DIRECT_CONTROL_RE,
            comparable,
        )
        or _has_generated_control_target_match(
            _GENERATED_START_STOP_CONTROL_RE,
            comparable,
        )
        or _has_generated_control_target_match(
            _GENERATED_TURN_PREFIX_RE,
            comparable,
        )
        or _has_generated_control_target_match(
            _GENERATED_TURN_SUFFIX_RE,
            comparable,
        )
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
