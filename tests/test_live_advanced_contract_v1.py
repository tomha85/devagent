from __future__ import annotations

import inspect

import devagent.live as live
from devagent.live import advanced_assistant, advanced_semantics, recursive_assistant


def test_advanced_semantics_are_public_live_api():
    assert live.LiveAdvancedKind.NUMERIC_COMPARISON.value == "NUMERIC_COMPARISON"
    assert callable(live.build_live_advanced_coverage)
    assert callable(live.extract_numeric_comparisons)
    assert callable(live.resolve_advanced_target)
    assert callable(live.required_advanced_tag_ids)
    assert callable(live.diagnose_advanced_target)


def test_recursive_assistant_builds_and_routes_advanced_coverage():
    init_source = inspect.getsource(recursive_assistant.RecursiveLiveCommissioningAssistant.__init__)
    answer_source = inspect.getsource(recursive_assistant.RecursiveLiveCommissioningAssistant.answer)
    history_source = inspect.getsource(recursive_assistant.RecursiveLiveCommissioningAssistant._preferred_history_tag_ids)

    assert "build_live_advanced_coverage" in init_source
    assert "_advanced_reply" in answer_source
    assert "advanced_coverage.numeric_comparisons" in history_source
    assert "advanced_coverage.models" in history_source


def test_advanced_live_modules_expose_no_plc_control_calls():
    source = "\n".join(
        (
            inspect.getsource(advanced_semantics),
            inspect.getsource(advanced_assistant),
            inspect.getsource(recursive_assistant),
        )
    ).casefold()
    for forbidden in (
        "write_value(",
        "set_value(",
        "call_method(",
        ".force(",
        ".reset(",
        ".download(",
        ".change_mode(",
    ):
        assert forbidden not in source


def test_advanced_semantics_do_not_import_vendor_parser_implementation_directly():
    source = inspect.getsource(advanced_semantics)
    assert "devagent.plc.siemens" not in source
    assert "devagent.plc.schneider" not in source
    assert "devagent.plc.rockwell" not in source
