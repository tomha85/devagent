# Structural actions

`rename_file`, `move_file`, and `delete_file` are first-class structured implementation actions in v0.7. They are accepted only when their paths are present in the evidence-backed engineering plan. Renames and moves require both source and destination to be planned, while delete requires the exact target path.

The Workspace implementation performs backup-before-mutation and refuses path escapes, secret paths, symlinks, dirty developer changes, directories, and destination overwrite.
