from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path

from devagent.plc.models import PLCOutcome, StaticCheck, StaticCheckStatus
from devagent.plc.production_models import EngineeringFinding, RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc import siemens_call_graph_v3 as _v3
from devagent.plc import siemens_identity_types_v8 as _v8
from devagent.plc import siemens_tia_v1 as _v1

_INSTALLED = False
_PREVIOUS_ANALYZER = _v8.analyze_siemens_tia_v8
_PREVIOUS_CAPABILITY = _v8.siemens_capability_profile_v8

@dataclass(frozen=True)
class SiemensV9SupportRegion:
    id: str
    region_type: str
    language: str
    owner: str
    locator: str
    semantic_state: str
    reason: str
    source_evidence_id: str | None = None

@dataclass(frozen=True)
class SiemensV9SupportContract:
    id: str
    regions: tuple[SiemensV9SupportRegion, ...]
    full: int
    partial: int
    opaque: int
    protected: int
    by_language: tuple[tuple[str, int, int, int, int], ...]
    accounting_complete: bool
    missing_statement_ids: tuple[str, ...]
    contract: str

@dataclass(frozen=True)
class SiemensV9Facts:
    support: SiemensV9SupportContract
    export_variant: str
    line_endings: str
    unicode_present: bool
    source_files: int
    source_bytes: int
    deterministic_manifest_sha256: str

def _facts(project):
    return getattr(project, "_siemens_v9_closeout_facts", None)

def _support_regions(project):
    regions = []
    for statement in project.logic_statements:
        state = statement.semantic_state.value
        reason = "bounded_source_theorem" if state == "FULL" else "source_region_partially_modeled" if state == "PARTIAL" else "source_region_opaque"
        regions.append(SiemensV9SupportRegion(
            f"SIEMENS-REG9:{statement.id}", "EXECUTABLE_STATEMENT", statement.language or "UNKNOWN",
            statement.source.program or statement.owner_name or "UNKNOWN", statement.locator or statement.source.locator,
            state, reason, statement.id,
        ))
    for routine in project.routines:
        if not routine.source_protected:
            continue
        owner = routine.program or routine.name
        if any(r.owner.casefold() == owner.casefold() and r.semantic_state == "OPAQUE" for r in regions):
            continue
        regions.append(SiemensV9SupportRegion(
            f"SIEMENS-REG9:PROTECTED:{routine.id}", "PROTECTED_BLOCK", routine.language or "UNKNOWN",
            owner, routine.name, "PROTECTED", "implementation_body_not_available_for_independent_proof", routine.id,
        ))
    v3facts = _v3._facts(project)
    if v3facts is not None:
        for call in v3facts.calls:
            regions.append(SiemensV9SupportRegion(
                f"SIEMENS-REG9:CALL:{call.id}", "CALL_BINDING", "CALL", call.caller_block,
                call.source.locator, call.semantic_state.value, call.resolution, call.id,
            ))
    statement_ids = {s.id for s in project.logic_statements}
    represented = {r.source_evidence_id for r in regions if r.region_type == "EXECUTABLE_STATEMENT"}
    missing = tuple(sorted(statement_ids - represented))
    complete = not missing and len(represented) == len(statement_ids)
    counts = Counter(r.semantic_state for r in regions)
    per_language = defaultdict(Counter)
    for region in regions:
        per_language[region.language.upper()][region.semantic_state] += 1
    by_language = tuple(
        (language, values["FULL"], values["PARTIAL"], values["OPAQUE"], values["PROTECTED"])
        for language, values in sorted(per_language.items())
    )
    contract = (
        "FULL" if complete and not counts["PARTIAL"] and not counts["OPAQUE"] and not counts["PROTECTED"]
        else "PARTIAL_FAIL_CLOSED" if regions else "NO_EXECUTABLE_LOGIC"
    )
    digest = hashlib.sha1("|".join(f"{r.id}:{r.semantic_state}:{r.reason}" for r in regions).encode()).hexdigest()[:18]
    return SiemensV9SupportContract(
        f"SIEMENS-SUPPORT9-{digest}", tuple(regions), counts["FULL"], counts["PARTIAL"], counts["OPAQUE"],
        counts["PROTECTED"], by_language, complete, missing, contract,
    )

def _source_manifest(path: Path):
    _root, files = _v8._preflight(path)
    digest = hashlib.sha256()
    total, endings, unicode_present = 0, set(), False
    for source, relative in files:
        payload = source.read_bytes()
        total += len(payload)
        endings.add("CRLF") if b"\r\n" in payload else None
        endings.add("LF") if b"\n" in payload.replace(b"\r\n", b"") else None
        try:
            payload.decode("ascii")
        except UnicodeDecodeError:
            unicode_present = True
        digest.update(relative.encode("utf-8", errors="surrogatepass")); digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii")); digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return len(files), total, digest.hexdigest(), "+".join(sorted(endings)) or "NONE", unicode_present

def _export_variant(project):
    text = " ".join([project.metadata.controller_name or "", project.metadata.processor_type or "", *project.warnings[:32]]).casefold()
    for version in ("v20","v19","v18","v17"):
        if version in text:
            return version.upper()
    return "UNDECLARED"

def siemens_capability_profile_v9(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-siemens-tia-capability-v9"
    if facts is None:
        profile.update({"support_contract":"NONE","support_regions":0,"coverage_accounting_complete":False})
        return profile
    support = facts.support
    profile.update({
        "support_contract": support.contract,
        "support_regions": len(support.regions),
        "support_full": support.full,
        "support_partial": support.partial,
        "support_opaque": support.opaque,
        "support_protected": support.protected,
        "coverage_accounting_complete": support.accounting_complete,
        "missing_support_statement_ids": list(support.missing_statement_ids),
        "support_by_language": {
            language:{"FULL":full,"PARTIAL":partial,"OPAQUE":opaque,"PROTECTED":protected}
            for language,full,partial,opaque,protected in support.by_language
        },
        "export_variant": facts.export_variant,
        "line_endings": facts.line_endings,
        "unicode_present": facts.unicode_present,
        "source_files": facts.source_files,
        "source_bytes": facts.source_bytes,
        "deterministic_manifest_sha256": facts.deterministic_manifest_sha256,
        "commercial_closeout_contract": (
            "Every imported executable statement, protected implementation boundary, and discovered call binding is represented. "
            "Unsupported regions remain visible as PARTIAL/OPAQUE/PROTECTED and cannot disappear from reporting."
        ),
    })
    return profile

def analyze_siemens_tia_v9(path):
    target = Path(path)
    files, bytes_total, manifest, endings, unicode_present = _source_manifest(target)
    base = _PREVIOUS_ANALYZER(target)
    project = base.project
    support = _support_regions(project)
    facts = SiemensV9Facts(support, _export_variant(project), endings, unicode_present, files, bytes_total, manifest)
    setattr(project, "_siemens_v9_closeout_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SIEMENS-TIA-EXPORT-V9")
    outcome = base.outcome
    if outcome is PLCOutcome.STATICALLY_VERIFIED and (
        not support.accounting_complete or support.partial or support.opaque or support.protected
    ):
        outcome = PLCOutcome.PARTIALLY_VERIFIED
    if not support.regions:
        outcome = PLCOutcome.BLOCKED
    checks = list(base.static_checks) + [
        StaticCheck(
            "SIEMENS_V9_SUPPORT_ACCOUNTING",
            StaticCheckStatus.PASS if support.accounting_complete else StaticCheckStatus.NOT_PROVEN,
            f"Support regions={len(support.regions)}, missing executable statements={len(support.missing_statement_ids)}.",
            (support.id,*support.missing_statement_ids),
        ),
        StaticCheck(
            "SIEMENS_V9_SUPPORT_CONTRACT",
            StaticCheckStatus.PASS if support.contract == "FULL" else StaticCheckStatus.WARN,
            f"Contract={support.contract}; FULL={support.full}, PARTIAL={support.partial}, OPAQUE={support.opaque}, PROTECTED={support.protected}.",
            tuple(r.id for r in support.regions),
        ),
        StaticCheck(
            "SIEMENS_V9_BUNDLE_MANIFEST", StaticCheckStatus.PASS,
            f"files={files}, bytes={bytes_total}, line_endings={endings}, unicode={unicode_present}, manifest_sha256={manifest}.",
            (manifest,),
        ),
    ]
    limits = list(base.limitations) + [
        "Siemens V9 commercial closeout is offline engineering-source verification. PARTIAL/OPAQUE/PROTECTED/runtime-dependent regions require explicit engineer evidence; DevAgent does not execute PLCSIM, HIL, a real PLC, or process physics."
    ]
    return replace(base, outcome=outcome, static_checks=checks, limitations=list(dict.fromkeys(limits)))

def _semantic_section(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    p = siemens_capability_profile_v9(project)
    rows = [
        f"| {language} | {values['FULL']} | {values['PARTIAL']} | {values['OPAQUE']} | {values['PROTECTED']} |"
        for language, values in p["support_by_language"].items()
    ]
    gaps = [
        f"- `{r.region_type}` `{r.owner}` / `{r.locator}` — **{r.semantic_state}**: {r.reason}"
        for r in facts.support.regions if r.semantic_state != "FULL"
    ]
    section = (
        "### Siemens V9 Support Contract / Commercial Closeout\n\n"
        f"- Contract: **{p['support_contract']}**\n"
        f"- Coverage accounting complete: **{'YES' if p['coverage_accounting_complete'] else 'NO'}**\n"
        f"- Regions: **{p['support_regions']}** (FULL={p['support_full']}, PARTIAL={p['support_partial']}, OPAQUE={p['support_opaque']}, PROTECTED={p['support_protected']})\n"
        f"- Export variant declared/detected: **{p['export_variant']}**\n"
        f"- Bundle: **{p['source_files']} files / {p['source_bytes']} bytes**; manifest SHA-256: `{p['deterministic_manifest_sha256']}`\n\n"
        "| Language / Region | FULL | PARTIAL | OPAQUE | PROTECTED |\n| --- | ---: | ---: | ---: | ---: |\n"
        + ("\n".join(rows) if rows else "| none | 0 | 0 | 0 | 0 |")
        + "\n\n#### Explicit unsupported / runtime-required regions\n\n"
        + ("\n".join(gaps) if gaps else "- None. All imported regions are FULL under the declared bounded static theorem.")
        + "\n\n> No unsupported region is omitted. FULL means supported static engineering-source semantics, not PLCSIM/HIL/physical-process proof.\n\n"
    )
    marker = "### Siemens V8 Canonical Data / Type Identity"
    return base.replace(marker, section+marker, 1) if marker in base else base+"\n\n"+section

def _evidence(previous, engineering):
    from devagent.plc.production_models import EvidenceItem
    items = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return items
    support = facts.support
    items.append(EvidenceItem(
        support.id, "SIEMENS_SUPPORT_CONTRACT_V9",
        f"{support.contract}: FULL={support.full}, PARTIAL={support.partial}, OPAQUE={support.opaque}, PROTECTED={support.protected}.",
        engineering.project.metadata.source_path, engineering.project.metadata.source_sha256,
        {"contract":support.contract,"accounting_complete":support.accounting_complete,"manifest_sha256":facts.deterministic_manifest_sha256}
    ))
    for region in support.regions:
        items.append(EvidenceItem(
            region.id, "SIEMENS_SUPPORT_REGION_V9",
            f"{region.owner}/{region.locator}: {region.semantic_state} ({region.reason})",
            payload={"region_type":region.region_type,"language":region.language,"semantic_state":region.semantic_state,"reason":region.reason,"source_evidence_id":region.source_evidence_id}
        ))
    return items

def _findings(previous, engineering, valid_evidence_ids):
    result = list(previous(engineering, valid_evidence_ids))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    support = facts.support
    result.append(EngineeringFinding(
        "ENG-SIEMENS-V9-COMMERCIAL-CLOSEOUT", "SIEMENS_COMMERCIAL_CLOSEOUT",
        "Siemens commercial engineering verification closeout",
        Severity.INFO if support.contract == "FULL" else Severity.HIGH,
        "Engineering Analysis → Cause/Effect → Risks → Optimization → Requirement Verification → FAT Plan → Regression → Release Readiness → Professional Report is backed by an explicit Siemens Support Contract "+support.contract+".",
        "Disposition every non-FULL support region with deeper source export or qualified engineer runtime evidence before customer release acceptance.",
        (support.id,) if support.id in valid_evidence_ids else (),
    ))
    return result

def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    support = facts.support
    if not support.accounting_complete:
        result.append(RiskFinding(
            stable_id("RISK","SIEMENS_SUPPORT_ACCOUNTING_V9",*support.missing_statement_ids),
            "SEMANTIC_COVERAGE","Siemens support-region accounting is incomplete",Severity.CRITICAL,
            f"{len(support.missing_statement_ids)} executable statement(s) are missing from V9 support accounting.",
            "An executable region could disappear from the customer report and create a false completeness claim.",
            "Block release until every executable statement has FULL/PARTIAL/OPAQUE/PROTECTED disposition.",
            (support.id,*support.missing_statement_ids),
        ))
    if support.partial or support.opaque or support.protected:
        gaps = [r for r in support.regions if r.semantic_state in {"PARTIAL","OPAQUE","PROTECTED"}]
        result.append(RiskFinding(
            stable_id("RISK","SIEMENS_SUPPORT_GAPS_V9",engineering.project.metadata.source_sha256),
            "SEMANTIC_COVERAGE","Siemens Support Contract contains non-FULL regions",Severity.HIGH,
            f"PARTIAL={support.partial}, OPAQUE={support.opaque}, PROTECTED={support.protected}.",
            "Static cause/effect, requirement, and release claims are incomplete across those exact source boundaries.",
            "Obtain deeper export or qualified FAT/PLCSIM/HIL/PLC evidence; do not hide or auto-promote them.",
            tuple(r.id for r in gaps),
        ))
    return result

def install():
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_integration_v1 as _integration
    previous_section = _integration._siemens_semantic_section
    previous_evidence = _integration._siemens_evidence_index
    previous_findings = _integration._siemens_findings
    previous_risks = _integration._siemens_detect_risks
    _v1.analyze_siemens_tia = analyze_siemens_tia_v9
    _v1.siemens_capability_profile = siemens_capability_profile_v9
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v9
    _integration.siemens_capability_profile = siemens_capability_profile_v9
    _integration._siemens_semantic_section = lambda project: _semantic_section(previous_section, project)
    _integration._siemens_evidence_index = lambda engineering: _evidence(previous_evidence, engineering)
    _integration._siemens_findings = lambda engineering, ids: _findings(previous_findings, engineering, ids)
    _integration._siemens_detect_risks = lambda e,v,x,f: _risks(previous_risks,e,v,x,f)
    _INSTALLED = True

__all__ = ["SiemensV9SupportRegion","SiemensV9SupportContract","SiemensV9Facts","analyze_siemens_tia_v9","siemens_capability_profile_v9","install"]
