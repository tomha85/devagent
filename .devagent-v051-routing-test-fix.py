from pathlib import Path

path = Path("tests/test_model_routing.py")
text = path.read_text(encoding="utf-8")
old = '    assert "investigator: openai/gpt-default (default)" in output\n'
new = '    assert "investigator: openai/gpt-default [CONTRACT-QUALIFIED] (default)" in output\n'
if old not in text:
    raise SystemExit("routing assertion anchor not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
