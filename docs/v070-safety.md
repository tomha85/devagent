# v0.7 structural safety contract

Structural file operations are deterministic harness operations, not shell commands. A move, rename, or delete must target an explicitly planned repository path. The harness rejects workspace escapes, sensitive paths, symlink traversal, directory targets, dirty developer files, and destination overwrite. Existing source content is copied into RunArtifacts backups before mutation.

These constraints are deliberately stricter than ordinary filesystem tools because structural refactors can otherwise destroy or silently replace developer work.
