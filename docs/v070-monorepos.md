# Large-monorepo discovery in v0.7

DevAgent keeps recursive repository inventory bounded to prevent pathological scans. v0.7 supplements that bounded walk with a second, bounded Git-index pass that considers only tracked high-value manifest, project, and CI paths. This preserves the scan budget while allowing components located after the normal 12,000-file frontier to contribute languages and repository-native verification capabilities.
