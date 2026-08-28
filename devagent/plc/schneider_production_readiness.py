from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from devagent.plc.schneider_closeout_v9 import (
    analyze_schneider_control_expert_v9,
    schneider_capability_profile_v9,
)

SCHEMA = "devagent-schneider-production-corpus-v1"
REPORT_SCHEMA = "devagent-schneider-production-corpus-report-v1"
REQUIRED_REAL_EXPORT_FAMILIES = (
    "M340", "M580", "UNITY_LEGACY", "MIXED_ST_LD_FBD", "DFB_DDT",
    "STATE_MACHINE", "INTERLOCK_FAULT_RECOVERY", "LARGE_INDUSTRIAL",
)
_HARDWARE_FAMILIES = {"M340", "M580", "UNITY_LEGACY"}
_ALLOWED_SOURCE_KINDS = {"REAL_CONTROL_EXPERT_EXPORT", "SYNTHETIC"}
_ALLOWED_ORIGINS = {"CUSTOMER", "LAB", "VENDOR_SAMPLE", "INTERNAL"}


class SchneiderProductionCorpusError(ValueError):
    pass


@dataclass(frozen=True)
class SchneiderProductionCaseResult:
    id: str
    path: str
    source_kind: str
    origin: str
    families: tuple[str, ...]
    controller_family: str | None
    observed_families: tuple[str, ...]
    bundle_sha256: str | None
    observed_bundle_sha256: str | None
    v9_manifest_sha256: str | None
    status: str
    support_contract: str | None
    support_regions: int
    support_full: int
    support_partial: int
    support_opaque: int
    support_protected: int
    source_files: int
    source_bytes: int
    deterministic: bool
    accounting_complete: bool
    metadata_consistent: bool
    audit_clean: bool
    hash_matches: bool
    real_export_eligible: bool
    findings: tuple[str, ...]


@dataclass(frozen=True)
class SchneiderProductionCorpusResult:
    schema: str
    corpus_id: str
    generated_at: str
    manifest_sha256: str
    corpus_root: str
    status: str
    target_readiness: str
    commercial_static_ready: bool
    runtime_execution_status: str
    required_families: tuple[str, ...]
    covered_real_families: tuple[str, ...]
    missing_real_families: tuple[str, ...]
    real_export_cases: int
    distinct_real_bundle_hashes: int
    cases: tuple[SchneiderProductionCaseResult, ...]
    blocking_findings: tuple[str, ...]


def _read_json(path: Path, max_bytes: int = 2 * 1024 * 1024) -> tuple[dict[str, Any], str]:
    payload = path.expanduser().resolve(strict=True).read_bytes()
    if len(payload) > max_bytes:
        raise SchneiderProductionCorpusError("Schneider production corpus manifest exceeds 2 MiB")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchneiderProductionCorpusError(f"Invalid Schneider production corpus JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SchneiderProductionCorpusError("Schneider production corpus manifest must be a JSON object")
    return value, hashlib.sha256(payload).hexdigest()


def _family(value: object) -> str:
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return {
        "LEGACY_UNITY_PRO": "UNITY_LEGACY",
        "UNITY_PRO_LEGACY": "UNITY_LEGACY",
        "MIXED_ST+LD+FBD": "MIXED_ST_LD_FBD",
        "DFB+DDT": "DFB_DDT",
        "CASE_STATE_MACHINE": "STATE_MACHINE",
        "CASE/STATE_MACHINE": "STATE_MACHINE",
        "INTERLOCK/FAULT/RECOVERY": "INTERLOCK_FAULT_RECOVERY",
        "LARGE_INDUSTRIAL_PROJECT": "LARGE_INDUSTRIAL",
    }.get(text, text)


def _resolve_case_path(root: Path, raw: object) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise SchneiderProductionCorpusError("Each corpus case requires a non-empty relative path")
    path = Path(text)
    if path.is_absolute():
        raise SchneiderProductionCorpusError(f"Corpus case path must be relative: {text}")
    resolved = (root / path).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SchneiderProductionCorpusError(f"Corpus case path escapes corpus root: {text}") from exc
    return resolved


def _observed_families(profile: dict[str, Any], controller_family: str | None) -> set[str]:
    observed: set[str] = set()
    if controller_family in _HARDWARE_FAMILIES:
        observed.add(controller_family)
    by_language = dict(profile.get("support_by_language", {}))
    languages = {
        str(name).upper() for name, counts in by_language.items()
        if sum(int(value or 0) for value in dict(counts).values()) > 0
    }
    if {"ST", "LD", "FBD"}.issubset(languages):
        observed.add("MIXED_ST_LD_FBD")
    if int(profile.get("ddt_types", 0)) > 0 and (
        int(profile.get("dfb_identity_types", 0)) > 0
        or int(profile.get("dfb_instance_identities", 0)) > 0
    ):
        observed.add("DFB_DDT")
    if int(profile.get("state_machines", 0)) > 0 and int(profile.get("state_machine_transitions", 0)) > 0:
        observed.add("STATE_MACHINE")
    guards = int(profile.get("classified_interlock_terms", 0)) + int(profile.get("classified_permissive_terms", 0))
    recovery = (
        int(profile.get("fault_entry_contracts", 0)) + int(profile.get("recovery_transitions", 0))
        + int(profile.get("stale_command_exit_hazards", 0)) + int(profile.get("recovery_bypass_exits", 0))
        + int(profile.get("uncommanded_fault_exits", 0))
    )
    if guards > 0 and recovery > 0:
        observed.add("INTERLOCK_FAULT_RECOVERY")
    if int(profile.get("source_bytes", 0)) >= 256 * 1024 or int(profile.get("support_regions", 0)) >= 200:
        observed.add("LARGE_INDUSTRIAL")
    return observed


def _snapshot(result) -> tuple[Any, ...]:
    profile = schneider_capability_profile_v9(result.project)
    return (
        result.project.metadata.source_sha256,
        profile.get("deterministic_manifest_sha256"), profile.get("support_contract"),
        profile.get("support_regions"), profile.get("support_full"), profile.get("support_partial"),
        profile.get("support_opaque"), profile.get("support_protected"),
        bool(profile.get("coverage_accounting_complete")), tuple(profile.get("duplicate_section_keys", ())),
        bool(profile.get("export_metadata_consistent")), tuple(profile.get("unknown_executable_source_tags", ())),
        tuple(profile.get("missing_source_sections", ())), int(profile.get("source_files", 0)),
        int(profile.get("source_bytes", 0)), tuple(sorted(dict(profile.get("suffix_counts", {})).items())),
    )


def _case_result(case: dict[str, Any], root: Path, run_twice: bool) -> SchneiderProductionCaseResult:
    case_id = str(case.get("id") or "").strip()
    if not case_id:
        raise SchneiderProductionCorpusError("Each corpus case requires an id")
    source_kind = str(case.get("source_kind") or "").strip().upper()
    if source_kind not in _ALLOWED_SOURCE_KINDS:
        raise SchneiderProductionCorpusError(f"{case_id}: invalid source_kind")
    origin = str(case.get("origin") or "").strip().upper()
    if origin not in _ALLOWED_ORIGINS:
        raise SchneiderProductionCorpusError(f"{case_id}: invalid origin")
    raw_families = case.get("families")
    if not isinstance(raw_families, list) or not raw_families:
        raise SchneiderProductionCorpusError(f"{case_id}: families must be a non-empty list")
    families = tuple(dict.fromkeys(_family(item) for item in raw_families))
    unknown = sorted(set(families) - set(REQUIRED_REAL_EXPORT_FAMILIES))
    if unknown:
        raise SchneiderProductionCorpusError(f"{case_id}: unknown qualification families: {', '.join(unknown)}")
    hardware = sorted(set(families) & _HARDWARE_FAMILIES)
    if len(hardware) > 1:
        raise SchneiderProductionCorpusError(f"{case_id}: one export cannot represent multiple hardware/legacy families")
    controller_family = _family(case.get("controller_family")) if case.get("controller_family") else None
    if controller_family is not None and controller_family not in _HARDWARE_FAMILIES:
        raise SchneiderProductionCorpusError(f"{case_id}: invalid controller_family")
    if hardware and controller_family != hardware[0]:
        raise SchneiderProductionCorpusError(
            f"{case_id}: controller_family {controller_family!r} must match hardware family {hardware[0]!r}"
        )

    path = _resolve_case_path(root, case.get("path"))
    expected_hash = str(case.get("bundle_sha256") or "").strip().lower() or None
    findings: list[str] = []
    real = source_kind == "REAL_CONTROL_EXPERT_EXPORT"
    if real:
        if len(expected_hash or "") != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash or ""):
            findings.append("real export is not pinned by a valid 64-character bundle_sha256")
        if not str(case.get("attested_by") or "").strip():
            findings.append("real export is missing attested_by")
        exported_at = str(case.get("exported_at") or "").strip()
        if not exported_at:
            findings.append("real export is missing exported_at")
        else:
            try:
                stamp = datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
            except ValueError:
                findings.append("real export exported_at is not valid ISO-8601")
            else:
                if stamp.tzinfo is None:
                    findings.append("real export exported_at must include a timezone offset")

    observed_hash = v9_manifest = support_contract = None
    observed_family_set: set[str] = set()
    support_regions = support_full = support_partial = support_opaque = support_protected = 0
    source_files = source_bytes = 0
    deterministic = accounting_complete = metadata_consistent = audit_clean = hash_matches = False
    try:
        first = analyze_schneider_control_expert_v9(path)
        profile = schneider_capability_profile_v9(first.project)
        observed_hash = str(first.project.metadata.source_sha256 or "").lower() or None
        v9_manifest = str(profile.get("deterministic_manifest_sha256") or "") or None
        support_contract = str(profile.get("support_contract") or "NONE")
        support_regions = int(profile.get("support_regions", 0))
        support_full = int(profile.get("support_full", 0))
        support_partial = int(profile.get("support_partial", 0))
        support_opaque = int(profile.get("support_opaque", 0))
        support_protected = int(profile.get("support_protected", 0))
        source_files = int(profile.get("source_files", 0))
        source_bytes = int(profile.get("source_bytes", 0))
        observed_family_set = _observed_families(profile, controller_family)
        missing_evidence = sorted(set(families) - observed_family_set)
        if missing_evidence:
            findings.append("manifest family classification is not substantiated by the analyzed export: " + ", ".join(missing_evidence))
        accounting_complete = bool(profile.get("coverage_accounting_complete"))
        metadata_consistent = bool(profile.get("export_metadata_consistent"))
        duplicate_sections = tuple(profile.get("duplicate_section_keys", ()))
        unknown_sources = tuple(profile.get("unknown_executable_source_tags", ()))
        missing_sections = tuple(profile.get("missing_source_sections", ()))
        audit_clean = accounting_complete and metadata_consistent and not duplicate_sections and not unknown_sources and not missing_sections and support_regions > 0
        if not accounting_complete:
            findings.append("V9 support accounting is incomplete")
        if not metadata_consistent:
            findings.append("Control Expert export metadata is inconsistent")
        if duplicate_sections:
            findings.append("duplicate section identity: " + ", ".join(duplicate_sections[:8]))
        if unknown_sources:
            findings.append("unknown executable source surface: " + ", ".join(unknown_sources[:8]))
        if missing_sections:
            findings.append("missing executable source section: " + ", ".join(missing_sections[:8]))
        if support_regions <= 0:
            findings.append("export has no qualified executable/support region")
        hash_matches = observed_hash == expected_hash if expected_hash else not real
        if expected_hash and not hash_matches:
            findings.append(f"bundle_sha256 mismatch: expected {expected_hash}, observed {observed_hash}")
        deterministic = _snapshot(first) == _snapshot(analyze_schneider_control_expert_v9(path)) if run_twice else True
        if not deterministic:
            findings.append("repeated V9 analysis produced a different deterministic snapshot")
    except Exception as exc:
        findings.append(f"analysis failed: {type(exc).__name__}: {exc}")

    status = "PASS" if not findings and audit_clean and deterministic and hash_matches else "FAIL"
    return SchneiderProductionCaseResult(
        case_id, str(case.get("path") or ""), source_kind, origin, families, controller_family,
        tuple(sorted(observed_family_set)), expected_hash, observed_hash, v9_manifest, status, support_contract,
        support_regions, support_full, support_partial, support_opaque, support_protected, source_files, source_bytes,
        deterministic, accounting_complete, metadata_consistent, audit_clean, hash_matches, real, tuple(findings),
    )


def qualify_schneider_production_corpus(
    manifest_path: Path, *, corpus_root: Path | None = None, run_twice: bool = True,
) -> SchneiderProductionCorpusResult:
    manifest_path = Path(manifest_path).expanduser().resolve(strict=True)
    manifest, manifest_hash = _read_json(manifest_path)
    if manifest.get("schema") != SCHEMA:
        raise SchneiderProductionCorpusError(f"Unsupported corpus schema; expected {SCHEMA}")
    corpus_id = str(manifest.get("corpus_id") or "").strip()
    cases_payload = manifest.get("cases")
    if not corpus_id:
        raise SchneiderProductionCorpusError("Schneider production corpus requires corpus_id")
    if not isinstance(cases_payload, list) or not cases_payload or len(cases_payload) > 100:
        raise SchneiderProductionCorpusError("Schneider production corpus requires 1-100 cases")
    if not all(isinstance(item, dict) for item in cases_payload):
        raise SchneiderProductionCorpusError("Every corpus case must be a JSON object")
    root = Path(corpus_root).expanduser().resolve(strict=True) if corpus_root else manifest_path.parent.resolve(strict=True)
    ids = [str(item.get("id") or "").strip().casefold() for item in cases_payload]
    if any(not item for item in ids):
        raise SchneiderProductionCorpusError("Every corpus case requires a non-empty id")
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise SchneiderProductionCorpusError("Duplicate corpus case ids: " + ", ".join(duplicates))

    cases = tuple(_case_result(item, root, run_twice) for item in cases_payload)
    real_passes = [item for item in cases if item.real_export_eligible and item.status == "PASS"]
    covered = tuple(sorted({family for item in real_passes for family in item.families}))
    missing = tuple(family for family in REQUIRED_REAL_EXPORT_FAMILIES if family not in set(covered))
    real_hashes = {item.observed_bundle_sha256 for item in real_passes if item.observed_bundle_sha256}
    blockers: list[str] = []
    failed = [item for item in cases if item.status != "PASS"]
    if failed:
        blockers.append(f"{len(failed)} corpus case(s) failed qualification")
    if missing:
        blockers.append("missing real-export families: " + ", ".join(missing))
    hardware_hashes: dict[str, set[str]] = {family: set() for family in _HARDWARE_FAMILIES}
    for item in real_passes:
        for family in set(item.families) & _HARDWARE_FAMILIES:
            if item.observed_bundle_sha256:
                hardware_hashes[family].add(item.observed_bundle_sha256)
    represented = [next(iter(values)) for values in hardware_hashes.values() if values]
    if len(represented) == len(_HARDWARE_FAMILIES) and len(set(represented)) != len(_HARDWARE_FAMILIES):
        blockers.append("M340, M580, and UNITY_LEGACY must use distinct real export bundle identities")
    ready = not blockers
    status = "FAIL" if failed else "PENDING_REAL_EXPORT_CORPUS" if missing else "STATIC_CORPUS_QUALIFIED" if ready else "FAIL"
    return SchneiderProductionCorpusResult(
        REPORT_SCHEMA, corpus_id, datetime.now(timezone.utc).isoformat(), manifest_hash, str(root), status,
        "9/10_STATIC_PRODUCTION_QUALIFIED", ready, "NOT_EXECUTED", REQUIRED_REAL_EXPORT_FAMILIES,
        covered, missing, sum(item.real_export_eligible for item in cases), len(real_hashes), cases, tuple(blockers),
    )


def result_payload(result: SchneiderProductionCorpusResult) -> dict[str, Any]:
    return asdict(result)


def render_markdown(result: SchneiderProductionCorpusResult) -> str:
    rows = []
    for item in result.cases:
        rows.append(
            f"| {item.id} | {item.source_kind} | {', '.join(item.families)} | {', '.join(item.observed_families)} | "
            f"{item.status} | {item.support_contract or 'NONE'} | P={item.support_partial}, O={item.support_opaque}, X={item.support_protected} | "
            f"{'YES' if item.deterministic else 'NO'} |"
        )
    blockers = "\n".join(f"- {item}" for item in result.blocking_findings) or "- None"
    return (
        "# Schneider Control Expert Production Corpus Qualification\n\n"
        f"- Status: **{result.status}**\n- Target: **{result.target_readiness}**\n"
        f"- Commercial static ready: **{'YES' if result.commercial_static_ready else 'NO'}**\n"
        f"- Runtime execution: **{result.runtime_execution_status}**\n"
        f"- Manifest SHA-256: `{result.manifest_sha256}`\n"
        f"- Covered real families: **{', '.join(result.covered_real_families) or 'none'}**\n"
        f"- Missing real families: **{', '.join(result.missing_real_families) or 'none'}**\n\n"
        "| Case | Source | Declared families | Observed evidence | Status | V9 contract | Explicit gaps | Deterministic |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        + ("\n".join(rows) if rows else "| none | - | - | - | - | - | - | - |")
        + "\n\n## Blocking findings\n\n" + blockers
        + "\n\n> `STATIC_CORPUS_QUALIFIED` proves deterministic handling and explicit fail-closed accounting across the registered real export corpus. It does not prove Simulator/HIL/real PLC execution, field wiring, scan timing, process physics, SIL, or PL.\n"
    )


def write_report(result: SchneiderProductionCorpusResult, *, json_path: Path, markdown_path: Path | None = None) -> None:
    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result_payload(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_path is not None:
        markdown_path = Path(markdown_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(result), encoding="utf-8")


__all__ = [
    "REPORT_SCHEMA", "REQUIRED_REAL_EXPORT_FAMILIES", "SCHEMA",
    "SchneiderProductionCaseResult", "SchneiderProductionCorpusError", "SchneiderProductionCorpusResult",
    "qualify_schneider_production_corpus", "render_markdown", "result_payload", "write_report",
]
