# Migration verification in v0.7

For migration tasks, DevAgent keeps the existing high-risk acceptance contract: compatibility must be preserved, a forward and rollback (or explicitly safe non-reversible) strategy must be present, and representative existing state must be exercised.

The v0.7 qualification fixture uses Python's SQLite driver to create an existing users row, apply a nullable-column migration, prove the existing row survives, write data using the expanded schema, execute rollback, and prove the supported original columns and data remain intact.
