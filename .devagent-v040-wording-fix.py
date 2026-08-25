from pathlib import Path

path = Path(__file__).resolve().parent / "tests/test_developer_review_report.py"
text = path.read_text(encoding="utf-8")
old = "Required acceptance criteria evidenced: 2/2"
new = "Required acceptance criteria satisfied: 2/2"
if old in text:
    path.write_text(text.replace(old, new), encoding="utf-8")
print("acceptance report wording normalized")
