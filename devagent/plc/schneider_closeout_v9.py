from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

from devagent.plc import schneider_call_graph_v3 as _v3
from devagent.plc import schneider_control_expert_v1 as _v1
from devagent.plc import schneider_identity_types_v8 as _v8
from devagent.plc.models import PLCOutcome, PLCEngineeringResult, PLCSemanticState, StaticCheck, StaticCheckStatus
from devagent.plc.production_models import EngineeringFinding, EvidenceItem, RiskFinding, Severity
from devagent.plc.production_utils import stable_id


_INSTALLED = False
_PREVIOUS_ANALYZER = _v8.analyze_schneider_control_expert_v8
_PREVIOUS_CAPABILITY = _v8.schneider_capability_profile_v8

_KNOWN_SOURCES = {
    "STSource": "ST",
    "LDSource": "LD",
    "FBDSource": "FBD",
    "SFCSource": "SFC",
    "ILSource": "IL",
}
_EXECUTABLE_OWNERS = {"program", "FBProgram"}
_EXPECTED_SUFFIXES = {".xef", ".xsy", ".xst", ".xld", ".xbd", ".xsf", ".xil", ".xdd", ".xdb", ".xhw", ".xcm"}


@dataclass(frozen=True)
class SchneiderV9SupportRegion:
    id: str
    region_type: str
    language: str
    owner: str
    locator: str
    semantic_state: str
    reason: str
    source_evidence_id: str | None = None


@dataclass(frozen=True)
class SchneiderV9SupportContract:
    id: str
    regions: tuple[SchneiderV9SupportRegion, ...]
    full: int
    partial: int
    opaque: int
    protected: int
    by_language: tuple[tuple[str, int, int, int, int], ...]
    accounting_complete: bool
    missing_statement_ids: tuple[str, ...]
    duplicate_section_keys: tuple[str, ...]
    contract: str


@dataclass(frozen=True)
class SchneiderV9SourceAudit:
    source_files: int
    source_bytes: int
    deterministic_manifest_sha256: str
    line_endings: str
    unicode_present: bool
    suffix_counts: tuple[tuple[str, int], ...]
    full_xef_present: bool
    granular_present: bool
    dtd_versions: tuple[str, ...]
    products: tuple[str, ...]
    project_names: tuple[str, ...]
    metadata_consistent: bool
    unknown_executable_source_tags: tuple[str, ...]
    missing_source_sections: tuple[str, ...]


@dataclass(frozen=True)
class SchneiderV9Facts:
    support: SchneiderV9SupportContract
    source_audit: SchneiderV9SourceAudit
    external_export_corpus_status: str
    runtime_execution_status: str


def _facts(project) -> SchneiderV9Facts | None:
    return getattr(project, "_schneider_v9_closeout_facts", None)


def _line_ending(payload: bytes) -> set[str]:
    result: set[str] = set()
    if b"\r\n" in payload:
        result.add("CRLF")
    if b"\n" in payload.replace(b"\r\n", b""):
        result.add("LF")
    if b"\r" in payload.replace(b"\r\n", b""):
        result.add("CR")
    return result


def _source_manifest(path: Path):
    _root, files, total = _v1._preflight_sources(Path(path))
    digest = hashlib.sha256()
    endings: set[str] = set()
    unicode_present = False
    suffixes: Counter[str] = Counter()
    dtd_versions: set[str] = set()
    products: set[str] = set()
    project_names: set[str] = set()
    unknown_sources: set[str] = set()
    missing_sections: set[str] = set()

    for source, relative in files:
        payload = source.read_bytes()
        endings.update(_line_ending(payload))
        suffixes[source.suffix.lower()] += 1
        try:
            payload.decode("ascii")
        except UnicodeDecodeError:
            unicode_present = True
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())

        root = ET.parse(source).getroot()
        for element in root.iter():
            local = _v1._local_name(element.tag)
            if local == "fileHeader":
                value = (element.attrib.get("DTDVersion") or "").strip()
                if value:
                    dtd_versions.add(value)
                product = (element.attrib.get("product") or "").strip()
                if product:
                    products.add(product)
            elif local == "contentHeader":
                name = (element.attrib.get("name") or "").strip()
                if name:
                    project_names.add(name)

        for owner in root.iter():
            owner_local = _v1._local_name(owner.tag)
            if owner_local not in _EXECUTABLE_OWNERS:
                continue
            ident = next((item for item in owner.iter() if _v1._local_name(item.tag) == "identProgram"), None)
            owner_name = (
                (ident.attrib.get("name") if ident is not None else None)
                or owner.attrib.get("name")
                or owner.attrib.get("nameOfFBType")
                or "UNKNOWN"
            )
            source_nodes = [
                item
                for item in owner.iter()
                if _v1._local_name(item.tag).endswith("Source")
                and item is not owner
            ]
            if not source_nodes:
                missing_sections.add(f"{relative}:{owner_name}")
                continue
            for node in source_nodes:
                local = _v1._local_name(node.tag)
                if local not in _KNOWN_SOURCES:
                    unknown_sources.add(f"{relative}:{owner_name}:{local}")

    audit = SchneiderV9SourceAudit(
        source_files=len(files),
        source_bytes=total,
        deterministic_manifest_sha256=digest.hexdigest(),
        line_endings="+".join(sorted(endings)) or "NONE",
        unicode_present=unicode_present,
        suffix_counts=tuple(sorted(suffixes.items())),
        full_xef_present=bool(suffixes[".xef"]),
        granular_present=any(suffix != ".xef" for suffix in suffixes),
        dtd_versions=tuple(sorted(dtd_versions)),
        products=tuple(sorted(products)),
        project_names=tuple(sorted(project_names, key=str.casefold)),
        metadata_consistent=len(dtd_versions) <= 1 and len(products) <= 1,
        unknown_executable_source_tags=tuple(sorted(unknown_sources, key=str.casefold)),
        missing_source_sections=tuple(sorted(missing_sections, key=str.casefold)),
    )
    return files, audit


def _statement_regions(project) -> list[SchneiderV9SupportRegion]:
    result = []
    for statement in project.logic_statements:
        state = statement.semantic_state.value
        if state == "FULL":
            reason = "bounded_source_theorem"
        elif state == "PARTIAL":
            reason = "source_region_partially_modeled"
        else:
            reason = "source_region_opaque"
        result.append(
            SchneiderV9SupportRegion(
                f"SCHNEIDER-REG9:{statement.id}",
                "EXECUTABLE_STATEMENT",
                statement.language or "UNKNOWN",
                statement.source.program or statement.owner_name or "UNKNOWN",
                statement.locator or statement.source.locator,
                state,
                reason,
                statement.id,
            )
        )
    return result


def _source_section_regions(path: Path, project) -> tuple[list[SchneiderV9SupportRegion], tuple[str, ...]]:
    _root, files, _total = _v1._preflight_sources(Path(path))
    descriptors: list[tuple[str, str, str, str]] = []
    for source, relative in files:
        root = ET.parse(source).getroot()
        for program in root.iter():
            if _v1._local_name(program.tag) != "program":
                continue
            ident = next((item for item in program.iter() if _v1._local_name(item.tag) == "identProgram"), None)
            section = ((ident.attrib.get("name") if ident is not None else None) or "UNKNOWN").strip()
            sources = [item for item in program.iter() if _v1._local_name(item.tag) in _KNOWN_SOURCES]
            if not sources:
                descriptors.append((relative, section, "UNKNOWN", "NO_SOURCE"))
                continue
            if len(sources) > 1:
                descriptors.append((relative, section, "AMBIGUOUS", "MULTIPLE_SOURCE_ELEMENTS"))
                continue
            local = _v1._local_name(sources[0].tag)
            descriptors.append((relative, section, _KNOWN_SOURCES[local], local))

    key_counts = Counter((section.casefold(), language.casefold()) for _rel, section, language, _why in descriptors)
    duplicates = tuple(
        sorted(
            f"{section}|{language}"
            for (section, language), count in key_counts.items()
            if count > 1 and language not in {"unknown", "ambiguous"}
        )
    )
    regions: list[SchneiderV9SupportRegion] = []
    for relative, section, language, source_kind in descriptors:
        key = (section.casefold(), language.casefold())
        matching = [
            item
            for item in project.logic_statements
            if item.owner_name.casefold() == section.casefold()
            and item.language.casefold() == language.casefold()
        ]
        if source_kind == "NO_SOURCE":
            state, reason = "OPAQUE", "section_exposes_no_supported_executable_source"
        elif source_kind == "MULTIPLE_SOURCE_ELEMENTS":
            state, reason = "OPAQUE", "section_exposes_multiple_executable_source_elements"
        elif f"{key[0]}|{key[1]}" in duplicates:
            state, reason = "PARTIAL", "duplicate_section_identity_across_export_bundle"
        elif not matching:
            state, reason = "OPAQUE", "executable_source_section_has_no_normalized_statement"
        elif any(item.semantic_state is PLCSemanticState.OPAQUE for item in matching):
            state, reason = "OPAQUE", "section_contains_opaque_statement"
        elif any(item.semantic_state is PLCSemanticState.PARTIAL for item in matching):
            state, reason = "PARTIAL", "section_contains_partial_statement"
        else:
            state, reason = "FULL", "all_normalized_section_statements_are_full"
        digest = hashlib.sha1(f"{relative}:{section}:{language}:{source_kind}".encode()).hexdigest()[:16]
        regions.append(
            SchneiderV9SupportRegion(
                f"SCHNEIDER-SECTION9-{digest}",
                "SOURCE_SECTION",
                language,
                section,
                relative,
                state,
                reason,
                None,
            )
        )
    return regions, duplicates


def _dfb_and_call_regions(project) -> list[SchneiderV9SupportRegion]:
    facts = _v3._facts(project)
    if facts is None:
        return []
    result: list[SchneiderV9SupportRegion] = []
    local_by_type = defaultdict(list)
    for logic in facts.local_logic:
        local_by_type[logic.dfb_type.casefold()].append(logic)

    for block in facts.dfb_types:
        if block.source_protected:
            state = "PROTECTED"
            reason = "implementation_body_not_available_for_independent_static_proof"
            region_type = "PROTECTED_DFB"
        else:
            modeled = local_by_type.get(block.name.casefold(), [])
            if block.language == "ST" and modeled:
                state = "FULL"
                reason = "bounded_dfb_local_boolean_theorem_available"
            elif block.language in {"LD", "FBD", "SFC", "IL"}:
                state = "PARTIAL"
                reason = "dfb_type_identity_available_but_behavior_not_fully_modeled"
            else:
                state = "PARTIAL"
                reason = "dfb_source_available_without_bounded_local_theorem"
            region_type = "DFB_TYPE"
        result.append(
            SchneiderV9SupportRegion(
                f"SCHNEIDER-REG9:DFB:{block.id}",
                region_type,
                block.language or "UNKNOWN",
                block.name,
                block.source.locator,
                state,
                reason,
                block.id,
            )
        )

    for call in facts.calls:
        result.append(
            SchneiderV9SupportRegion(
                f"SCHNEIDER-REG9:CALL:{call.id}",
                "CALL_BINDING",
                "CALL",
                call.caller_name,
                call.source.locator,
                call.semantic_state.value,
                call.resolution,
                call.id,
            )
        )
    return result


def _support_regions(path: Path, project) -> SchneiderV9SupportContract:
    regions = _statement_regions(project)
    sections, duplicates = _source_section_regions(path, project)
    regions.extend(sections)
    regions.extend(_dfb_and_call_regions(project))

    statement_ids = {item.id for item in project.logic_statements}
    represented = {
        item.source_evidence_id
        for item in regions
        if item.region_type == "EXECUTABLE_STATEMENT" and item.source_evidence_id
    }
    missing = tuple(sorted(statement_ids - represented))
    accounting_complete = not missing and represented == statement_ids

    counts = Counter(item.semantic_state for item in regions)
    per_language: dict[str, Counter[str]] = defaultdict(Counter)
    for region in regions:
        per_language[region.language.upper()][region.semantic_state] += 1
    by_language = tuple(
        (
            language,
            values["FULL"],
            values["PARTIAL"],
            values["OPAQUE"],
            values["PROTECTED"],
        )
        for language, values in sorted(per_language.items())
    )
    if not regions:
        contract = "NO_EXECUTABLE_LOGIC"
    elif (
        accounting_complete
        and not duplicates
        and not counts["PARTIAL"]
        and not counts["OPAQUE"]
        and not counts["PROTECTED"]
    ):
        contract = "FULL"
    else:
        contract = "PARTIAL_FAIL_CLOSED"

    digest = hashlib.sha1(
        "|".join(
            f"{item.id}:{item.semantic_state}:{item.reason}"
            for item in sorted(regions, key=lambda value: value.id)
        ).encode()
    ).hexdigest()[:18]
    return SchneiderV9SupportContract(
        f"SCHNEIDER-SUPPORT9-{digest}",
        tuple(regions),
        counts["FULL"],
        counts["PARTIAL"],
        counts["OPAQUE"],
        counts["PROTECTED"],
        by_language,
        accounting_complete,
        missing,
        duplicates,
        contract,
    )


def schneider_capability_profile_v9(project) -> dict[str, object]:
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-schneider-control-expert-capability-v9"
    if facts is None:
        profile.update(
            {
                "support_contract": "NONE",
                "support_regions": 0,
                "coverage_accounting_complete": False,
                "commercial_closeout_status": "NOT_ANALYZED",
            }
        )
        return profile
    support = facts.support
    audit = facts.source_audit
    profile.update(
        {
            "support_contract": support.contract,
            "support_regions": len(support.regions),
            "support_full": support.full,
            "support_partial": support.partial,
            "support_opaque": support.opaque,
            "support_protected": support.protected,
            "coverage_accounting_complete": support.accounting_complete,
            "missing_support_statement_ids": list(support.missing_statement_ids),
            "duplicate_section_keys": list(support.duplicate_section_keys),
            "support_by_language": {
                language: {
                    "FULL": full,
                    "PARTIAL": partial,
                    "OPAQUE": opaque,
                    "PROTECTED": protected,
                }
                for language, full, partial, opaque, protected in support.by_language
            },
            "source_files": audit.source_files,
            "source_bytes": audit.source_bytes,
            "deterministic_manifest_sha256": audit.deterministic_manifest_sha256,
            "line_endings": audit.line_endings,
            "unicode_present": audit.unicode_present,
            "suffix_counts": dict(audit.suffix_counts),
            "full_xef_present": audit.full_xef_present,
            "granular_export_present": audit.granular_present,
            "dtd_versions": list(audit.dtd_versions),
            "products": list(audit.products),
            "project_names": list(audit.project_names),
            "export_metadata_consistent": audit.metadata_consistent,
            "unknown_executable_source_tags": list(audit.unknown_executable_source_tags),
            "missing_source_sections": list(audit.missing_source_sections),
            "real_control_expert_export_corpus": facts.external_export_corpus_status,
            "simulator_hil_real_plc_execution": facts.runtime_execution_status,
            "commercial_closeout_status": "IMPLEMENTATION_QUALIFIED_PENDING_EXTERNAL_EVIDENCE",
            "commercial_closeout_contract": (
                "Every normalized executable statement, discovered Control Expert source section, protected DFB boundary, "
                "and DFB call binding receives an explicit FULL/PARTIAL/OPAQUE/PROTECTED disposition. Source bundle identity "
                "is deterministic and metadata inconsistencies remain fail-closed. Real customer export corpus and runtime "
                "execution evidence remain separate external qualification gates."
            ),
        }
    )
    return profile


def analyze_schneider_control_expert_v9(path) -> PLCEngineeringResult:
    target = Path(path)
    _files, audit = _source_manifest(target)
    base = _PREVIOUS_ANALYZER(target)
    project = base.project
    support = _support_regions(target, project)
    facts = SchneiderV9Facts(
        support=support,
        source_audit=audit,
        external_export_corpus_status="PENDING_EXTERNAL_EVIDENCE",
        runtime_execution_status="NOT_EXECUTED",
    )
    setattr(project, "_schneider_v9_closeout_facts", facts)

    outcome = base.outcome
    closeout_gap = (
        not support.accounting_complete
        or support.partial
        or support.opaque
        or support.protected
        or support.duplicate_section_keys
        or not audit.metadata_consistent
        or audit.unknown_executable_source_tags
        or audit.missing_source_sections
    )
    if outcome is PLCOutcome.STATICALLY_VERIFIED and closeout_gap:
        outcome = PLCOutcome.PARTIALLY_VERIFIED
    if not support.regions:
        outcome = PLCOutcome.BLOCKED

    checks = [item for item in base.static_checks if not item.id.startswith("SCHNEIDER_V9_")]
    checks.extend(
        [
            StaticCheck(
                "SCHNEIDER_V9_SUPPORT_ACCOUNTING",
                StaticCheckStatus.PASS if support.accounting_complete else StaticCheckStatus.NOT_PROVEN,
                f"Support regions={len(support.regions)}, missing executable statements={len(support.missing_statement_ids)}.",
                (support.id, *support.missing_statement_ids),
            ),
            StaticCheck(
                "SCHNEIDER_V9_SUPPORT_CONTRACT",
                StaticCheckStatus.PASS if support.contract == "FULL" else StaticCheckStatus.WARN,
                (
                    f"Contract={support.contract}; FULL={support.full}, PARTIAL={support.partial}, "
                    f"OPAQUE={support.opaque}, PROTECTED={support.protected}, "
                    f"duplicate section identities={len(support.duplicate_section_keys)}."
                ),
                tuple(item.id for item in support.regions),
            ),
            StaticCheck(
                "SCHNEIDER_V9_EXPORT_METADATA",
                StaticCheckStatus.PASS
                if audit.metadata_consistent
                and not audit.unknown_executable_source_tags
                and not audit.missing_source_sections
                else StaticCheckStatus.NOT_PROVEN,
                (
                    f"DTD versions={list(audit.dtd_versions)}, products={list(audit.products)}, "
                    f"unknown executable source tags={len(audit.unknown_executable_source_tags)}, "
                    f"missing source sections={len(audit.missing_source_sections)}."
                ),
                tuple(
                    [
                        *audit.unknown_executable_source_tags,
                        *audit.missing_source_sections,
                    ]
                ),
            ),
            StaticCheck(
                "SCHNEIDER_V9_BUNDLE_MANIFEST",
                StaticCheckStatus.PASS,
                (
                    f"files={audit.source_files}, bytes={audit.source_bytes}, line_endings={audit.line_endings}, "
                    f"unicode={audit.unicode_present}, manifest_sha256={audit.deterministic_manifest_sha256}."
                ),
                (audit.deterministic_manifest_sha256,),
            ),
            StaticCheck(
                "SCHNEIDER_V9_EXTERNAL_EXPORT_CORPUS",
                StaticCheckStatus.NOT_PROVEN,
                (
                    "Commercial external corpus gate remains pending: qualified real Control Expert M340/M580/Unity "
                    "exports are not embedded in this repository result."
                ),
            ),
            StaticCheck(
                "SCHNEIDER_V9_RUNTIME_EXECUTION",
                StaticCheckStatus.NOT_PROVEN,
                "Control Expert Simulator, HIL, and real Modicon PLC execution were not performed by this static closeout.",
            ),
        ]
    )
    limitations = list(base.limitations)
    limitations.extend(
        [
            "Schneider V9 commercial closeout is an offline engineering-source support/accounting contract. FULL means the imported source region is covered by the bounded static theorem; it is not proof of process physics, scan timing, field wiring, forces, downloaded-image equivalence, SIL, or PL.",
            "Real Control Expert export-corpus qualification remains an external evidence gate until representative M340/M580/legacy Unity and mixed-language customer exports are imported and qualified.",
            "Control Expert Simulator, HIL, and real Modicon PLC execution remain engineer-controlled runtime evidence and are not executed by DevAgent.",
        ]
    )
    return replace(
        base,
        outcome=outcome,
        static_checks=checks,
        limitations=list(dict.fromkeys(limitations)),
    )


def _evidence(previous, engineering):
    items = list(previous(engineering))
    items = [
        replace(
            item,
            summary=item.summary.replace("Schneider V8", "Schneider V9").replace(
                "Schneider Control Expert V1 support contract",
                "Schneider Control Expert V9 support contract",
            ),
        )
        if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE"
        else item
        for item in items
    ]
    facts = _facts(engineering.project)
    if facts is None:
        return items
    existing = {item.id for item in items}
    support = facts.support
    if support.id not in existing:
        items.append(
            EvidenceItem(
                support.id,
                "SCHNEIDER_SUPPORT_CONTRACT_V9",
                (
                    f"{support.contract}: FULL={support.full}, PARTIAL={support.partial}, "
                    f"OPAQUE={support.opaque}, PROTECTED={support.protected}."
                ),
                engineering.project.metadata.source_path,
                engineering.project.metadata.source_sha256,
                {
                    "contract": support.contract,
                    "accounting_complete": support.accounting_complete,
                    "manifest_sha256": facts.source_audit.deterministic_manifest_sha256,
                    "duplicate_section_keys": list(support.duplicate_section_keys),
                },
            )
        )
    for region in support.regions:
        if region.id in existing:
            continue
        items.append(
            EvidenceItem(
                region.id,
                "SCHNEIDER_SUPPORT_REGION_V9",
                f"{region.owner}/{region.locator}: {region.semantic_state} ({region.reason})",
                payload={
                    "region_type": region.region_type,
                    "language": region.language,
                    "semantic_state": region.semantic_state,
                    "reason": region.reason,
                    "source_evidence_id": region.source_evidence_id,
                },
            )
        )
    return items


def _findings(previous, engineering, valid_evidence_ids):
    result = list(previous(engineering, valid_evidence_ids))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    support = facts.support
    result.append(
        EngineeringFinding(
            "ENG-SCHNEIDER-V9-COMMERCIAL-CLOSEOUT",
            "SCHNEIDER_COMMERCIAL_CLOSEOUT",
            "Schneider commercial engineering verification closeout",
            Severity.INFO if support.contract == "FULL" else Severity.HIGH,
            (
                "Engineering review, cause/effect, requirement verification, FAT planning, risks, regression, "
                f"evidence, and release-readiness reporting are backed by explicit Schneider Support Contract {support.contract}."
            ),
            (
                "Disposition every non-FULL source region and complete the external real-export/runtime evidence gates "
                "before customer production acceptance."
            ),
            (support.id,) if support.id in valid_evidence_ids else (),
        )
    )
    return result


def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    support = facts.support
    audit = facts.source_audit
    if not support.accounting_complete:
        result.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_SUPPORT_ACCOUNTING_V9", *support.missing_statement_ids),
                "SEMANTIC_COVERAGE",
                "Schneider support-region accounting is incomplete",
                Severity.CRITICAL,
                f"{len(support.missing_statement_ids)} executable statement(s) are missing from V9 support accounting.",
                "A source region could disappear from the customer report and create a false completeness claim.",
                "Block release until every executable statement has explicit FULL/PARTIAL/OPAQUE/PROTECTED disposition.",
                (support.id, *support.missing_statement_ids),
            )
        )
    if support.partial or support.opaque or support.protected:
        gaps = [
            item
            for item in support.regions
            if item.semantic_state in {"PARTIAL", "OPAQUE", "PROTECTED"}
        ]
        result.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_SUPPORT_GAPS_V9", engineering.project.metadata.source_sha256),
                "SEMANTIC_COVERAGE",
                "Schneider Support Contract contains non-FULL regions",
                Severity.HIGH,
                f"PARTIAL={support.partial}, OPAQUE={support.opaque}, PROTECTED={support.protected}.",
                "Static cause/effect, requirements, and release claims are incomplete across those exact source boundaries.",
                "Obtain deeper XML export or qualified FAT/Simulator/HIL/PLC evidence; never hide or auto-promote the gap.",
                tuple(item.id for item in gaps),
            )
        )
    if support.duplicate_section_keys or not audit.metadata_consistent:
        evidence = tuple([*support.duplicate_section_keys, *audit.dtd_versions, *audit.products])
        result.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_EXPORT_IDENTITY_V9", *evidence),
                "EXPORT_IDENTITY",
                "Schneider export bundle identity is inconsistent or ambiguous",
                Severity.HIGH,
                (
                    f"Duplicate section identities={len(support.duplicate_section_keys)}, "
                    f"DTD versions={list(audit.dtd_versions)}, products={list(audit.products)}."
                ),
                "Mixing unrelated or revision-mismatched granular exports can corrupt project-wide ownership and traceability.",
                "Re-export one coherent Control Expert project/revision and rerun V9 closeout.",
                evidence,
            )
        )
    if audit.unknown_executable_source_tags or audit.missing_source_sections:
        gaps = tuple([*audit.unknown_executable_source_tags, *audit.missing_source_sections])
        result.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_SOURCE_SURFACE_V9", *gaps),
                "SEMANTIC_COVERAGE",
                "Schneider export contains unrecognized or missing executable source surface",
                Severity.HIGH,
                (
                    f"Unknown source tags={len(audit.unknown_executable_source_tags)}, "
                    f"missing source sections={len(audit.missing_source_sections)}."
                ),
                "Executable behavior may exist outside the qualified V1-V8 parser surface.",
                "Keep the affected region OPAQUE and obtain a supported Control Expert XML export or engineer runtime evidence.",
                gaps,
            )
        )
    return result


def _render(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = schneider_capability_profile_v9(project)
    rows = [
        f"| {language} | {values['FULL']} | {values['PARTIAL']} | {values['OPAQUE']} | {values['PROTECTED']} |"
        for language, values in profile["support_by_language"].items()
    ]
    gaps = [
        f"- `{item.region_type}` `{item.owner}` / `{item.locator}` — **{item.semantic_state}**: {item.reason}"
        for item in facts.support.regions
        if item.semantic_state != "FULL"
    ]
    suffix_text = ", ".join(f"{key}:{value}" for key, value in profile["suffix_counts"].items()) or "none"
    section = (
        "### Schneider V9 Support Contract / Commercial Closeout\n\n"
        f"- Contract: **{profile['support_contract']}**\n"
        f"- Coverage accounting complete: **{'YES' if profile['coverage_accounting_complete'] else 'NO'}**\n"
        f"- Regions: **{profile['support_regions']}** "
        f"(FULL={profile['support_full']}, PARTIAL={profile['support_partial']}, "
        f"OPAQUE={profile['support_opaque']}, PROTECTED={profile['support_protected']})\n"
        f"- Export bundle: **{profile['source_files']} files / {profile['source_bytes']} bytes**; "
        f"suffixes: **{suffix_text}**\n"
        f"- Manifest SHA-256: `{profile['deterministic_manifest_sha256']}`\n"
        f"- Export metadata consistent: **{'YES' if profile['export_metadata_consistent'] else 'NO'}**\n"
        f"- Real Control Expert export corpus: **{profile['real_control_expert_export_corpus']}**\n"
        f"- Simulator / HIL / real PLC: **{profile['simulator_hil_real_plc_execution']}**\n\n"
        "| Language / Region | FULL | PARTIAL | OPAQUE | PROTECTED |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        + ("\n".join(rows) if rows else "| none | 0 | 0 | 0 | 0 |")
        + "\n\n#### Explicit unsupported / runtime-required regions\n\n"
        + ("\n".join(gaps) if gaps else "- None inside this imported source bundle.")
        + "\n\n> V9 FULL is static engineering-source coverage only. It is not Simulator/HIL/real PLC, field-wiring, scan-timing, SIL, or PL proof.\n\n"
    )
    marker = "### Schneider V8 Canonical Symbols / Types / I/O Identity"
    return base.replace(marker, section + marker, 1) if marker in base else base + "\n\n" + section


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _root
    from devagent.plc import schneider_integration_v1 as _integration
    from devagent.plc import schneider_report_install_v1 as _report

    previous_evidence = _integration._evidence_index
    previous_findings = _integration._findings
    previous_risks = _integration._detect_risks
    previous_render = _report._render

    _root.analyze_schneider_control_expert = analyze_schneider_control_expert_v9
    _root.schneider_capability_profile = schneider_capability_profile_v9
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v9
    _integration.schneider_capability_profile = schneider_capability_profile_v9

    _integration._evidence_index = lambda engineering: _evidence(previous_evidence, engineering)
    _integration._findings = lambda engineering, ids: _findings(previous_findings, engineering, ids)
    _integration._detect_risks = lambda e, v, x, f: _risks(previous_risks, e, v, x, f)
    _report._render = lambda project: _render(previous_render, project)
    _INSTALLED = True


__all__ = [
    "SchneiderV9Facts",
    "SchneiderV9SourceAudit",
    "SchneiderV9SupportContract",
    "SchneiderV9SupportRegion",
    "analyze_schneider_control_expert_v9",
    "schneider_capability_profile_v9",
    "install",
]
