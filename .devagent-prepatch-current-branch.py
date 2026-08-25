from pathlib import Path

path = Path("README.md")
text = path.read_text(encoding="utf-8")
old = "- one commit is created and pushed to the selected remote,"
new = "- one commit is created and pushed to the selected remote branch,"
if text.count(old) != 1:
    raise RuntimeError(f"README prepatch expected one match, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("README continuation prepatch applied.")
