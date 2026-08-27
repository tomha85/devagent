from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from devagent.plc.analysis import build_dependency_graph
from devagent.plc.fat_procedure_v12 import enrich_fat_procedures
from devagent.plc.models import (
    CanonicalPLCProject,
    FATTestCase,
    PLCBooleanTerm,
    PLCDataType,
    PLCDataTypeMember,
    PLCEngineeringResult,
    PLCLogicPath,
    PLCLogicStatement,
    PLCOutcome,
    PLCOutputLogic,
    PLCProgram,
    PLCRoutine,
    PLCProjectMetadata,
    PLCSourceRef,
    PLCSemanticState,
    PLCTag,
    PLCTask,
    StaticCheck,
    StaticCheckStatus,
)


class SiemensInputError(ValueError):
    pass


_SUPPORTED_SUFFIXES = {".scl", ".db", ".udt", ".xml", ".stl", ".awl"}
_PROPRIETARY_SUFFIX_PREFIXES = (".ap", ".zap")
_MAX_FILES = 5000
_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_BLOCK_START = re.compile(
    r'^\s*(ORGANIZATION_BLOCK|FUNCTION_BLOCK|FUNCTION|DATA_BLOCK|TYPE)\s+(?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE,
)
_END_BY_KIND = {
    "ORGANIZATION_BLOCK": "END_ORGANIZATION_BLOCK",
    "FUNCTION_BLOCK": "END_FUNCTION_BLOCK",
    "FUNCTION": "END_FUNCTION",
    "DATA_BLOCK": "END_DATA_BLOCK",
    "TYPE": "END_TYPE",
}
_VAR_START = re.compile(r"^\s*(VAR(?:_INPUT|_OUTPUT|_IN_OUT|_TEMP|_STAT)?|CONST)\b", re.IGNORECASE)
_DECLARATION = re.compile(
    r'^\s*(?P<names>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*))*)\s*:\s*(?P<dtype>[^;:=]+)',
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(r"^\s*(?P<lhs>[^:;]+?)\s*:=\s*(?P<rhs>.+?)\s*;?\s*$", re.DOTALL)
_CONTROL_OPEN = re.compile(r"^\s*(IF|ELSIF|CASE|FOR|WHILE|REPEAT)\b", re.IGNORECASE)
_CONTROL_CLOSE = re.compile(r"^\s*(END_IF|END_CASE|END_FOR|END_WHILE|UNTIL)\b", re.IGNORECASE)
_ELSE = re.compile(r"^\s*ELSE\b", re.IGNORECASE)
_CALL_START = re.compile(r'^\s*(?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\s*\(', re.IGNORECASE)
_REF_TOKEN = re.compile(
    r'(?:(?:"[^"]+")|(?:[A-Za-z_][A-Za-z0-9_]*))(?:\.(?:(?:"[^"]+")|(?:[A-Za-z_][A-Za-z0-9_]*)))*'
)
_BOOL_TOKEN = re.compile(
    r'\s*(\(|\)|\bAND\b|\bOR\b|\bNOT\b|\bTRUE\b|\bFALSE\b|(?:(?:"[^"]+")|(?:[A-Za-z_][A-Za-z0-9_]*))(?:\.(?:(?:"[^"]+")|(?:[A-Za-z_][A-Za-z0-9_]*)))*)',
    re.IGNORECASE,
)
_KEYWORDS = {
    "AND", "OR", "NOT", "TRUE", "FALSE", "IF", "THEN", "ELSIF", "ELSE", "END_IF",
    "CASE", "OF", "END_CASE", "FOR", "TO", "BY", "DO", "END_FOR", "WHILE", "END_WHILE",
    "REPEAT", "UNTIL", "END_REPEAT", "RETURN", "BEGIN", "VAR", "END_VAR",
}


@dataclass(frozen=True)
class _SourceBlock:
    kind: str
    name: str
    source_path: Path
    relative_path: str
    start_line: int
    lines: tuple[str, ...]


@dataclass
class _BuildState:
    tags: list[PLCTag]
    data_types: list[PLCDataType]
    tasks: list[PLCTask]
    programs: list[PLCProgram]
    routines: list[PLCRoutine]
    statements: list[PLCLogicStatement]
    output_logic: list[PLCOutputLogic]
    warnings: list[str]
    source_block_names: set[str]
    protected_blocks: int = 0
    xml_networks: int = 0
    xml_networks_withheld: int = 0
    scl_source_files: int = 0
    xml_files: int = 0
    structural_source_files: int = 0


def _clean_name(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _normal_ref(value: str) -> str:
    parts = [_clean_name(part) for part in str(value).strip().split(".")]
    return ".".join(part for part in parts if part)


def _local_name(tag: str) -> str:
    return str(tag).split("}", 1)[-1]


def _child_text(parent, name: str) -> str | None:
    for child in parent.iter():
        if _local_name(child.tag).casefold() == name.casefold():
            text = (child.text or "").strip()
            if text:
                return text
    return None


def _supported_sources(path: Path) -> tuple[Path, list[tuple[Path, str]]]:
    target = path.expanduser().resolve(strict=True)
    lower_suffix = target.suffix.lower()
    if target.is_file() and lower_suffix.startswith(_PROPRIETARY_SUFFIX_PREFIXES):
        raise SiemensInputError(
            "TIA Portal .ap*/.zap* project archives are proprietary project containers. Export PLC blocks/tag tables with TIA Portal Openness/XML or GenerateSource (.scl/.db/.udt) and analyze that export bundle."
        )
    if target.is_file():
        if lower_suffix not in _SUPPORTED_SUFFIXES:
            raise SiemensInputError(
                f"Unsupported Siemens engineering artifact {target.name}; expected TIA Openness/XML or generated source (.scl/.db/.udt/.stl/.awl)."
            )
        return target.parent, [(target, target.name)]
    if not target.is_dir():
        raise SiemensInputError(f"Siemens project export path is not a file or directory: {target}")

    files: list[tuple[Path, str]] = []
    for item in sorted(target.rglob("*"), key=lambda value: str(value).casefold()):
        if not item.is_file() or item.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        relative = item.relative_to(target).as_posix()
        files.append((item, relative))
        if len(files) > _MAX_FILES:
            raise SiemensInputError(f"Siemens export bundle exceeds {_MAX_FILES} supported source files")
    if not files:
        raise SiemensInputError(
            "No supported TIA Portal export artifacts were found. Export blocks/tag tables to XML and/or GenerateSource files (.scl/.db/.udt)."
        )
    return target, files


def _bundle_sha(files: list[tuple[Path, str]]) -> str:
    if len(files) == 1:
        return hashlib.sha256(files[0][0].read_bytes()).hexdigest()
    digest = hashlib.sha256()
    total = 0
    for path, relative in files:
        payload = path.read_bytes()
        total += len(payload)
        if total > _MAX_TOTAL_BYTES:
            raise SiemensInputError(f"Siemens export bundle exceeds {_MAX_TOTAL_BYTES // (1024 * 1024)} MiB production limit")
        file_sha = hashlib.sha256(payload).hexdigest()
        digest.update(relative.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_sha.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def detect_siemens_input(path: Path) -> bool:
    target = path.expanduser().resolve(strict=False)
    if target.is_dir():
        try:
            _, files = _supported_sources(target)
        except (OSError, SiemensInputError):
            return False
        return any(item.suffix.lower() in _SUPPORTED_SUFFIXES for item, _ in files)
    suffix = target.suffix.lower()
    if suffix in {".scl", ".db", ".udt", ".stl", ".awl"}:
        return True
    if suffix != ".xml" or not target.exists():
        return False
    try:
        head = target.read_text(encoding="utf-8-sig", errors="replace")[:128_000]
    except OSError:
        return False
    markers = ("SW.Blocks.", "SW.Tags.PlcTag", "Siemens", "Openness", "Simatic")
    return any(marker.casefold() in head.casefold() for marker in markers)


def _strip_scl_comments(text: str) -> str:
    # Preserve newlines so source line references stay stable.
    text = re.sub(r"\(\*.*?\*\)", lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)), text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def _extract_source_blocks(path: Path, relative: str) -> list[_SourceBlock]:
    text = _strip_scl_comments(path.read_text(encoding="utf-8-sig", errors="replace"))
    lines = text.splitlines()
    result: list[_SourceBlock] = []
    index = 0
    while index < len(lines):
        match = _BLOCK_START.match(lines[index])
        if match is None:
            index += 1
            continue
        kind = match.group(1).upper()
        name = _clean_name(match.group("name"))
        end_marker = _END_BY_KIND[kind]
        end = index + 1
        while end < len(lines) and not re.match(rf"^\s*{re.escape(end_marker)}\b", lines[end], flags=re.IGNORECASE):
            end += 1
        if end >= len(lines):
            raise SiemensInputError(f"{relative}:{index + 1}: {kind} {name} has no {end_marker}")
        result.append(_SourceBlock(kind, name, path, relative, index + 1, tuple(lines[index : end + 1])))
        index = end + 1
    return result


def _dedupe_append_tag(tags: list[PLCTag], tag: PLCTag) -> None:
    key = (tag.scope.casefold(), tag.name.casefold())
    for current in tags:
        if (current.scope.casefold(), current.name.casefold()) == key:
            return
    tags.append(tag)


def _parse_declarations(block: _SourceBlock, state: _BuildState) -> dict[str, str]:
    types: dict[str, str] = {}
    current_section: str | None = None
    for raw in block.lines[1:]:
        stripped = raw.strip()
        if not stripped:
            continue
        if re.match(r"^BEGIN\b", stripped, flags=re.IGNORECASE):
            break
        var = _VAR_START.match(stripped)
        if var:
            current_section = var.group(1).upper()
            continue
        if re.match(r"^END_VAR\b", stripped, flags=re.IGNORECASE):
            current_section = None
            continue
        if current_section is None:
            continue
        declaration = _DECLARATION.match(stripped)
        if declaration is None:
            continue
        dtype = declaration.group("dtype").strip()
        names = [_clean_name(item.strip()) for item in declaration.group("names").split(",")]
        for name in names:
            if not name:
                continue
            types[name.casefold()] = dtype
            if block.kind == "TYPE":
                continue
            if block.kind == "DATA_BLOCK":
                tag_name = f"{block.name}.{name}"
                scope = "controller"
            else:
                tag_name = name
                scope = f"program:{block.name}"
            _dedupe_append_tag(
                state.tags,
                PLCTag(
                    id=f"SIEMENS-TAG:{scope}:{tag_name}",
                    name=tag_name,
                    scope=scope,
                    data_type=dtype,
                    tag_type=current_section,
                ),
            )
    if block.kind == "TYPE":
        members = tuple(
            PLCDataTypeMember(name=current.name.split(".")[-1], data_type=current.data_type)
            for current in state.tags
            if current.scope.casefold() == f"type:{block.name}".casefold()
        )
        # TYPE members are collected independently below because they are not runtime tags.
        type_members: list[PLCDataTypeMember] = []
        current_section = None
        for raw in block.lines[1:]:
            stripped = raw.strip()
            var = _VAR_START.match(stripped)
            if var:
                current_section = var.group(1).upper()
                continue
            if re.match(r"^END_VAR\b", stripped, flags=re.IGNORECASE):
                current_section = None
                continue
            if current_section is None:
                continue
            declaration = _DECLARATION.match(stripped)
            if declaration is None:
                continue
            dtype = declaration.group("dtype").strip()
            for raw_name in declaration.group("names").split(","):
                name = _clean_name(raw_name.strip())
                if name:
                    type_members.append(PLCDataTypeMember(name=name, data_type=dtype))
        if not any(item.name.casefold() == block.name.casefold() for item in state.data_types):
            state.data_types.append(PLCDataType(id=f"SIEMENS-TYPE:{block.name}", name=block.name, family="UDT", members=tuple(type_members)))
    return types


def _extract_refs(text: str) -> tuple[str, ...]:
    result: list[str] = []
    for match in _REF_TOKEN.finditer(text):
        token = _normal_ref(match.group(0))
        if not token or token.upper() in _KEYWORDS:
            continue
        if token not in result:
            result.append(token)
    return tuple(result)


def _lhs_ref(value: str) -> str | None:
    refs = _extract_refs(value)
    if len(refs) != 1:
        return None
    # Reject indexed/indirect addressing in V1 static assignment theorem.
    if "[" in value or "]" in value or "%" in value:
        return None
    return refs[0]


def _tokenize_bool(expr: str) -> list[str] | None:
    tokens: list[str] = []
    position = 0
    while position < len(expr):
        if expr[position:].strip() == "":
            break
        match = _BOOL_TOKEN.match(expr, position)
        if match is None:
            return None
        token = match.group(1)
        tokens.append(token)
        position = match.end()
    return tokens


def _parse_bool_ast(expr: str):
    tokens = _tokenize_bool(expr)
    if not tokens:
        return None
    position = 0

    def parse_primary():
        nonlocal position
        if position >= len(tokens):
            raise ValueError
        token = tokens[position]
        upper = token.upper()
        if token == "(":
            position += 1
            node = parse_or()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError
            position += 1
            return node
        if upper == "NOT":
            position += 1
            return ("NOT", parse_primary())
        position += 1
        if upper == "TRUE":
            return ("CONST", True)
        if upper == "FALSE":
            return ("CONST", False)
        if upper in _KEYWORDS or token == ")":
            raise ValueError
        return ("VAR", _normal_ref(token))

    def parse_and():
        nonlocal position
        node = parse_primary()
        while position < len(tokens) and tokens[position].upper() == "AND":
            position += 1
            node = ("AND", node, parse_primary())
        return node

    def parse_or():
        nonlocal position
        node = parse_and()
        while position < len(tokens) and tokens[position].upper() == "OR":
            position += 1
            node = ("OR", node, parse_and())
        return node

    try:
        root = parse_or()
    except ValueError:
        return None
    return root if position == len(tokens) else None


def _dnf(node, negated: bool = False) -> list[dict[str, bool]] | None:
    kind = node[0]
    if kind == "CONST":
        value = bool(node[1])
        value = not value if negated else value
        return [{}] if value else []
    if kind == "VAR":
        return [{str(node[1]): not negated}]
    if kind == "NOT":
        return _dnf(node[1], not negated)
    if kind in {"AND", "OR"}:
        effective = "OR" if (kind == "AND" and negated) else "AND" if (kind == "OR" and negated) else kind
        left = _dnf(node[1], negated)
        right = _dnf(node[2], negated)
        if left is None or right is None:
            return None
        if effective == "OR":
            combined = [*left, *right]
        else:
            combined = []
            for a in left:
                for b in right:
                    merged = dict(a)
                    conflict = False
                    for key, value in b.items():
                        if key in merged and merged[key] != value:
                            conflict = True
                            break
                        merged[key] = value
                    if not conflict:
                        combined.append(merged)
        unique: list[dict[str, bool]] = []
        seen: set[tuple[tuple[str, bool], ...]] = set()
        for item in combined:
            key = tuple(sorted(item.items(), key=lambda pair: pair[0].casefold()))
            if key not in seen:
                seen.add(key)
                unique.append(item)
        if len(unique) > 32 or any(len(item) > 16 for item in unique):
            return None
        return unique
    return None


def _looks_bool(expr: str, lhs: str, type_map: dict[str, str]) -> bool:
    dtype = type_map.get(lhs.casefold(), "").strip().upper()
    if dtype in {"BOOL", "BOOLEAN"}:
        return True
    return bool(re.search(r"\b(?:AND|OR|NOT|TRUE|FALSE)\b", expr, flags=re.IGNORECASE))


def _logical_statements(block: _SourceBlock) -> list[tuple[int, str]]:
    begin = None
    for index, raw in enumerate(block.lines):
        if re.match(r"^\s*BEGIN\b", raw, flags=re.IGNORECASE):
            begin = index + 1
            break
    if begin is None:
        return []
    result: list[tuple[int, str]] = []
    buffer: list[str] = []
    first_line = 0
    for offset in range(begin, len(block.lines) - 1):
        raw = block.lines[offset].strip()
        if not raw:
            continue
        if not buffer:
            first_line = block.start_line + offset
        if _CONTROL_OPEN.match(raw) or _CONTROL_CLOSE.match(raw) or _ELSE.match(raw):
            if buffer:
                result.append((first_line, " ".join(buffer)))
                buffer = []
            result.append((block.start_line + offset, raw))
            continue
        buffer.append(raw)
        if ";" in raw:
            result.append((first_line, " ".join(buffer)))
            buffer = []
    if buffer:
        result.append((first_line, " ".join(buffer)))
    return result


def _statement_id(block: _SourceBlock, line: int, text: str) -> str:
    digest = hashlib.sha1(f"{block.relative_path}:{block.name}:{line}:{text}".encode("utf-8")).hexdigest()[:14]
    return f"SIEMENS-SCL-{digest}"


def _output_logic_id(statement_id: str, output: str) -> str:
    digest = hashlib.sha1(f"{statement_id}:{output}".encode("utf-8")).hexdigest()[:14]
    return f"SIEMENS-BOOL-{digest}"


def _parse_block_logic(block: _SourceBlock, type_map: dict[str, str], state: _BuildState, artifact: str, controller: str) -> None:
    if block.kind not in {"ORGANIZATION_BLOCK", "FUNCTION_BLOCK", "FUNCTION"}:
        return
    control_depth = 0
    for line, text in _logical_statements(block):
        upper = text.strip().upper()
        if _CONTROL_CLOSE.match(text):
            control_depth = max(0, control_depth - 1)
            statement = PLCLogicStatement(
                _statement_id(block, line, text), "SCL", "program", block.name, block.name, f"Line {line}", text,
                (), (), (), PLCSemanticState.PARTIAL,
                PLCSourceRef(artifact, controller, program=block.name, routine=block.name, line=str(line)),
            )
            state.statements.append(statement)
            continue
        if _ELSE.match(text):
            statement = PLCLogicStatement(
                _statement_id(block, line, text), "SCL", "program", block.name, block.name, f"Line {line}", text,
                (), (), (), PLCSemanticState.PARTIAL,
                PLCSourceRef(artifact, controller, program=block.name, routine=block.name, line=str(line)),
            )
            state.statements.append(statement)
            continue
        control_line = _CONTROL_OPEN.match(text)
        if control_line:
            reads = _extract_refs(text)
            statement = PLCLogicStatement(
                _statement_id(block, line, text), "SCL", "program", block.name, block.name, f"Line {line}", text,
                reads, (), (), PLCSemanticState.PARTIAL,
                PLCSourceRef(artifact, controller, program=block.name, routine=block.name, line=str(line)),
            )
            state.statements.append(statement)
            control_depth += 1
            continue

        statement_id = _statement_id(block, line, text)
        source = PLCSourceRef(artifact, controller, program=block.name, routine=block.name, line=str(line))
        assignment = _ASSIGNMENT.match(text)
        if assignment is not None:
            lhs = _lhs_ref(assignment.group("lhs"))
            rhs = assignment.group("rhs").strip()
            reads = _extract_refs(rhs)
            writes = (lhs,) if lhs else ()
            semantic = PLCSemanticState.PARTIAL
            paths: list[dict[str, bool]] | None = None
            if lhs and control_depth == 0 and _looks_bool(rhs, lhs, type_map):
                ast = _parse_bool_ast(rhs)
                if ast is not None:
                    paths = _dnf(ast)
                    if paths is not None:
                        semantic = PLCSemanticState.FULL
            elif lhs and control_depth == 0:
                # Direct source/literal copy is a bounded local dataflow fact. More
                # complex arithmetic/call/type-conversion expressions stay PARTIAL.
                rhs_refs = _extract_refs(rhs)
                direct_ref = len(rhs_refs) == 1 and _normal_ref(rhs.strip()) == rhs_refs[0]
                literal = bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?|TRUE|FALSE", rhs, flags=re.IGNORECASE))
                if direct_ref or literal:
                    semantic = PLCSemanticState.FULL
            statement = PLCLogicStatement(
                statement_id, "SCL", "program", block.name, block.name, f"Line {line}", text,
                reads, writes, (), semantic, source,
            )
            state.statements.append(statement)
            if lhs and paths is not None:
                state.output_logic.append(
                    PLCOutputLogic(
                        id=_output_logic_id(statement_id, lhs),
                        output_tag=lhs,
                        instruction="ASSIGN_BOOL",
                        paths=tuple(
                            PLCLogicPath(tuple(PLCBooleanTerm(tag, required) for tag, required in sorted(path.items(), key=lambda item: item[0].casefold())))
                            for path in paths
                        ),
                        source=source,
                        language="SCL",
                        origin="SIEMENS_SCL_ASSIGNMENT",
                        semantic_state=PLCSemanticState.FULL,
                    )
                )
            continue

        calls: tuple[str, ...] = ()
        call = _CALL_START.match(text)
        if call is not None:
            calls = (_clean_name(call.group("name")),)
        state.statements.append(
            PLCLogicStatement(
                statement_id, "SCL", "program", block.name, block.name, f"Line {line}", text,
                _extract_refs(text), (), calls, PLCSemanticState.PARTIAL, source,
            )
        )


def _add_source_block(block: _SourceBlock, state: _BuildState, artifact: str, controller: str) -> None:
    state.source_block_names.add(block.name.casefold())
    type_map = _parse_declarations(block, state)
    if block.kind == "TYPE":
        return
    if block.kind == "DATA_BLOCK":
        return
    routine_type = "SCL"
    routine_id = f"SIEMENS-ROUTINE:{block.relative_path}:{block.kind}:{block.name}"
    state.routines.append(PLCRoutine(routine_id, block.name, block.name, routine_type, False))
    state.programs.append(
        PLCProgram(
            id=f"SIEMENS-PROGRAM:{block.relative_path}:{block.name}",
            name=block.name,
            tag_ids=tuple(tag.id for tag in state.tags if tag.scope.casefold() == f"program:{block.name}".casefold()),
            routine_ids=(routine_id,),
            main_routine_name=block.name if block.kind == "ORGANIZATION_BLOCK" else None,
        )
    )
    if block.kind == "ORGANIZATION_BLOCK":
        state.tasks.append(
            PLCTask(
                id=f"SIEMENS-OB:{block.relative_path}:{block.name}",
                name=block.name,
                task_type="ORGANIZATION_BLOCK",
                scheduled_programs=(block.name,),
            )
        )
    _parse_block_logic(block, type_map, state, artifact, controller)


def _protection_flag(block) -> bool:
    for child in block.iter():
        name = _local_name(child.tag).casefold()
        if "protect" not in name and "knowhow" not in name:
            continue
        text = (child.text or "").strip().casefold()
        if text in {"true", "yes", "1", "protected", "knowhowprotected"}:
            return True
    return False


def _interface_tags(block, block_name: str, state: _BuildState) -> None:
    for section in block.iter():
        if _local_name(section.tag).casefold() != "section":
            continue
        section_name = str(section.attrib.get("Name") or section.attrib.get("name") or "INTERFACE")
        for member in section.iter():
            if _local_name(member.tag).casefold() != "member":
                continue
            name = _clean_name(str(member.attrib.get("Name") or member.attrib.get("name") or ""))
            dtype = str(member.attrib.get("Datatype") or member.attrib.get("DataType") or member.attrib.get("datatype") or "UNKNOWN")
            if not name:
                continue
            _dedupe_append_tag(
                state.tags,
                PLCTag(
                    id=f"SIEMENS-TAG:program:{block_name}:{name}",
                    name=name,
                    scope=f"program:{block_name}",
                    data_type=dtype,
                    tag_type=section_name,
                ),
            )


def _parse_xml(path: Path, relative: str, state: _BuildState, artifact: str, controller: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SiemensInputError(f"Invalid Siemens Openness/XML export {relative}: {exc}") from exc
    state.xml_files += 1

    # PLC tag-table exports.
    for element in root.iter():
        if _local_name(element.tag) != "SW.Tags.PlcTag":
            continue
        name = _clean_name(_child_text(element, "Name") or "")
        dtype = _child_text(element, "DataTypeName") or _child_text(element, "DataType") or "UNKNOWN"
        address = _child_text(element, "LogicalAddress")
        if name:
            _dedupe_append_tag(
                state.tags,
                PLCTag(
                    id=f"SIEMENS-TAG:controller:{name}",
                    name=name,
                    scope="controller",
                    data_type=dtype,
                    tag_type="PLC_TAG",
                    description=f"TIA logical address {address}" if address else None,
                ),
            )

    block_kinds = {"SW.Blocks.OB", "SW.Blocks.FB", "SW.Blocks.FC", "SW.Blocks.DB"}
    for block in root.iter():
        kind_tag = _local_name(block.tag)
        if kind_tag not in block_kinds:
            continue
        kind = kind_tag.rsplit(".", 1)[-1]
        block_name = _clean_name(_child_text(block, "Name") or f"{kind}_{block.attrib.get('ID', 'unknown')}")
        language = (_child_text(block, "ProgrammingLanguage") or ("DB" if kind == "DB" else "UNKNOWN")).upper()
        protected = _protection_flag(block)
        if protected:
            state.protected_blocks += 1
        _interface_tags(block, block_name, state)
        if kind == "DB":
            continue
        if block_name.casefold() in state.source_block_names:
            # Generated SCL source is the preferred executable representation;
            # retain XML interface/tag information without double-counting logic.
            continue
        routine_id = f"SIEMENS-XML-ROUTINE:{relative}:{kind}:{block_name}"
        state.routines.append(PLCRoutine(routine_id, block_name, block_name, language, protected))
        state.programs.append(
            PLCProgram(
                id=f"SIEMENS-XML-PROGRAM:{relative}:{block_name}",
                name=block_name,
                tag_ids=tuple(tag.id for tag in state.tags if tag.scope.casefold() == f"program:{block_name}".casefold()),
                routine_ids=(routine_id,),
                main_routine_name=block_name if kind == "OB" else None,
            )
        )
        if kind == "OB":
            state.tasks.append(
                PLCTask(
                    id=f"SIEMENS-XML-OB:{relative}:{block_name}",
                    name=block_name,
                    task_type="ORGANIZATION_BLOCK",
                    scheduled_programs=(block_name,),
                )
            )
        compile_units = [child for child in block.iter() if _local_name(child.tag) == "SW.Blocks.CompileUnit"]
        if not compile_units:
            state.warnings.append(
                f"TIA XML block {block_name} ({language}) exposes no executable compile-unit body; interface is retained but behavior remains NOT_PROVEN."
            )
            continue
        for index, unit in enumerate(compile_units, start=1):
            state.xml_networks += 1
            unit_language = (_child_text(unit, "ProgrammingLanguage") or language).upper()
            text = ET.tostring(unit, encoding="unicode")[:8192]
            semantic = PLCSemanticState.OPAQUE
            state.xml_networks_withheld += 1
            statement_id = f"SIEMENS-XML-{hashlib.sha1(f'{relative}:{block_name}:{index}:{text}'.encode()).hexdigest()[:14]}"
            state.statements.append(
                PLCLogicStatement(
                    id=statement_id,
                    language=unit_language,
                    owner_type="program",
                    owner_name=block_name,
                    routine=block_name,
                    locator=f"Network {index}",
                    text=text,
                    reads=(),
                    writes=(),
                    calls=(),
                    semantic_state=semantic,
                    source=PLCSourceRef(artifact, controller, program=block_name, routine=block_name, line=f"Network {index}"),
                )
            )
            state.warnings.append(
                f"TIA XML {unit_language} network {block_name}/Network {index} is structurally imported but V1 withholds executable behavior proof; export/generated SCL source when available for deeper deterministic review."
            )


def _parse_structural_source(path: Path, relative: str, state: _BuildState, artifact: str, controller: str) -> None:
    state.structural_source_files += 1
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    name = path.stem
    language = "STL" if path.suffix.lower() in {".stl", ".awl"} else path.suffix.lstrip(".").upper()
    routine_id = f"SIEMENS-STRUCT:{relative}:{name}"
    state.routines.append(PLCRoutine(routine_id, name, name, language, False))
    state.programs.append(PLCProgram(f"SIEMENS-STRUCT-PROGRAM:{relative}:{name}", name, (), (routine_id,), None))
    state.statements.append(
        PLCLogicStatement(
            id=f"SIEMENS-STRUCT-STMT:{hashlib.sha1((relative + text).encode()).hexdigest()[:14]}",
            language=language,
            owner_type="program",
            owner_name=name,
            routine=name,
            locator="Source",
            text=text[:8192],
            reads=(),
            writes=(),
            calls=(),
            semantic_state=PLCSemanticState.OPAQUE,
            source=PLCSourceRef(artifact, controller, program=name, routine=name, line="Source"),
        )
    )
    state.warnings.append(f"TIA {language} source {relative} is retained structurally but executable semantics are NOT_PROVEN in Siemens V1.")


def _negative_assignment(paths: tuple[PLCLogicPath, ...]) -> dict[str, bool] | None:
    variables = sorted({term.tag for path in paths for term in path.terms}, key=str.casefold)
    if not variables or len(variables) > 8:
        return None
    for values in itertools.product((False, True), repeat=len(variables)):
        assignment = dict(zip(variables, values))
        if not any(all(assignment.get(term.tag) == term.required for term in path.terms) for path in paths):
            return assignment
    return None


def _siemens_fat_tests(project: CanonicalPLCProject) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL or logic.instruction != "ASSIGN_BOOL":
            continue
        for index, path in enumerate(logic.paths, start=1):
            preconditions = {term.tag: term.required for term in path.terms}
            if not preconditions:
                continue
            digest = hashlib.sha1(f"{logic.id}:true:{index}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SIEMENS-{digest}",
                    title=f"Verify SCL Boolean assignment for {logic.output_tag} at {logic.source.locator}",
                    source=logic.source,
                    output_tag=logic.output_tag,
                    preconditions=dict(sorted(preconditions.items(), key=lambda item: item[0].casefold())),
                    expected=f"{logic.output_tag}=TRUE while the modeled SCL Boolean expression evaluates TRUE",
                    limitations=(
                        "Generated from bounded top-level Siemens SCL Boolean assignment semantics; no PLC scan was executed.",
                        "Task timing, I/O update behavior, calls, process physics, and other writers remain outside this local theorem.",
                    ),
                    scenario="POSITIVE_PATH",
                )
            )
        blocked = _negative_assignment(logic.paths)
        if blocked:
            digest = hashlib.sha1(f"{logic.id}:false".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SIEMENS-{digest}",
                    title=f"Verify SCL Boolean inhibit for {logic.output_tag} at {logic.source.locator}",
                    source=logic.source,
                    output_tag=logic.output_tag,
                    preconditions=dict(sorted(blocked.items(), key=lambda item: item[0].casefold())),
                    expected=f"{logic.output_tag}=FALSE while every modeled SCL Boolean path is FALSE",
                    limitations=(
                        "Generated from bounded top-level Siemens SCL Boolean assignment semantics; no PLC scan was executed.",
                        "Other writers, called blocks, task timing, and physical I/O behavior are not simulated.",
                    ),
                    scenario="NEGATIVE_PATH",
                )
            )

    # Partial assignment statements still receive a precise engineer-controlled
    # runtime review procedure, but never a static PASS claim.
    for statement in project.logic_statements:
        if statement.language != "SCL" or statement.semantic_state is PLCSemanticState.FULL or not statement.writes:
            continue
        output = statement.writes[0]
        digest = hashlib.sha1(f"{statement.id}:runtime".encode()).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-SIEMENS-RUNTIME-{digest}",
                title=f"Verify partial SCL behavior for {output} at {statement.source.locator}",
                source=statement.source,
                output_tag=output,
                preconditions={},
                expected=(
                    f"Observed {output} behavior must match the intended branch/call/sequence behavior represented by the source statement. "
                    "PASS requires engineer-executed runtime evidence."
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario="SCL_RUNTIME",
                limitations=(
                    "The statement is traceable but lies outside the Siemens V1 bounded top-level assignment theorem.",
                    "DevAgent does not connect to, control, or execute PLCSIM/HIL/real PLC software.",
                ),
            )
        )
    return enrich_fat_procedures(project, tests)


def siemens_capability_profile(project: CanonicalPLCProject) -> dict[str, object]:
    full = sum(item.semantic_state is PLCSemanticState.FULL for item in project.logic_statements)
    partial = sum(item.semantic_state is PLCSemanticState.PARTIAL for item in project.logic_statements)
    opaque = sum(item.semantic_state is PLCSemanticState.OPAQUE for item in project.logic_statements)
    protected = sum(item.source_protected for item in project.routines)
    contract = "COMPLETE" if project.logic_statements and not partial and not opaque and not protected and not project.warnings else "PARTIAL_FAIL_CLOSED"
    if not project.logic_statements:
        contract = "NO_EXECUTABLE_LOGIC"
    return {
        "schema": "devagent-siemens-tia-capability-v1",
        "static_contract": contract,
        "source_sha256": project.metadata.source_sha256,
        "tags": len(project.tags),
        "programs": len(project.programs),
        "routines": len(project.routines),
        "tasks": len(project.tasks),
        "scl_statements": project.st_statement_total,
        "full_statements": full,
        "partial_statements": partial,
        "opaque_statements": opaque,
        "boolean_output_logic": len(project.output_logic),
        "protected_blocks": protected,
        "warnings": list(project.warnings),
        "runtime_evidence_required": True,
        "devagent_executes_external_plc_software": False,
    }


def _siemens_checks(project: CanonicalPLCProject, graph, fat_tests: list[FATTestCase]) -> list[StaticCheck]:
    profile = siemens_capability_profile(project)
    source_objects = list(project.logic_statements)
    provenance = bool(source_objects) and all(item.source.artifact and item.source.routine for item in source_objects)
    dependency_edges = [item for item in graph.edges if item.kind == "DEPENDS_ON"]
    partial = int(profile["partial_statements"])
    opaque = int(profile["opaque_statements"])
    checks = [
        StaticCheck(
            "SIEMENS_TIA_EXPORT_BUNDLE",
            StaticCheckStatus.PASS,
            "Artifact set is a read-only Siemens TIA Portal Openness/XML or generated-source engineering export bundle.",
            (project.metadata.source_path, project.metadata.source_sha256),
        ),
        StaticCheck(
            "SOURCE_PROVENANCE",
            StaticCheckStatus.PASS if provenance else StaticCheckStatus.NOT_PROVEN,
            f"All {len(source_objects)} normalized Siemens logic object(s) retain source provenance." if provenance else "No executable normalized Siemens logic, or source provenance is incomplete.",
        ),
        StaticCheck(
            "SIEMENS_SCL_SEMANTICS",
            StaticCheckStatus.PASS if project.st_statement_total and partial == 0 and opaque == 0 else StaticCheckStatus.WARN,
            f"Modeled {project.st_statement_semantic_count}/{project.st_statement_total} Siemens SCL statement(s) with bounded deterministic semantics; {partial} PARTIAL and {opaque} OPAQUE logic object(s) remain withheld.",
        ),
        StaticCheck(
            "SIEMENS_XML_NETWORK_SEMANTICS",
            StaticCheckStatus.WARN if opaque else StaticCheckStatus.PASS,
            f"{opaque} TIA XML/structural network or source object(s) remain OPAQUE to deterministic behavior proof." if opaque else "No opaque TIA XML network/source behavior remains in the analyzed export bundle.",
        ),
        StaticCheck(
            "DEPENDENCY_GRAPH",
            StaticCheckStatus.PASS if dependency_edges else StaticCheckStatus.WARN,
            f"Dependency graph contains {len(graph.edges)} edge(s), including {len(dependency_edges)} deterministic DEPENDS_ON edge(s).",
        ),
        StaticCheck(
            "FAT_TEST_TRACEABILITY",
            StaticCheckStatus.PASS if fat_tests and all(item.source.artifact and item.source.routine for item in fat_tests) else StaticCheckStatus.WARN,
            f"Generated {len(fat_tests)} source-traceable engineer FAT procedure(s); every result remains NOT_RUN until engineer evidence is imported." if fat_tests else "No bounded FAT procedure could be generated from this export bundle.",
        ),
        StaticCheck(
            "EXTERNAL_EXECUTION",
            StaticCheckStatus.NOT_PROVEN,
            "DevAgent does not execute PLCSIM, HIL, or a real Siemens PLC; runtime machine behavior is not claimed as verified.",
        ),
    ]
    return checks


def analyze_siemens_tia(path: Path) -> PLCEngineeringResult:
    root, files = _supported_sources(path)
    project_sha = _bundle_sha(files)
    source_label = str(path.expanduser().resolve(strict=True))
    controller = root.name or Path(source_label).stem or "SiemensExport"
    artifact = source_label
    state = _BuildState([], [], [], [], [], [], [], [], set())

    # Parse generated SCL first so XML duplicates can contribute interfaces/tags
    # without double-counting the same executable block body.
    for source, relative in files:
        if source.suffix.lower() not in {".scl", ".db", ".udt"}:
            continue
        blocks = _extract_source_blocks(source, relative)
        if not blocks:
            if source.suffix.lower() == ".scl":
                state.warnings.append(f"No SCL block declaration was recognized in {relative}; source is retained outside deterministic proof.")
            continue
        state.scl_source_files += 1
        for block in blocks:
            _add_source_block(block, state, artifact, controller)

    for source, relative in files:
        suffix = source.suffix.lower()
        if suffix == ".xml":
            _parse_xml(source, relative, state, artifact, controller)
        elif suffix in {".stl", ".awl"}:
            _parse_structural_source(source, relative, state, artifact, controller)

    # Stable de-duplication if one export bundle repeats the same block interface.
    programs: dict[str, PLCProgram] = {}
    routines: dict[str, PLCRoutine] = {}
    tasks: dict[str, PLCTask] = {}
    for item in state.programs:
        programs.setdefault(item.id, item)
    for item in state.routines:
        routines.setdefault(item.id, item)
    for item in state.tasks:
        tasks.setdefault(item.id, item)

    scl_statements = [item for item in state.statements if item.language == "SCL"]
    metadata = PLCProjectMetadata(
        vendor="Siemens",
        engineering_tool="TIA Portal / Openness engineering export",
        source_path=source_label,
        source_sha256=project_sha,
        schema_revision="SIEMENS-TIA-EXPORT-V1",
        software_revision=None,
        target_type="TIA_OPENNESS_EXPORT_BUNDLE",
        controller_name=controller,
        processor_type=None,
        major_revision=None,
        minor_revision=None,
        full_project=True,
    )
    project = CanonicalPLCProject(
        metadata=metadata,
        tags=state.tags,
        data_types=state.data_types,
        tasks=list(tasks.values()),
        programs=list(programs.values()),
        routines=list(routines.values()),
        rungs=[],
        logic_statements=state.statements,
        output_logic=state.output_logic,
        warnings=list(dict.fromkeys(state.warnings)),
        unknown_instruction_names=[],
        partially_modeled_instruction_names=sorted({item.language for item in state.statements if item.semantic_state is not PLCSemanticState.FULL}),
        instruction_total=len(state.statements),
        instruction_semantic_count=sum(item.semantic_state is PLCSemanticState.FULL for item in state.statements),
        st_statement_total=len(scl_statements),
        st_statement_semantic_count=sum(item.semantic_state is PLCSemanticState.FULL for item in scl_statements),
        branch_rung_total=0,
        branch_rung_semantic_count=0,
        aoi_internal_total=0,
        aoi_internal_modeled_count=0,
        aoi_call_total=0,
        aoi_call_bound_count=0,
    )
    graph = build_dependency_graph(project)
    fat_tests = _siemens_fat_tests(project)
    checks = _siemens_checks(project, graph, fat_tests)
    profile = siemens_capability_profile(project)
    outcome = PLCOutcome.STATICALLY_VERIFIED if profile["static_contract"] == "COMPLETE" else PLCOutcome.PARTIALLY_VERIFIED
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED
    limitations = [
        "Siemens V1 analyzes TIA Portal Openness/XML and generated engineering-source exports offline; it does not open proprietary .ap*/.zap* projects.",
        "DevAgent does not connect to, write to, download to, or execute PLCSIM, HIL, or a Siemens PLC.",
        "Only bounded top-level SCL assignment/Boolean dataflow is eligible for static proof in V1. IF/CASE/loop/call semantics and LAD/FBD/GRAPH/STL XML networks remain PARTIAL/OPAQUE unless explicitly modeled by a later theorem.",
        "Protected/interface-only blocks remain outside behavioral proof. Export/unlock the implementation according to the customer's approved engineering process when deeper review is required.",
        "Generated FAT procedures are engineer-executed recommendations and remain NOT_RUN until authenticated execution evidence is imported.",
        *project.warnings,
    ]
    return PLCEngineeringResult(outcome, project, graph, fat_tests, checks, list(dict.fromkeys(limitations)))


__all__ = [
    "SiemensInputError",
    "analyze_siemens_tia",
    "detect_siemens_input",
    "siemens_capability_profile",
]
