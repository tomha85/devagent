from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from devagent.plc.production_models import PLCRequirement, RequirementVerificationMode

_REQ_PREFIX = re.compile(r"^\s*(?:[-*•]\s*)?(?:(REQ[-_ ]?[A-Za-z0-9_.-]+|[A-Z]{2,10}[-_][0-9][A-Za-z0-9_.-]*)\s*[:.)-]?\s*)?(.*)$")
_NUMBERED = re.compile(r"^\s*(?:[-*•]|\d+(?:\.\d+)*[.)])\s+(.*)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_MAX_REQUIREMENT_BYTES = 25 * 1024 * 1024
_MAX_DOCX_XML_BYTES = 12 * 1024 * 1024
_MAX_REQUIREMENTS = 10_000
_MAX_EXTRACTED_TEXT = 20_000_000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_id(source_sha: str, locator: str, text: str, explicit: str | None = None) -> str:
    if explicit:
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", explicit.strip()).strip("-")
        if normalized:
            return normalized.upper()
    digest = hashlib.sha1(f"{source_sha}:{locator}:{text}".encode("utf-8")).hexdigest()[:10]
    return f"REQ-{digest.upper()}"


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _verification_mode(value: object | None, *, source: str) -> RequirementVerificationMode:
    if value is None or not str(value).strip():
        return RequirementVerificationMode.DYNAMIC
    normalized = str(value).strip().upper()
    try:
        return RequirementVerificationMode(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Requirement verification_mode must be DYNAMIC or STATIC at {source}; received {value!r}"
        ) from exc


def _requirements_from_lines(path: Path, text: str, source_sha: str) -> list[PLCRequirement]:
    result: list[PLCRequirement] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = _normalize_text(raw)
        if not line or line.startswith("#"):
            continue
        numbered = _NUMBERED.match(line)
        candidate = numbered.group(1) if numbered else line
        match = _REQ_PREFIX.match(candidate)
        if not match:
            continue
        explicit, body = match.groups()
        body = _normalize_text(body)
        if len(body) < 8:
            continue
        if not explicit and not numbered and not re.search(r"\b(shall|must|should|required|requirement|will)\b", body, flags=re.IGNORECASE):
            continue
        chunks = [body]
        if not explicit and len(body) > 500:
            chunks = [chunk for chunk in _SENTENCE_SPLIT.split(body) if len(chunk.strip()) >= 8]
        for offset, chunk in enumerate(chunks, start=1):
            locator = f"line {line_no}" if len(chunks) == 1 else f"line {line_no} sentence {offset}"
            req_id = _stable_id(source_sha, locator, chunk, explicit if offset == 1 else None)
            key = chunk.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(
                PLCRequirement(
                    req_id,
                    chunk,
                    str(path),
                    locator,
                    source_sha,
                    RequirementVerificationMode.DYNAMIC,
                )
            )
    return result


def _requirements_from_csv(path: Path, text: str, source_sha: str) -> list[PLCRequirement]:
    reader = csv.DictReader(io.StringIO(text))
    result: list[PLCRequirement] = []
    for row_no, row in enumerate(reader, start=2):
        lowered = {str(k or "").strip().casefold(): str(v or "").strip() for k, v in row.items()}
        body = next((lowered[key] for key in ("requirement", "text", "description", "shall") if lowered.get(key)), "")
        if not body:
            continue
        explicit = next((lowered[key] for key in ("id", "requirement_id", "req_id") if lowered.get(key)), None)
        raw_mode = next(
            (lowered[key] for key in ("verification_mode", "verification", "mode") if lowered.get(key)),
            None,
        )
        locator = f"row {row_no}"
        result.append(
            PLCRequirement(
                _stable_id(source_sha, locator, body, explicit),
                _normalize_text(body),
                str(path),
                locator,
                source_sha,
                _verification_mode(raw_mode, source=f"{path}:{locator}"),
            )
        )
    return result


def _requirements_from_json(path: Path, text: str, source_sha: str) -> list[PLCRequirement]:
    loaded = json.loads(text)
    if isinstance(loaded, dict):
        loaded = loaded.get("requirements", loaded.get("items", []))
    if not isinstance(loaded, list):
        raise ValueError(f"Requirement JSON must contain a list or a requirements list: {path}")
    result: list[PLCRequirement] = []
    for index, item in enumerate(loaded, start=1):
        explicit = None
        raw_mode: object | None = None
        if isinstance(item, str):
            body = item
        elif isinstance(item, dict):
            body = str(item.get("text") or item.get("requirement") or item.get("description") or "")
            explicit = str(item.get("id") or item.get("requirement_id") or "") or None
            raw_mode = item.get("verification_mode", item.get("mode"))
        else:
            continue
        body = _normalize_text(body)
        if not body:
            continue
        locator = f"item {index}"
        result.append(
            PLCRequirement(
                _stable_id(source_sha, locator, body, explicit),
                body,
                str(path),
                locator,
                source_sha,
                _verification_mode(raw_mode, source=f"{path}:{locator}"),
            )
        )
    return result


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > _MAX_DOCX_XML_BYTES:
            raise ValueError(f"DOCX requirement XML exceeds {_MAX_DOCX_XML_BYTES} bytes: {path}")
        payload = archive.read(info)
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError(f"DOCX requirement XML contains DTD/entity declarations: {path}")
    root = ET.fromstring(payload)
    paragraphs: list[str] = []
    for paragraph in root.iter():
        if paragraph.tag.rsplit("}", 1)[-1] != "p":
            continue
        values = [node.text or "" for node in paragraph.iter() if node.tag.rsplit("}", 1)[-1] == "t"]
        text = _normalize_text("".join(values))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:
        raise ValueError("PDF requirement ingestion needs optional package 'pypdf'; convert to TXT/MD/CSV/JSON/DOCX or install pypdf") from exc
    reader = PdfReader(str(path))
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        value = page.extract_text() or ""
        total += len(value)
        if total > _MAX_EXTRACTED_TEXT:
            raise ValueError(f"Extracted PDF requirement text exceeds {_MAX_EXTRACTED_TEXT} characters: {path}")
        parts.append(value)
    return "\n".join(parts)


def ingest_requirements(paths: list[Path] | tuple[Path, ...]) -> list[PLCRequirement]:
    result: list[PLCRequirement] = []
    ids: set[str] = set()
    for supplied in paths:
        path = supplied.expanduser().resolve(strict=True)
        size = path.stat().st_size
        if size > _MAX_REQUIREMENT_BYTES:
            raise ValueError(f"Requirement artifact exceeds {_MAX_REQUIREMENT_BYTES} bytes: {path}")
        source_sha = _sha256(path)
        suffix = path.suffix.casefold()
        if suffix == ".docx":
            items = _requirements_from_lines(path, _docx_text(path), source_sha)
        elif suffix == ".pdf":
            items = _requirements_from_lines(path, _pdf_text(path), source_sha)
        else:
            text = path.read_text(encoding="utf-8-sig")
            if suffix == ".csv":
                items = _requirements_from_csv(path, text, source_sha)
            elif suffix == ".json":
                items = _requirements_from_json(path, text, source_sha)
            elif suffix in {".txt", ".md", ".rst", ""}:
                items = _requirements_from_lines(path, text, source_sha)
            else:
                raise ValueError(f"Unsupported requirement artifact type {suffix or '<none>'}: {path}")
        for item in items:
            req_id = item.id
            if req_id in ids:
                digest = hashlib.sha1(f"{item.source_sha256}:{item.source_locator}:{item.text}".encode()).hexdigest()[:6].upper()
                item = PLCRequirement(
                    f"{req_id}-{digest}",
                    item.text,
                    item.source_path,
                    item.source_locator,
                    item.source_sha256,
                    item.verification_mode,
                )
            ids.add(item.id)
            result.append(item)
            if len(result) > _MAX_REQUIREMENTS:
                raise ValueError(f"Requirement count exceeds production limit {_MAX_REQUIREMENTS}")
    return result
