# v0.7 qualification scope

The v0.7 release gate extends Production Qualification v4 while preserving the primary invariant `false_verified == 0`.

New required evidence covers:

- strict provider-contract support for structural rename, move, and delete actions;
- a full isolated-worktree structural refactor using rename and delete operations;
- Maven and Gradle Kotlin DSL discovery for Java repositories;
- real Maven/JUnit execution on the production runner;
- .NET solution/project and test-project discovery;
- a real package-free .NET build on the production runner;
- a real SQLite forward migration, representative existing-state preservation, and rollback;
- recovery of a tracked .NET manifest beyond the normal 12,000-file discovery frontier.

The release is mergeable only when the normal Python matrix, wheel smoke, real Linux sandbox smoke, and the complete Production Qualification catalog all pass on the exact PR head.
