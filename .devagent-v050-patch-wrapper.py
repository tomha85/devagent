from __future__ import annotations

import runpy
from pathlib import Path

root = Path(__file__).resolve().parent
path = root / '.devagent-v050-patch.py'
text = path.read_text(encoding='utf-8')
old = 'def test_cli_help_describes_unrestricted_input_path(capsys) -> None:'
new = 'def test_help_describes_unrestricted_input_path(capsys: pytest.CaptureFixture[str]) -> None:'
if old in text:
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
runpy.run_path(str(path), run_name='__main__')

version_test = root / 'tests' / 'test_production_v040.py'
version_text = version_test.read_text(encoding='utf-8')
version_anchor = '    assert version == "0.4.0"\n'
version_replacement = '    assert version == "0.5.0"\n'
if version_replacement not in version_text:
    if version_text.count(version_anchor) != 1:
        raise SystemExit(
            'expected exactly one 0.4.0 release-version assertion in tests/test_production_v040.py'
        )
    version_test.write_text(
        version_text.replace(version_anchor, version_replacement, 1),
        encoding='utf-8',
    )
