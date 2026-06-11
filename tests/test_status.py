from __future__ import annotations

from kanboard_agent_worker.status import parse_agent_directives, parse_kanboard_status


def test_parse_kanboard_status_strips_marker_and_returns_status() -> None:
    parsed = parse_kanboard_status("Finished the work.\n\nKANBOARD_STATUS: done\n")

    assert parsed.status == "done"
    assert parsed.text == "Finished the work."


def test_parse_kanboard_status_strips_invalid_marker_without_status() -> None:
    parsed = parse_kanboard_status("Need something.\n\nKANBOARD_STATUS: waiting\n")

    assert parsed.status is None
    assert parsed.text == "Need something."


def test_parse_agent_directives_allows_multiple_subtasks_and_agent_aliases() -> None:
    parsed = parse_agent_directives(
        "\n".join(
            [
                "Created follow-up work.",
                "KANBAN_SUBTASK Add API coverage",
                "KANBAN_SUBTASK_AGENT codex",
                "KANBAN_SUBTASK Verify UX",
                "KANBAN_SUBTASK_AGET claude",
            ]
        )
    )

    assert parsed.text == "Created follow-up work."
    assert parsed.subtasks[0].title == "Add API coverage"
    assert parsed.subtasks[0].assignee == "codex"
    assert parsed.subtasks[1].title == "Verify UX"
    assert parsed.subtasks[1].assignee == "claude"
