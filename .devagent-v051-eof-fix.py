from pathlib import Path

for name in ("tests/test_cli.py", "tests/test_source_control_publish.py"):
    path = Path(name)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
