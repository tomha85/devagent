from pathlib import Path

path = Path("tests/test_production_v040.py")
text = path.read_text(encoding="utf-8")
old = '    assert version == "0.5.0"\n'
new = '    assert version == "0.5.1"\n'
if old not in text:
    raise SystemExit("version assertion anchor not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
