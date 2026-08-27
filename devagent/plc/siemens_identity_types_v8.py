from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re

from devagent.plc.models import PLCDependencyEdge, PLCOutcome, PLCSemanticState, StaticCheck, StaticCheckStatus
from devagent.plc.production_models import RequirementStatus, RequirementVerification, RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc import siemens_recovery_v7 as _v7
from devagent.plc import siemens_tia_v1 as _v1

_INSTALLED = False
_PREVIOUS_ANALYZER = _v7.analyze_siemens_tia_v7
_PREVIOUS_CAPABILITY = _v7.siemens_capability_profile_v7
_PREVIOUS_VERIFY = None
_SIMPLE = {"BOOL","BOOLEAN","BYTE","WORD","DWORD","LWORD","SINT","USINT","INT","UINT","DINT","UDINT","LINT","ULINT","REAL","LREAL","TIME","LTIME"}
_ARRAY = re.compile(r"^\s*ARRAY\s*\[(?P<dims>[^\]]+)\]\s+OF\s+(?P<element>.+?)\s*$", re.I|re.S)
_ENUM = re.compile(r"TYPE\s+(?P<name>\"[^\"]+\"|[A-Za-z_]\w*)\s*:\s*\((?P<body>.*?)\)\s*;\s*END_TYPE\b", re.I|re.S)
_STRUCT = re.compile(r"TYPE\s+(?P<name>\"[^\"]+\"|[A-Za-z_]\w*).*?\bSTRUCT\b(?P<body>.*?)\bEND_STRUCT\b.*?\bEND_TYPE\b", re.I|re.S)
_MEMBER = re.compile(r'^\s*(?P<name>"[^"]+"|[A-Za-z_]\w*)\s*:\s*(?P<dtype>[^;]+);', re.I|re.M)
_MAX_DEPTH = 16
_MAX_MEMBERS = 4096

@dataclass(frozen=True)
class SiemensV8TypeIdentity:
    id: str
    name: str
    kind: str
    element_type: str | None = None
    dimensions: tuple[str, ...] = ()
    members: tuple[tuple[str, str], ...] = ()
    enum_literals: tuple[str, ...] = ()

@dataclass(frozen=True)
class SiemensV8SymbolIdentity:
    id: str
    scope: str
    display_path: str
    canonical_path: tuple[str, ...]
    data_type: str
    type_id: str | None
    origin: str
    constant: bool = False
    synthetic: bool = False

@dataclass(frozen=True)
class SiemensV8ReferenceBinding:
    id: str
    statement_id: str
    access: str
    raw_ref: str
    canonical_symbol_id: str | None
    canonical_display: str | None
    resolution: str
    semantic_state: PLCSemanticState

@dataclass(frozen=True)
class SiemensV8Facts:
    types: tuple[SiemensV8TypeIdentity, ...]
    symbols: tuple[SiemensV8SymbolIdentity, ...]
    bindings: tuple[SiemensV8ReferenceBinding, ...]
    whole_member_overlaps: tuple[tuple[str, str], ...]
    ambiguous_references: tuple[str, ...]
    unresolved_references: tuple[str, ...]

def _facts(project):
    return getattr(project, "_siemens_v8_identity_facts", None)

def _clean(value) -> str:
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:].strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text

def _split_ref(value: str) -> tuple[str, ...]:
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:].lstrip()
    out, buf, quoted, bracket = [], [], False, 0
    for char in text:
        if char == '"':
            quoted = not quoted
            continue
        if not quoted:
            bracket += char == "["
            bracket -= char == "]" and bracket > 0
            if char == "." and bracket == 0:
                part = _clean("".join(buf))
                if part:
                    out.append(part)
                buf = []
                continue
        buf.append(char)
    part = _clean("".join(buf))
    if part:
        out.append(part)
    return tuple(out)

def _type_name(value) -> str:
    text = _clean(value)
    return text.split(":=", 1)[0].strip() if ":=" in text else text

def _array(value):
    match = _ARRAY.match(_type_name(value))
    if not match:
        return None
    return tuple(x.strip() for x in match.group("dims").split(",") if x.strip()), _type_name(match.group("element"))

def _preflight(path: Path):
    root, files = _v1._supported_sources(path)
    total = 0
    for source, _relative in files:
        size = source.stat().st_size
        total += size
        if total > _v1._MAX_TOTAL_BYTES:
            raise _v1.SiemensInputError(
                f"Siemens export bundle exceeds {_v1._MAX_TOTAL_BYTES // (1024*1024)} MiB production limit"
            )
    return root, files

def _source_types(files):
    result = {}
    for source, _relative in files:
        if source.suffix.lower() not in {".scl",".udt",".db"}:
            continue
        text = source.read_text(encoding="utf-8-sig", errors="replace")
        for match in _ENUM.finditer(text):
            name = _clean(match.group("name"))
            literals = []
            for raw in match.group("body").split(","):
                literal = _clean(raw.split(":=",1)[0])
                if literal and literal.casefold() not in {x.casefold() for x in literals}:
                    literals.append(literal)
            if literals:
                result[name.casefold()] = SiemensV8TypeIdentity(
                    f"SIEMENS-TYPE8:ENUM:{name.casefold()}", name, "ENUM", enum_literals=tuple(literals)
                )
        for match in _STRUCT.finditer(text):
            name = _clean(match.group("name"))
            members = tuple(
                (_clean(item.group("name")), _type_name(item.group("dtype")))
                for item in _MEMBER.finditer(match.group("body"))
            )
            if members:
                result[name.casefold()] = SiemensV8TypeIdentity(
                    f"SIEMENS-TYPE8:STRUCT:{name.casefold()}", name, "STRUCT", members=members[:_MAX_MEMBERS]
                )
    return result

def _build_types(project, files):
    result = _source_types(files)
    for dtype in project.data_types:
        name = _clean(dtype.name)
        members = tuple((_clean(m.name), _type_name(m.data_type)) for m in dtype.members)
        result.setdefault(
            name.casefold(),
            SiemensV8TypeIdentity(f"SIEMENS-TYPE8:UDT:{name.casefold()}", name, "UDT", members=members[:_MAX_MEMBERS]),
        )
    referenced = [tag.data_type for tag in project.tags]
    referenced += [member_type for item in result.values() for _name, member_type in item.members]
    for raw in referenced:
        name = _type_name(raw)
        arr = _array(name)
        if arr:
            dims, element = arr
            key = name.casefold()
            result.setdefault(
                key,
                SiemensV8TypeIdentity(
                    f"SIEMENS-TYPE8:ARRAY:{hashlib.sha1(key.encode()).hexdigest()[:14]}",
                    name, "ARRAY", element_type=element, dimensions=dims,
                ),
            )
        elif name.upper() in _SIMPLE:
            result.setdefault(
                name.casefold(), SiemensV8TypeIdentity(f"SIEMENS-TYPE8:SIMPLE:{name.upper()}", name.upper(), "SIMPLE")
            )
    return tuple(sorted(result.values(), key=lambda x: (x.kind, x.name.casefold())))

def _symbol_id(scope, parts):
    key = f"{scope.casefold()}::" + ".".join(x.casefold() for x in parts)
    return f"SIEMENS-SYM8:{hashlib.sha1(key.encode()).hexdigest()[:18]}"

def _add_symbol(store, scope, parts, dtype, type_id, origin, constant=False, synthetic=False):
    if not parts:
        return
    key = (scope.casefold(), tuple(x.casefold() for x in parts))
    store.setdefault(
        key,
        SiemensV8SymbolIdentity(
            _symbol_id(scope, parts), scope, ".".join(parts), key[1], _type_name(dtype) or "UNKNOWN",
            type_id, origin, constant, synthetic,
        ),
    )

def _expand(store, type_map, scope, prefix, dtype, origin, depth=0, seen=()):
    if depth >= _MAX_DEPTH:
        return
    arr = _array(dtype)
    if arr:
        _dims, element = arr
        wildcard = prefix[:-1] + (prefix[-1] + "[*]",)
        info = type_map.get(element.casefold())
        _add_symbol(store, scope, wildcard, element, info.id if info else None, origin, synthetic=True)
        _expand(store, type_map, scope, wildcard, element, origin, depth+1, seen)
        return
    info = type_map.get(_type_name(dtype).casefold())
    if not info or info.kind not in {"UDT","STRUCT"} or info.name.casefold() in seen:
        return
    seen = (*seen, info.name.casefold())
    for name, member_type in info.members:
        child = (*prefix, name)
        child_info = type_map.get(_type_name(member_type).casefold())
        _add_symbol(store, scope, child, member_type, child_info.id if child_info else None, origin, synthetic=True)
        _expand(store, type_map, scope, child, member_type, origin, depth+1, seen)

def _build_symbols(project, types):
    type_map = {item.name.casefold(): item for item in types}
    store = {}
    for tag in project.tags:
        scope = str(tag.scope or "controller")
        parts = _split_ref(tag.name)
        dtype = _type_name(tag.data_type)
        info = type_map.get(dtype.casefold())
        _add_symbol(
            store, scope, parts, dtype, info.id if info else None, tag.id,
            constant=bool(tag.constant or str(tag.tag_type or "").upper() == "CONST"),
        )
        _expand(store, type_map, scope, parts, dtype, tag.id)
        if scope.casefold() == "controller" and len(parts) > 1:
            _add_symbol(store, "controller", (parts[0],), "UNKNOWN", None, f"DB_ROOT:{parts[0]}", synthetic=True)
    return tuple(sorted(store.values(), key=lambda x: (x.scope.casefold(), x.canonical_path)))

def _normalize_index(parts):
    return tuple(re.sub(r"\[[^\]]+\]", "[*]", part).casefold() for part in parts)

def _resolve(statement, raw_ref, symbols):
    parts = _split_ref(raw_ref)
    if not parts:
        return None, "EMPTY"
    exact = {(s.scope.casefold(), s.canonical_path): s for s in symbols}
    key = tuple(x.casefold() for x in parts)
    wildcard = _normalize_index(parts)
    block = statement.source.program or statement.owner_name or ""
    local_scope = f"program:{block}".casefold()
    def find(scope):
        return exact.get((scope,key)) or exact.get((scope,wildcard))
    if str(raw_ref).lstrip().startswith("#"):
        symbol = find(local_scope)
        return (symbol, "EXACT_LOCAL") if symbol else (None, "UNRESOLVED_LOCAL")
    symbol = find(local_scope)
    if symbol:
        return symbol, "LOCAL_SHADOW"
    symbol = find("controller")
    if symbol:
        return symbol, "EXACT_CONTROLLER"
    candidates = [
        s for s in symbols if s.scope.casefold() == "controller"
        and (s.canonical_path[:len(key)] == key or key[:len(s.canonical_path)] == s.canonical_path)
    ]
    if len(candidates) == 1:
        return candidates[0], "UNIQUE_CONTROLLER_PREFIX"
    if len(candidates) > 1:
        roots = {s.canonical_path[:1] for s in candidates}
        if len(key) == 1 and len(roots) == 1:
            root = exact.get(("controller", next(iter(roots))))
            if root:
                return root, "SYNTHETIC_DB_ROOT"
        return None, "AMBIGUOUS_PREFIX"
    return None, "UNRESOLVED"

def _indexed_refs(statement):
    pattern = re.compile(
        r'(?:"[^"]+"|[A-Za-z_]\w*)\[[^\]]+\](?:\.(?:"[^"]+"|[A-Za-z_]\w*)(?:\[[^\]]+\])?)*'
    )
    lhs = statement.text.split(":=",1)[0] if ":=" in statement.text else ""
    return tuple(
        ("WRITE" if match.group(0) in lhs else "READ", match.group(0))
        for match in pattern.finditer(statement.text)
    )

def _bindings(project, symbols):
    result, seen = [], set()
    for statement in project.logic_statements:
        refs = [*(("READ",r) for r in statement.reads), *(("WRITE",r) for r in statement.writes), *_indexed_refs(statement)]
        for access, raw in refs:
            key = (statement.id, access, str(raw))
            if key in seen:
                continue
            seen.add(key)
            symbol, resolution = _resolve(statement, str(raw), symbols)
            digest = hashlib.sha1(f"{statement.id}:{access}:{raw}:{resolution}".encode()).hexdigest()[:16]
            result.append(
                SiemensV8ReferenceBinding(
                    f"SIEMENS-BIND8-{digest}", statement.id, access, str(raw),
                    symbol.id if symbol else None,
                    f"{symbol.scope}::{symbol.display_path}" if symbol else None,
                    resolution,
                    PLCSemanticState.FULL if symbol else PLCSemanticState.PARTIAL,
                )
            )
    return tuple(result)

def _overlap(left, right):
    if left.scope.casefold() != right.scope.casefold():
        return False
    a, b = left.canonical_path, right.canonical_path
    for x, y in zip(a,b):
        x = re.sub(r"\[[^\]]+\]", "[*]", x)
        y = re.sub(r"\[[^\]]+\]", "[*]", y)
        if x != y:
            return False
    return True

def _writer_overlaps(bindings, symbols):
    by_id = {s.id:s for s in symbols}
    writers = defaultdict(set)
    for binding in bindings:
        if binding.access == "WRITE" and binding.canonical_symbol_id:
            writers[binding.canonical_symbol_id].add(binding.statement_id)
    ids, result = sorted(writers), []
    for i, left_id in enumerate(ids):
        for right_id in ids[i:]:
            if _overlap(by_id[left_id], by_id[right_id]) and len(writers[left_id] | writers[right_id]) > 1:
                pair = (left_id,right_id)
                if pair not in result:
                    result.append(pair)
    return tuple(result)

def siemens_capability_profile_v8(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-siemens-tia-capability-v8"
    if facts is None:
        profile.update({"canonical_symbols":0,"canonical_types":0,"identity_contract":"NONE"})
        return profile
    extended = getattr(project, "_siemens_v4_extended_facts", None)
    typed_comparisons = sum(bool(getattr(action,"comparison",None)) for action in getattr(extended,"actions",()))
    profile.update({
        "canonical_symbols": len(facts.symbols),
        "canonical_types": len(facts.types),
        "udt_types": sum(t.kind=="UDT" for t in facts.types),
        "struct_types": sum(t.kind=="STRUCT" for t in facts.types),
        "array_types": sum(t.kind=="ARRAY" for t in facts.types),
        "enum_types": sum(t.kind=="ENUM" for t in facts.types),
        "constant_symbols": sum(s.constant for s in facts.symbols),
        "reference_bindings": len(facts.bindings),
        "ambiguous_references": len(facts.ambiguous_references),
        "unresolved_references": len(facts.unresolved_references),
        "whole_member_writer_overlaps": len(facts.whole_member_overlaps),
        "typed_comparisons": typed_comparisons,
        "typed_comparison_types": ["BOOL","INT","DINT","REAL","TIME","ENUM"],
        "identity_contract": "COMPLETE" if not facts.ambiguous_references and not facts.unresolved_references else "PARTIAL_FAIL_CLOSED",
        "canonical_identity_contract": (
            "block-local shadowing then controller scope; quote-aware DB/UDT/STRUCT/ARRAY identity; "
            "whole/member overlap; unresolved/ambiguous identities fail closed"
        ),
    })
    return profile

def analyze_siemens_tia_v8(path):
    target = Path(path)
    _root, files = _preflight(target)
    base = _PREVIOUS_ANALYZER(target)
    project = base.project
    types = _build_types(project, files)
    symbols = _build_symbols(project, types)
    bindings = _bindings(project, symbols)
    overlaps = _writer_overlaps(bindings, symbols)
    facts = SiemensV8Facts(
        types, symbols, bindings, overlaps,
        tuple(b.id for b in bindings if b.resolution.startswith("AMBIGUOUS")),
        tuple(b.id for b in bindings if b.canonical_symbol_id is None and not b.resolution.startswith("AMBIGUOUS")),
    )
    setattr(project, "_siemens_v8_identity_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SIEMENS-TIA-EXPORT-V8")
    for binding in bindings:
        if binding.canonical_symbol_id:
            project_edge = PLCDependencyEdge(binding.statement_id, binding.canonical_symbol_id, f"CANONICAL_{binding.access}", binding.id)
            if project_edge not in base.graph.edges:
                base.graph.edges.append(project_edge)
    bad_full = {s.id for s in project.logic_statements if s.semantic_state is PLCSemanticState.FULL}
    identity_gap = any(b.statement_id in bad_full and b.canonical_symbol_id is None for b in bindings)
    outcome = base.outcome
    if outcome is PLCOutcome.STATICALLY_VERIFIED and (identity_gap or overlaps):
        outcome = PLCOutcome.PARTIALLY_VERIFIED
    checks = list(base.static_checks) + [
        StaticCheck(
            "SIEMENS_V8_CANONICAL_IDENTITY",
            StaticCheckStatus.PASS if not facts.ambiguous_references and not facts.unresolved_references else StaticCheckStatus.NOT_PROVEN,
            f"Canonical symbols={len(symbols)}, bindings={len(bindings)}, ambiguous={len(facts.ambiguous_references)}, unresolved={len(facts.unresolved_references)}.",
            tuple((*facts.ambiguous_references,*facts.unresolved_references)),
        ),
        StaticCheck(
            "SIEMENS_V8_WRITER_OVERLAP",
            StaticCheckStatus.NOT_PROVEN if overlaps else StaticCheckStatus.PASS,
            f"Whole-structure/member writer-overlap pairs={len(overlaps)}.",
            tuple(x for pair in overlaps for x in pair),
        ),
    ]
    limits = list(base.limitations) + [
        "Siemens V8 canonical identity/type analysis is fail-closed for unresolved, ambiguous, indirect, dynamically indexed, malformed, or recursively excessive shapes.",
        "ARRAY identity supports ownership/traceability; dynamic index runtime behavior is not promoted to static proof.",
    ]
    return replace(base, outcome=outcome, static_checks=checks, limitations=list(dict.fromkeys(limits)))

def _semantic_section(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    p = siemens_capability_profile_v8(project)
    section = (
        "### Siemens V8 Canonical Data / Type Identity\n\n"
        f"- Canonical symbols/members: **{p['canonical_symbols']}**\n"
        f"- UDT / STRUCT / ARRAY / ENUM: **{p['udt_types']} / {p['struct_types']} / {p['array_types']} / {p['enum_types']}**\n"
        f"- Bindings: **{p['reference_bindings']}**; ambiguous: **{p['ambiguous_references']}**; unresolved: **{p['unresolved_references']}**\n"
        f"- Whole/member writer overlaps: **{p['whole_member_writer_overlaps']}**\n"
        f"- Identity contract: **{p['identity_contract']}**\n"
        "- Typed comparison identity covers BOOL/INT/DINT/REAL/TIME and discovered ENUM types without guessing cross-scope symbols.\n\n"
    )
    marker = "### Siemens V7 Recovery / Reset / Restart Verification"
    return base.replace(marker, section+marker, 1) if marker in base else base+"\n\n"+section

def _evidence(previous, engineering):
    from devagent.plc.production_models import EvidenceItem
    items = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return items
    sha = engineering.project.metadata.source_sha256
    for item in facts.types:
        items.append(EvidenceItem(
            item.id, "SIEMENS_TYPE_IDENTITY_V8", f"{item.kind} {item.name}", source_sha256=sha,
            payload={"kind":item.kind,"element_type":item.element_type,"dimensions":list(item.dimensions),"members":[{"name":n,"data_type":t} for n,t in item.members],"enum_literals":list(item.enum_literals)}
        ))
    for item in facts.symbols:
        items.append(EvidenceItem(
            item.id, "SIEMENS_SYMBOL_IDENTITY_V8", f"{item.scope}::{item.display_path}: {item.data_type}", source_sha256=sha,
            payload={"scope":item.scope,"canonical_path":list(item.canonical_path),"data_type":item.data_type,"type_id":item.type_id,"constant":item.constant,"synthetic":item.synthetic,"origin":item.origin}
        ))
    for item in facts.bindings:
        items.append(EvidenceItem(
            item.id, "SIEMENS_REFERENCE_BINDING_V8", f"{item.access} {item.raw_ref} -> {item.canonical_display or item.resolution}",
            payload={"statement_id":item.statement_id,"canonical_symbol_id":item.canonical_symbol_id,"resolution":item.resolution,"semantic_state":item.semantic_state.value}
        ))
    return items

def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    gaps = (*facts.ambiguous_references,*facts.unresolved_references)
    if gaps:
        result.append(RiskFinding(
            stable_id("RISK","SIEMENS_IDENTITY_V8",engineering.project.metadata.source_sha256),
            "SYMBOL_IDENTITY","Siemens references are not all canonically resolved",Severity.HIGH,
            f"Ambiguous={len(facts.ambiguous_references)}, unresolved={len(facts.unresolved_references)}.",
            "Multiple-writer, cause/effect, and requirement scope could be wrong if identity were guessed.",
            "Correct/export symbol/type evidence or retain the affected region as PARTIAL/OPAQUE.",tuple(gaps)
        ))
    if facts.whole_member_overlaps:
        result.append(RiskFinding(
            stable_id("RISK","SIEMENS_WHOLE_MEMBER_V8",*("|".join(x) for x in facts.whole_member_overlaps)),
            "MULTIPLE_WRITERS","Whole-structure and member writers overlap",Severity.HIGH,
            f"{len(facts.whole_member_overlaps)} canonical overlap pair(s) found.",
            "A whole DB/UDT/ARRAY write can overwrite a separately written member.",
            "Disposition writer ownership/order and rerun requirement/FAT/regression checks.",
            tuple(x for pair in facts.whole_member_overlaps for x in pair)
        ))
    return result

def _scope_requirement(requirement, engineering, evidence, tests):
    assert _PREVIOUS_VERIFY is not None
    result = _PREVIOUS_VERIFY(requirement, engineering, evidence, tests)
    if str(engineering.project.metadata.vendor).casefold() != "siemens":
        return result
    if result.status not in {RequirementStatus.STATICALLY_VERIFIED,RequirementStatus.CONFLICT}:
        return result
    facts = _facts(engineering.project)
    if facts is None:
        return result
    scopes = defaultdict(set)
    for symbol in facts.symbols:
        if symbol.canonical_path:
            scopes[symbol.canonical_path[-1]].add(symbol.scope.casefold())
    ambiguous = [
        tag for tag in result.matched_tags
        if "." not in tag and "::" not in tag and len(scopes.get(_clean(tag).casefold(),())) > 1
    ]
    if not ambiguous:
        return result
    return RequirementVerification(
        result.requirement_id, RequirementStatus.TRACEABLE_NOT_PROVEN,
        "Siemens V8 withheld the static verdict because unqualified matched symbol(s) exist in multiple canonical scopes: "+", ".join(ambiguous)+".",
        result.evidence_ids,result.matched_tags,result.linked_test_ids,result.confidence,result.ai_assisted,
    )

def install():
    global _INSTALLED, _PREVIOUS_VERIFY
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import production as _production
    from devagent.plc import production_verification as _verification
    from devagent.plc import siemens_integration_v1 as _integration
    previous_section = _integration._siemens_semantic_section
    previous_evidence = _integration._siemens_evidence_index
    previous_risks = _integration._siemens_detect_risks
    _PREVIOUS_VERIFY = _verification.verify_requirement
    _verification.verify_requirement = _scope_requirement
    _production.verify_requirement = _scope_requirement
    _v1.analyze_siemens_tia = analyze_siemens_tia_v8
    _v1.siemens_capability_profile = siemens_capability_profile_v8
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v8
    _integration.siemens_capability_profile = siemens_capability_profile_v8
    _integration._siemens_semantic_section = lambda project: _semantic_section(previous_section, project)
    _integration._siemens_evidence_index = lambda engineering: _evidence(previous_evidence, engineering)
    _integration._siemens_detect_risks = lambda e,v,x,f: _risks(previous_risks,e,v,x,f)
    _INSTALLED = True

__all__ = ["SiemensV8TypeIdentity","SiemensV8SymbolIdentity","SiemensV8ReferenceBinding","SiemensV8Facts","analyze_siemens_tia_v8","siemens_capability_profile_v8","install"]
