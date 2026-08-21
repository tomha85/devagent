# DevAgent

A lightweight local AI development agent for fixing bugs and implementing bounded features in an existing application repository.

DevAgent points at a repository on your laptop and runs a simple engineering loop:

**Gather -> Act -> Verify**

It can inspect files, search code, make backed-up edits, run validation commands, remember recent attempts/lessons, and iterate with OpenAI, Claude, or Grok.

## Repository structure

```text
devagent/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── main.py
├── agent/
│   ├── __init__.py
│   ├── loop.py
│   ├── tools.py
│   ├── llm.py
│   ├── memory.py
│   └── prompts.py
└── examples/
    └── sample_task.md
```

## Features

- Local CLI powered by Typer + Rich
- OpenAI / Claude / Grok provider adapters
- Bounded Gather -> Act -> Verify loop
- Workspace path confinement
- Sensitive-file guards for common secret/key files
- Backup before replacing an existing file
- Blocks destructive Git/system/network shell commands
- Requires successful verification after the latest code modification before reporting completion
- Local attempts + lessons memory under `.devagent/`

## Install

```bash
git clone https://github.com/tomha85/devagent.git
cd devagent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and provide the API key for the provider you want to use:

```dotenv
DEFAULT_PROVIDER=claude

OPENAI_API_KEY=
ANTHROPIC_API_KEY=
XAI_API_KEY=
```

`.env` is ignored by Git. Never commit real API keys.

## Usage

Primary command:

```bash
python main.py fix \
  --repo /path/to/your-app \
  --task "Fix the bug where the user ID is missing" \
  --provider claude \
  --max-steps 8
```

For compatibility with the original DevAgent spec, `run` is an alias for `fix`:

```bash
python main.py run \
  --repo /path/to/your-app \
  --task "Fix the bug where the user ID is missing"
```

`--provider` is optional. When omitted, DevAgent uses `DEFAULT_PROVIDER` from `.env`.

Supported values:

- `openai`
- `claude`
- `grok`

## How the loop works

### 1. Gather

DevAgent first sees a bounded repository file listing and Git working-tree status. The model can then request file reads or text searches to understand the implementation before editing.

### 2. Act

The model may create a file, replace a complete file, or perform a small exact-text replacement. Existing files are copied first to:

```text
<target-repo>/.devagent/backups/<timestamp>/...
```

### 3. Verify

After a modification, DevAgent requires a successful verification command before accepting `finish`. Examples include:

```text
pytest
python -m py_compile
ruff
mypy
npm test
npm run build
go test
cargo test
git diff --check
```

If verification fails, the output is fed back into the next loop step so the model can diagnose and retry.

## Local memory

DevAgent records local runtime state inside the target application repository:

```text
.devagent/
├── backups/
└── memory/
    ├── attempts.jsonl
    └── lessons.md
```

`.devagent/` is intended to remain local and should not be committed to the application repository.

## Safety model

DevAgent is intentionally conservative. Its terminal tool blocks common destructive or publishing commands such as `sudo`, file deletion, Git commit/push/reset/clean/rebase/merge, direct network transfer commands, and package installation. File changes should happen through DevAgent's backup-aware file tools.

This is still an early development agent, not a security sandbox. Run it only against repositories you can restore and review the resulting diff before committing changes.

## Example task

See [`examples/sample_task.md`](examples/sample_task.md).

## Status

Version `0.1.0` skeleton. Next milestones can add structured patch plans, approval modes, richer repository indexing, test selection, Git diff review, multi-agent roles, and evaluation fixtures.
