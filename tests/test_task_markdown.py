from __future__ import annotations

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
        metadata={"kanboard_worker.codex-node1.thread_id": "thread-123"},
        worker_username="codex-node1",
        system_prompt="Prefer small changes.",
    )

    assert "# System Prompt" in prompt
    assert "Prefer small changes." in prompt
    assert "Username: codex-node1" in prompt
    assert "kanboard_worker.codex-node1.thread_id" in prompt
    assert "1000 alice: Please fix this" in prompt
    assert "Your final response from this turn will be posted as a Kanboard card comment" in prompt
