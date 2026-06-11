from __future__ import annotations

from kanboard_agent_worker.config import RosterEntry
from kanboard_agent_worker.task_markdown import build_agent_prompt, extract_section, replace_output_section


def test_extract_section() -> None:
    markdown = "## Spec\nDo this\n\n## Config\nagent: codex\n\n## Output\nold"

    assert extract_section(markdown, "Spec") == "Do this"
    assert extract_section(markdown, "Config") == "agent: codex"


def test_replace_output_section() -> None:
    markdown = "## Spec\nDo this\n\n## Output\nold\n"

    assert replace_output_section(markdown, "new").strip() == "## Spec\nDo this\n\n## Output\nnew"


def test_build_agent_prompt_contains_identity_metadata_conversation_and_system_prompt() -> None:
    prompt = build_agent_prompt(
        {"id": "7", "title": "Fix bug", "description": "## Spec\nDo the thing", "project_id": "1"},
        comments=[{"username": "alice", "date_creation": "1000", "comment": "Please fix this"}],
        metadata={"kanboard_worker.codex-node1.session_id": "session-123"},
        roster=(RosterEntry(name="claude", description="Handles UI work"),),
        worker_username="codex-node1",
        system_prompt="Prefer small changes.",
    )

    assert "# System Prompt" in prompt
    assert "Prefer small changes." in prompt
    assert "Username: codex-node1" in prompt
    assert "kanboard_worker.codex-node1.session_id" in prompt
    assert "claude: Handles UI work" in prompt
    assert "1000 alice: Please fix this" in prompt
    assert "Use the available Kanboard tools for attachments, creating subtasks" in prompt
    assert "Use the Kanboard move_column tool" in prompt
    assert "Your final response from this turn will be posted as a Kanboard card comment" in prompt


def test_build_agent_prompt_omits_missing_config_section() -> None:
    prompt = build_agent_prompt(
        {"id": "7", "title": "Fix bug", "description": "## Spec\nDo the thing"},
        worker_username="codex-node1",
    )

    assert "## Config" not in prompt
    assert "## Spec\nDo the thing" in prompt


def test_build_agent_prompt_includes_subtask_as_primary_spec() -> None:
    prompt = build_agent_prompt(
        {"id": "7", "title": "Parent task", "description": "## Spec\nParent context"},
        subtask={"id": "11", "title": "Do subtask work"},
        worker_username="claude",
    )

    assert "Task #7: Parent task / Subtask #11: Do subtask work" in prompt
    assert "## Spec\nSubtask #11: Do subtask work" in prompt
    assert "Parent task context:\nParent context" in prompt
