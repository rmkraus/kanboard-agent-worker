from __future__ import annotations

from kanboard_agent_worker.status import parse_kanboard_status


def test_parse_kanboard_status_strips_marker_and_returns_status() -> None:
    parsed = parse_kanboard_status("Finished the work.\n\nKANBOARD_STATUS: done\n")

    assert parsed.status == "done"
    assert parsed.text == "Finished the work."


def test_parse_kanboard_status_strips_invalid_marker_without_status() -> None:
    parsed = parse_kanboard_status("Need something.\n\nKANBOARD_STATUS: waiting\n")

    assert parsed.status is None
    assert parsed.text == "Need something."
