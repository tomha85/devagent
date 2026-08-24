from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from devagent.models import Capability, Component, RepositoryFact, RepositoryModel
from devagent.safety import SKIP_DIRECTORIES, is_secret_path


_LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".cc": "c++",
    ".cpp": "c++",
    ".h": "c/c++",
    ".cs": "c#",
    ".rb": "ruby",
    ".php": "php",
}

_MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "pytest.ini",
    "package.json",
    "tsconfig.json",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "gradlew",
    "cmakelists.txt",
    "makefile",
    "meson.build",
    "dockerfile",
    "jenkinsfile",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
}


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path, limit: int = 12_000) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file() and not is_secret_path(relative):
            files.append(path)
            if len(files) >= limit:
                break
    return files


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _package_capabilities(path: Path, relative: str) -> tuple[list[str], list[Capability]]:
    frameworks: list[str] = []
    capabilities: list[Capability] = []
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return frameworks, capabilities
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    for name, framework in (("react", "react"), ("next", "next.js"), ("vite", "vite"), ("vitest", "vitest"), ("jest", "jest"), ("playwright", "playwright"), ("cypress", "cypress")):
        if name in dependencies:
            frameworks.append(framework)
    scripts = package.get("scripts", {})
    preferred = (
        ("test:unit", "test", False),
        ("test", "test", False),
        ("test:e2e", "integration", True),
        ("lint", "lint", True),
        ("typecheck", "typecheck", True),
        ("build", "build", True),
    )
    component = Path(relative).parent.as_posix()
    for script, kind, broad in preferred:
        if script in scripts:
            command = ("npm", "run", script) if component == "." else ("npm", "--prefix", component, "run", script)
            capabilities.append(Capability(kind, command, relative, component, broad))
    return sorted(set(frameworks)), capabilities


def _manifest_capabilities(path: Path, root: Path) -> tuple[list[str], list[Capability]]:
    relative = path.relative_to(root).as_posix()
    name = path.name.lower()
    frameworks: list[str] = []
    capabilities: list[Capability] = []
    component = path.parent.relative_to(root).as_posix() or "."
    if name == "package.json":
        return _package_capabilities(path, relative)
    if name in {"pyproject.toml", "pytest.ini"}:
        content = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "pytest" in content:
            frameworks.append("pytest")
            command = ("python", "-m", "pytest", "-q") if component == "." else ("python", "-m", "pytest", "-q", component)
            capabilities.append(Capability("test", command, relative, component, False))
        if "ruff" in content:
            capabilities.append(Capability("lint", ("python", "-m", "ruff", "check", "."), relative, component, True))
        if "mypy" in content:
            capabilities.append(Capability("typecheck", ("python", "-m", "mypy", "."), relative, component, True))
    elif name == "requirements.txt":
        content = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "pytest" in content:
            frameworks.append("pytest")
            command = ("python", "-m", "pytest", "-q") if component == "." else ("python", "-m", "pytest", "-q", component)
            capabilities.append(Capability("test", command, relative, component, False))
    elif name == "cargo.toml":
        manifest_args = () if component == "." else ("--manifest-path", relative)
        capabilities.extend((Capability("test", ("cargo", "test", *manifest_args), relative, component), Capability("build", ("cargo", "check", *manifest_args), relative, component, True)))
    elif name == "go.mod":
        command = ("go", "test", "./...") if component == "." else ("go", "-C", component, "test", "./...")
        capabilities.append(Capability("test", command, relative, component))
    elif name == "pom.xml":
        command = ("mvn", "test") if component == "." else ("mvn", "-f", relative, "test")
        capabilities.append(Capability("test", command, relative, component))
    elif name in {"build.gradle", "gradlew"}:
        executable = "./gradlew" if component == "." and (path.parent / "gradlew").exists() else (f"./{component}/gradlew" if (path.parent / "gradlew").exists() else "gradle")
        command = (executable, "test") if component == "." else (executable, "-p", component, "test")
        capabilities.append(Capability("test", command, relative, component))
    elif name == "cmakelists.txt":
        build_dir = "build" if component == "." else f"{component}/build"
        capabilities.append(Capability("test", ("ctest", "--test-dir", build_dir), relative, component))
    elif name == "makefile":
        content = path.read_text(encoding="utf-8", errors="ignore")
        if any(line.startswith("test:") for line in content.splitlines()):
            command = ("make", "test") if component == "." else ("make", "-C", component, "test")
            capabilities.append(Capability("test", command, relative, component))
    return frameworks, capabilities


def _ci_capabilities(path: Path, root: Path) -> list[Capability]:
    """Extract simple argv commands from CI run/command lines as high-value evidence."""
    relative = path.relative_to(root).as_posix()
    commands: list[Capability] = []
    allowed = {"python", "python3", "pytest", "npm", "pnpm", "yarn", "go", "cargo", "mvn", "gradle", "./gradlew", "make", "ctest", "dotnet", "ruff", "mypy"}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = raw_line.strip()
        value = ""
        if stripped.startswith("run:"):
            value = stripped[4:].strip()
        elif stripped.startswith("- run:"):
            value = stripped[6:].strip()
        elif stripped.startswith("command:"):
            value = stripped[8:].strip()
        if not value or value in {"|", ">"}:
            continue
        try:
            argv = tuple(shlex.split(value))
        except ValueError:
            continue
        if not argv or argv[0] not in allowed or any(token in argv for token in ("|", "&&", ";")):
            continue
        lowered = " ".join(argv).lower()
        if "lint" in lowered:
            kind, broad = "lint", True
        elif any(term in lowered for term in ("build", "compile", "check")) and "test" not in lowered:
            kind, broad = "build", True
        elif any(term in lowered for term in ("e2e", "integration")):
            kind, broad = "integration", True
        elif "test" in lowered or "pytest" in lowered or "ctest" in lowered:
            kind, broad = "test", False
        else:
            continue
        commands.append(Capability(kind, argv, relative, ".", broad))
    return commands


def _test_locations(files: Iterable[Path], root: Path, component: Path) -> list[str]:
    locations: set[str] = set()
    for path in files:
        try:
            relative_component = path.relative_to(component)
        except ValueError:
            continue
        parts = relative_component.parts
        if any(part.lower() in {"test", "tests", "spec", "specs", "__tests__"} for part in parts):
            locations.add(path.parent.relative_to(root).as_posix())
        elif path.name.startswith("test_") or ".test." in path.name or ".spec." in path.name:
            locations.add(path.parent.relative_to(root).as_posix() or ".")
    return sorted(locations)[:30]


def discover_repository(root: Path | str) -> RepositoryModel:
    root_path = Path(root).expanduser().resolve()
    files = _walk(root_path)
    ci_files = [
        path
        for path in files
        if path.relative_to(root_path).as_posix().startswith(".github/workflows/")
        or path.name.lower() in {"jenkinsfile", ".gitlab-ci.yml", "azure-pipelines.yml"}
    ]
    manifests = [path for path in files if path.name.lower() in _MANIFESTS or path.suffix.lower() in {".sln", ".csproj"}]
    component_paths = {root_path}
    for manifest in manifests:
        if manifest.parent != root_path and manifest not in ci_files:
            component_paths.add(manifest.parent)
    components: list[Component] = []
    fact_sources: dict[str, set[str]] = {}

    for component_path in sorted(component_paths):
        owned_files = [path for path in files if path == component_path or component_path in path.parents]
        language_counts = Counter(_LANGUAGE_EXTENSIONS[path.suffix.lower()] for path in owned_files if path.suffix.lower() in _LANGUAGE_EXTENSIONS)
        component_manifests = [path for path in manifests if path.parent == component_path]
        frameworks: list[str] = []
        capabilities: list[Capability] = []
        for manifest in component_manifests:
            detected_frameworks, detected_capabilities = _manifest_capabilities(manifest, root_path)
            frameworks.extend(detected_frameworks)
            capabilities.extend(detected_capabilities)
        if component_path == root_path:
            for ci_file in ci_files:
                capabilities.extend(_ci_capabilities(ci_file, root_path))
        relative_component = component_path.relative_to(root_path).as_posix() or "."
        component = Component(
            path=relative_component,
            languages=[name for name, _ in language_counts.most_common()],
            frameworks=sorted(set(frameworks)),
            manifests=[path.relative_to(root_path).as_posix() for path in component_manifests],
            test_locations=_test_locations(owned_files, root_path, component_path),
            capabilities=list(dict.fromkeys(capabilities)),
        )
        components.append(component)
        for language in component.languages:
            fact_sources.setdefault(f"Component {relative_component} uses {language}", set()).update(component.manifests or [relative_component])
        for capability in component.capabilities:
            fact_sources.setdefault(f"{capability.kind.title()} command: {' '.join(capability.command)}", set()).add(capability.source)

    now = datetime.now(timezone.utc).isoformat()
    facts: list[RepositoryFact] = []
    for statement, sources in sorted(fact_sources.items()):
        fingerprints = {}
        for source in sources:
            source_path = root_path / source
            if source_path.is_file():
                fingerprints[source] = _fingerprint(source_path)
        facts.append(RepositoryFact(statement, 1.0, tuple(sorted(sources)), fingerprints, now))

    dirty = {
        path
        for command in (("diff", "--name-only"), ("diff", "--cached", "--name-only"), ("ls-files", "--others", "--exclude-standard"))
        for path in (_git(root_path, *command) or "").splitlines()
        if path and not path.startswith(".devagent/")
    }
    return RepositoryModel(
        root=str(root_path),
        kind="monorepo" if len(component_paths) > 1 else "single-component",
        components=components,
        facts=facts,
        git_branch=_git(root_path, "branch", "--show-current"),
        git_head=_git(root_path, "rev-parse", "HEAD"),
        dirty_files=sorted(dirty),
    )


def facts_are_current(root: Path | str, facts: Iterable[RepositoryFact]) -> bool:
    root_path = Path(root).resolve()
    for fact in facts:
        for relative, expected in fact.fingerprints.items():
            path = root_path / relative
            if not path.is_file() or _fingerprint(path) != expected:
                return False
    return True
