from pathlib import Path

path = Path(__file__).resolve().parent / ".github" / "workflows" / "ci.yml"
text = path.read_text(encoding="utf-8")
anchor = '''  package:\n    name: Build and install wheel\n'''
block = '''  qualification:\n    name: Functional qualification\n    runs-on: ubuntu-latest\n\n    steps:\n      - name: Checkout\n        uses: actions/checkout@v4\n\n      - name: Set up Python\n        uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n          cache: pip\n          cache-dependency-path: |\n            pyproject.toml\n            requirements.txt\n\n      - name: Install development package\n        run: |\n          python -m pip install --upgrade pip\n          python -m pip install -e ".[dev]"\n\n      - name: Run functional qualification catalog\n        run: |\n          python -m devagent.qualification \\\n            --catalog evaluation/benchmark_v2.json \\\n            --report .devagent/functional-qualification.json\n\n  package:\n    name: Build and install wheel\n'''
if block not in text:
    if anchor not in text:
        raise SystemExit("CI package anchor not found")
    path.write_text(text.replace(anchor, block, 1), encoding="utf-8")
print("functional qualification CI gate applied")
