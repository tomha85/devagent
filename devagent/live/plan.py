from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import LiveConfigurationError
from .security import validate_opcua_endpoint

_MAX_PLAN_TAGS = 200
_PLC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

ProjectLoader = Callable[[Path], Any]


class LivePlanReferenceStatus(str, Enum):
    RESOLVED = "RESOLVED"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    ALIAS_REQUIRES_EXPLICIT = "ALIAS_REQUIRES_EXPLICIT"
    EXTERNAL_ACCESS_BLOCKED = "EXTERNAL_ACCESS_BLOCKED"


@dataclass(frozen=True)
class LivePlanReference:
    reference: str
    roles: tuple[str, ...]
    fat_test_ids: tuple[str, ...]
    status: LivePlanReferenceStatus
    candidate_tag_ids: tuple[str, ...]
    selected_tag_id: str | None
    reason: str


@dataclass(frozen=True)
class LiveCommissionPlan:
    plc_id: str
    plc_name: str
    endpoint: str
    engineering_project_path: Path
    vendor: str
    project_sha256: str | None
    references: tuple[LivePlanReference, ...]
    required_tag_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return bool(self.required_tag_ids) and all(
            item.status is LivePlanReferenceStatus.RESOLVED
            for item in self.references
        )

    @property
    def unresolved(self) -> tuple[LivePlanReference, ...]:
        return tuple(
            item
            for item in self.references
            if item.status is not LivePlanReferenceStatus.RESOLVED
        )


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    text = text.replace("\\", ".").replace("/", ".")
    parts = []
    for raw in text.split("."):
        part = raw.strip().strip('"')
        if part:
            parts.append(part)
    return ".".join(parts)


def _scope_program(scope: str) -> str | None:
    raw = str(scope or "").strip()
    if ":" not in raw:
        return None
    prefix, value = raw.split(":", 1)
    if prefix.strip().casefold() != "program" or not value.strip():
        return None
    return value.strip()


def _tag_forms(tag: Any) -> tuple[str, ...]:
    name = str(getattr(tag, "name", "") or "").strip()
    if not name:
        return ()
    forms = {_normalize(name)}
    program = _scope_program(str(getattr(tag, "scope", "") or ""))
    if program:
        forms.add(_normalize(f"{program}.{name}"))
        forms.add(_normalize(f"Program:{program}.{name}"))
    return tuple(sorted(form for form in forms if form))


def _external_access_blocked(tag: Any) -> bool:
    value = str(getattr(tag, "external_access", "") or "").strip().casefold()
    return value in {"none", "noaccess", "disabled", "false"}


def _project_loader(path: Path) -> Any:
    from devagent.plc.plc_dispatch import analyze_plc_project

    return analyze_plc_project(path)


def _engineering_parts(loaded: Any) -> tuple[Any, tuple[Any, ...]]:
    project = getattr(loaded, "project", None)
    fat_tests = getattr(loaded, "fat_tests", None)
    if project is None or fat_tests is None:
        raise LiveConfigurationError(
            "PLC engineering analysis did not provide project + FAT test surfaces required for Live plan generation"
        )
    if getattr(project, "tags", None) is None:
        raise LiveConfigurationError("PLC engineering project does not expose canonical tags")
    return project, tuple(fat_tests)


def _collect_fat_references(fat_tests: Iterable[Any]) -> list[tuple[str, str, str]]:
    references: list[tuple[str, str, str]] = []
    for test in fat_tests:
        test_id = str(getattr(test, "id", "") or "").strip() or "UNKNOWN-FAT"
        output = str(getattr(test, "output_tag", "") or "").strip()
        if output:
            references.append((output, "OUTPUT", test_id))
        preconditions = getattr(test, "preconditions", {}) or {}
        if not isinstance(preconditions, dict):
            raise LiveConfigurationError(f"FAT test {test_id} preconditions must be a mapping")
        for tag_name in preconditions:
            text = str(tag_name).strip()
            if text:
                references.append((text, "PRECONDITION", test_id))
        for tag_name in tuple(getattr(test, "watch_tags", ()) or ()):
            text = str(tag_name).strip()
            if text:
                references.append((text, "WATCH", test_id))
    return references


def _resolve_reference(reference: str, tags: tuple[Any, ...]) -> tuple[LivePlanReferenceStatus, tuple[Any, ...], Any | None, str]:
    target = _normalize(reference)
    candidates = tuple(tag for tag in tags if target and target in _tag_forms(tag))
    if not candidates:
        return (
            LivePlanReferenceStatus.UNMATCHED,
            (),
            None,
            "No canonical engineering tag has this exact FAT reference identity.",
        )
    if len(candidates) > 1:
        return (
            LivePlanReferenceStatus.AMBIGUOUS,
            candidates,
            None,
            "Multiple canonical tags match the exact FAT reference; scope must be made explicit before Live use.",
        )
    selected = candidates[0]
    if getattr(selected, "alias_for", None):
        return (
            LivePlanReferenceStatus.ALIAS_REQUIRES_EXPLICIT,
            candidates,
            None,
            "The FAT reference resolves to an alias tag; Live V1 requires an explicit engineering-to-NodeId mapping for aliases.",
        )
    if _external_access_blocked(selected):
        return (
            LivePlanReferenceStatus.EXTERNAL_ACCESS_BLOCKED,
            candidates,
            None,
            "Engineering metadata explicitly blocks external access for this tag.",
        )
    return (
        LivePlanReferenceStatus.RESOLVED,
        candidates,
        selected,
        "Exact canonical FAT reference resolved to one externally eligible engineering tag.",
    )


def build_live_commission_plan(
    engineering: Any,
    *,
    engineering_project_path: Path,
    plc_id: str,
    endpoint: str,
    plc_name: str | None = None,
) -> LiveCommissionPlan:
    clean_plc_id = str(plc_id).strip()
    if not _PLC_ID.fullmatch(clean_plc_id):
        raise LiveConfigurationError(f"PLC id {clean_plc_id!r} must match {_PLC_ID.pattern}")
    clean_endpoint = validate_opcua_endpoint(str(endpoint).strip())
    project, fat_tests = _engineering_parts(engineering)
    raw_refs = _collect_fat_references(fat_tests)
    if not raw_refs:
        raise LiveConfigurationError(
            "Engineering analysis produced no FAT output/precondition/watch tag references for automatic Live planning"
        )

    tags = tuple(project.tags)
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for reference, role, test_id in raw_refs:
        key = _normalize(reference)
        if not key:
            continue
        if key not in grouped:
            grouped[key] = {
                "reference": reference,
                "roles": [],
                "test_ids": [],
            }
            order.append(key)
        if role not in grouped[key]["roles"]:
            grouped[key]["roles"].append(role)
        if test_id not in grouped[key]["test_ids"]:
            grouped[key]["test_ids"].append(test_id)

    references: list[LivePlanReference] = []
    selected_ids: list[str] = []
    seen_selected: set[str] = set()
    for key in order:
        group = grouped[key]
        status, candidates, selected, reason = _resolve_reference(group["reference"], tags)
        candidate_ids = tuple(
            str(getattr(tag, "id", "") or "").strip()
            for tag in candidates
            if str(getattr(tag, "id", "") or "").strip()
        )
        selected_id = None
        if selected is not None:
            selected_id = str(getattr(selected, "id", "") or "").strip()
            if not selected_id:
                raise LiveConfigurationError(
                    f"Resolved canonical tag for FAT reference {group['reference']!r} has no tag id"
                )
            if selected_id not in seen_selected:
                seen_selected.add(selected_id)
                selected_ids.append(selected_id)
        references.append(
            LivePlanReference(
                reference=group["reference"],
                roles=tuple(group["roles"]),
                fat_test_ids=tuple(group["test_ids"]),
                status=status,
                candidate_tag_ids=candidate_ids,
                selected_tag_id=selected_id,
                reason=reason,
            )
        )

    if len(selected_ids) > _MAX_PLAN_TAGS:
        raise LiveConfigurationError(
            f"Automatic Live plan resolved {len(selected_ids)} tags; V1 limit is {_MAX_PLAN_TAGS}. Split the commissioning scope."
        )

    metadata = getattr(project, "metadata", None)
    vendor = str(getattr(metadata, "vendor", "UNKNOWN") or "UNKNOWN")
    project_sha = getattr(metadata, "source_sha256", None)
    resolved_path = Path(engineering_project_path).expanduser().resolve(strict=False)
    return LiveCommissionPlan(
        plc_id=clean_plc_id,
        plc_name=(str(plc_name).strip() if plc_name and str(plc_name).strip() else clean_plc_id),
        endpoint=clean_endpoint,
        engineering_project_path=resolved_path,
        vendor=vendor,
        project_sha256=str(project_sha) if project_sha else None,
        references=tuple(references),
        required_tag_ids=tuple(selected_ids),
    )


def analyze_and_build_live_commission_plan(
    project_path: Path,
    *,
    plc_id: str,
    endpoint: str,
    plc_name: str | None = None,
    project_loader: ProjectLoader | None = None,
) -> LiveCommissionPlan:
    source = Path(project_path).expanduser().resolve(strict=True)
    loader = project_loader or _project_loader
    engineering = loader(source)
    return build_live_commission_plan(
        engineering,
        engineering_project_path=source,
        plc_id=plc_id,
        endpoint=endpoint,
        plc_name=plc_name,
    )


def _config_payload(plan: LiveCommissionPlan) -> dict[str, Any]:
    if not plan.complete:
        unresolved = ", ".join(
            f"{item.reference}={item.status.value}" for item in plan.unresolved
        )
        raise LiveConfigurationError(
            "Automatic Live plan is incomplete and cannot be written as an executable commissioning config: "
            + unresolved
        )
    return {
        "schema": "devagent-live-commission-v1",
        "plcs": [
            {
                "plc_id": plan.plc_id,
                "plc_name": plan.plc_name,
                "endpoint": plan.endpoint,
                "engineering_project": str(plan.engineering_project_path),
                "required_tag_ids": list(plan.required_tag_ids),
                "require_all_mappings": True,
            }
        ],
    }


def _report_payload(plan: LiveCommissionPlan, config_sha256: str | None) -> dict[str, Any]:
    return {
        "schema": "devagent-live-commission-plan-v1",
        "mode": "READ_ONLY",
        "complete": plan.complete,
        "plc_id": plan.plc_id,
        "plc_name": plan.plc_name,
        "endpoint": plan.endpoint,
        "vendor": plan.vendor,
        "engineering_project": str(plan.engineering_project_path),
        "project_sha256": plan.project_sha256,
        "required_tag_ids": list(plan.required_tag_ids),
        "config_sha256": config_sha256,
        "references": [
            {
                "reference": item.reference,
                "roles": list(item.roles),
                "fat_test_ids": list(item.fat_test_ids),
                "status": item.status.value,
                "candidate_tag_ids": list(item.candidate_tag_ids),
                "selected_tag_id": item.selected_tag_id,
                "reason": item.reason,
            }
            for item in plan.references
        ],
    }


def write_live_commission_plan(
    output_path: Path,
    plan: LiveCommissionPlan,
) -> tuple[Path, Path]:
    config_payload = _config_payload(plan)
    target = Path(output_path).expanduser().resolve(strict=False)
    report = target.with_name(target.name + ".plan.json")
    if target.exists():
        raise FileExistsError(target)
    if report.exists():
        raise FileExistsError(report)
    target.parent.mkdir(parents=True, exist_ok=True)

    config_bytes = (json.dumps(config_payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    report_bytes = (
        json.dumps(_report_payload(plan, config_sha), indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    # Exclusive creates prevent a concurrent process from silently replacing a plan.
    with target.open("xb") as stream:
        stream.write(config_bytes)
    try:
        with report.open("xb") as stream:
            stream.write(report_bytes)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target, report


__all__ = [
    "LiveCommissionPlan",
    "LivePlanReference",
    "LivePlanReferenceStatus",
    "analyze_and_build_live_commission_plan",
    "build_live_commission_plan",
    "write_live_commission_plan",
]
