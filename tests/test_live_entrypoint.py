from __future__ import annotations

import sys
import types

from devagent.entrypoint import main


def test_entrypoint_routes_live_without_touching_software_or_plc(monkeypatch) -> None:
    module = types.ModuleType("devagent.live.cli")
    calls: list[list[str]] = []

    def fake_main(argv):
        calls.append(list(argv))
        return 17

    module.main = fake_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "devagent.live.cli", module)

    assert main(["live", "probe", "opc.tcp://127.0.0.1:4840/"]) == 17
    assert calls == [["probe", "opc.tcp://127.0.0.1:4840/"]]
