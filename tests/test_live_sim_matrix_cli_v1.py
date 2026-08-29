from __future__ import annotations

from devagent.entrypoint import main as entrypoint_main
from devagent.live.sim_cli import main as sim_main


def test_sim_list_scenarios_prints_ground_truth_without_starting_server(capsys) -> None:
    rc = sim_main(["--list-scenarios"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "DEVAGENT LIVE REALISTIC SIMULATOR MATRIX" in output
    assert "healthy" in output
    assert "drive_fault" in output
    assert "safety_trip" in output
    assert "logic_conflict" in output
    assert "stuck_on_conflict" in output
    assert "Expected health:" in output
    assert "Ground truth:" in output


def test_top_level_entrypoint_routes_live_sim_to_matrix_cli(capsys) -> None:
    rc = entrypoint_main(["live", "sim", "--list-scenarios"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "DEVAGENT LIVE REALISTIC SIMULATOR MATRIX" in output
    assert "downstream_blocker" in output
    assert "multi_blocker" in output
