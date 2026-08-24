# Sample Task

Repository:

```text
/path/to/your-app
```

Task:

```text
Fix the bug where the user ID is missing from the account details response. Keep the existing API contract, add or update tests, and verify the relevant test suite passes.
```

Example command:

```bash
python main.py fix \
  --repo /path/to/your-app \
  --task "Fix the bug where the user ID is missing from the account details response" \
  --provider claude \
  --max-steps 10
```

DevAgent will inspect the repository, make bounded edits with local backups under `.devagent/backups/`, run verification, and store local attempts/lessons under `.devagent/memory/`.
