from pathlib import Path

path = Path(__file__).resolve().parent / "devagent/orchestrator.py"
text = path.read_text(encoding="utf-8")
old = "import json\nimport difflib\nimport subprocess\n"
new = "import json\nimport difflib\nimport re\nimport subprocess\n"
if new not in text:
    if old not in text:
        raise SystemExit("orchestrator import anchor missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("acceptance import fix applied")
