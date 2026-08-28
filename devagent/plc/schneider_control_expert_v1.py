from __future__ import annotations

from dataclasses import dataclass, field
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
    PLCProjectMetadata,
    PLCRoutine,
    PLCSourceRef,
    PLCSemanticState,
    PLCTag,
    PLCTask,
    StaticCheck,
    StaticCheckStatus,
)


class SchneiderInputError(ValueError):
    pass


_SUPPORTED_SUFFIXES = {
    ".xef",  # full project XML export
    ".xsy",  # variables
    ".xst",  # Structured Text section
    ".xld",  # Ladder section
    ".xbd",  # Function Block Diagram section
    ".xsf",  # Sequential Function Chart section
    ".xil",  # Instruction List section
    ".xdd",  # derived data type
    ".xdb",  # DFB export
    ".xhw",  # I/O configuration export
    ".xcm",  # communication network export
}
_PROPRIETARY_OR_ARCHIVE_SUFFIXES = {".stu", ".sta", ".zef"}
_MAX_FILES = 5000
_MAX_TOTAL_BYTES = 100 * 1024 * 1024

_CONTROL_OPEN = re.compile(r"^\s*(IF|CASE|FOR|WHILE|REPEAT)\b", re.IGNORECASE)
_CONTROL_MID = re.compile(r"^\s*(ELSIF|ELSE)\b", re.IGNORECASE)
_CONTROL_CLOSE = re.compile(r"^\s*(END_IF|END_CASE|END_FOR|END_WHILE|UNTIL|END_REPEAT)\b", re.IGNORECASE)
_ASSIGNMENT = re.compile(r"^\s*(?P<lhs>[^:;]+?)\s*:=\s*(?P<rhs>.+?)\s*;?\s*$", re.DOTALL)
_CALL = re.compile(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*\(", re.IGNORECASE)
_REF_TOKEN = re.compile(r"%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_BOOL_TOKEN = re.compile(
    r"\s*(\(|\)|\bAND\b|\bOR\b|\bNOT\b|\bTRUE\b|\bFALSE\b|%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
    re.IGNORECASE,
)
_KEYWORDS = {
    "AND", "OR", "NOT", "TRUE", "FALSE", "IF", "THEN", "ELSIF", "ELSE", "END_IF",
    "CASE", "OF", "END_CASE", "FOR", "TO", "BY", "DO", "END_FOR", "WHILE", "END_WHILE",
    "REPEAT", "UNTIL", "END_REPEAT", "RETURN",
}


@dataclass
class _BuildState:
    tags: list[PLCTag] = field(default_factory=list)
    data_types: list[PLCDataType] = field(default_factory=list)
    tasks: list[PLCTask] = field(default_factory=list)
    programs: list[PLCProgram] = field(default_factory=list)
    routines: list[PLCRoutine] = field(default_factory=list)
    statements: list[PLCLogicStatement] = field(default_factory=list)
    output_logic: list[PLCOutputLogic] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    project_name: str | None = None
    product: str | None = None
    dtd_version: str | None = None
    parsed_sections: int = 0
    st_sections: int = 0
    ld_sections: int = 0
    opaque_sections: int = 0


def _local_name(tag: str) -> str:
    return str(tag).split("}", 1)[-1]


def _preflight_sources(path: Path) -> tuple[Path, list[tuple[Path, str]], int]:
    target = path.expanduser().resolve(strict=True)
    suffix = target.suffix.lower()
    if target.is_file() and suffix in _PROPRIETARY_OR_ARCHIVE_SUFFIXES:
        if suffix == ".zef":
            raise SchneiderInputError(
                "Schneider .ZEF is an export package/archive, not the V1 canonical XML source surface. "
                "Extract/export the contained .XEF with EcoStruxure Control Expert and analyze the .XEF (or granular .XST/.XLD/.XBD/.XSF/.XSY files)."
            )
        raise SchneiderInputError(
            f"Schneider {suffix.upper()} is a Control Expert work/archive format. Export the project to .XEF first; DevAgent V1 does not parse proprietary work/archive containers."
        )
    if target.is_file():
        if suffix not in _SUPPORTED_SUFFIXES:
            raise SchneiderInputError(
                f"Unsupported Schneider Control Expert artifact {target.name}; expected .XEF or granular XML exchange exports "
                "(.XSY/.XST/.XLD/.XBD/.XSF/.XIL/.XDD/.XDB/.XHW/.XCM)."
            )
        size = target.stat().st_size
        if size > _MAX_TOTAL_BYTES:
            raise SchneiderInputError("Schneider export exceeds 100 MiB production limit")
        return target.parent, [(target, target.name)], size

    if not target.is_dir():
        raise SchneiderInputError(f"Schneider export path is not a file or directory: {target}")

    files: list[tuple[Path, str]] = []
    total = 0
    for item in sorted(target.rglob("*"), key=lambda value: str(value).casefold()):
        if not item.is_file() or item.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        resolved = item.resolve(strict=True)
        try:
            relative = resolved.relative_to(target).as_posix()
        except ValueError as exc:
            raise SchneiderInputError(f"Schneider export source escapes the selected directory: {item}") from exc
        files.append((resolved, relative))
        total += resolved.stat().st_size
        if len(files) > _MAX_FILES:
            raise SchneiderInputError(f"Schneider export exceeds {_MAX_FILES} supported source files")
        if total > _MAX_TOTAL_BYTES:
            raise SchneiderInputError("Schneider export bundle exceeds 100 MiB production limit")
    if not files:
        raise SchneiderInputError(
            "No supported EcoStruxure Control Expert XML exchange artifacts were found. Export the project to .XEF or export sections/variables to .XST/.XLD/.XBD/.XSF/.XSY."
        )
    return target, files, total


def _bundle_sha(files: list[tuple[Path, str]]) -> str:
    if len(files) == 1:
        return hashlib.sha256(files[0][0].read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for source, relative in files:
        payload = source.read_bytes()
        digest.update(relative.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def detect_schneider_input(path: Path) -> bool:
    target = path.expanduser().resolve(strict=False)
    if target.is_dir():
        try:
            _, files, _ = _preflight_sources(target)
        except (OSError, SchneiderInputError):
            return False
        return bool(files)
    return target.suffix.lower() in _SUPPORTED_SUFFIXES


def _strip_comments(text: str) -> str:
    text = re.sub(
        r"\(\*.*?\*\)",
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
        text,
        flags=re.DOTALL,
    )
    return re.sub(r"//[^\n]*", "", text)


def _extract_refs(text: str) -> tuple[str, ...]:
    result: list[str] = []
    for match in _REF_TOKEN.finditer(text):
        token = match.group(0)
        if token.upper() in _KEYWORDS:
            continue
        if re.fullmatch(r"[A-Za-z]+#[A-Za-z0-9_.]+", token):
            continue
        if token not in result:
            result.append(token)
    return tuple(result)


def _lhs_ref(value: str) -> str | None:
    stripped = value.strip()
    if "[" in stripped or "]" in stripped or "(" in stripped or ")" in stripped:
        return None
    if re.fullmatch(r"%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", stripped):
        return stripped
    return None


def _tokenize_bool(expr: str) -> list[str] | None:
    tokens: list[str] = []
    position = 0
    while position < len(expr):
        if expr[position:].strip() == "":
            break
        match = _BOOL_TOKEN.match(expr, position)
        if match is None:
            return None
        tokens.append(match.group(1))
        position = match.end()
    return tokens or None


def _parse_bool_ast(expr: str):
    tokens = _tokenize_bool(expr)
    if not tokens:
        return None
    position = 0

    def primary():
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
            return ("NOT", primary())
        position += 1
        if upper == "TRUE":
            return ("CONST", True)
        if upper == "FALSE":
            return ("CONST", False)
        return ("REF", token)

    def parse_and():
        nonlocal position
        node = primary()
        while position < len(tokens) and tokens[position].upper() == "AND":
            position += 1
            node = ("AND", node, primary())
        return node

    def parse_or():
        nonlocal position
        node = parse_and()
        while position < len(tokens) and tokens[position].upper() == "OR":
            position += 1
            node = ("OR", node, parse_and())
        return node

    try:
        node = parse_or()
        return node if position == len(tokens) else None
    except ValueError:
        return None


def _dnf(node, negated: bool = False) -> list[dict[str, bool]] | None:
    kind = node[0]
    if kind == "CONST":
        value = bool(node[1])
        return [{}] if value != negated else []
    if kind == "REF":
        return [{str(node[1]): not negated}]
    if kind == "NOT":
        return _dnf(node[1], not negated)
    if kind in {"AND", "OR"}:
        effective = kind
        if negated:
            effective = "OR" if kind == "AND" else "AND"
        left = _dnf(node[1], negated)
        right = _dnf(node[2], negated)
        if left is None or right is None:
            return None
        if effective == "OR":
            merged = [*left, *right]
        else:
            merged = []
            for lhs in left:
                for rhs in right:
                    conflict = any(key in lhs and lhs[key] != value for key, value in rhs.items())
                    if conflict:
                        continue
                    merged.append({**lhs, **rhs})
        unique: list[dict[str, bool]] = []
        seen: set[tuple[tuple[str, bool], ...]] = set()
        for item in merged:
            key = tuple(sorted(item.items(), key=lambda pair: pair[0].casefold()))
            if key not in seen:
                seen.add(key)
                unique.append(dict(key))
        return unique
    return None


def _bool_paths(expr: str) -> tuple[PLCLogicPath, ...] | None:
    ast = _parse_bool_ast(expr)
    if ast is None:
        return None
    dnf = _dnf(ast)
    if dnf is None or len(dnf) > 64:
        return None
    paths = []
    for assignment in dnf:
        paths.append(
            PLCLogicPath(
                tuple(PLCBooleanTerm(tag, required) for tag, required in sorted(assignment.items(), key=lambda item: item[0].casefold()))
            )
        )
    return tuple(paths)


def _dedupe_tag(state: _BuildState, tag: PLCTag) -> None:
    key = (tag.scope.casefold(), tag.name.casefold())
    if not any((item.scope.casefold(), item.name.casefold()) == key for item in state.tags):
        state.tags.append(tag)


def _collect_header(root: ET.Element, state: _BuildState) -> None:
    for element in root.iter():
        local = _local_name(element.tag)
        if local == "contentHeader" and not state.project_name:
            state.project_name = element.attrib.get("name") or state.project_name
        elif local == "fileHeader":
            state.product = element.attrib.get("product") or state.product
            state.dtd_version = element.attrib.get("DTDVersion") or state.dtd_version


def _collect_variables(root: ET.Element, state: _BuildState, relative: str) -> None:
    for element in root.iter():
        if _local_name(element.tag) != "variables":
            continue
        name = (element.attrib.get("name") or "").strip()
        if not name:
            continue
        dtype = (element.attrib.get("typeName") or element.attrib.get("type") or "UNKNOWN").strip()
        address = (
            element.attrib.get("topologicalAddress")
            or element.attrib.get("address")
            or element.attrib.get("locatedAddress")
        )
        _dedupe_tag(
            state,
            PLCTag(
                id=f"SCHNEIDER-TAG:controller:{name}",
                name=name,
                scope="controller",
                data_type=dtype,
                tag_type="CONTROL_EXPERT_VARIABLE",
                description=f"Control Expert address {address}; source {relative}" if address else f"Control Expert export source {relative}",
            ),
        )


def _add_section_identity(state: _BuildState, name: str, language: str, task: str | None, relative: str) -> None:
    routine_id = f"SCHNEIDER-ROUTINE:{relative}:{name}:{language}"
    if not any(item.id == routine_id for item in state.routines):
        state.routines.append(PLCRoutine(routine_id, name, name, language, False))
    program_id = f"SCHNEIDER-PROGRAM:{relative}:{name}"
    if not any(item.id == program_id for item in state.programs):
        state.programs.append(PLCProgram(program_id, name, (), (routine_id,), name))
    if task:
        task_id = f"SCHNEIDER-TASK:{task}"
        existing = next((item for item in state.tasks if item.id == task_id), None)
        sections = tuple(dict.fromkeys([*(existing.scheduled_programs if existing else ()), name]))
        if existing:
            state.tasks[state.tasks.index(existing)] = PLCTask(task_id, task, "CONTROL_EXPERT_TASK", scheduled_programs=sections)
        else:
            state.tasks.append(PLCTask(task_id, task, "CONTROL_EXPERT_TASK", scheduled_programs=(name,)))


def _statement_source(artifact: str, controller: str, section: str, line: str) -> PLCSourceRef:
    return PLCSourceRef(artifact, controller, program=section, routine=section, line=line)


def _parse_st_source(text: str, section: str, relative: str, state: _BuildState, artifact: str, controller: str) -> None:
    clean = _strip_comments(text)
    depth = 0
    for line_no, raw in enumerate(clean.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if _CONTROL_CLOSE.match(stripped):
            depth = max(0, depth - 1)
            continue
        if _CONTROL_OPEN.match(stripped) or _CONTROL_MID.match(stripped):
            refs = _extract_refs(stripped)
            statement_id = f"SCHNEIDER-ST-{hashlib.sha1(f'{relative}:{section}:{line_no}:{stripped}'.encode()).hexdigest()[:14]}"
            state.statements.append(
                PLCLogicStatement(
                    statement_id, "ST", "program", section, section, f"Line {line_no}", stripped,
                    refs, (), (), PLCSemanticState.PARTIAL,
                    _statement_source(artifact, controller, section, str(line_no)),
                )
            )
            if _CONTROL_OPEN.match(stripped):
                depth += 1
            continue

        for chunk_index, chunk in enumerate(filter(None, (part.strip() for part in stripped.split(";"))), start=1):
            locator = f"Line {line_no}" if chunk_index == 1 else f"Line {line_no}.{chunk_index}"
            source = _statement_source(artifact, controller, section, locator.removeprefix("Line "))
            assignment = _ASSIGNMENT.match(chunk + ";")
            if assignment:
                lhs = _lhs_ref(assignment.group("lhs"))
                rhs = assignment.group("rhs").strip()
                reads = _extract_refs(rhs)
                writes = (lhs,) if lhs else ()
                paths = _bool_paths(rhs) if depth == 0 and lhs else None
                semantic = PLCSemanticState.FULL if paths is not None else PLCSemanticState.PARTIAL
                statement_id = f"SCHNEIDER-ST-{hashlib.sha1(f'{relative}:{section}:{locator}:{chunk}'.encode()).hexdigest()[:14]}"
                state.statements.append(
                    PLCLogicStatement(
                        statement_id, "ST", "program", section, section, locator, chunk + ";",
                        reads, writes, (), semantic, source,
                    )
                )
                if semantic is PLCSemanticState.FULL and lhs is not None and paths is not None:
                    state.output_logic.append(
                        PLCOutputLogic(
                            f"SCHNEIDER-LOGIC:{statement_id}", lhs, "ASSIGN_BOOL", paths, source,
                            language="ST", origin="CONTROL_EXPERT_ST", semantic_state=PLCSemanticState.FULL,
                        )
                    )
                continue
            call = _CALL.match(chunk)
            calls = (call.group("name"),) if call else ()
            refs = _extract_refs(chunk)
            statement_id = f"SCHNEIDER-ST-{hashlib.sha1(f'{relative}:{section}:{locator}:{chunk}'.encode()).hexdigest()[:14]}"
            state.statements.append(
                PLCLogicStatement(
                    statement_id, "ST", "program", section, section, locator, chunk + ";",
                    refs, (), calls, PLCSemanticState.PARTIAL, source,
                )
            )


def _parse_ld_source(source_element: ET.Element, section: str, relative: str, state: _BuildState, artifact: str, controller: str) -> None:
    line_index = 0
    for element in source_element.iter():
        if _local_name(element.tag) != "typeLine":
            continue
        line_index += 1
        terms: list[PLCBooleanTerm] = []
        output: str | None = None
        supported = True
        has_logic = False
        for child in list(element):
            local = _local_name(child.tag)
            if local in {"HLink", "VLink", "emptyCell", "emptyLine", "shortCircuit"}:
                continue
            if local == "contact":
                has_logic = True
                kind = (child.attrib.get("typeContact") or "").casefold()
                name = (child.attrib.get("contactVariableName") or "").strip()
                if kind == "opencontact" and name:
                    terms.append(PLCBooleanTerm(name, True))
                elif kind == "closedcontact" and name:
                    terms.append(PLCBooleanTerm(name, False))
                else:
                    supported = False
            elif local == "coil":
                has_logic = True
                kind = (child.attrib.get("typeCoil") or "").casefold()
                name = (child.attrib.get("coilVariableName") or "").strip()
                if kind == "coil" and name and output is None:
                    output = name
                else:
                    supported = False
            else:
                has_logic = True
                supported = False
        if not has_logic:
            continue
        locator = f"LD Line {line_index}"
        statement_id = f"SCHNEIDER-LD-{hashlib.sha1(f'{relative}:{section}:{line_index}'.encode()).hexdigest()[:14]}"
        source = _statement_source(artifact, controller, section, locator)
        reads = tuple(dict.fromkeys(term.tag for term in terms))
        writes = (output,) if output else ()
        semantic = PLCSemanticState.FULL if supported and output and terms else PLCSemanticState.OPAQUE
        state.statements.append(
            PLCLogicStatement(
                statement_id, "LD", "program", section, section, locator,
                ET.tostring(element, encoding="unicode")[:8192], reads, writes, (), semantic, source,
            )
        )
        if semantic is PLCSemanticState.FULL and output:
            state.output_logic.append(
                PLCOutputLogic(
                    f"SCHNEIDER-LOGIC:{statement_id}", output, "LD_COIL",
                    (PLCLogicPath(tuple(terms)),), source,
                    language="LD", origin="CONTROL_EXPERT_LD", semantic_state=PLCSemanticState.FULL,
                )
            )
        else:
            state.warnings.append(
                f"Control Expert LD {section}/{locator} contains branching, edge/stateful, block, control, or other geometry outside the V1 simple-series theorem; behavior remains OPAQUE."
            )


def _opaque_source(element: ET.Element, language: str, section: str, relative: str, state: _BuildState, artifact: str, controller: str) -> None:
    payload = ET.tostring(element, encoding="unicode")[:8192]
    statement_id = f"SCHNEIDER-{language}-{hashlib.sha1(f'{relative}:{section}:{payload}'.encode()).hexdigest()[:14]}"
    state.statements.append(
        PLCLogicStatement(
            statement_id, language, "program", section, section, "Source", payload,
            (), (), (), PLCSemanticState.OPAQUE,
            _statement_source(artifact, controller, section, "Source"),
        )
    )
    state.opaque_sections += 1
    state.warnings.append(
        f"Control Expert {language} section {section} is structurally imported but executable behavior remains OPAQUE in Schneider V1; engineer FAT evidence is required."
    )


def _parse_program(program: ET.Element, relative: str, state: _BuildState, artifact: str, controller: str) -> None:
    ident = next((item for item in program.iter() if _local_name(item.tag) == "identProgram"), None)
    if ident is None:
        return
    section = (ident.attrib.get("name") or f"Section_{state.parsed_sections + 1}").strip()
    task = (ident.attrib.get("task") or "").strip() or None
    source = next(
        (
            item for item in program.iter()
            if _local_name(item.tag) in {"STSource", "LDSource", "FBDSource", "SFCSource", "ILSource"}
        ),
        None,
    )
    if source is None:
        _add_section_identity(state, section, "UNKNOWN", task, relative)
        state.warnings.append(f"Control Expert section {section} exposes no supported executable source element.")
        return
    local = _local_name(source.tag)
    language = {"STSource": "ST", "LDSource": "LD", "FBDSource": "FBD", "SFCSource": "SFC", "ILSource": "IL"}[local]
    _add_section_identity(state, section, language, task, relative)
    state.parsed_sections += 1
    if language == "ST":
        state.st_sections += 1
        _parse_st_source("".join(source.itertext()), section, relative, state, artifact, controller)
    elif language == "LD":
        state.ld_sections += 1
        _parse_ld_source(source, section, relative, state, artifact, controller)
    else:
        _opaque_source(source, language, section, relative, state, artifact, controller)


def _collect_data_types(root: ET.Element, state: _BuildState, relative: str) -> None:
    for element in root.iter():
        local = _local_name(element.tag).casefold()
        if local not in {"ddt", "deriveddatatype", "ddtsource"}:
            continue
        name = (element.attrib.get("name") or element.attrib.get("typeName") or "").strip()
        if not name or any(item.name.casefold() == name.casefold() for item in state.data_types):
            continue
        members = []
        for child in element.iter():
            if _local_name(child.tag) != "variables":
                continue
            member = (child.attrib.get("name") or "").strip()
            dtype = (child.attrib.get("typeName") or "UNKNOWN").strip()
            if member:
                members.append(PLCDataTypeMember(member, dtype))
        state.data_types.append(PLCDataType(f"SCHNEIDER-DDT:{relative}:{name}", name, "DDT", tuple(members)))


def _parse_exchange_file(path: Path, relative: str, state: _BuildState, artifact: str, controller: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SchneiderInputError(f"Invalid Control Expert XML exchange artifact {relative}: {exc}") from exc
    _collect_header(root, state)
    _collect_variables(root, state, relative)
    _collect_data_types(root, state, relative)
    programs = [item for item in root.iter() if _local_name(item.tag) == "program"]
    for program in programs:
        _parse_program(program, relative, state, artifact, controller)


def _negative_assignment(paths: tuple[PLCLogicPath, ...]) -> dict[str, bool] | None:
    variables = sorted({term.tag for path in paths for term in path.terms}, key=str.casefold)
    if not variables or len(variables) > 8:
        return None
    for values in itertools.product((False, True), repeat=len(variables)):
        assignment = dict(zip(variables, values))
        if not any(all(assignment.get(term.tag) == term.required for term in path.terms) for path in paths):
            return assignment
    return None


def _fat_tests(project: CanonicalPLCProject) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL:
            continue
        for index, path in enumerate(logic.paths, start=1):
            preconditions = {term.tag: term.required for term in path.terms}
            if not preconditions:
                continue
            digest = hashlib.sha1(f"{logic.id}:true:{index}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-{digest}",
                    title=f"Verify {logic.language} Boolean command for {logic.output_tag} at {logic.source.locator}",
                    source=logic.source,
                    output_tag=logic.output_tag,
                    preconditions=dict(sorted(preconditions.items(), key=lambda item: item[0].casefold())),
                    expected=f"{logic.output_tag}=TRUE while the bounded modeled Boolean path is TRUE",
                    limitations=(
                        "Generated from bounded Control Expert V1 local Boolean semantics; no PLC scan was executed.",
                        "Task timing, I/O refresh, EFB/DFB state, process physics, and other writers remain outside this local theorem.",
                    ),
                    scenario="POSITIVE_PATH",
                )
            )
        blocked = _negative_assignment(logic.paths)
        if blocked:
            digest = hashlib.sha1(f"{logic.id}:false".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-{digest}",
                    title=f"Verify {logic.language} Boolean inhibit for {logic.output_tag} at {logic.source.locator}",
                    source=logic.source,
                    output_tag=logic.output_tag,
                    preconditions=dict(sorted(blocked.items(), key=lambda item: item[0].casefold())),
                    expected=f"{logic.output_tag}=FALSE while every bounded modeled Boolean path is FALSE",
                    limitations=(
                        "Generated from bounded Control Expert V1 local Boolean semantics; no PLC scan was executed.",
                        "Other writers, task ordering, EFB/DFB state, I/O refresh, and physical behavior are not simulated.",
                    ),
                    scenario="NEGATIVE_PATH",
                )
            )
    for statement in project.logic_statements:
        if statement.semantic_state is PLCSemanticState.FULL or not statement.writes:
            continue
        output = statement.writes[0]
        digest = hashlib.sha1(f"{statement.id}:runtime".encode()).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-SCHNEIDER-RUNTIME-{digest}",
                title=f"Verify runtime-dependent {statement.language} behavior for {output} at {statement.source.locator}",
                source=statement.source,
                output_tag=output,
                preconditions={},
                expected=(
                    f"Observed {output} behavior must match the intended Control Expert sequence/call/state behavior represented by the source. "
                    "PASS requires engineer-executed runtime evidence."
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario=f"{statement.language}_RUNTIME",
                limitations=(
                    "This source is traceable but lies outside the Schneider V1 bounded local Boolean theorem.",
                    "DevAgent does not launch, connect to, or control Control Expert Simulator, HIL, or a real Modicon PLC.",
                ),
            )
        )
    return enrich_fat_procedures(project, tests)


def schneider_capability_profile(project: CanonicalPLCProject) -> dict[str, object]:
    full = sum(item.semantic_state is PLCSemanticState.FULL for item in project.logic_statements)
    partial = sum(item.semantic_state is PLCSemanticState.PARTIAL for item in project.logic_statements)
    opaque = sum(item.semantic_state is PLCSemanticState.OPAQUE for item in project.logic_statements)
    if not project.logic_statements:
        contract = "NO_EXECUTABLE_LOGIC"
    elif partial or opaque or project.warnings:
        contract = "PARTIAL_FAIL_CLOSED"
    else:
        contract = "COMPLETE"
    return {
        "schema": "devagent-schneider-control-expert-capability-v1",
        "static_contract": contract,
        "source_sha256": project.metadata.source_sha256,
        "tags": len(project.tags),
        "data_types": len(project.data_types),
        "programs": len(project.programs),
        "routines": len(project.routines),
        "tasks": len(project.tasks),
        "logic_statements": len(project.logic_statements),
        "full_statements": full,
        "partial_statements": partial,
        "opaque_statements": opaque,
        "boolean_output_logic": len(project.output_logic),
        "warnings": list(project.warnings),
        "runtime_evidence_required": True,
        "devagent_executes_external_plc_software": False,
    }


def _checks(project: CanonicalPLCProject, graph, fat_tests: list[FATTestCase]) -> list[StaticCheck]:
    profile = schneider_capability_profile(project)
    full = int(profile["full_statements"])
    partial = int(profile["partial_statements"])
    opaque = int(profile["opaque_statements"])
    provenance = bool(project.logic_statements) and all(item.source.artifact and item.source.routine for item in project.logic_statements)
    depends = [item for item in graph.edges if item.kind == "DEPENDS_ON"]
    return [
        StaticCheck(
            "SCHNEIDER_CONTROL_EXPERT_EXPORT",
            StaticCheckStatus.PASS,
            "Artifact set is a read-only EcoStruxure Control Expert / Unity Pro XML exchange export (.XEF or granular X* exports).",
            (project.metadata.source_path, project.metadata.source_sha256),
        ),
        StaticCheck(
            "SOURCE_PROVENANCE",
            StaticCheckStatus.PASS if provenance else StaticCheckStatus.NOT_PROVEN,
            f"All {len(project.logic_statements)} normalized Schneider logic object(s) retain source provenance." if provenance else "No executable normalized Schneider logic, or source provenance is incomplete.",
        ),
        StaticCheck(
            "SCHNEIDER_BOUNDED_SEMANTICS",
            StaticCheckStatus.PASS if project.logic_statements and partial == 0 and opaque == 0 else StaticCheckStatus.WARN,
            f"Modeled {full}/{len(project.logic_statements)} Control Expert logic statement/network object(s) with bounded deterministic semantics; {partial} PARTIAL and {opaque} OPAQUE remain withheld.",
        ),
        StaticCheck(
            "DEPENDENCY_GRAPH",
            StaticCheckStatus.PASS if depends else StaticCheckStatus.WARN,
            f"Dependency graph contains {len(graph.edges)} edge(s), including {len(depends)} deterministic DEPENDS_ON edge(s).",
        ),
        StaticCheck(
            "FAT_TEST_TRACEABILITY",
            StaticCheckStatus.PASS if fat_tests and all(item.source.artifact and item.source.routine for item in fat_tests) else StaticCheckStatus.WARN,
            f"Generated {len(fat_tests)} source-traceable engineer FAT procedure(s); results remain NOT_RUN until qualified engineer execution evidence is imported." if fat_tests else "No bounded FAT procedure could be generated from this export bundle.",
        ),
        StaticCheck(
            "EXTERNAL_EXECUTION",
            StaticCheckStatus.NOT_PROVEN,
            "DevAgent does not execute EcoStruxure Control Expert Simulator, HIL, or a real Modicon PLC; runtime machine behavior is not claimed as verified.",
        ),
    ]


def analyze_schneider_control_expert(path: Path) -> PLCEngineeringResult:
    root, files, _total = _preflight_sources(path)
    project_sha = _bundle_sha(files)
    source_label = str(path.expanduser().resolve(strict=True))
    artifact = source_label
    provisional_controller = root.name or Path(source_label).stem or "SchneiderExport"
    state = _BuildState()

    for source, relative in files:
        _parse_exchange_file(source, relative, state, artifact, provisional_controller)

    controller = state.project_name or provisional_controller
    if controller != provisional_controller:
        statements = []
        for item in state.statements:
            src = item.source
            statements.append(
                PLCLogicStatement(
                    item.id, item.language, item.owner_type, item.owner_name, item.routine, item.locator, item.text,
                    item.reads, item.writes, item.calls, item.semantic_state,
                    PLCSourceRef(src.artifact, controller, src.program, src.routine, src.rung, src.aoi, src.line),
                )
            )
        state.statements = statements
        state.output_logic = [
            PLCOutputLogic(
                item.id, item.output_tag, item.instruction, item.paths,
                PLCSourceRef(item.source.artifact, controller, item.source.program, item.source.routine, item.source.rung, item.source.aoi, item.source.line),
                item.language, item.origin, item.semantic_state,
            )
            for item in state.output_logic
        ]

    metadata = PLCProjectMetadata(
        vendor="Schneider Electric",
        engineering_tool="EcoStruxure Control Expert / Unity Pro XML exchange export",
        source_path=source_label,
        source_sha256=project_sha,
        schema_revision=state.dtd_version,
        software_revision=state.product,
        target_type="CONTROL_EXPERT_EXPORT",
        controller_name=controller,
        processor_type=None,
        major_revision=None,
        minor_revision=None,
        full_project=any(source.suffix.lower() == ".xef" for source, _ in files),
    )
    project = CanonicalPLCProject(
        metadata=metadata,
        tags=state.tags,
        data_types=state.data_types,
        tasks=state.tasks,
        programs=state.programs,
        routines=state.routines,
        rungs=[],
        logic_statements=state.statements,
        output_logic=state.output_logic,
        warnings=list(dict.fromkeys(state.warnings)),
        unknown_instruction_names=[],
        partially_modeled_instruction_names=sorted({item.language for item in state.statements if item.semantic_state is not PLCSemanticState.FULL}),
        instruction_total=len(state.statements),
        instruction_semantic_count=sum(item.semantic_state is PLCSemanticState.FULL for item in state.statements),
        st_statement_total=sum(item.language == "ST" for item in state.statements),
        st_statement_semantic_count=sum(item.language == "ST" and item.semantic_state is PLCSemanticState.FULL for item in state.statements),
    )
    graph = build_dependency_graph(project)
    fat_tests = _fat_tests(project)
    checks = _checks(project, graph, fat_tests)
    profile = schneider_capability_profile(project)
    if profile["static_contract"] == "COMPLETE":
        outcome = PLCOutcome.STATICALLY_VERIFIED
    elif profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED
    else:
        outcome = PLCOutcome.PARTIALLY_VERIFIED
    limitations = [
        "Schneider V1 analyzes EcoStruxure Control Expert / Unity Pro XML exchange exports offline. .STU/.STA work/archive formats are not parsed directly; export .XEF first.",
        ".ZEF is treated as an export package boundary in V1; extract/export its .XEF and analyze that canonical XML source instead of relying on archive internals.",
        "Bounded top-level ST Boolean assignments and simple series LD contact-to-coil networks are eligible for local static proof. IF/CASE/loop/call/stateful ST and complex LD/FBD/SFC/IL behavior remain PARTIAL/OPAQUE unless explicitly modeled by a later theorem.",
        "DevAgent does not connect to, write to, download to, or execute Control Expert Simulator, HIL, or a real Modicon PLC.",
        "Generated FAT procedures are engineer-executed recommendations and remain NOT_RUN until authenticated execution evidence is imported.",
        *project.warnings,
    ]
    return PLCEngineeringResult(outcome, project, graph, fat_tests, checks, list(dict.fromkeys(limitations)))


__all__ = [
    "SchneiderInputError",
    "analyze_schneider_control_expert",
    "detect_schneider_input",
    "schneider_capability_profile",
]
