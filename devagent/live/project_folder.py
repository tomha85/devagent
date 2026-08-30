from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from devagent.plc.plc_dispatch import detect_plc_vendor


class LiveProjectFileKind(str, Enum):
    PLC_ENGINEERING = "PLC_ENGINEERING"
    IO_LIST = "IO_LIST"
    TAG_DESCRIPTION = "TAG_DESCRIPTION"
    REQUIREMENTS = "REQUIREMENTS"
    FAT_TEST = "FAT_TEST"
    DRAWING = "DRAWING"
    SUPPLEMENTAL = "SUPPLEMENTAL"
    OTHER = "OTHER"


@dataclass(frozen=True)
class LiveProjectFolderFile:
    relative_path: str
    kind: LiveProjectFileKind
    suffix: str
    size_bytes: int


@dataclass(frozen=True)
class LiveProjectFolderIntake:
    root: Path
    primary_project: Path
    vendor: str
    files: tuple[LiveProjectFolderFile, ...]
    warnings: tuple[str, ...] = ()

    @property
    def supplemental_files(self) -> tuple[LiveProjectFolderFile, ...]:
        primary_relative = None
        try:
            if self.primary_project.is_file():
                primary_relative = self.primary_project.relative_to(self.root).as_posix()
        except ValueError:
            primary_relative = None
        return tuple(
            item
            for item in self.files
            if item.relative_path != primary_relative
            and item.kind is not LiveProjectFileKind.PLC_ENGINEERING
        )

    def counts_by_kind(self) -> dict[LiveProjectFileKind, int]:
        result = {kind: 0 for kind in LiveProjectFileKind}
        for item in self.files:
            result[item.kind] += 1
        return result

    def render_text(self) -> str:
        counts = self.counts_by_kind()
        lines = [
            "DEVAGENT LIVE PROJECT WORKSPACE",
            f"Workspace root: {self.root}",
            f"Authoritative PLC engineering input: {self.primary_project}",
            f"Detected vendor: {self.vendor}",
            f"Files discovered: {len(self.files)}",
            f"Supplemental files: {len(self.supplemental_files)}",
            "Context inventory:",
        ]
        for kind in LiveProjectFileKind:
            count = counts[kind]
            if count:
                lines.append(f"- {kind.value}: {count}")
        if self.files:
            lines.append("Files:")
            for item in self.files[:40]:
                lines.append(
                    f"- [{item.kind.value}] {item.relative_path} ({item.size_bytes} bytes)"
                )
            if len(self.files) > 40:
                lines.append(f"- ... {len(self.files) - 40} more")
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"- {item}" for item in self.warnings)
        lines.extend(
            [
                "Authority boundary:",
                "- Canonical PLC logic comes only from the authoritative supported engineering export.",
                "- I/O lists, tag descriptions, requirements, FAT files, drawings, and other documents are supplemental context and must not override PLC logic semantics.",
                "- Live runtime conclusions still require safely reconciled trusted CURRENT OPC UA evidence.",
            ]
        )
        return "\n".join(lines)


_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

_DRAWING_SUFFIXES = {".pdf", ".dwg", ".dxf"}
_SUPPLEMENTAL_SUFFIXES = {
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".xlsx",
    ".xls",
    ".docx",
    ".pdf",
}


def _name_tokens(path: Path) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", path.stem.casefold())
        if token
    }


def _classify_file(path: Path, *, primary_project: Path) -> LiveProjectFileKind:
    try:
        if primary_project.is_file() and path.resolve() == primary_project.resolve():
            return LiveProjectFileKind.PLC_ENGINEERING
    except OSError:
        pass

    tokens = _name_tokens(path)
    compact = "".join(sorted(tokens))
    suffix = path.suffix.casefold()

    if (
        "io" in tokens
        or "iolist" in compact
        or {"input", "output"}.issubset(tokens)
        or "pointlist" in compact
    ):
        return LiveProjectFileKind.IO_LIST
    if (
        "tag" in tokens
        or "tags" in tokens
        or "symbol" in tokens
        or "symbols" in tokens
    ) and (
        "description" in tokens
        or "descriptions" in tokens
        or "list" in tokens
        or "table" in tokens
        or suffix in {".csv", ".tsv", ".xlsx", ".xls"}
    ):
        return LiveProjectFileKind.TAG_DESCRIPTION
    if tokens & {"requirement", "requirements", "urs", "fds", "sds", "spec", "specification"}:
        return LiveProjectFileKind.REQUIREMENTS
    if tokens & {"fat", "sat", "test", "tests", "commissioning", "verification"}:
        return LiveProjectFileKind.FAT_TEST
    if suffix in _DRAWING_SUFFIXES or tokens & {"drawing", "drawings", "schematic", "schematics"}:
        return LiveProjectFileKind.DRAWING
    if suffix in _SUPPLEMENTAL_SUFFIXES:
        return LiveProjectFileKind.SUPPLEMENTAL
    return LiveProjectFileKind.OTHER


def _safe_files(root: Path, *, max_files: int) -> tuple[Path, ...]:
    files: list[Path] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        if not candidate.is_file() or candidate.is_symlink():
            continue
        files.append(candidate)
        if len(files) > max_files:
            raise ValueError(
                f"Project folder contains more than the Live V1 limit of {max_files} files. "
                "Use a smaller engineering workspace or point --project-folder at the PLC-specific folder."
            )
    return tuple(files)


def _inside_root(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _explicit_primary(root: Path, value: Path) -> tuple[Path, str]:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve(strict=False)
    if not _inside_root(root, candidate):
        raise ValueError("--primary-project must resolve inside --project-folder")
    if not candidate.exists():
        raise ValueError(f"Primary PLC engineering input does not exist: {candidate}")
    vendor = detect_plc_vendor(candidate)
    return candidate, vendor


def _auto_primary(root: Path, files: tuple[Path, ...]) -> tuple[Path, str]:
    rockwell = tuple(path for path in files if path.suffix.casefold() == ".l5x")

    directory_vendor: str | None = None
    directory_error: str | None = None
    try:
        directory_vendor = detect_plc_vendor(root)
    except ValueError as exc:
        directory_error = str(exc)

    if rockwell and directory_vendor in {"SIEMENS", "SCHNEIDER"}:
        examples = ", ".join(path.relative_to(root).as_posix() for path in rockwell[:5])
        raise ValueError(
            "Project folder contains multiple vendor engineering surfaces: "
            f"{directory_vendor} bundle plus Rockwell .L5X ({examples}). "
            "Separate vendor projects or choose --primary-project explicitly."
        )
    if len(rockwell) == 1 and directory_vendor is None:
        return rockwell[0], "ROCKWELL"
    if len(rockwell) > 1 and directory_vendor is None:
        examples = ", ".join(path.relative_to(root).as_posix() for path in rockwell[:12])
        raise ValueError(
            "Project folder contains multiple Rockwell .L5X candidates and Live will not guess "
            f"which one is authoritative: {examples}. Use --primary-project <relative-path>."
        )
    if directory_vendor is not None:
        return root, directory_vendor

    # Last bounded attempt for a single supported file export such as a nested .XEF.
    detected: list[tuple[Path, str]] = []
    for path in files:
        if path.suffix.casefold() in _SUPPLEMENTAL_SUFFIXES and path.suffix.casefold() not in {".xml"}:
            continue
        try:
            vendor = detect_plc_vendor(path)
        except ValueError:
            continue
        detected.append((path, vendor))
    unique = {(path.resolve(strict=False), vendor) for path, vendor in detected}
    if len(unique) == 1:
        path, vendor = next(iter(unique))
        return path, vendor
    if len(unique) > 1:
        examples = ", ".join(
            f"{path.relative_to(root).as_posix()} ({vendor})"
            for path, vendor in detected[:12]
        )
        raise ValueError(
            "Project folder contains multiple supported PLC engineering candidates and Live "
            f"will not select one implicitly: {examples}. Use --primary-project."
        )

    detail = f" Last detector detail: {directory_error}" if directory_error else ""
    raise ValueError(
        "No supported authoritative PLC engineering export was found in the project folder. "
        "Expected Rockwell .L5X, Siemens TIA exported source/XML bundle, or Schneider Control Expert .XEF/X* export."
        + detail
    )


def inspect_live_project_folder(
    folder: Path,
    *,
    primary_project: Path | None = None,
    max_files: int = 1000,
) -> LiveProjectFolderIntake:
    root = folder.expanduser().resolve(strict=False)
    if not root.exists():
        raise ValueError(f"Project folder does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"--project-folder must be a directory: {root}")
    if max_files < 1:
        raise ValueError("max_files must be >= 1")

    files = _safe_files(root, max_files=max_files)
    if not files:
        raise ValueError(f"Project folder is empty: {root}")

    if primary_project is not None:
        primary, vendor = _explicit_primary(root, primary_project)
    else:
        primary, vendor = _auto_primary(root, files)

    inventory = tuple(
        LiveProjectFolderFile(
            relative_path=path.relative_to(root).as_posix(),
            kind=_classify_file(path, primary_project=primary),
            suffix=path.suffix.casefold(),
            size_bytes=path.stat().st_size,
        )
        for path in files
    )
    warnings: list[str] = []
    if not any(
        item.kind in {LiveProjectFileKind.IO_LIST, LiveProjectFileKind.TAG_DESCRIPTION}
        for item in inventory
    ):
        warnings.append(
            "No separate I/O-list or tag-description file was recognized; this is allowed when the PLC export itself carries sufficient tag metadata."
        )

    return LiveProjectFolderIntake(
        root=root,
        primary_project=primary,
        vendor=vendor,
        files=inventory,
        warnings=tuple(warnings),
    )


__all__ = [
    "LiveProjectFileKind",
    "LiveProjectFolderFile",
    "LiveProjectFolderIntake",
    "inspect_live_project_folder",
]
