from __future__ import annotations

import runpy
from pathlib import Path

path = Path(__file__).with_name('.devagent-v050-patch.py')
text = path.read_text(encoding='utf-8')
old = 'def test_cli_help_describes_unrestricted_input_path(capsys) -> None:'
new = 'def test_help_describes_unrestricted_input_path(capsys: pytest.CaptureFixture[str]) -> None:'
if old in text:
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
runpy.run_path(str(path), run_name='__main__')
