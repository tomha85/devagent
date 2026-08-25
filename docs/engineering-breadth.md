# Engineering breadth in DevAgent v0.7

DevAgent v0.7 expands the deterministic engineering surface without weakening the existing evidence and safety gates.

## Structural refactoring

The structured implementation contract now supports `delete_file`, `move_file`, and `rename_file` in addition to text replacement and file creation. Structural operations are constrained to planned repository paths, back up source content before mutation, reject pre-existing developer changes, refuse overwrite of an existing destination, reject symlink traversal, and remain inside the selected workspace.

## Java and .NET

Repository discovery recognizes Maven, Maven Wrapper, Gradle, Gradle Kotlin DSL, .NET solutions, and C#/F#/Visual Basic project files. Production qualification executes a real Maven Java test project and a real .NET build on the GitHub runner rather than treating manifest recognition alone as proof of support.

## Database migrations

Migration capability is qualified with a real SQLite schema change. The fixture verifies a forward migration, preservation of representative existing rows, and an explicit rollback path that restores the original supported schema.

## Huge monorepositories

The normal recursive inventory remains bounded at 12,000 files. When that frontier is reached, DevAgent additionally consults the Git index for tracked high-value manifests and CI files. This lets deep components remain discoverable without turning repository discovery into an unbounded full-content scan.

## Qualification meaning

A green v0.7 Production Qualification means every cataloged v0.7 case passed, including the real structural-refactor, Java, .NET, migration, sandbox, and large-repository fixtures. It is not a claim that every possible third-party project or migration framework is automatically supported.
