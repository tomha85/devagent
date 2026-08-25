from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from devagent.models import Capability, CapabilityProvenance, Component, RepositoryFact, RepositoryModel
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
    ".fs": "f#",
    ".vb": "visual-basic",
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
    "mvnw",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
    "cmakelists.txt",
    "makefile",
    "meson.build",
    "dockerfile",
    "jenkinsfile",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
}
_PROJECT_SUFFIXES = {".sln", ".slnx", ".csproj", ".fsproj", ".vbproj"}


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path, limit: int = 12_000) -> list[Path]:
    """Walk deterministically while pruning generated/vendor trees before descent."""
    files: list[Path] = []
    maximum = min(max(0, int(limit)), 12_000)
    if maximum == 0:
        return files
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_current = current_path.relative_to(root)
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in SKIP_DIRECTORIES
            and not is_secret_path(relative_current / directory)
        )
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(root)
            try:
                resolved = path.resolve()
                resolved_relative = resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if (
                resolved.is_file()
                and not is_secret_path(relative)
                and not is_secret_path(resolved_relative)
            ):
                files.append(path)
                if len(files) >= maximum:
                    return files
    return files


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _is_safe_generated_untracked_path(path: str) -> bool:
    parts = Path(path).parts
    return (
        path == ".devagent"
        or path.startswith(".devagent/")
        or "__pycache__" in parts
        or ".pytest_cache" in parts
        or path.endswith(".pyc")
    )


def _is_manifest(path: Path) -> bool:
    return path.name.lower() in _MANIFESTS or path.suffix.lower() in _PROJECT_SUFFIXES


def _is_ci_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return relative.startswith(".github/workflows/") or path.name.lower() in {
        "jenkinsfile",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
    }


def _priority_repository_files(root: Path, limit: int = 20_000) -> list[Path]:
    """Recover high-value tracked manifests/CI files even after the bounded walk is full.

    A huge monorepo can contain more than the normal 12k inventory budget before a deep
    component is reached. Git's index is a cheap authoritative directory of tracked paths;
    we inspect only manifest/project/CI candidates and keep the result bounded.
    """
    listed = _git(root, "ls-files")
    if listed is None:
        return []
    priority: list[Path] = []
    for raw in listed.splitlines():
        if len(priority) >= limit:
            break
        if not raw or "\x00" in raw:
            continue
        relative = Path(raw)
        if any(part in SKIP_DIRECTORIES for part in relative.parts) or is_secret_path(relative):
            continue
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved_relative = resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not resolved.is_file() or is_secret_path(resolved_relative):
            continue
        if _is_manifest(candidate) or _is_ci_file(candidate, root):
            priority.append(candidate)
    return priority


def _manifest_languages(path: Path) -> set[str]:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in {"pom.xml", "mvnw", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradlew"}:
        return {"java"}
    if suffix == ".csproj":
        return {"c#"}
    if suffix == ".fsproj":
        return {"f#"}
    if suffix == ".vbproj":
        return {"visual-basic"}
    return set()


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


def _java_maven_command(path: Path, root: Path, component: str) -> tuple[str, ...]:
    wrapper = path.parent / "mvnw"
    if wrapper.is_file():
        executable = "./mvnw" if component == "." else f"./{component}/mvnw"
    else:
        executable = "mvn"
    return (executable, "test") if component == "." else (executable, "-f", path.relative_to(root).as_posix(), "test")


def _dotnet_capabilities(path: Path, root: Path, component: str) -> list[Capability]:
    relative = path.relative_to(root).as_posix()
    suffix = path.suffix.lower()
    capabilities: list[Capability] = []
    if suffix in {".sln", ".slnx"}:
        capabilities.append(Capability("build", ("dotnet", "build", relative), relative, component, True))
        return capabilities
    if suffix not in {".csproj", ".fsproj", ".vbproj"}:
        return capabilities

    text = path.read_text(encoding="utf-8", errors="ignore").lower()
    assets = path.parent / "obj" / "project.assets.json"
    no_restore = ("--no-restore",) if assets.is_file() else ()
    capabilities.append(
        Capability("build", ("dotnet", "build", relative, *no_restore), relative, component, True)
    )
    if "<istestproject>true" in text or "microsoft.net.test.sdk" in text:
        capabilities.append(
            Capability("test", ("dotnet", "test", relative, *no_restore), relative, component, False)
        )
    return capabilities


def _manifest_capabilities(path: Path, root: Path) -> tuple[list[str], list[Capability]]:
    relative = path.relative_to(root).as_posix()
    name = path.name.lower()
    suffix = path.suffix.lower()
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
        frameworks.append("maven")
        capabilities.append(Capability("test", _java_maven_command(path, root, component), relative, component))
    elif name in {"build.gradle", "build.gradle.kts", "gradlew"}:
        frameworks.append("gradle")
        executable = "./gradlew" if component == "." and (path.parent / "gradlew").exists() else (f"./{component}/gradlew" if (path.parent / "gradlew").exists() else "gradle")
        command = (executable, "test") if component == "." else (executable, "-p", component, "test")
        capabilities.append(Capability("test", command, relative, component))
    elif suffix in _PROJECT_SUFFIXES:
        frameworks.append("dotnet")
        capabilities.extend(_dotnet_capabilities(path, root, component))
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
    allowed = {"python", "python3", "pytest", "npm", "pnpm", "yarn", "go", "cargo", "mvn", "gradle", "./gradlew", "./mvnw", "make", "ctest", "dotnet", "ruff", "mypy"}
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


def _python_test_files(files: Iterable[Path], component: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in files:
        if path.suffix.casefold() != ".py":
            continue
        try:
            relative = path.relative_to(component)
        except ValueError:
            continue
        lowered_parts = tuple(part.casefold() for part in relative.parts)
        name = path.name.casefold()
        if (
            any(part in {"test", "tests"} for part in lowered_parts)
            or name.startswith("test_")
            or name.endswith("_test.py")
        ):
            candidates.append(path)
    return sorted(candidates)


def _probe_environment(home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "SYSTEMROOT", "WINDIR"}
    }
    environment.update(
        {
            "HOME": str(home),
            "CI": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    return environment


def _run_probe(root: Path, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
    try:
        with tempfile.TemporaryDirectory(prefix="devagent-probe-") as temporary:
            return subprocess.run(
                argv,
                cwd=root,
                env=_probe_environment(Path(temporary)),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _pytest_collected_count(output: str) -> int:
    matches = re.findall(r"(\d+)\s+tests?\s+collected", output, re.IGNORECASE)
    if matches:
        return int(matches[-1])
    node_ids = [line for line in output.splitlines() if "::" in line and not line.lstrip().startswith(("<", "="))]
    return len(node_ids)


def _uses_unittest_conventions(paths: Iterable[Path]) -> bool:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r"(?:from\s+unittest\s+import|import\s+unittest|unittest\.TestCase)", text):
            return True
    return False


def _probe_python_test_capability(
    root: Path, component: str, test_files: list[Path]
) -> tuple[Capability | None, list[str]]:
    if not test_files:
        return None, []
    relative_tests = [path.relative_to(root).as_posix() for path in test_files]
    target = () if component == "." else (component,)
    diagnostics = [f"pytest candidate detected from {len(test_files)} Python test file(s)"]
    pytest_probe = (
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        *target,
    )
    completed = _run_probe(root, pytest_probe)
    collected = _pytest_collected_count(
        f"{completed.stdout}\n{completed.stderr}" if completed is not None else ""
    )
    if completed is not None and completed.returncode == 0 and collected > 0:
        command = ("python", "-m", "pytest", "-q", *target)
        detail = (
            f"probed from Python test files and successful pytest collection ({collected} collected)"
        )
        diagnostics.append(
            f"pytest collection probe: PASS; tests collected: {collected}; capability promoted"
        )
        return (
            Capability(
                "test",
                command,
                relative_tests[0],
                component,
                False,
                CapabilityProvenance.PROBED,
                detail,
                collected,
            ),
            diagnostics,
        )
    result = "unavailable" if completed is None else f"exit {completed.returncode}"
    diagnostics.append(f"pytest collection probe: not promoted ({result})")
    if not _uses_unittest_conventions(test_files):
        return None, diagnostics

    diagnostics.append("unittest candidate detected from explicit unittest conventions")
    unittest_command = (
        ("python", "-m", "unittest", "discover")
        if component == "."
        else ("python", "-m", "unittest", "discover", "-s", component)
    )
    unittest_probe = (
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-v",
        *(("-s", component) if component != "." else ()),
    )
    completed = _run_probe(root, unittest_probe)
    output = f"{completed.stdout}\n{completed.stderr}" if completed is not None else ""
    match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    discovered = int(match.group(1)) if match else 0
    if completed is not None and completed.returncode in {0, 1} and discovered > 0:
        detail = (
            f"probed from unittest conventions and local discovery ({discovered} discovered)"
        )
        diagnostics.append(
            f"unittest discovery probe: PASS; tests discovered: {discovered}; capability promoted"
        )
        return (
            Capability(
                "test",
                unittest_command,
                relative_tests[0],
                component,
                False,
                CapabilityProvenance.PROBED,
                detail,
                discovered,
            ),
            diagnostics,
        )
    diagnostics.append("unittest discovery probe: not promoted")
    return None, diagnostics


def discover_repository(root: Path | str, *, probe_capabilities: bool = True) -> RepositoryModel:
    root_path = Path(root).expanduser().resolve()
    walked_files = _walk(root_path)
    files = list(walked_files)
    seen = {path.relative_to(root_path).as_posix() for path in files}
    for path in _priority_repository_files(root_path):
        relative = path.relative_to(root_path).as_posix()
        if relative not in seen:
            files.append(path)
            seen.add(relative)

    ci_files = [path for path in files if _is_ci_file(path, root_path)]
    manifests = [path for path in files if _is_manifest(path)]
    component_paths = {root_path}
    for manifest in manifests:
        if manifest.parent != root_path and manifest not in ci_files:
            component_paths.add(manifest.parent)
    components: list[Component] = []
    fact_sources: dict[str, set[str]] = {}
    capability_diagnostics: list[str] = []

    for component_path in sorted(component_paths):
        owned_files = [path for path in files if path == component_path or component_path in path.parents]
        language_counts = Counter(_LANGUAGE_EXTENSIONS[path.suffix.lower()] for path in owned_files if path.suffix.lower() in _LANGUAGE_EXTENSIONS)
        component_manifests = [path for path in manifests if path.parent == component_path]
        inferred_languages = set().union(*(_manifest_languages(path) for path in component_manifests)) if component_manifests else set()
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
        if probe_capabilities and not any(capability.kind == "test" and capability.trusted for capability in capabilities):
            probed, diagnostics = _probe_python_test_capability(
                root_path,
                relative_component,
                _python_test_files(owned_files, component_path),
            )
            capability_diagnostics.extend(
                f"{relative_component}: {diagnostic}" for diagnostic in diagnostics
            )
            if probed is not None:
                capabilities.append(probed)
                frameworks.append("pytest" if "pytest" in probed.command else "unittest")
        ordered_languages = [name for name, _ in language_counts.most_common()]
        for language in sorted(inferred_languages):
            if language not in ordered_languages:
                ordered_languages.append(language)
        component = Component(
            path=relative_component,
            languages=ordered_languages,
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

    tracked_dirty = {
        path
        for command in (("diff", "--name-only"), ("diff", "--cached", "--name-only"))
        for path in (_git(root_path, *command) or "").splitlines()
        if path
    }
    untracked_dirty = {
        path
        for path in (
            _git(root_path, "ls-files", "--others", "--exclude-standard") or ""
        ).splitlines()
        if path and not _is_safe_generated_untracked_path(path)
    }
    return RepositoryModel(
        root=str(root_path),
        kind="monorepo" if len(component_paths) > 1 else "single-component",
        components=components,
        facts=facts,
        git_branch=_git(root_path, "branch", "--show-current"),
        git_head=_git(root_path, "rev-parse", "HEAD"),
        dirty_files=sorted(tracked_dirty | untracked_dirty),
        inventory_file_count=len(walked_files),
        capability_diagnostics=capability_diagnostics,
    )


def facts_are_current(root: Path | str, facts: Iterable[RepositoryFact]) -> bool:
    root_path = Path(root).resolve()
    for fact in facts:
        for relative, expected in fact.fingerprints.items():
            path = root_path / relative
            if not path.is_file() or _fingerprint(path) != expected:
                return False
    return True
